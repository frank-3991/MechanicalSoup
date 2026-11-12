from contextlib import nullcontext
import io
import os
import sys
import tempfile
from unittest import mock

import pytest
import requests
import requests_mock
import setpath  # noqa:F401, must come before 'import mechanicalsoup'
from bs4 import BeautifulSoup
from requests.cookies import RequestsCookieJar
from utils import mock_get, prepare_mock_browser

import mechanicalsoup


def test_default_soup_config_not_shared_between_browsers():
    browser = mechanicalsoup.Browser()
    browser.soup_config["custom"] = True

    other_browser = mechanicalsoup.Browser()

    assert other_browser.soup_config == {'features': 'lxml'}


def test_submit_online(httpbin):
    """Complete and submit the pizza form at http://httpbin.org/forms/post """
    browser = mechanicalsoup.Browser()
    page = browser.get(httpbin + "/forms/post")
    form = page.soup.form

    form.find("input", {"name": "custname"})["value"] = "Philip J. Fry"
    # leave custtel blank without value
    assert "value" not in form.find("input", {"name": "custtel"}).attrs
    form.find("input", {"name": "size", "value": "medium"})["checked"] = ""
    form.find("input", {"name": "topping", "value": "cheese"})["checked"] = ""
    form.find("input", {"name": "topping", "value": "onion"})["checked"] = ""
    form.find("textarea", {"name": "comments"}).insert(0, "freezer")

    response = browser.submit(form, page.url)

    # helpfully the form submits to http://httpbin.org/post which simply
    # returns the request headers in json format
    json = response.json()
    data = json["form"]
    assert data["custname"] == "Philip J. Fry"
    assert data["custtel"] == ""  # web browser submits "" for input left blank
    assert data["size"] == "medium"
    assert data["topping"] == ["cheese", "onion"]
    assert data["comments"] == "freezer"

    assert json["headers"]["User-Agent"].startswith('python-requests/')
    assert 'MechanicalSoup' in json["headers"]["User-Agent"]


def test_get_request_kwargs(httpbin):
    """Return kwargs without a submit"""
    browser = mechanicalsoup.Browser()
    page = browser.get(httpbin + "/forms/post")
    form = page.soup.form
    form.find("input", {"name": "custname"})["value"] = "Philip J. Fry"
    request_kwargs = browser.get_request_kwargs(form, page.url)
    assert "method" in request_kwargs
    assert "url" in request_kwargs
    assert "data" in request_kwargs
    assert ("custname", "Philip J. Fry") in request_kwargs["data"]


def test_get_request_kwargs_when_method_is_in_kwargs(httpbin):
    """Raise TypeError exception"""
    browser = mechanicalsoup.Browser()
    page = browser.get(httpbin + "/forms/post")
    form = page.soup.form
    kwargs = {"method": "post"}
    with pytest.raises(TypeError):
        browser.get_request_kwargs(form, page.url, **kwargs)


def test_get_request_kwargs_when_url_is_in_kwargs(httpbin):
    """Raise TypeError exception"""
    browser = mechanicalsoup.Browser()
    page = browser.get(httpbin + "/forms/post")
    form = page.soup.form
    kwargs = {"url": httpbin + "/forms/post"}
    with pytest.raises(TypeError):
        # pylint: disable=redundant-keyword-arg
        browser.get_request_kwargs(form, page.url, **kwargs)


def test_get_request_kwargs_when_submit_element_has_formaction_attribute():
    form_html = """
        <form method="post" action="https://example.com/submit">
            <button formaction="https://example.com/submit1"></button>
        </form>
    """
    form = BeautifulSoup(form_html, "lxml").form
    submit = form.button
    request_kwargs = mechanicalsoup.Browser.get_request_kwargs(
        form, "https://example.com", submit
    )
    assert request_kwargs["url"] == "https://example.com/submit1"


def test_get_request_kwargs_many_submit_elements_have_formaction_attribute():
    form_html = """
        <form method="post" action="https://example.com/submit">
            <button formaction="https://example.com/submit1"></button>
            <button formaction="https://example.com/submit2"></button>
            <button formaction="https://example.com/submit3"></button>
        </form>
    """
    form = BeautifulSoup(form_html, "lxml").form
    submit = form.find_all("button")[1]  # Choose the second button
    request_kwargs = mechanicalsoup.Browser.get_request_kwargs(
        form, "https://example.com", submit
    )
    assert request_kwargs["url"] == "https://example.com/submit2"


