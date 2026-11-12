from copy import deepcopy
from urllib.parse import urlparse

import requests
from requests.cookies import (
    RequestsCookieJar,
    extract_cookies_to_jar,
    merge_cookies,
)
from requests.utils import get_netrc_auth, rewind_body


def _origin(url):
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def _same_origin(first_url, second_url):
    return _origin(first_url) == _origin(second_url)


def _copy_cookiejar(cookiejar):
    try:
        return deepcopy(cookiejar)
    except TypeError:
        copied = RequestsCookieJar()
        copied.set_policy(cookiejar._policy)
        for cookie in cookiejar:
            copied.set_cookie(cookie)
        return copied


def _merge_cookiejar(target, source):
    try:
        merge_cookies(target, source)
    except AttributeError:
        for cookie in source:
            target.set_cookie(cookie)


def _cookie_matches_origin(cookie, url):
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    domain = cookie.domain.lstrip(".")
    domain_matches = hostname == domain or hostname.endswith(f".{domain}")
    cookie_origin = getattr(cookie, "_mechanicalsoup_origin", None)
    if cookie_origin is not None:
        scheme, _, port = cookie_origin
        return (
            scheme == parsed.scheme
            and port == parsed.port
            and domain_matches
        )

    default_ports = {"http": 80, "https": 443}
    default_port = default_ports.get(parsed.scheme)
    port_matches = (
        cookie.port == str(parsed.port)
        or (
            cookie.port is None
            and parsed.port in (None, default_port)
        )
    )
    return domain_matches and port_matches


def _tag_cookie_origin(cookiejar, url):
    origin = _origin(url)
    for cookie in cookiejar:
        if not cookie.domain or _cookie_matches_origin(cookie, url):
            cookie._mechanicalsoup_origin = origin


def wrap_session(session, allow_cross_origin_credentials=False):
    if isinstance(session, BoundaryAwareSession):
        session.allow_cross_origin_credentials = (
            allow_cross_origin_credentials
        )
        return session

    wrapped = BoundaryAwareSession(
        allow_cross_origin_credentials=allow_cross_origin_credentials
    )
    for attribute in requests.Session.__attrs__:
        setattr(wrapped, attribute, getattr(session, attribute))
    return wrapped


