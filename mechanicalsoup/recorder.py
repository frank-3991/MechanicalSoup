"""Optional local recording and controlled replay of browser operations.

This module implements a *recorder* that can be attached to a
:class:`~mechanicalsoup.Browser` (or
:class:`~mechanicalsoup.StatefulBrowser`) to log each navigation or form
submission to a local JSON Lines file, and a *replay* mechanism that can
re-send a recorded operation against a locally controlled target.

Recording is entirely opt-in: when no recorder is attached, request
handling and session adapter behavior are exactly as before. Failures
while writing a record never change the return value or the exceptions
of the original request.

Uploaded file contents are never persisted by the recorder; only field
names and file names are stored. Authentication data, cookies and
sensitive form values can be redacted according to caller-specified
rules, and must be supplied again by the caller when replaying a record
that lacks them.
"""

import json
import re
import threading

from .utils import LinkNotFoundError

#: Placeholder stored in place of a redacted value. The placeholder
#: itself is never sent on the wire: replay refuses to send a request
#: that still contains it, unless the caller supplies a replacement.
REDACTED = "<redacted>"

#: HTTP methods considered to have write side effects. Replaying one of
#: these requires explicit confirmation from the caller.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Keyword arguments of `requests.Session.request` that belong to the
#: prepared request (as opposed to `Session.send`).
_REQUEST_KWARGS = (
    "headers", "files", "data", "params", "auth", "cookies", "hooks", "json",
)

#: Keyword arguments forwarded to `requests.Session.send` in addition to
#: the settings merged from the environment.
_SEND_KWARGS = ("timeout", "allow_redirects", "stream", "verify", "cert",
                "proxies")


class ReplayError(LinkNotFoundError):
    """Base class for errors raised when a record cannot be replayed.

    Derived from :class:`~mechanicalsoup.LinkNotFoundError` so that all
    exceptions specific to MechanicalSoup share a common base class.
    """


class ReplayIncompleteError(ReplayError):
    """Raised when a record lacks information required to send it again.

    This happens when authentication data, cookies or form values were
    redacted during recording and the caller did not supply a
    replacement, or when a file upload cannot be reconstructed.
    """


class ReplayConfirmationRequired(ReplayError):
    """Raised when replaying a request with write side effects without
    explicit confirmation (``confirm_write=True``)."""


class Record:
    """A single recorded operation (one navigation or one submission).

    A record captures the request as sent (method, URL, headers, ordered
    form fields, cookies, authentication), the redirect chain that
    followed, the final response status and the resulting page state.
    Use :meth:`to_dict`/:meth:`from_dict` for (de)serialization.
    """

    def __init__(self, operation, method, url, headers=None, fields=None,
                 cookies=None, auth=None, files=None, redirects=None,
                 response_status=None, final_url=None, page_url=None,
                 status="pending", error=None):
        #: ``"navigation"`` for GET-like operations, ``"submission"``
        #: for form submissions.
        self.operation = operation
        self.method = method.upper()
        self.url = url
        #: Request headers as a dict (redaction rules already applied).
        self.headers = dict(headers or {})
        #: Ordered list of ``(name, value)`` form field pairs. Order is
        #: preserved between recording and replay.
        self.fields = [tuple(pair) for pair in (fields or [])]
        #: Cookies sent with the request, as a dict.
        self.cookies = dict(cookies or {})
        #: ``None``, ``{"type": "basic", "username": ..., "password": ...}``
        #: or ``{"type": "other", "repr": ...}``.
        self.auth = auth
        #: Ordered list of uploaded files as dicts with ``name`` (field
        #: name), ``filename`` and ``one_shot`` (True if the content was
        #: a one-time stream). File contents are never stored.
        self.files = [dict(f) for f in (files or [])]
        #: Redirect chain as a list of ``{"method", "url",
        #: "status_code"}`` dicts, in the order they happened.
        self.redirects = [dict(r) for r in (redirects or [])]
        self.response_status = response_status
        self.final_url = final_url
        #: URL of the page displayed after the operation (the URL the
        #: browser state would reflect).
        self.page_url = page_url
        #: One of ``"pending"``, ``"completed"``, ``"unknown"`` (the
        #: request was sent but the outcome is unknown) or
        #: ``"not_sent"`` (the request never reached the server).
        self.status = status
        self.error = error

    @property
    def is_write(self):
        """Whether the recorded method has write side effects."""
        return self.method in WRITE_METHODS

    def to_dict(self):
        return {
            "operation": self.operation,
            "method": self.method,
            "url": self.url,
            "headers": self.headers,
            "fields": [list(pair) for pair in self.fields],
            "cookies": self.cookies,
            "auth": self.auth,
            "files": self.files,
            "redirects": self.redirects,
            "response_status": self.response_status,
            "final_url": self.final_url,
            "page_url": self.page_url,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            operation=data["operation"],
            method=data["method"],
            url=data["url"],
            headers=data.get("headers"),
            fields=data.get("fields"),
            cookies=data.get("cookies"),
            auth=data.get("auth"),
            files=data.get("files"),
            redirects=data.get("redirects"),
            response_status=data.get("response_status"),
            final_url=data.get("final_url"),
            page_url=data.get("page_url"),
            status=data.get("status", "pending"),
            error=data.get("error"),
        )