def test__request(httpbin):
    form_html = f"""
    <form method="post" action="{httpbin.url}/post">
      <input name="customer" value="Philip J. Fry"/>
      <input name="telephone" value="555"/>
      <textarea name="comments">freezer</textarea>
      <fieldset>
        <legend> Pizza Size </legend>
        <p><input type=RADIO name=size value="small">Small</p>
        <p><input type=radiO name=size value="medium" checked>Medium</p>
        <p><input type=radio name=size value="large">Large</p>
      </fieldset>
      <fieldset>
        <legend> Pizza Toppings </legend>
        <p><input type=CHECKBOX name="topping" value="bacon" checked>Bacon</p>
        <p><input type=checkBox name="topping" value="cheese">Extra Cheese</p>
        <p><input type=checkbox name="topping" value="onion" checked>Onion</p>
        <p><input type=checkbox name="topping" value="mushroom">Mushroom</p>
      </fieldset>
      <select name="shape">
        <option value="round">Round</option>
        <option value="square" selected>Square</option>
      </select>
    </form>
    """

    form = BeautifulSoup(form_html, "lxml").form

    browser = mechanicalsoup.Browser()
    response = browser._request(form)

    data = response.json()['form']
    assert data["customer"] == "Philip J. Fry"
    assert data["telephone"] == "555"
    assert data["comments"] == "freezer"
    assert data["size"] == "medium"
    assert data["topping"] == ["bacon", "onion"]
    assert data["shape"] == "square"

    assert "application/x-www-form-urlencoded" in response.request.headers[
        "Content-Type"]


valid_enctypes_file_submit = {"multipart/form-data": True,
                              "application/x-www-form-urlencoded": False
                              }

default_enctype = "application/x-www-form-urlencoded"


@pytest.mark.parametrize("file_field", [
  """<input name="pic" type="file" />""",
  ""])
@pytest.mark.parametrize("submit_file", [
    True,
    False
])
@pytest.mark.parametrize("enctype", [
  pytest.param("multipart/form-data"),
  pytest.param("application/x-www-form-urlencoded"),
  pytest.param("Invalid enctype")
])
def test_enctype_and_file_submit(httpbin, enctype, submit_file, file_field):
    # test if enctype is respected when specified
    # and if files are processed correctly
    form_html = f"""
    <form method="post" action="{httpbin.url}/post" enctype="{enctype}">
      <input name="in" value="test" />
      {file_field}
    </form>
    """
    form = BeautifulSoup(form_html, "lxml").form

    valid_enctype = (enctype in valid_enctypes_file_submit and
                     valid_enctypes_file_submit[enctype])
    if submit_file and file_field:
        # create a temporary file for testing file upload
        file_content = b":-)"
        pic_filedescriptor, pic_path = tempfile.mkstemp()
        pic_filename = os.path.basename(pic_path)
        os.write(pic_filedescriptor, file_content)
        os.close(pic_filedescriptor)
        if valid_enctype:
            # Correct encoding => send the content
            expected_content = file_content
        else:
            # Encoding doesn't allow sending the content, we expect
            # the filename as a normal text field.
            expected_content = os.path.basename(pic_path.encode())
    else:
        pic_path = None
        expected_content = b""

    # update the form (if needed) and submit
    browser = mechanicalsoup.Browser()
    with open(pic_path, "rb") if pic_path is not None else nullcontext() as fd:
        if pic_path is not None:
            tag = form.find("input", {"name": "pic"})
            tag["value"] = fd
        response = browser._request(form)

    if enctype not in valid_enctypes_file_submit:
        expected_enctype = default_enctype
    else:
        expected_enctype = enctype
    assert expected_enctype in response.request.headers["Content-Type"]

    resp = response.json()
    assert resp["form"]["in"] == "test"

    found = False
    found_in = None

    for key, value in resp.items():
        if value:
            if "pic" in value:
                content = value["pic"].encode()
                assert not found
                assert key in ("files", "form")
                found = True
                found_in = key
            if key == "files" and not valid_enctype:
                assert not value

    assert found == bool(file_field)
    if file_field:
        assert content == expected_content

        if valid_enctype:
            assert found_in == "files"
            if submit_file:
                assert ("filename=\"" + pic_filename + "\""
                        ).encode() in response.request.body
            else:
                assert b"filename=\"\"" in response.request.body
        else:
            assert found_in == "form"

    if submit_file and file_field:
        os.remove(pic_path)


