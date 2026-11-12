"""Experiment: POST refresh/replay behavior of MechanicalSoup StatefulBrowser."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import mechanicalsoup
from mechanicalsoup import Browser

LOG = []          # every request the server receives
WRITES = []       # every accepted write


def record(handler, kind):
    length = int(handler.headers.get("Content-Length") or 0)
    body = handler.rfile.read(length).decode() if length else ""
    entry = {
        "seq": len(LOG) + 1,
        "kind": kind,
        "method": handler.command,
        "path": handler.path,
        "cookie": handler.headers.get("Cookie"),
        "body": body,
    }
    LOG.append(entry)
    return entry


FORM_PAGE = """<html><body>
<form id="f-direct" method="post" action="/submit-direct">
  <input type="text" name="item" value="widget"/><input type="submit"/>
</form>
<form id="f-303" method="post" action="/submit-303">
  <input type="text" name="item" value="widget"/><input type="submit"/>
</form>
<form id="f-307" method="post" action="/submit-307">
  <input type="text" name="item" value="widget"/><input type="submit"/>
</form>
<form id="f-drop" method="post" action="/submit-drop">
  <input type="text" name="item" value="widget"/><input type="submit"/>
</form>
</body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _html(self, text, cookie=None, status=200):
        payload = ("<html><body>" + text + "</body></html>").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(payload)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location, status, cookie=None):
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def do_GET(self):
        if self.path == "/form":
            record(self, "get-form")
            self._html(FORM_PAGE, cookie="sid=FORM-1; Path=/")
        elif self.path == "/result303":
            record(self, "result")
            self._html("result after 303")
        else:
            record(self, "get-other")
            self._html("other")

    def do_POST(self):
        if self.path == "/submit-direct":
            e = record(self, "write")
            WRITES.append(e)
            self._html("direct result", cookie="sid=AFTER-DIRECT; Path=/")
        elif self.path == "/submit-303":
            e = record(self, "write")
            WRITES.append(e)
            self._redirect("/result303", 303, cookie="sid=AFTER-303; Path=/")
        elif self.path == "/submit-307":
            record(self, "redirector")  # no write here; write happens at target
            self._redirect("/write307", 307, cookie="sid=AFTER-307; Path=/")
        elif self.path == "/write307":
            e = record(self, "write")
            WRITES.append(e)
            self._html("result after 307")
        elif self.path == "/submit-drop":
            e = record(self, "write")
            WRITES.append(e)
            # Simulate response loss: write accepted, connection dropped
            # without any response bytes.
            self.close_connection = True
            self.connection.shutdown(1)
            self.connection.close()
        else:
            record(self, "post-other")
            self._html("other")


def state_snapshot(br, label):
    req = br._StatefulBrowser__state.request
    snap = {
        "label": label,
        "browser.url": br.url,
        "session.cookies": (requests.utils.dict_from_cookiejar(
            br.session.cookies) if br.session else None),
        "saved_request": None if req is None else {
            "method": req.method,
            "url": req.url,
            "cookie_header": req.headers.get("Cookie"),
            "body": (req.body if isinstance(req.body, str) else
                     (req.body.decode() if req.body else req.body)),
        },
    }
    print("\n--- STATE [%s] ---" % label)
    print(json.dumps(snap, indent=2))
    return snap


def show_writes(tag):
    print("\n=== WRITES after %s: %d ===" % (tag, len(WRITES)))
    for w in WRITES:
        print("  seq=%d %s %s cookie=%s body=%s" %
              (w["seq"], w["method"], w["path"], w["cookie"], w["body"]))