class Recorder:
    """Records browser operations to a local JSON Lines file.

    :param path: Path of the file records are appended to. Each line is
        a JSON object describing one operation.
    :param redact_fields: Names of form fields whose values must not be
        stored. A name may be a plain string (exact match) or a compiled
        regular expression (matched against the field name). Redacted
        values are replaced with :data:`REDACTED`.
    :param redact_cookies: Names of cookies to redact, same matching
        rules as ``redact_fields``.
    :param redact_headers: Names of request headers to redact, same
        matching rules as ``redact_fields``.
    :param redact_auth: If True (the default), authentication
        credentials are redacted from the record.

    Recording failures (e.g. the file cannot be written) are swallowed:
    they never change the result or the exceptions of the recorded
    request.
    """

    def __init__(self, path, redact_fields=(), redact_cookies=(),
                 redact_headers=("Authorization", "Proxy-Authorization"),
                 redact_auth=True):
        self.path = path
        self.redact_fields = tuple(redact_fields)
        self.redact_cookies = tuple(redact_cookies)
        self.redact_headers = tuple(redact_headers)
        self.redact_auth = redact_auth
        self._lock = threading.Lock()

    @staticmethod
    def _matches(rules, name):
        for rule in rules:
            if isinstance(rule, re.Pattern):
                if rule.search(name):
                    return True
            elif rule == name:
                return True
        return False

    def _redact_mapping(self, mapping, rules):
        return {k: (REDACTED if self._matches(rules, k) else v)
                for k, v in mapping.items()}

    def _redact_fields(self, fields):
        return [(k, REDACTED if self._matches(self.redact_fields, k) else v)
                for k, v in fields]

    def _redact_auth(self, auth):
        if auth is None:
            return None
        if isinstance(auth, tuple) and len(auth) == 2:
            username, password = auth
            if self.redact_auth:
                password = REDACTED
            return {"type": "basic", "username": username,
                    "password": password}
        # Auth callables and exotic objects cannot be serialized; keep
        # only a marker so that replay knows credentials are missing.
        return {"type": "other", "repr": REDACTED}

    def build_record(self, operation, method, url, kwargs):
        """Create a :class:`Record` describing the request that is about
        to be sent, applying the redaction rules."""
        headers = dict(kwargs.get("headers") or {})
        headers = self._redact_mapping(headers, self.redact_headers)

        data = kwargs.get("data") or {}
        params = kwargs.get("params") or {}
        if hasattr(data, "items"):
            data = list(data.items())
        if hasattr(params, "items"):
            params = list(params.items())
        if method.upper() == "GET":
            fields = list(params) + list(data)
        else:
            fields = list(data) + list(params)
        fields = self._redact_fields(
            [(k, v) for k, v in fields if isinstance(v, str)])

        cookies = self._redact_mapping(dict(kwargs.get("cookies") or {}),
                                       self.redact_cookies)

        files = []
        raw_files = kwargs.get("files") or {}
        if hasattr(raw_files, "items"):
            raw_files = list(raw_files.items())
        for name, spec in raw_files:
            filename = None
            content = spec
            if isinstance(spec, (tuple, list)) and len(spec) >= 2:
                filename, content = spec[0], spec[1]
            files.append({
                "name": name,
                "filename": filename,
                # File contents are never persisted. A one-time stream
                # (file-like object) is additionally flagged so that
                # replay can refuse to re-send a consumed stream.
                "one_shot": hasattr(content, "read"),
            })

        return Record(
            operation=operation,
            method=method,
            url=url,
            headers=headers,
            fields=fields,
            cookies=cookies,
            auth=self._redact_auth(kwargs.get("auth")),
            files=files,
        )

    def write(self, record):
        """Append ``record`` to the record file. Never raises."""
        try:
            line = json.dumps(record.to_dict())
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            # Recording must never interfere with the original request.
            pass

    def records(self):
        """Read back all records from the record file."""
        return load_records(self.path)