def test__request_select_none(httpbin):
    """Make sure that a <select> with no options selected
    submits the first option, as it does in a browser."""
    form_html = f"""
    <form method="post" action={httpbin.url}/post>
      <select name="shape">
        <option value="round">Round</option>
        <option value="square">Square</option>
      </select>
    </form>"""

    form = BeautifulSoup(form_html, "lxml").form
    browser = mechanicalsoup.Browser()
    response = browser._request(form)
    assert response.json()['form'] == {'shape': 'round'}


def test__request_disabled_attr(httpbin):
    """Make sure that disabled form controls are not submitted."""
    form_html = f"""
    <form method="post" action="{httpbin.url}/post">
      <input disabled name="nosubmit" value="1" />
    </form>"""

    browser = mechanicalsoup.Browser()
    response = browser._request(BeautifulSoup(form_html, "lxml").form)
    assert response.json()['form'] == {}


def test__request_disabled_fieldset(httpbin):
    """Controls inside a disabled <fieldset> must not be submitted, but the
    fieldset's first <legend> stays enabled (per the HTML specification)."""
    form_html = f"""
    <form method="post" action="{httpbin.url}/post">
      <input name="enabled" value="1" />
      <fieldset disabled>
        <legend>
          <input name="in_legend" value="legend" />
        </legend>
        <input name="text" value="2" />
        <input type="checkbox" name="check" value="on" checked />
        <select name="sel"><option value="x" selected>x</option></select>
        <textarea name="area">hi</textarea>
        <button name="btn" value="go">Go</button>
        <fieldset>
          <input name="nested" value="3" />
        </fieldset>
      </fieldset>
    </form>"""

    browser = mechanicalsoup.Browser()
    response = browser._request(BeautifulSoup(form_html, "lxml").form)
    # Only the control outside the fieldset and the one in its first <legend>
    # are submitted; everything else in the disabled fieldset (including a
    # nested, non-disabled fieldset) is omitted.
    assert response.json()['form'] == {'enabled': '1', 'in_legend': 'legend'}


@pytest.mark.parametrize("keyword", [
    pytest.param('method'),
    pytest.param('url'),
])
def test_request_keyword_error(keyword):
    """Make sure exception is raised if kwargs duplicates an arg."""
    form_html = "<form></form>"
    browser = mechanicalsoup.Browser()
    with pytest.raises(TypeError, match="multiple values for"):
        browser._request(BeautifulSoup(form_html, "lxml").form,
                         'myurl', **{keyword: 'somevalue'})


def test_no_404(httpbin):
    browser = mechanicalsoup.Browser()
    resp = browser.get(httpbin + "/nosuchpage")
    assert resp.status_code == 404


def test_404(httpbin):
    browser = mechanicalsoup.Browser(raise_on_404=True)
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser.get(httpbin + "/nosuchpage")
    resp = browser.get(httpbin.url)
    assert resp.status_code == 200


def test_set_cookiejar(httpbin):
    """Set cookies locally and test that they are received remotely."""
    # construct a phony cookiejar and attach it to the session
    jar = RequestsCookieJar()
    jar.set('field', 'value')
    assert jar.get('field') == 'value'

    browser = mechanicalsoup.Browser()
    browser.set_cookiejar(jar)
    resp = browser.get(httpbin + "/cookies")
    assert resp.json() == {'cookies': {'field': 'value'}}


