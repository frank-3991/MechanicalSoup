import json
import re

import pytest
import setpath  # noqa:F401, must come before 'import mechanicalsoup'
from utils import mock_get, prepare_mock_browser

import mechanicalsoup
from mechanicalsoup import (Recorder, ReplayConfirmationRequired,
                            ReplayIncompleteError, load_records,
                            replay_record)

SUBMIT_FORM = '''
<html><body>
  <form method="post" action="mock://form.com/post">
    <input type="text" name="user" value="alice" />
    <input type="password" name="password" value="" />
    <input type="text" name="comment" value="" />
    <input type="submit" name="save" value="Save" />
  </form>
</body></html>
'''


def make_recorder(tmp_path, **kwargs):
    return Recorder(str(tmp_path / "records.jsonl"), **kwargs)


def read_records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_no_recorder_by_default():
    """Without a recorder, requests behave exactly as before."""
    browser, mock = prepare_mock_browser()
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    assert browser.recorder is None
    resp = browser.open("mock://form.com")
    assert resp.status_code == 200


def test_record_navigation(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)

    resp = browser.open("mock://form.com")
    assert resp.status_code == 200

    (record,) = recorder.records()
    assert record.operation == "navigation"
    assert record.method == "GET"
    assert record.url == "mock://form.com"
    assert record.status == "completed"
    assert record.response_status == 200
    assert record.final_url == "mock://form.com"
    assert record.page_url == "mock://form.com"
    assert record.redirects == []