def load_records(path):
    """Load the records stored in the file at ``path``."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(Record.from_dict(json.loads(line)))
    return records


def _redacted_names(items):
    """Return the names of entries whose value is the redaction marker."""
    return [name for name, value in items if value == REDACTED]


def replay_record(browser, record, confirm_write=False, fields=None,
                  cookies=None, auth=None, headers=None, files=None,
                  allow_redirects=None, **kwargs):
    """Replay a single recorded operation on ``browser``.

    The record must contain all the information needed to send the
    request again: values that were redacted during recording
    (authentication, cookies, sensitive form fields) must be supplied
    by the caller through the corresponding argument, otherwise a
    :class:`ReplayIncompleteError` is raised and nothing is sent.

    :param browser: The :class:`~mechanicalsoup.Browser` (or
        :class:`~mechanicalsoup.StatefulBrowser`) used to send the
        request. The target should be locally controlled; replaying
        against a third-party server may repeat side effects.
    :param record: A :class:`Record` (or a dict as produced by
        :meth:`Record.to_dict`) describing one operation. Only this
        single operation is replayed: redirects recorded in the original
        chain are *not* re-sent, so a write that appeared in the chain
        is performed exactly once.
    :param confirm_write: Must be True to replay a request with write
        side effects (POST, PUT, PATCH, DELETE), otherwise
        :class:`ReplayConfirmationRequired` is raised.
    :param fields: Dict of replacement values for redacted form fields.
    :param cookies: Dict of replacement values for redacted cookies.
    :param auth: Auth to send with the replayed request, e.g. a
        ``(username, password)`` tuple. Required if the record's
        credentials were redacted.
    :param headers: Dict of replacement values for redacted headers.
    :param files: Dict mapping a file field name to a file spec (as
        accepted by requests). Required if the record contains file
        uploads, since file contents are never persisted. A record whose
        upload was a one-time stream that has been consumed cannot be
        replayed.
    :param allow_redirects: Defaults to False so that the replay
        performs only the recorded operation and does not repeat a write
        that occurred inside the original redirect chain.
    :param \\*\\*kwargs: Extra arguments forwarded to
        `requests.Session.request`.

    :return: The response of the replayed request, with a ``soup``
        attribute added by :func:`Browser.add_soup`.
    """
    from .browser import Browser  # Avoid a circular import.

    if isinstance(record, dict):
        record = Record.from_dict(record)

    if record.is_write and not confirm_write:
        raise ReplayConfirmationRequired(
            f"Refusing to replay {record.method} {record.url}: this "
            "method has write side effects. Pass confirm_write=True to "
            "confirm that the request should be sent again."
        )

    # --- Gather everything needed to rebuild the request, checking that
    # --- no redacted value is left unsupplied.
    missing = []

    merged_fields = []
    replacements = dict(fields or {})
    for name, value in record.fields:
        if value == REDACTED:
            if name in replacements:
                value = replacements[name]
            else:
                missing.append(f"form field {name!r}")
        merged_fields.append((name, value))

    merged_headers = dict(record.headers)
    for name, value in (headers or {}).items():
        merged_headers[name] = value
    for name in _redacted_names(merged_headers.items()):
        missing.append(f"header {name!r}")

    merged_cookies = dict(record.cookies)
    for name, value in (cookies or {}).items():
        merged_cookies[name] = value
    for name in _redacted_names(merged_cookies.items()):
        missing.append(f"cookie {name!r}")

    if record.auth is not None and auth is None:
        redacted = (
            record.auth.get("type") == "other"
            or record.auth.get("password") == REDACTED
        )
        if redacted:
            missing.append("authentication credentials")

    file_supplies = dict(files or {})
    replay_files = {}
    for entry in record.files:
        name = entry["name"]
        if name in file_supplies:
            replay_files[name] = file_supplies[name]
        elif entry["one_shot"]:
            missing.append(
                f"file field {name!r} (its content was a one-time "
                "stream that has been consumed and was not stored)")
        else:
            missing.append(
                f"file field {name!r} (file contents are not stored in "
                "records; supply it with the files argument)")

    if missing:
        raise ReplayIncompleteError(
            "Record is missing information required to send it again: "
            + ", ".join(missing)
        )

    # --- Rebuild the request, preserving the recorded field order.
    request_kwargs = {
        "method": record.method,
        "url": record.url,
        "headers": merged_headers,
    }
    if record.method == "GET":
        request_kwargs["params"] = merged_fields
    else:
        request_kwargs["data"] = merged_fields
    if merged_cookies:
        request_kwargs["cookies"] = merged_cookies
    if auth is not None:
        request_kwargs["auth"] = auth
    elif record.auth is not None and record.auth.get("type") == "basic":
        request_kwargs["auth"] = (record.auth["username"],
                                  record.auth["password"])
    if replay_files:
        request_kwargs["files"] = replay_files
    if allow_redirects is None:
        # Replay only the recorded operation: the redirect chain of the
        # original request is part of the record, not re-followed, so a
        # write inside the chain is not sent twice.
        allow_redirects = False
    request_kwargs["allow_redirects"] = allow_redirects
    request_kwargs.update(kwargs)

    response = browser.session.request(**request_kwargs)
    return Browser.add_soup(response, browser.soup_config)
