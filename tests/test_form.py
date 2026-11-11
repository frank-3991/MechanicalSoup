import sys
from urllib.parse import parse_qsl, urlparse

import bs4
import pytest
import setpath  # noqa:F401, must come before 'import mechanicalsoup'
from utils import mock_get, prepare_mock_browser, setup_mock_browser

import mechanicalsoup


def test_construct_form_fail():
    """Form objects must be constructed from form html elements."""
    soup = bs4.BeautifulSoup('<notform>This is not a form</notform>', 'lxml')
    tag = soup.find('notform')
    assert isinstance(tag, bs4.element.Tag)
    with pytest.warns(FutureWarning, match="from a 'notform'"):
        mechanicalsoup.Form(tag)


def test_submit_online(httpbin):
    """Complete and submit the pizza form at http://httpbin.org/forms/post """
    browser = mechanicalsoup.Browser()
    page = browser.get(httpbin + "/forms/post")
    form = mechanicalsoup.Form(page.soup.form)

    input_data = {"custname": "Philip J. Fry"}
    form.input(input_data)

    check_data = {"size": "large", "topping": ["cheese"]}
    form.check(check_data)
    check_data = {"size": "medium", "topping": "onion"}
    form.check(check_data)

    form.textarea({"comments": "warm"})
    form.textarea({"comments": "actually, no, not warm"})
    form.textarea({"comments": "freezer"})

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


def test_submit_set(httpbin):
    """Complete and submit the pizza form at http://httpbin.org/forms/post """
    browser = mechanicalsoup.Browser()
    page = browser.get(httpbin + "/forms/post")
    form = mechanicalsoup.Form(page.soup.form)

    form["custname"] = "Philip J. Fry"

    form["size"] = "medium"
    form["topping"] = ("cheese", "onion")

    form["comments"] = "freezer"

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


@pytest.mark.parametrize("expected_post", [
    pytest.param(
        [
            ('text', 'Setting some text!'),
            ('comment', 'Testing preview page'),
            ('preview', 'Preview Page'),
        ], id='preview'),
    pytest.param(
        [
            ('text', '= Heading =\n\nNew page here!\n'),
            ('comment', 'Created new page'),
            ('save', 'Submit changes'),
        ], id='save'),
    pytest.param(
        [
            ('text', '= Heading =\n\nNew page here!\n'),
            ('comment', 'Testing choosing cancel button'),
            ('cancel', 'Cancel'),
        ], id='cancel'),
])
def test_choose_submit(expected_post):
    browser, url = setup_mock_browser(expected_post=expected_post)
    browser.open(url)
    form = browser.select_form('#choose-submit-form')
    browser['text'] = dict(expected_post)['text']
    browser['comment'] = dict(expected_post)['comment']
    submit = form.choose_submit(expected_post[2][0])
    assert submit == form.submit_chosen
    res = browser.submit_selected()
    assert res.status_code == 200 and res.text == 'Success!'


@pytest.mark.parametrize("value", [
    pytest.param('continue', id='first'),
    pytest.param('cancel', id='second'),
])
def test_choose_submit_from_selector(value):
    """Test choose_submit by passing a CSS selector argument."""
    text = """
    <form method="post" action="mock://form.com/post">
      <input type="submit" name="do" value="continue" />
      <input type="submit" name="do" value="cancel" />
    </form>"""
    browser, url = setup_mock_browser(expected_post=[('do', value)], text=text)
    browser.open(url)
    form = browser.select_form()
    submits = form.form.select(f'input[value="{value}"]')
    assert len(submits) == 1
    form.choose_submit(submits[0])
    res = browser.submit_selected()
    assert res.status_code == 200 and res.text == 'Success!'


choose_submit_fail_form = '''
<html>
  <form id="choose-submit-form">
    <input type="submit" name="test_submit" value="Test Submit" />
  </form>
</html>
'''


@pytest.mark.parametrize("select_name", [
    pytest.param({'name': 'does_not_exist', 'fails': True}, id='not found'),
    pytest.param({'name': 'test_submit', 'fails': False}, id='found'),
])
def test_choose_submit_fail(select_name):
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page(choose_submit_fail_form)
    form = browser.select_form('#choose-submit-form')
    if select_name['fails']:
        with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
            form.choose_submit(select_name['name'])
    else:
        assert form.form.input == form.choose_submit(select_name['name'])