class BoundaryAwareSession(requests.Session):
    """A requests session that keeps redirect credentials origin-scoped."""

    def __init__(self, allow_cross_origin_credentials=False):
        super().__init__()
        self.allow_cross_origin_credentials = allow_cross_origin_credentials

    def prepare_request(self, request):
        headers = request.headers
        explicit_cookie = headers.get("Cookie") if headers else None
        prepared_request = super().prepare_request(request)
        prepared_request._explicit_cookie = explicit_cookie
        return prepared_request

    def send(self, request, **kwargs):
        # Requests normally writes response cookies to the session before it
        # follows a redirect. Keep a transaction instead, so an error in the
        # middle of a redirect chain cannot leave partially updated state.
        if not kwargs.get("allow_redirects", True):
            return super().send(request, **kwargs)

        original_cookies = self.cookies
        redirect_cookies = _copy_cookiejar(original_cookies)
        self.cookies = redirect_cookies
        _tag_cookie_origin(self.cookies, request.url)
        if not hasattr(request, "_explicit_cookie"):
            request._explicit_cookie = request.headers.get("Cookie")

        try:
            response = super().send(request, **kwargs)
        except Exception:
            self.cookies = original_cookies
            raise

        self.cookies = original_cookies
        _merge_cookiejar(self.cookies, redirect_cookies)
        return response

    def resolve_redirects(
        self,
        resp,
        req,
        stream=False,
        timeout=None,
        verify=True,
        cert=None,
        proxies=None,
        yield_requests=False,
        **adapter_kwargs,
    ):
        history = []
        url = self.get_redirect_target(resp)
        previous_fragment = urlparse(req.url).fragment
        original_explicit_cookie = getattr(
            req,
            "_explicit_cookie",
            None,
        )

        while url:
            prepared_request = req.copy()
            prepared_request._explicit_cookie = original_explicit_cookie

            history.append(resp)
            resp.history = history[1:]

            try:
                resp.content
            except (
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError,
                RuntimeError,
            ):
                resp.raw.read(decode_content=False)

            if len(resp.history) >= self.max_redirects:
                raise requests.exceptions.TooManyRedirects(
                    f"Exceeded {self.max_redirects} redirects.",
                    response=resp,
                )

            resp.close()

            if url.startswith("//"):
                parsed_redirect_url = urlparse(resp.url)
                url = f"{parsed_redirect_url.scheme}:{url}"

            parsed = urlparse(url)
            if parsed.fragment == "" and previous_fragment:
                parsed = parsed._replace(fragment=previous_fragment)
            elif parsed.fragment:
                previous_fragment = parsed.fragment
            url = parsed.geturl()

            if not parsed.netloc:
                url = requests.compat.urljoin(
                    resp.url,
                    requests.utils.requote_uri(url),
                )
            else:
                url = requests.utils.requote_uri(url)

            prepared_request.url = requests.utils.to_native_string(url)
            self.rebuild_method(prepared_request, resp)

            if resp.status_code not in (
                requests.codes.temporary_redirect,
                requests.codes.permanent_redirect,
            ):
                for header in (
                    "Content-Length",
                    "Content-Type",
                    "Transfer-Encoding",
                ):
                    prepared_request.headers.pop(header, None)
                prepared_request.body = None

            headers = prepared_request.headers
            headers.pop("Cookie", None)

            extract_cookies_to_jar(
                prepared_request._cookies,
                req,
                resp.raw,
            )
            _tag_cookie_origin(resp.cookies, prepared_request.url)
            _merge_cookiejar(prepared_request._cookies, resp.cookies)
            _merge_cookiejar(
                prepared_request._cookies,
                self.cookies,
            )

            cross_origin = not _same_origin(req.url, prepared_request.url)
            if cross_origin and not self.allow_cross_origin_credentials:
                prepared_request.headers.pop("Authorization", None)
                prepared_request.headers.pop("Cookie", None)

            if cross_origin:
                for cookie in list(prepared_request._cookies):
                    if not _cookie_matches_origin(
                        cookie,
                        prepared_request.url,
                    ):
                        prepared_request._cookies.clear(
                            cookie.domain,
                            cookie.path,
                            cookie.name,
                        )

            if (
                cross_origin
                and self.allow_cross_origin_credentials
                and original_explicit_cookie is not None
            ):
                prepared_request.headers["Cookie"] = original_explicit_cookie

            prepared_request.prepare_cookies(prepared_request._cookies)

            proxies = self.rebuild_proxies(prepared_request, proxies)
            self._rebuild_origin_auth(prepared_request, resp)

            rewindable = prepared_request._body_position is not None and (
                "Content-Length" in headers
                or "Transfer-Encoding" in headers
            )
            if rewindable:
                rewind_body(prepared_request)

            req = prepared_request

            if yield_requests:
                yield req
                continue

            resp = self.send(
                req,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
                allow_redirects=False,
                **adapter_kwargs,
            )

            extract_cookies_to_jar(
                self.cookies,
                prepared_request,
                resp.raw,
            )
            _tag_cookie_origin(resp.cookies, prepared_request.url)
            _merge_cookiejar(self.cookies, resp.cookies)
            _merge_cookiejar(self.cookies, prepared_request._cookies)
            url = self.get_redirect_target(resp)
            yield resp

    def _rebuild_origin_auth(self, prepared_request, response):
        if _same_origin(
            response.request.url,
            prepared_request.url,
        ) or self.allow_cross_origin_credentials:
            new_auth = (
                get_netrc_auth(prepared_request.url)
                if self.trust_env
                else None
            )
            if new_auth is not None:
                prepared_request.prepare_auth(new_auth)