def test_get_cookiejar(httpbin):
    """Test that cookies set by the remote host update our session."""
    browser = mechanicalsoup.Browser()
    resp = browser.get(httpbin + "/cookies/set?k1=v1&k2=v2")
    assert resp.json() == {'cookies': {'k1': 'v1', 'k2': 'v2'}}

    jar = browser.get_cookiejar()
    assert jar.get('k1') == 'v1'
    assert jar.get('k2') == 'v2'


def test_post(httpbin):
    browser = mechanicalsoup.Browser()
    data = {'color': 'blue', 'colorblind': 'True'}
    resp = browser.post(httpbin + "/post", data)
    assert resp.status_code == 200 and resp.json()['form'] == data


def test_put(httpbin):
    browser = mechanicalsoup.Browser()
    data = {'color': 'blue', 'colorblind': 'True'}
    resp = browser.put(httpbin + "/put", data)
    assert resp.status_code == 200 and resp.json()['form'] == data


@pytest.mark.parametrize("http_html_expected_encoding", [
    pytest.param((None, 'utf-8', 'utf-8')),
    pytest.param(('utf-8', 'utf-8', 'utf-8')),
    pytest.param(('utf-8', None, 'utf-8')),
    pytest.param(('utf-8', 'ISO-8859-1', 'utf-8')),
])
def test_encoding(httpbin, http_html_expected_encoding):
    http_encoding = http_html_expected_encoding[0]
    html_encoding = http_html_expected_encoding[1]
    expected_encoding = http_html_expected_encoding[2]

    url = 'mock://encoding'
    text = (
        '<!doctype html>'
        + '<html lang="fr">'
        + (
            (
                '<head><meta charset="'
                + html_encoding
                + '"><title>Titleéàè</title></head>'
            ) if html_encoding
            else ''
        )
        + '<body></body>'
        + '</html>'
    )

    browser, adapter = prepare_mock_browser()
    mock_get(
        adapter,
        url=url,
        reply=(
            text.encode(http_encoding)
            if http_encoding
            else text.encode("utf-8")
        ),
        content_type=(
            'text/html'
            + (
                ';charset=' + http_encoding
                if http_encoding
                else ''
            )
        )
    )
    browser.open(url)
    assert browser.page.original_encoding == expected_encoding