def test_choose_submit_twice():
    """Test that choose_submit can be used multiple times."""
    text = '''
    <form>
      <input type="submit" name="test1" value="Test1" />
      <input type="submit" name="test2" value="Test2" />
    </form>
    '''
    soup = bs4.BeautifulSoup(text, 'lxml')
    form = mechanicalsoup.Form(soup.form)
    test1 = form.choose_submit('test1')
    test2 = form.choose_submit('test2')
    assert test1 != test2


choose_submit_multiple_match_form = '''
<html>
  <form id="choose-submit-form">
    <input type="submit" name="test_submit" value="First Submit" />
    <input type="submit" name="test_submit" value="Second Submit" />
  </form>
</html>
'''


def test_choose_submit_multiple_match():
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page(choose_submit_multiple_match_form)
    form = browser.select_form('#choose-submit-form')
    with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
        form.choose_submit('test_submit')


submit_form_noaction = '''
<html>
  <body>
    <form id="choose-submit-form">
      <input type="text" name="text1" value="someValue1" />
      <input type="text" name="text2" value="someValue2" />
      <input type="submit" name="save" />
    </form>
  </body>
</html>
'''


def test_form_noaction():
    browser, url = setup_mock_browser()
    browser.open_fake_page(submit_form_noaction, url=url)
    form = browser.select_form('#choose-submit-form')
    form['text1'] = 'newText1'
    res = browser.submit_selected()
    assert res.status_code == 200 and browser.url == url


submit_form_action = '''
<html>
  <body>
    <form id="choose-submit-form" action="mock://form.com">
      <input type="text" name="text1" value="someValue1" />
      <input type="text" name="text2" value="someValue2" />
      <input type="submit" name="save" />
    </form>
  </body>
</html>
'''


def test_form_action():
    browser, url = setup_mock_browser()
    # for info about example.com see: https://tools.ietf.org/html/rfc2606
    browser.open_fake_page(submit_form_action,
                           url="http://example.com/invalid/")
    form = browser.select_form('#choose-submit-form')
    form['text1'] = 'newText1'
    res = browser.submit_selected()
    assert res.status_code == 200 and browser.url == url


set_select_form = '''
<html>
  <form method="post" action="mock://form.com/post">
    <select name="entree">
      <option value="tofu" selected="selected">Tofu Stir Fry</option>
      <option value="curry">Red Curry</option>
      <option value="tempeh">Tempeh Tacos</option>
    </select>
    <input type="submit" value="Select" />
  </form>
</html>
'''


@pytest.mark.parametrize("option", [
    pytest.param({'result': [('entree', 'tofu')], 'default': True},
                 id='default'),
    pytest.param({'result': [('entree', 'curry')], 'default': False},
                 id='selected'),
])
def test_set_select(option):
    '''Test the branch of Form.set that finds "select" elements.'''
    browser, url = setup_mock_browser(expected_post=option['result'],
                                      text=set_select_form)
    browser.open(url)
    browser.select_form('form')
    if not option['default']:
        browser[option['result'][0][0]] = option['result'][0][1]
    res = browser.submit_selected()
    assert res.status_code == 200 and res.text == 'Success!'


set_select_multiple_form = '''
<form method="post" action="mock://form.com/post">
  <select name="instrument" multiple>
    <option value="piano">Piano</option>
    <option value="bass">Bass</option>
    <option value="violin">Violin</option>
  </select>
  <input type="submit" value="Select Multiple" />
</form>
'''


@pytest.mark.parametrize("options", [
    pytest.param('bass', id='select one (str)'),
    pytest.param(('bass',), id='select one (tuple)'),
    pytest.param(('piano', 'violin'), id='select two'),
])
def test_set_select_multiple(options):
    """Test a <select multiple> element."""
    # When a browser submits multiple selections, the qsl looks like:
    #  name=option1&name=option2
    if not isinstance(options, list) and not isinstance(options, tuple):
        expected = [('instrument', options)]
    else:
        expected = [('instrument', option) for option in options]
    browser, url = setup_mock_browser(expected_post=expected,
                                      text=set_select_multiple_form)
    browser.open(url)
    form = browser.select_form('form')
    form.set_select({'instrument': options})
    res = browser.submit_selected()
    assert res.status_code == 200 and res.text == 'Success!'


