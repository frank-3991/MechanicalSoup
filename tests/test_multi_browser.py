"""Integration tests for interleaved scraping with multiple browsers.

These tests exercise two StatefulBrowser instances used in an
interleaved way, covering independent sessions as well as an explicitly
shared requests.Session. They pin down the boundary between what is
shared through the session (cookies) and what always remains per
browser (current page, URL and selected form), and between forms built
on an independently copied page tree (isolated) and forms wrapping the
same page tree (linked, by the existing contract).
"""

import copy
from urllib.parse import parse_qsl

import pytest
import setpath  # noqa:F401, must come before 'import mechanicalsoup'
from utils import mock_get, prepare_mock_browser

import mechanicalsoup
import requests
import requests_mock

BASE = 'mock://site.test'
URL_A = BASE + '/page-a'
URL_A2 = BASE + '/page-a2'
URL_B = BASE + '/page-b'
URL_GONE = BASE + '/gone'
POST_A = BASE + '/post-a'
POST_B = BASE + '/post-b'


def form_page(origin, post_url):
    """A page whose form has the same control names as every other
    page built here, so that cross-browser mix-ups would be visible.
    The hidden ``origin`` field identifies which page's form actually
    got submitted."""
    return f'''
<html>
  <body>
    <form method="post" action="{post_url}">
      <input type="hidden" name="origin" value="{origin}" />
      <input type="text" name="comment" value="" />
      <input type="submit" name="preview" value="Preview" />
      <input type="submit" name="save" value="Save" />
    </form>
  </body>
</html>
'''


class Recorder:
    """Capture the requests reaching a mocked endpoint so that the
    final request content (fields, chosen submit button, cookies) can
    be checked after the fact."""

    def __init__(self):
        self.requests = []

    def callback(self, request, context):
        self.requests.append(request)
        return 'Success!'

    def register_post(self, adapter, url):
        adapter.register_uri('POST', url, text=self.callback)

    def body(self, index=0):
        return parse_qsl(self.requests[index].text)

    def cookies(self, index=0):
        return self.requests[index].headers.get('Cookie')


def independent_browsers():
    """Two browsers with fully independent sessions and adapters."""
    browser_a, adapter_a = prepare_mock_browser()
    browser_b, adapter_b = prepare_mock_browser()
    return (browser_a, adapter_a), (browser_b, adapter_b)


def shared_session_browsers():
    """Two browsers explicitly sharing one requests.Session (and thus
    one cookie jar), served by one mock adapter."""
    session = requests.Session()
    adapter = requests_mock.Adapter()
    session.mount('mock://', adapter)
    browser_a = mechanicalsoup.StatefulBrowser(session=session)
    browser_b = mechanicalsoup.StatefulBrowser(session=session)
    return (browser_a, adapter), (browser_b, adapter)


@pytest.fixture(params=[independent_browsers, shared_session_browsers],
                ids=['independent-sessions', 'shared-session'])
def browser_pair(request):
    """A pair of browsers, either independent or sharing a session."""
    return request.param()


def register_pages(adapter, with_a2=False):
    mock_get(adapter, URL_A, form_page('page-a', POST_A))
    mock_get(adapter, URL_B, form_page('page-b', POST_B))
    if with_a2:
        mock_get(adapter, URL_A2, form_page('page-a2', POST_A))


def open_and_fill(browser, url, comment, submit_name):
    browser.open(url)
    browser.select_form('form')
    browser['comment'] = comment
    browser.form.choose_submit(submit_name)


@pytest.mark.parametrize('submit_order', ['ab', 'ba'])
def test_interleaved_submissions_independent_sessions(submit_order):
    """Two browsers alternately fill and submit same-named controls.
    Whatever the submission order, each browser sends the fields and
    the submit button of its own page."""
    (browser_a, adapter_a), (browser_b, adapter_b) = \
        independent_browsers()
    register_pages(adapter_a)
    register_pages(adapter_b)
    recorder_a, recorder_b = Recorder(), Recorder()
    recorder_a.register_post(adapter_a, POST_A)
    recorder_b.register_post(adapter_b, POST_B)

    # Interleave the filling of both forms.
    browser_a.open(URL_A)
    browser_b.open(URL_B)
    browser_a.select_form('form')
    browser_a['comment'] = 'draft-from-a'
    browser_a.form.choose_submit('save')
    browser_b.select_form('form')
    browser_b['comment'] = 'comment-from-b'
    browser_b.form.choose_submit('preview')
    # A changes its mind after B filled its own same-named control.
    browser_a['comment'] = 'comment-from-a'

    # Filling one browser's form did not move the other browser.
    assert browser_a.url == URL_A
    assert browser_b.url == URL_B
    origin_a = browser_a.page.select_one('input[name="origin"]')
    origin_b = browser_b.page.select_one('input[name="origin"]')
    assert origin_a['value'] == 'page-a'
    assert origin_b['value'] == 'page-b'

    browsers = {'a': browser_a, 'b': browser_b}
    for key in submit_order:
        browsers[key].submit_selected()

    assert recorder_a.body() == [
        ('origin', 'page-a'), ('comment', 'comment-from-a'),
        ('save', 'Save')]
    assert recorder_b.body() == [
        ('origin', 'page-b'), ('comment', 'comment-from-b'),
        ('preview', 'Preview')]
    # Independent sessions: no cookie is exchanged in either direction.
    assert recorder_a.cookies() is None
    assert recorder_b.cookies() is None