@pytest.mark.parametrize("reply, is_html", [
    pytest.param(b'<html><body>hi</body></html>', True, id='lowercase'),
    pytest.param(b'<HTML><BODY>hi</BODY></HTML>', True, id='uppercase'),
    pytest.param(b'  \n<!DocType html><html></html>', True, id='doctype'),
    pytest.param('<html><body>hi</body></html>'.encode('utf-16'), True,
                 id='utf-16'),
    pytest.param(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR', False, id='png'),
    pytest.param(b'{"key": "value"}', False, id='json'),
])
def test_looks_like_html_without_content_type(reply, is_html):
    """Detect HTML from the leading bytes when Content-Type is missing."""
    browser, adapter = prepare_mock_browser()
    url = 'mock://looks-like-html'
    mock_get(adapter, url=url, reply=reply, content_type='')
    response = browser.session.get(url)
    mechanicalsoup.Browser.add_soup(response, {'features': 'lxml'})
    assert (response.soup is not None) == is_html


def test_looks_like_html_does_not_decode_whole_body():
    """Content-Type sniffing must not decode the full (possibly huge) body."""
    browser, adapter = prepare_mock_browser()
    url = 'mock://binary'
    mock_get(adapter, url=url, reply=b'\x89PNG\r\n\x1a\n' + os.urandom(4096),
             content_type='')
    response = browser.session.get(url)

    def fail_on_decode(self):
        raise AssertionError("response body should not be decoded")

    monkeypatched = type(response)
    with mock.patch.object(monkeypatched, 'text', property(fail_on_decode)):
        mechanicalsoup.Browser.add_soup(response, {'features': 'lxml'})

    assert response.soup is None


class NonSeekableStream(io.BytesIO):
    """A readable stream that cannot be rewound, e.g. a network pipe."""
    def seekable(self):
        return False

    def seek(self, *args, **kwargs):
        raise io.UnsupportedOperation("not seekable")

    def tell(self):
        raise io.UnsupportedOperation("not seekable")


upload_form_template = """
<form method="post" action="mock://form.com/post"
      enctype="multipart/form-data">
  <input name="customer" value="Philip J. Fry" />
  <input name="pic" type="file" />
</form>
"""


def prepare_upload_form(file_value):
    form = BeautifulSoup(upload_form_template, "lxml").form
    form.find("input", {"name": "pic"})["value"] = file_value
    return form


def prepare_upload_browser():
    """A plain Browser whose requests are handled by a mock adapter."""
    adapter = requests_mock.Adapter()
    browser = mechanicalsoup.Browser(requests_adapters={"mock": adapter})
    return browser, adapter


def test_upload_retry_replayable_stream():
    """A seekable file input is rewound and resent in full on retry."""
    browser, adapter = prepare_upload_browser()
    url = "mock://form.com/post"
    adapter.register_uri(
        "POST", url,
        [{"exc": requests.exceptions.ConnectionError},
         {"text": "Success!", "status_code": 200}])

    content = b"file content to upload"
    form = prepare_upload_form(io.BytesIO(content))
    response = browser.submit(form, retries=1)

    assert response.status_code == 200
    assert len(adapter.request_history) == 2
    # Each attempt carries the current form fields and the full file
    # content: no empty body and no duplicated bytes on the retry.
    for request in adapter.request_history:
        assert b'name="customer"' in request.body
        assert b"Philip J. Fry" in request.body
        assert request.body.count(content) == 1


def test_upload_retry_partially_consumed_stream():
    """A seekable stream is replayed from its initial position, even if
    the upload started mid-stream: the retry sends the same bytes as the
    first attempt, not the whole stream and not an empty body."""
    browser, adapter = prepare_upload_browser()
    url = "mock://form.com/post"
    adapter.register_uri(
        "POST", url,
        [{"exc": requests.exceptions.ConnectionError},
         {"text": "Success!", "status_code": 200}])

    stream = io.BytesIO(b"prefix:suffix")
    stream.seek(7)  # upload starts at "suffix"
    form = prepare_upload_form(stream)
    response = browser.submit(form, retries=1)

    assert response.status_code == 200
    assert len(adapter.request_history) == 2
    for request in adapter.request_history:
        assert b"suffix" in request.body
        assert b"prefix" not in request.body


def test_upload_retry_non_replayable_stream_fails():
    """A non-seekable stream cannot be replayed: the retry fails
    explicitly instead of sending empty or duplicated content."""
    browser, adapter = prepare_upload_browser()
    url = "mock://form.com/post"
    adapter.register_uri(
        "POST", url,
        [{"exc": requests.exceptions.ConnectionError},
         {"text": "Success!", "status_code": 200}])

    form = prepare_upload_form(NonSeekableStream(b"one-shot content"))
    with pytest.raises(mechanicalsoup.UploadNotReplayableError):
        browser.submit(form, retries=1)

    # Only the first attempt was sent; the retry never hit the network.
    assert len(adapter.request_history) == 1
    assert b"one-shot content" in adapter.request_history[0].body


def test_upload_no_retry_keeps_one_shot_behavior():
    """Without retries, a non-replayable stream is uploaded normally."""
    browser, adapter = prepare_upload_browser()
    url = "mock://form.com/post"
    adapter.register_uri("POST", url, text="Success!")

    form = prepare_upload_form(NonSeekableStream(b"one-shot content"))
    response = browser.submit(form)

    assert response.status_code == 200
    assert len(adapter.request_history) == 1
    assert b"one-shot content" in adapter.request_history[0].body


def make_response(status_code, content=b""):
    response = requests.Response()
    response.status_code = status_code
    response.headers["Content-Type"] = "text/plain"
    response._content = content
    response.close = mock.Mock()
    return response


def test_upload_retry_on_status_closes_failed_response():
    """On a retryable status, the failed response is closed before
    retrying; the final response is returned to the caller unclosed."""
    browser = mechanicalsoup.Browser()
    resp_503 = make_response(503, b"try again later")
    resp_200 = make_response(200, b"ok")
    browser.session.request = mock.Mock(side_effect=[resp_503, resp_200])

    form = prepare_upload_form(io.BytesIO(b"content"))
    response = browser.submit(form, retries=1, retry_statuses=(503,))

    assert response is resp_200
    resp_503.close.assert_called_once_with()
    resp_200.close.assert_not_called()
    # Both attempts were built from the same form fields.
    assert browser.session.request.call_count == 2
    for call in browser.session.request.call_args_list:
        assert ("customer", "Philip J. Fry") in call.kwargs["data"]


def test_upload_retry_exhausted_returns_last_response():
    """When retries are exhausted on a retryable status, the last
    response is returned (and left open), as in one-shot behavior."""
    browser = mechanicalsoup.Browser()
    responses = [make_response(503) for _ in range(3)]
    browser.session.request = mock.Mock(side_effect=responses)

    form = prepare_upload_form(io.BytesIO(b"content"))
    response = browser.submit(form, retries=2, retry_statuses=(503,))

    assert response is responses[-1]
    assert browser.session.request.call_count == 3
    for earlier in responses[:-1]:
        earlier.close.assert_called_once_with()
    responses[-1].close.assert_not_called()


def test_upload_retry_exhausted_reraises_network_error():
    """When retries are exhausted on network errors, the last error
    propagates, and the browser stays usable for new requests."""
    browser, adapter = prepare_upload_browser()
    url = "mock://form.com/post"
    adapter.register_uri(
        "POST", url,
        [{"exc": requests.exceptions.ConnectionError},
         {"exc": requests.exceptions.ConnectionError}])

    form = prepare_upload_form(io.BytesIO(b"content"))
    with pytest.raises(requests.exceptions.ConnectionError):
        browser.submit(form, retries=1)
    assert len(adapter.request_history) == 2

    # The same browser remains usable: the failed attempts left no
    # stale stream, response, or state behind to pollute new requests.
    adapter.register_uri("POST", url, text="Success!")
    form = prepare_upload_form(io.BytesIO(b"new content"))
    response = browser.submit(form)
    assert response.status_code == 200
    assert len(adapter.request_history) == 3
    last_body = adapter.request_history[-1].body
    assert b"new content" in last_body
    assert b"new content" != b"content"


def test_failed_upload_does_not_pollute_next_request():
    """After an upload fails, a new upload with the same browser sends
    only the new stream's content."""
    browser, adapter = prepare_upload_browser()
    url = "mock://form.com/post"
    adapter.register_uri(
        "POST", url,
        [{"exc": requests.exceptions.ConnectionError},
         {"text": "Success!", "status_code": 200}])

    old_stream = NonSeekableStream(b"old content")
    form = prepare_upload_form(old_stream)
    with pytest.raises(mechanicalsoup.UploadNotReplayableError):
        browser.submit(form, retries=1)

    form = prepare_upload_form(io.BytesIO(b"new content"))
    response = browser.submit(form)

    assert response.status_code == 200
    assert len(adapter.request_history) == 2
    new_body = adapter.request_history[-1].body
    assert b"new content" in new_body
    assert b"old content" not in new_body


def test_upload_retry_closed_stream_fails():
    """A stream closed between attempts cannot be replayed."""
    browser, adapter = prepare_upload_browser()
    url = "mock://form.com/post"
    adapter.register_uri(
        "POST", url,
        [{"exc": requests.exceptions.ConnectionError},
         {"text": "Success!", "status_code": 200}])

    stream = io.BytesIO(b"content")
    form = prepare_upload_form(stream)

    original_request = browser.session.request

    def close_stream_on_failure(**kwargs):
        try:
            return original_request(**kwargs)
        finally:
            stream.close()

    browser.session.request = mock.Mock(side_effect=close_stream_on_failure)
    # First (and only) attempt fails and the stream gets closed; the
    # retry must fail explicitly rather than send anything.
    with pytest.raises(mechanicalsoup.UploadNotReplayableError):
        browser.submit(form, retries=1)
    assert len(adapter.request_history) == 1


def test_upload_retry_negative_retries_rejected():
    browser = mechanicalsoup.Browser()
    form = prepare_upload_form(io.BytesIO(b"content"))
    with pytest.raises(ValueError):
        browser.submit(form, retries=-1)


if __name__ == '__main__':
    pytest.main(sys.argv)