def test_form_not_found():
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page(page_with_various_fields)
    form = browser.select_form('form')
    with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
        form.input({'foo': 'bar', 'nosuchname': 'nosuchval'})
    with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
        form.check({'foo': 'bar', 'nosuchname': 'nosuchval'})
    with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
        form.check({'entree': 'cheese'})
    with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
        form.check({'topping': 'tofu'})
    with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
        form.textarea({'bar': 'value', 'foo': 'nosuchval'})
    with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
        form.set_radio({'size': 'tiny'})
    with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
        form.set_select({'entree': ('no_multiple', 'no_multiple')})


def test_form_set_radio_checkbox(capsys):
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page(page_with_various_fields,
                           url="http://example.com/invalid/")
    form = browser.select_form("form")
    form.set_radio({"size": "small"})
    form.set_checkbox({"topping": "cheese"})
    browser.form.print_summary()
    out, err = capsys.readouterr()
    # Different versions of bs4 show either <input></input> or
    # <input/>. Normalize before comparing.
    out = out.replace('></input>', '/>')
    assert out == """<input name="foo"/>
<textarea name="bar"></textarea>
<select name="entree">
<option selected="selected" value="tofu">Tofu Stir Fry</option>
<option value="curry">Red Curry</option>
<option value="tempeh">Tempeh Tacos</option>
</select>
<input name="topping" type="checkbox" value="bacon"/>
<input checked="" name="topping" type="Checkbox" value="cheese"/>
<input name="topping" type="checkbox" value="onion"/>
<input name="topping" type="checkbox" value="mushroom"/>
<input checked="" name="size" type="Radio" value="small"/>
<input name="size" type="radio" value="medium"/>
<input name="size" type="radio" value="large"/>
<button name="action" value="cancel">Cancel</button>
<input type="submit" value="Select"/>
"""
    assert err == ""


page_with_radio = '''
<html>
  <form method="post">
    <input type=checkbox name="foo" value="bacon"> This is a checkbox
  </form>
</html>
'''


def test_form_check_uncheck():
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page(page_with_radio, url="http://example.com/invalid/")
    form = browser.select_form('form')
    assert "checked" not in form.form.find("input", {"name": "foo"}).attrs

    form["foo"] = True
    assert form.form.find("input", {"name": "foo"}).attrs["checked"] == ""

    # Test explicit unchecking (skipping the call to Form.uncheck_all)
    form.set_checkbox({"foo": False}, uncheck_other_boxes=False)
    assert "checked" not in form.form.find("input", {"name": "foo"}).attrs


page_with_various_fields = '''
<html>
  <form method="post">
    <input name="foo">
    <textarea name="bar">
    </textarea>
    <select name="entree">
      <option value="tofu" selected="selected">  Tofu Stir Fry </option>
      <option value="curry">    Red Curry</option>
      <option value="tempeh">Tempeh Tacos    </option>
    </select>
    <fieldset>
     <legend> Pizza Toppings </legend>
     <p><label> <input type=checkbox name="topping"
      value="bacon"> Bacon </label></p>
     <p><label> <input type=Checkbox name="topping"
      value="cheese" checked>Extra Cheese   </label></p>
     <p><label> <input type=checkbox name="topping"
      value="onion" checked> Onion </label></p>
     <p><label> <input type=checkbox name="topping"
      value="mushroom"> Mushroom </label></p>
    </fieldset>
    <p><input name="size" type=Radio value="small">Small</p>
    <p><input name="size" type=radio value="medium">Medium</p>
    <p><input name="size" type=radio value="large">Large</p>
    <button name="action"  value="cancel">Cancel</button>
    <input type="submit" value="Select" />
  </form>
</html>
'''