def test_record_submission_preserves_field_order(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    mock.register_uri("POST", "mock://form.com/post", text="Success!")

    browser.open("mock://form.com")
    browser.select_form()
    browser["comment"] = "hello"
    resp = browser.submit_selected()
    assert resp.text == "Success!"

    records = recorder.records()
    assert len(records) == 2
    record = records[1]
    assert record.operation == "submission"
    assert record.method == "POST"
    assert record.url == "mock://form.com/post"
    assert record.status == "completed"
    # Field order matches the order of controls in the form.
    assert record.fields == [("user", "alice"), ("password", ""),
                             ("comment", "hello"), ("save", "Save")]


def test_record_redirect_chain(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock.register_uri(
        "GET", "mock://form.com/start", status_code=302,
        headers={"Location": "mock://form.com/middle"})
    mock.register_uri(
        "GET", "mock://form.com/middle", status_code=302,
        headers={"Location": "mock://form.com/end"})
    mock_get(mock, "mock://form.com/end", "<html>done</html>")

    resp = browser.open("mock://form.com/start")
    assert resp.url == "mock://form.com/end"

    (record,) = recorder.records()
    assert record.status == "completed"
    assert [r["url"] for r in record.redirects] == [
        "mock://form.com/start", "mock://form.com/middle"]
    assert [r["status_code"] for r in record.redirects] == [302, 302]
    assert record.final_url == "mock://form.com/end"
    assert record.page_url == "mock://form.com/end"


def test_redaction_rules(tmp_path):
    recorder = make_recorder(
        tmp_path,
        redact_fields=["password", re.compile("token")],
        redact_cookies=["session"],
    )
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    mock.register_uri("POST", "mock://form.com/post", text="Success!")

    browser.open("mock://form.com")
    browser.select_form()
    browser["password"] = "s3cret"
    browser.submit_selected(
        cookies={"session": "abc", "other": "ok"},
        auth=("alice", "p4ssw0rd"),
        headers={"X-Api-Token": "zzz"},
    )

    record = recorder.records()[-1]
    assert ("password", "s3cret") not in record.fields
    assert ("password", "<redacted>") in record.fields
    assert ("user", "alice") in record.fields
    assert record.cookies == {"session": "<redacted>", "other": "ok"}
    assert record.auth == {"type": "basic", "username": "alice",
                           "password": "<redacted>"}
    # The raw password must not appear anywhere in the record file.
    raw = open(recorder.path, encoding="utf-8").read()
    assert "s3cret" not in raw
    assert "p4ssw0rd" not in raw


def test_file_content_not_recorded(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock.register_uri("POST", "mock://form.com/upload", text="ok")

    with open(__file__, "rb") as f:
        browser.post("mock://form.com/upload",
                     files={"file": ("test_recorder.py", f)})

    (record,) = recorder.records()
    assert record.files == [{"name": "file", "filename": "test_recorder.py",
                             "one_shot": True}]
    raw = open(recorder.path, encoding="utf-8").read()
    assert "test_record_redirect_chain" not in raw  # no file content


def test_recording_failure_does_not_affect_request(tmp_path):
    # Point the recorder at a path that cannot be written.
    recorder = Recorder(str(tmp_path / "no-such-dir" / "records.jsonl"))
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)

    resp = browser.open("mock://form.com")
    assert resp.status_code == 200
    assert resp.soup is not None


def test_recording_failure_does_not_affect_exception(tmp_path):
    recorder = Recorder(str(tmp_path / "no-such-dir" / "records.jsonl"))
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    browser.open("mock://form.com")
    browser.select_form()
    # The POST endpoint is not registered: the adapter raises.
    with pytest.raises(Exception):
        browser.submit_selected()


def test_failed_request_recorded_as_unknown(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    browser.open("mock://form.com")
    browser.select_form()
    # No POST endpoint registered: sending fails after preparation.
    with pytest.raises(Exception):
        browser.submit_selected()

    record = recorder.records()[-1]
    assert record.method == "POST"
    assert record.status == "unknown"
    assert record.error is not None


def test_unpreparable_request_recorded_as_not_sent(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    # A malformed URL fails during request preparation, before
    # anything is sent to the server.
    with pytest.raises(Exception):
        browser.get("http://[::1")

    (record,) = recorder.records()
    assert record.status == "not_sent"
    assert record.error is not None


def test_replay_navigation(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    browser.open("mock://form.com")

    (record,) = recorder.records()

    # Replay on a fresh browser against the locally controlled mock.
    replay_browser, mock2 = prepare_mock_browser()
    mock_get(mock2, "mock://form.com", SUBMIT_FORM)
    resp = replay_record(replay_browser, record)
    assert resp.status_code == 200
    assert resp.soup is not None


def test_replay_post_requires_confirmation(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock.register_uri("POST", "mock://form.com/post", text="ok")
    browser.post("mock://form.com/post", data=[("a", "1")])

    (record,) = recorder.records()
    replay_browser, _ = prepare_mock_browser()
    with pytest.raises(ReplayConfirmationRequired):
        replay_record(replay_browser, record)


def test_replay_post_with_confirmation(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock.register_uri("POST", "mock://form.com/post", text="ok")
    browser.post("mock://form.com/post",
                 data=[("a", "1"), ("b", "2"), ("a", "3")])

    (record,) = recorder.records()

    replay_browser, mock2 = prepare_mock_browser()
    mock2.register_uri("POST", "mock://form.com/post", text="replayed")
    resp = replay_record(replay_browser, record, confirm_write=True)
    assert resp.text == "replayed"
    # Field order (and duplicates) are preserved between record and
    # replay.
    from urllib.parse import parse_qsl
    sent = parse_qsl(mock2.request_history[-1].text)
    assert sent == [("a", "1"), ("b", "2"), ("a", "3")]


def test_replay_does_not_repeat_redirect_write(tmp_path):
    """Replaying a POST that originally redirected must not re-send the
    write that occurred inside the redirect chain."""
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock.register_uri(
        "POST", "mock://form.com/post", status_code=302,
        headers={"Location": "mock://form.com/done"})
    mock_get(mock, "mock://form.com/done", "<html>done</html>")
    browser.post("mock://form.com/post", data=[("a", "1")])

    (record,) = recorder.records()
    assert [r["status_code"] for r in record.redirects] == [302]

    replay_browser, mock2 = prepare_mock_browser()
    mock2.register_uri(
        "POST", "mock://form.com/post", status_code=302,
        headers={"Location": "mock://form.com/done"})
    mock_get(mock2, "mock://form.com/done", "<html>done</html>")
    resp = replay_record(replay_browser, record, confirm_write=True)
    # Only the recorded operation itself is sent: the redirect target
    # is not requested again.
    assert resp.status_code == 302
    assert len(mock2.request_history) == 1
    assert mock2.request_history[-1].method == "POST"


def test_replay_redacted_field_requires_supplement(tmp_path):
    recorder = make_recorder(tmp_path, redact_fields=["password"])
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock.register_uri("POST", "mock://form.com/post", text="ok")
    browser.post("mock://form.com/post",
                 data=[("user", "alice"), ("password", "s3cret")])

    (record,) = recorder.records()
    replay_browser, mock2 = prepare_mock_browser()
    mock2.register_uri("POST", "mock://form.com/post", text="ok")

    # Missing supplement: refuse to send.
    with pytest.raises(ReplayIncompleteError, match="password"):
        replay_record(replay_browser, record, confirm_write=True)
    assert len(mock2.request_history) == 0

    # Caller supplies the redacted value.
    resp = replay_record(replay_browser, record, confirm_write=True,
                         fields={"password": "n3w-secret"})
    assert resp.status_code == 200
    from urllib.parse import parse_qsl
    sent = parse_qsl(mock2.request_history[-1].text)
    assert sent == [("user", "alice"), ("password", "n3w-secret")]


def test_replay_redacted_auth_requires_supplement(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    browser.open("mock://form.com", auth=("alice", "p4ssw0rd"))

    (record,) = recorder.records()
    assert record.auth["password"] == "<redacted>"

    replay_browser, mock2 = prepare_mock_browser()
    mock_get(mock2, "mock://form.com", SUBMIT_FORM)
    with pytest.raises(ReplayIncompleteError, match="authentication"):
        replay_record(replay_browser, record)

    resp = replay_record(replay_browser, record,
                         auth=("alice", "p4ssw0rd"))
    assert resp.status_code == 200
    assert "Authorization" in mock2.request_history[-1].headers


def test_replay_redacted_cookie_requires_supplement(tmp_path):
    recorder = make_recorder(tmp_path, redact_cookies=["session"])
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    browser.open("mock://form.com", cookies={"session": "abc"})

    (record,) = recorder.records()
    replay_browser, mock2 = prepare_mock_browser()
    mock_get(mock2, "mock://form.com", SUBMIT_FORM)
    with pytest.raises(ReplayIncompleteError, match="cookie"):
        replay_record(replay_browser, record)

    resp = replay_record(replay_browser, record,
                         cookies={"session": "abc"})
    assert resp.status_code == 200
    assert "session=abc" in mock2.request_history[-1].headers["Cookie"]


def test_replay_consumed_upload_stream_refused(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock.register_uri("POST", "mock://form.com/upload", text="ok")

    with open(__file__, "rb") as f:
        browser.post("mock://form.com/upload",
                     files={"file": ("test_recorder.py", f)})
    # The one-time stream is now consumed.

    (record,) = recorder.records()
    replay_browser, mock2 = prepare_mock_browser()
    mock2.register_uri("POST", "mock://form.com/upload", text="ok")
    with pytest.raises(ReplayIncompleteError, match="one-time stream"):
        replay_record(replay_browser, record, confirm_write=True)
    assert len(mock2.request_history) == 0

    # Supplying the file again makes the replay possible.
    with open(__file__, "rb") as f:
        resp = replay_record(replay_browser, record, confirm_write=True,
                             files={"file": ("test_recorder.py", f)})
    assert resp.status_code == 200


def test_replay_state_reflects_only_completed_requests(tmp_path):
    """The browser state is untouched when a replay is refused, and
    reflects the replayed request when it completes."""
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    browser.open("mock://form.com")
    (record,) = recorder.records()

    replay_browser, mock2 = prepare_mock_browser()
    mock_get(mock2, "mock://form.com", SUBMIT_FORM)
    replay_browser.open_fake_page("<html>original</html>",
                                  url="mock://form.com/original")

    # A refused replay does not change the page/form state.
    redacted = mechanicalsoup.Record(
        operation="submission", method="POST", url="mock://form.com/post",
        fields=[("password", "<redacted>")])
    with pytest.raises(ReplayIncompleteError):
        replay_record(replay_browser, redacted, confirm_write=True)
    assert replay_browser.url == "mock://form.com/original"

    # A completed replay is reflected in the state the caller chooses to
    # update; the low-level replay itself returns the response.
    resp = replay_record(replay_browser, record)
    assert resp.soup is not None


def test_load_records_and_dict_replay(tmp_path):
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock_get(mock, "mock://form.com", SUBMIT_FORM)
    browser.open("mock://form.com")

    records = load_records(recorder.path)
    assert len(records) == 1

    replay_browser, mock2 = prepare_mock_browser()
    mock_get(mock2, "mock://form.com", SUBMIT_FORM)
    # A plain dict (as stored in the file) is accepted too.
    resp = replay_record(replay_browser, records[0].to_dict())
    assert resp.status_code == 200


def test_record_post_positional_data(tmp_path):
    """Positional ``data`` (as in session.post) is recorded correctly."""
    recorder = make_recorder(tmp_path)
    browser, mock = prepare_mock_browser()
    browser.recorder = recorder
    mock.register_uri("POST", "mock://form.com/post", text="ok")

    resp = browser.post("mock://form.com/post",
                        {"color": "blue", "colorblind": "True"})
    assert resp.status_code == 200

    (record,) = recorder.records()
    assert record.method == "POST"
    assert record.fields == [("color", "blue"), ("colorblind", "True")]
    from urllib.parse import parse_qsl
    sent = parse_qsl(mock.request_history[-1].text)
    assert sent == [("color", "blue"), ("colorblind", "True")]
    assert mock.request_history[-1].query == ""