def test_navigation_failure_and_reselect_keep_pages_apart(browser_pair):
    """After one browser fails a navigation and then re-selects a form
    on another page, the other browser still submits the fields and the
    submit button of its own page."""
    (browser_a, adapter_a), (browser_b, adapter_b) = browser_pair
    for adapter in dict.fromkeys((adapter_a, adapter_b)):
        register_pages(adapter, with_a2=True)
        mock_get(adapter, URL_GONE, 'gone', status_code=404)
    recorder_a, recorder_b = Recorder(), Recorder()
    recorder_a.register_post(adapter_a, POST_A)
    recorder_b.register_post(adapter_b, POST_B)

    open_and_fill(browser_a, URL_A, 'comment-from-a', 'save')
    open_and_fill(browser_b, URL_B, 'comment-from-b', 'preview')

    # A failed navigation leaves the failing browser's state untouched.
    browser_a.raise_on_404 = True
    with pytest.raises(mechanicalsoup.LinkNotFoundError):
        browser_a.open(URL_GONE)
    assert browser_a.url == URL_A
    assert browser_a.form.form.select_one(
        'input[name="comment"]')['value'] == 'comment-from-a'

    # A moves to another page and re-selects a form there.
    open_and_fill(browser_a, URL_A2, 'rewritten-by-a', 'preview')

    # B is still on its own page with its own filled form.
    assert browser_b.url == URL_B
    assert browser_b.form.form.select_one(
        'input[name="comment"]')['value'] == 'comment-from-b'

    browser_b.submit_selected()
    browser_a.submit_selected()

    assert recorder_b.body() == [
        ('origin', 'page-b'), ('comment', 'comment-from-b'),
        ('preview', 'Preview')]
    assert recorder_a.body() == [
        ('origin', 'page-a2'), ('comment', 'rewritten-by-a'),
        ('preview', 'Preview')]


def test_shared_session_shares_cookies_but_not_state():
    """With an explicitly shared session, cookies set while one browser
    browses are sent by the other browser, but page, URL and selected
    form remain per-browser."""
    (browser_a, adapter), (browser_b, _) = shared_session_browsers()
    session = browser_a.session

    def set_cookie_page(request, context):
        # Simulate a server Set-Cookie landing in the shared jar.
        session.cookies.set('shared', 'from-a', domain='site.test',
                            path='/')
        return form_page('page-a', POST_A)

    adapter.register_uri('GET', URL_A, text=set_cookie_page)
    mock_get(adapter, URL_A2, form_page('page-a2', POST_A))
    mock_get(adapter, URL_B, form_page('page-b', POST_B))
    recorder_b = Recorder()
    recorder_b.register_post(adapter, POST_B)

    browser_a.open(URL_A)
    browser_b.open(URL_B)
    # The cookie from A's response was sent on B's own request.
    assert adapter.request_history[-1].headers.get('Cookie') == \
        'shared=from-a'

    browser_b.select_form('form')
    browser_b['comment'] = 'comment-from-b'
    browser_b.form.choose_submit('preview')

    # A navigates elsewhere: B's page, URL and form do not move.
    browser_a.open(URL_A2)
    assert browser_b.url == URL_B
    assert browser_b.page.select_one(
        'input[name="origin"]')['value'] == 'page-b'
    assert browser_b.form.form.select_one(
        'input[name="comment"]')['value'] == 'comment-from-b'

    browser_b.submit_selected()
    assert recorder_b.body() == [
        ('origin', 'page-b'), ('comment', 'comment-from-b'),
        ('preview', 'Preview')]
    # The shared cookie is also part of B's final POST request.
    assert recorder_b.cookies() == 'shared=from-a'