def test_form_print_summary(capsys):
    browser = mechanicalsoup.StatefulBrowser()
    browser.open_fake_page(page_with_various_fields,
                           url="http://example.com/invalid/")
    browser.select_form("form")
    browser.form.print_summary()
    out, err = capsys.readouterr()
    # Different versions of bs4 show either <input></input> or
    # <input/>. Normalize before comparing.
    out = out.replace('></input>', '/>')
    assert out == """<input name="foo"/>
<textarea name="bar"></textarea>
<select name="entree">
<option selected="selected" value="tofu">Tofu Stir Fry</option>
<option value="curry">Red Curry</option>
<option value="tempeh">Tempeh Tacos</option>
</select>
<input name="topping" type="checkbox" value="bacon"/>
<input checked="" name="topping" type="Checkbox" value="cheese"/>
<input checked="" name="topping" type="checkbox" value="onion"/>
<input name="topping" type="checkbox" value="mushroom"/>
<input name="size" type="Radio" value="small"/>
<input name="size" type="radio" value="medium"/>
<input name="size" type="radio" value="large"/>
<button name="action" value="cancel">Cancel</button>
<input type="submit" value="Select"/>
"""
    assert err == ""


def test_issue180():
    """Test that a KeyError is not raised when Form.choose_submit is called
    on a form where a submit element is missing its name-attribute."""
    browser = mechanicalsoup.StatefulBrowser()
    html = '''
<form>
  <input type="submit" value="Invalid" />
  <input type="submit" name="valid" value="Valid" />
</form>
'''
    browser.open_fake_page(html)
    form = browser.select_form()
    with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
        form.choose_submit('not_found')


def test_issue158():
    """Test that form elements are processed in their order on the page
    and that elements with duplicate name-attributes are not clobbered."""
    issue158_form = '''
<form method="post" action="mock://form.com/post">
  <input name="box" type="hidden" value="1"/>
  <input checked="checked" name="box" type="checkbox" value="2"/>
  <input name="box" type="hidden" value="0"/>
  <input type="submit" value="Submit" />
</form>
'''
    expected_post = [('box', '1'), ('box', '2'), ('box', '0')]
    browser, url = setup_mock_browser(expected_post=expected_post,
                                      text=issue158_form)
    browser.open(url)
    browser.select_form()
    res = browser.submit_selected()
    assert res.status_code == 200 and res.text == 'Success!'
    browser.close()


def test_duplicate_submit_buttons():
    """Tests that duplicate submits doesn't break form submissions
    See issue https://github.com/MechanicalSoup/MechanicalSoup/issues/264"""
    issue264_form = '''
<form method="post" action="mock://form.com/post">
  <input name="box" type="hidden" value="1"/>
  <input name="search" type="submit" value="Search"/>
  <input name="search" type="submit" value="Search"/>
</form>
'''
    expected_post = [('box', '1'), ('search', 'Search')]
    browser, url = setup_mock_browser(expected_post=expected_post,
                                      text=issue264_form)
    browser.open(url)
    browser.select_form()
    res = browser.submit_selected()
    assert res.status_code == 200 and res.text == 'Success!'
    browser.close()


@pytest.mark.parametrize("expected_post", [
    pytest.param([('sub2', 'val2')], id='submit button'),
    pytest.param([('sub4', 'val4')], id='typeless button'),
    pytest.param([('sub5', 'val5')], id='submit input'),
])
def test_choose_submit_buttons(expected_post):
    """Buttons of type reset and button are not valid submits"""
    text = """
    <form method="post" action="mock://form.com/post">
      <button type="butTon" name="sub1" value="val1">Val1</button>
      <button type="suBmit" name="sub2" value="val2">Val2</button>
      <button type="reset" name="sub3" value="val3">Val3</button>
      <button name="sub4" value="val4">Val4</button>
      <input type="subMit" name="sub5" value="val5">
    </form>
    """
    browser, url = setup_mock_browser(expected_post=expected_post, text=text)
    browser.open(url)
    browser.select_form()
    res = browser.submit_selected(btnName=expected_post[0][0])
    assert res.status_code == 200 and res.text == 'Success!'