def recent_requests(n=4):
    for e in LOG[-n:]:
        print("  server saw seq=%d %s %s cookie=%s body=%s [%s]" %
              (e["seq"], e["method"], e["path"], e["cookie"], e["body"],
               e["kind"]))


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_port

    print("\n########## A. POST -> 200 HTML directly ##########")
    br = mechanicalsoup.StatefulBrowser()
    br.open(base + "/form")
    state_snapshot(br, "A: after open /form")
    br.select_form("#f-direct")
    br.submit_selected()
    state_snapshot(br, "A: after submit_selected (direct 200)")
    show_writes("A submit")
    print("refresh() now replays the saved request:")
    br.refresh()
    state_snapshot(br, "A: after refresh")
    show_writes("A refresh")
    recent_requests(2)

    print("\n########## B. POST -> 303 redirect ##########")
    br2 = mechanicalsoup.StatefulBrowser()
    br2.open(base + "/form")
    br2.select_form("#f-303")
    br2.submit_selected()
    state_snapshot(br2, "B: after submit_selected (303)")
    n_writes = len(WRITES)
    br2.refresh()
    state_snapshot(br2, "B: after refresh")
    print("writes delta from refresh: %d" % (len(WRITES) - n_writes))
    recent_requests(2)

    print("\n########## C. POST -> 307 redirect ##########")
    br3 = mechanicalsoup.StatefulBrowser()
    br3.open(base + "/form")
    br3.select_form("#f-307")
    br3.submit_selected()
    state_snapshot(br3, "C: after submit_selected (307)")
    n_writes = len(WRITES)
    br3.refresh()
    state_snapshot(br3, "C: after refresh")
    print("writes delta from refresh: %d" % (len(WRITES) - n_writes))
    show_writes("C refresh")

    print("\n########## D. Re-submit selected form (303 endpoint) ##########")
    br4 = mechanicalsoup.StatefulBrowser()
    br4.open(base + "/form")
    form = br4.select_form("#f-303")
    br4.submit_selected()
    n_writes = len(WRITES)
    Browser.submit(br4, form=form, url=base + "/form")
    print("writes delta from re-submit: %d" % (len(WRITES) - n_writes))
    recent_requests(2)

    print("\n########## E. submit_selected(update_state=False) ##########")
    br5 = mechanicalsoup.StatefulBrowser()
    br5.open(base + "/form")
    br5.select_form("#f-direct")
    br5.submit_selected(update_state=False)
    state_snapshot(br5, "E: after submit update_state=False")
    n_writes = len(WRITES)
    br5.refresh()
    print("writes delta from refresh: %d" % (len(WRITES) - n_writes))
    recent_requests(2)

    print("\n########## F. Cookie: saved request vs current jar ##########")
    br6 = mechanicalsoup.StatefulBrowser()
    br6.open(base + "/form")
    br6.select_form("#f-direct")
    br6.submit_selected()
    print("jar now:", requests.utils.dict_from_cookiejar(br6.session.cookies))
    saved = br6._StatefulBrowser__state.request
    print("saved request Cookie header:", saved.headers.get("Cookie"))
    print("server then saw on refresh:")
    br6.refresh()
    recent_requests(1)

    print("\n########## G. Write accepted, response lost ##########")
    br7 = mechanicalsoup.StatefulBrowser()
    br7.open(base + "/form")
    br7.select_form("#f-drop")
    n_writes = len(WRITES)
    try:
        br7.submit_selected()
        print("no exception (unexpected)")
    except requests.exceptions.ConnectionError as ex:
        print("client got ConnectionError: %s" % type(ex).__name__)
    print("writes delta: %d (server accepted the write although client "
          "saw only an error)" % (len(WRITES) - n_writes))
    state_snapshot(br7, "G: after failed submit")

    print("\n########## H. After close() ##########")
    br8 = mechanicalsoup.StatefulBrowser()
    br8.open(base + "/form")
    br8.select_form("#f-direct")
    br8.submit_selected()
    url_before = br8.url
    br8.close()
    print("page still readable:", br8.page is not None,
          "| url still:", br8.url == url_before,
          "| session:", br8.session)
    try:
        br8.refresh()
    except Exception as ex:
        print("refresh() after close() -> %s: %s" % (type(ex).__name__, ex))

    srv.shutdown()


if __name__ == "__main__":
    main()