def test_independent_sessions_do_not_share_cookies():
    """With independent sessions, a cookie in one browser's jar is not
    sent by the other browser."""
    (browser_a, adapter_a), (browser_b, adapter_b) = \
        independent_browsers()
    register_pages(adapter_a)
    register_pages(adapter_b)
    browser_a.session.cookies.set('shared', 'from-a',
                                  domain='site.test', path='/')

    browser_b.open(URL_B)
    assert adapter_b.request_history[-1].headers.get('Cookie') is None

    # Sanity check: A does send its own cookie.
    browser_a.open(URL_A)
    assert adapter_a.request_history[-1].headers.get('Cookie') == \
        'shared=from-a'


def test_forms_from_copied_page_are_isolated():
    """Forms taken from an independently copied page tree: field edits
    and submit-button choices are isolated between the two trees."""
    browser, adapter = prepare_mock_browser()
    register_pages(adapter)
    recorder = Recorder()
    recorder.register_post(adapter, POST_A)

    browser.open(URL_A)
    copied_page = copy.copy(browser.page)
    form_original = mechanicalsoup.Form(browser.page.select_one('form'))
    form_copied = mechanicalsoup.Form(copied_page.select_one('form'))

    form_original.set_input({'comment': 'original-comment'})
    form_original.choose_submit('save')
    form_copied.set_input({'comment': 'copied-comment'})
    form_copied.choose_submit('preview')

    # Field edits live in separate trees.
    assert browser.page.select_one(
        'input[name="comment"]')['value'] == 'original-comment'
    assert copied_page.select_one(
        'input[name="comment"]')['value'] == 'copied-comment'
    # The chosen submit buttons are independent as well.
    assert form_original.submit_chosen['value'] == 'Save'
    assert form_copied.submit_chosen['value'] == 'Preview'

    mechanicalsoup.Browser.submit(browser, form_original, URL_A)
    mechanicalsoup.Browser.submit(browser, form_copied, URL_A)
    assert recorder.body(0) == [
        ('origin', 'page-a'), ('comment', 'original-comment'),
        ('save', 'Save')]
    assert recorder.body(1) == [
        ('origin', 'page-a'), ('comment', 'copied-comment'),
        ('preview', 'Preview')]


def test_forms_sharing_one_page_tree_are_linked():
    """Two Form objects wrapping the same page tree share the tree
    content by the existing contract: a field set through one is
    visible through the other. Only the chosen submit button is stored
    on each Form object itself."""
    browser, adapter = prepare_mock_browser()
    register_pages(adapter)
    recorder = Recorder()
    recorder.register_post(adapter, POST_A)

    browser.open(URL_A)
    browser.select_form('form')
    form_state = browser.form
    form_alias = mechanicalsoup.Form(browser.page.select_one('form'))

    form_state.set_input({'comment': 'shared-comment'})
    # Same underlying tree: the edit shows up through the other Form.
    assert form_alias.form.select_one(
        'input[name="comment"]')['value'] == 'shared-comment'

    # The submit choice is per-Form-object state, not tree content.
    form_state.choose_submit('save')
    assert form_alias.submit_chosen is None

    browser.submit_selected()
    assert recorder.body() == [
        ('origin', 'page-a'), ('comment', 'shared-comment'),
        ('save', 'Save')]


def test_old_page_form_submission_after_navigation():
    """A form kept from an old page can still be submitted after the
    browser moved to a new page; it sends the old page's fields and
    does not disturb the browser's current state."""
    browser, adapter = prepare_mock_browser()
    register_pages(adapter)
    recorder_a, recorder_b = Recorder(), Recorder()
    recorder_a.register_post(adapter, POST_A)
    recorder_b.register_post(adapter, POST_B)

    open_and_fill(browser, URL_A, 'old-page-comment', 'save')
    old_form = browser.form
    old_url = browser.url

    # The browser enters a new page; the old form is no longer current.
    browser.open(URL_B)
    assert browser.get_current_form() is None

    mechanicalsoup.Browser.submit(browser, old_form, old_url)
    assert recorder_a.body() == [
        ('origin', 'page-a'), ('comment', 'old-page-comment'),
        ('save', 'Save')]
    # The low-level submit did not move the browser away from page-b.
    assert browser.url == URL_B

    # The new page's form is unaffected and usable as usual.
    browser.select_form('form')
    browser['comment'] = 'new-page-comment'
    browser.form.choose_submit('preview')
    browser.submit_selected()
    assert recorder_b.body() == [
        ('origin', 'page-b'), ('comment', 'new-page-comment'),
        ('preview', 'Preview')]


if __name__ == '__main__':
    pytest.main(__file__)