@pytest.mark.parametrize("fail, selected, expected_post", [
    pytest.param(False, 'with_value', [('selector', 'with_value')],
                 id='Option with value'),
    pytest.param(False, 'Without value', [('selector', 'Without value')],
                 id='Option without value'),
    pytest.param(False, 'We have a value here', [('selector', 'with_value')],
                 id='Option with value selected by its text'),
    pytest.param(True, 'Unknown option', None,
                 id='Unknown option, must raise a LinkNotFound exception')
])
def test_option_without_value(fail, selected, expected_post):
    """Option tag in select can have no value option"""
    text = """
    <form method="post" action="mock://form.com/post">
      <select name="selector">
        <option value="with_value">We have a value here</option>
        <option>Without value</option>
      </select>
      <button type="submit">Submit</button>
    </form>
    """
    browser, url = setup_mock_browser(expected_post=expected_post,
                                      text=text)
    browser.open(url)
    browser.select_form()
    if fail:
        with pytest.raises(mechanicalsoup.utils.LinkNotFoundError):
            browser['selector'] = selected
    else:
        browser['selector'] = selected

        res = browser.submit_selected()
        assert res.status_code == 200 and res.text == 'Success!'


def request_target(request):
    """Return the scheme://host/path part of a captured request URL."""
    parts = urlparse(request.url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def request_fields(request):
    """Return the ordered (name, value) fields of a captured request,
    taken from the query string (GET forms) and from the body (POST
    forms)."""
    query = urlparse(request.url).query
    return parse_qsl(query) + parse_qsl(request.text or "")


def assert_submission(request, method, target, fields):
    """Check a captured request, with failure messages that identify
    whether the mismatch is in the HTTP method, the submission target,
    or the collected fields."""
    assert request.method == method, (
        f"submission method: expected {method}, got {request.method} "
        f"for {request.url}")
    actual_target = request_target(request)
    assert actual_target == target, (
        f"submission target: expected {target}, got {actual_target}")
    actual_fields = request_fields(request)
    assert actual_fields == fields, (
        f"field collection: expected {fields}, got {actual_fields}")


def mock_post_reply(mock, url, marker):
    """Register a POST endpoint that replies with an identifiable page."""
    mock.register_uri(
        'POST', url,
        text=f'<html><body>{marker}</body></html>',
        headers={'Content-Type': 'text/html'})


formaction_form = '''
<html>
  <body>
    <form id="formaction-form" method="post" action="mock://form.com/default">
      <input type="hidden" name="box" value="1" />
      <input type="checkbox" name="box" value="2" checked="checked" />
      <input type="hidden" name="box" value="0" />
      <select name="multi" multiple="multiple">
        <option value="a" selected="selected">A</option>
        <option value="b" selected="selected">B</option>
      </select>
      <button type="submit" name="go" value="default">Default</button>
      <button type="submit" name="go" value="override"
              formaction="mock://form.com/override">Override</button>
    </form>
  </body>
</html>
'''

# Same-name fields and multi-value fields, in page order. The submit
# control is appended last, at its position in the form.
formaction_base_fields = [
    ('box', '1'),
    ('box', '2'),
    ('box', '0'),
    ('multi', 'a'),
    ('multi', 'b'),
]


@pytest.mark.parametrize("submit_value, target", [
    pytest.param('default', 'mock://form.com/default',
                 id='default form action'),
    pytest.param('override', 'mock://form.com/override',
                 id='button formaction override'),
])
def test_submit_button_formaction(submit_value, target):
    """A submit button's formaction overrides the form's action, while
    the collected fields (including same-name and multi-value fields)
    keep the same order, and the browser state reflects the endpoint
    that actually answered."""
    url = 'mock://form.com'
    browser, mock = prepare_mock_browser()
    mock_get(mock, url, formaction_form)
    mock_post_reply(mock, 'mock://form.com/default', 'default target')
    mock_post_reply(mock, 'mock://form.com/override', 'override target')

    browser.open(url)
    form = browser.select_form('#formaction-form')
    button = form.form.select_one(f'button[value="{submit_value}"]')
    assert button is not None
    form.choose_submit(button)
    res = browser.submit_selected()

    assert res.status_code == 200, (
        f"response status: expected 200, got {res.status_code}")
    # One GET (page open) followed by one POST (form submission).
    assert len(mock.request_history) == 2
    request = mock.request_history[-1]
    assert_submission(request, 'POST', target,
                      formaction_base_fields + [('go', submit_value)])

    # The final page state comes from the endpoint that was submitted to.
    marker = 'default target' if submit_value == 'default' \
        else 'override target'
    assert browser.url == target, (
        f"final page URL: expected {target}, got {browser.url}")
    assert marker in browser.page.get_text(), (
        f"final page content: expected {marker!r} in page")


disabled_controls_form = '''
<html>
  <body>
    <form id="disabled-form" method="{method}"
          action="http://form.test/submit">
      <input type="text" name="active" value="yes" />
      <input type="text" name="disabled_field" value="no"
             disabled="disabled" />
      <fieldset disabled="disabled">
        <input type="text" name="in_disabled_fieldset" value="no" />
      </fieldset>
      <input type="submit" name="go" value="Go" />
      <input type="submit" name="disabled_submit" value="No"
             disabled="disabled" />
    </form>
  </body>
</html>
'''


@pytest.mark.parametrize("method", [
    pytest.param('get', id='GET form'),
    pytest.param('post', id='POST form'),
])
def test_submit_disabled_controls(method):
    """Disabled controls (including controls inside a disabled fieldset)
    are never submitted, whatever the submission method. GET forms send
    the fields in the query string, POST forms in the request body."""
    # Use the http scheme (mocked locally) because requests drops the
    # query string of non-http URLs, which would discard GET parameters.
    # The 'http://' mount prefix replaces the session's default adapter.
    url = 'http://form.test'
    browser, mock = prepare_mock_browser(scheme='http://')
    mock_get(mock, url, disabled_controls_form.format(method=method))
    mock_post_reply(mock, 'http://form.test/submit', 'submitted')
    mock_get(mock, 'http://form.test/submit',
             '<html><body>submitted</body></html>')

    browser.open(url)
    browser.select_form('#disabled-form')
    res = browser.submit_selected(btnName='go')

    assert res.status_code == 200, (
        f"response status: expected 200, got {res.status_code}")
    assert len(mock.request_history) == 2
    request = mock.request_history[-1]
    assert_submission(request, method.upper(), 'http://form.test/submit',
                      [('active', 'yes'), ('go', 'Go')])
    # No disabled control leaked into the request, in either location.
    assert 'disabled' not in urlparse(request.url).query
    assert 'disabled' not in (request.text or "")
    assert browser.url.startswith('http://form.test/submit'), (
        f"final page URL: unexpected {browser.url}")


flaky_form = '''
<html>
  <body>
    <form id="flaky-form" method="post" action="mock://form.com/flaky">
      <input type="hidden" name="box" value="1" />
      <input type="checkbox" name="box" value="2" checked="checked" />
      <input type="text" name="comment" value="" />
      <input type="submit" name="save" value="Save" />
    </form>
  </body>
</html>
'''


def test_failed_submit_does_not_corrupt_form():
    """A failed submission must not mutate the form: retrying the same
    form afterwards sends exactly the same fields, in the same order,
    and the failed attempt leaves the browser state untouched."""
    url = 'mock://form.com'
    browser, mock = prepare_mock_browser()
    mock_get(mock, url, flaky_form)
    mock.register_uri('POST', 'mock://form.com/flaky', [
        {'text': '<html><body>error</body></html>', 'status_code': 500},
        {'text': '<html><body>recovered</body></html>',
         'status_code': 200},
    ])

    browser.open(url)
    browser.select_form('#flaky-form')
    browser['comment'] = 'first attempt'

    # Keep the form selected when the submission fails, so that the
    # same form can be retried.
    res_fail = browser.submit_selected(update_state=False)
    assert res_fail.status_code == 500, (
        f"response status: expected 500, got {res_fail.status_code}")
    assert browser.url == url, (
        f"failed submission moved the browser to {browser.url}")

    res_ok = browser.submit_selected()
    assert res_ok.status_code == 200, (
        f"response status: expected 200, got {res_ok.status_code}")

    expected_fields = [
        ('box', '1'),
        ('box', '2'),
        ('comment', 'first attempt'),
        ('save', 'Save'),
    ]
    posts = [r for r in mock.request_history if r.method == 'POST']
    assert len(posts) == 2
    for request in posts:
        assert_submission(request, 'POST', 'mock://form.com/flaky',
                          expected_fields)

    assert browser.url == 'mock://form.com/flaky', (
        f"final page URL: unexpected {browser.url}")
    assert 'recovered' in browser.page.get_text(), (
        "final page content: expected 'recovered' in page")


if __name__ == '__main__':
    pytest.main(sys.argv)
