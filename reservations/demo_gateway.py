"""A PRACTICE payment gateway for showing the online payments without a PayMongo account (`python demo.py`, or `python manage.py demo_paymongo`).

It speaks the small part of PayMongo's API the shop uses, the way PayMongo's documentation describes it -- HTTP Basic with the secret key as the user
name, POST /v2/checkout_sessions, GET /v1/checkout_sessions/{id}, POST /v1/checkout_sessions/{id}/expire, POST /v1/refunds -- and serves a hosted
payment page with Authorize / Fail buttons, like PayMongo's own test page. It is NOT PayMongo: every page says so, it keeps its sessions in memory
only, and it listens on this computer only (127.0.0.1). The shop talks to it only when PAYMONGO_API_BASE points here AND the shop is the practice
shop or a laptop copy (reservations/paymongo.py api_base_allowed), so a practice payment can never create a booking in the live shop."""
import base64
import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CHANNELS = (("gcash", "GCash"), ("paymaya", "Maya"), ("qrph", "QR Ph"))

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Demo payment page</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background: #eef1ef; color: #14211b; }}
  .banner {{ background: #14211b; color: #e3f0a8; text-align: center; padding: .55rem 1rem; font-size: .85rem; letter-spacing: .02em; }}
  .wrap {{ max-width: 27rem; margin: 2rem auto; padding: 0 1rem; }}
  .card {{ background: #fff; border-radius: 14px; box-shadow: 0 10px 30px rgba(20, 33, 27, .08); padding: 1.6rem; }}
  h1 {{ font-size: 1.05rem; margin: 0 0 .2rem; }}
  .shop {{ color: #5b6b62; font-size: .85rem; margin: 0 0 1.2rem; }}
  .amount {{ font-size: 2rem; font-weight: 700; margin: .2rem 0 .1rem; }}
  .what {{ color: #5b6b62; font-size: .85rem; margin: 0 0 1.3rem; }}
  .methods {{ display: grid; gap: .6rem; margin: 0 0 1.2rem; }}
  .methods label {{ display: flex; align-items: center; gap: .7rem; padding: .8rem .9rem; border: 2px solid #dfe5e1; border-radius: 10px; cursor: pointer; font-weight: 600; }}
  .methods input {{ accent-color: #14211b; }}
  .methods label:has(input:checked) {{ border-color: #14211b; background: #f6f9f7; }}
  button {{ width: 100%; padding: .85rem 1rem; border-radius: 10px; font-size: 1rem; font-weight: 700; cursor: pointer; border: 2px solid #14211b; }}
  .go {{ background: #14211b; color: #fff; }}
  .no {{ background: #fff; color: #14211b; margin-top: .6rem; }}
  .note {{ color: #5b6b62; font-size: .78rem; line-height: 1.45; margin: 1rem 0 0; text-align: center; }}
  a {{ color: #14211b; }}
</style></head><body>
<div class="banner">DEMO PAYMENT PAGE &mdash; a practice page, not PayMongo. No real money moves.</div>
<div class="wrap"><div class="card">{body}</div>
<p class="note">Practice gateway running on this computer for the project demonstration.</p></div></body></html>"""


def _page(body):
    return PAGE.format(body=body)


def make_server(host="127.0.0.1", port=8801, key="sk_test_demo"):
    """The practice gateway as an (unstarted) HTTP server. Only a local address is accepted."""
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("The practice gateway only listens on this computer (127.0.0.1).")
    state = {"sessions": {}, "refunds": {}, "n": 0}
    lock = threading.Lock()
    expected_auth = "Basic " + base64.b64encode(f"{key}:".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, payload=None, content_type="application/json"):
            body = (json.dumps(payload) if content_type == "application/json" else payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type != "application/json" else ""))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def authorised(self):
            return self.headers.get("Authorization") == expected_auth

        def read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw) if raw else {}
            except ValueError:
                return {}

        def session_json(self, sid):
            session = state["sessions"][sid]
            return {"data": {"id": sid, "type": "checkout_session", "attributes": {
                "status": session["status"], "reference_number": session["attrs"].get("reference_number"), "metadata": session["attrs"].get("metadata", {}),
                "livemode": False, "payments": session["payments"], "checkout_url": f"http://{host}:{port}/pay/{sid}"}}}

        def paid(self, session):
            return any(p["attributes"]["status"] == "paid" for p in session["payments"])

        def payment_page(self, sid):
            session = state["sessions"].get(sid)
            if session is None:
                return self.reply(404, _page("<h1>Payment page not found</h1><p>This practice payment page does not exist (the practice gateway was restarted).</p>"), "text/html")
            attrs = session["attrs"]
            back = html.escape(attrs.get("cancel_url", "#"), quote=True)
            line = (attrs.get("line_items") or [{}])[0]
            if self.paid(session):
                return self.reply(200, _page("<h1>Already paid</h1><p>This payment was already made.</p>"), "text/html")
            if session["status"] != "active":
                return self.reply(200, _page(f'<h1>This payment page is closed</h1><p>It can no longer be paid.</p><p><a href="{back}">Back to the shop</a></p>'), "text/html")
            options = "".join(
                f'<label><input type="radio" name="channel" value="{value}"{" checked" if i == 0 else ""}> {html.escape(label)}</label>' for i, (value, label) in enumerate(CHANNELS))
            body = (
                f'<h1 id="dp-title">Pay Arabela Gown Rental</h1><p class="shop">Demo checkout</p>'
                f'<div class="amount" id="dp-amount">PHP {session["amount"] / 100:,.2f}</div>'
                f'<p class="what" id="dp-what">{html.escape(str(line.get("name", "")))}</p>'
                f'<form method="post" action="/pay/{html.escape(sid, quote=True)}/authorize"><div class="methods">{options}</div>'
                f'<button class="go" id="dp-authorize" type="submit">Authorize payment</button></form>'
                f'<form method="post" action="/pay/{html.escape(sid, quote=True)}/fail"><button class="no" id="dp-fail" type="submit">Fail the payment</button></form>'
                f'<p class="note"><a id="dp-back" href="{back}">Back to the shop</a> without paying</p>'
            )
            return self.reply(200, _page(body), "text/html")

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self.reply(200, _page("<h1>Practice payment gateway</h1><p>This is the demo stand-in for PayMongo. It is running.</p>"), "text/html")
            if path.startswith("/pay/"):
                return self.payment_page(path.split("/")[2])
            if path.startswith("/v1/checkout_sessions/") or path.startswith("/v2/checkout_sessions/"):
                if not self.authorised():
                    return self.reply(401, {"errors": [{"code": "unauthorized", "detail": "The secret key is not right."}]})
                sid = path.rsplit("/", 1)[-1]
                if sid not in state["sessions"]:
                    return self.reply(404, {"errors": [{"code": "resource_not_found", "detail": "No such checkout session."}]})
                return self.reply(200, self.session_json(sid))
            if path.startswith("/v1/refunds/") or path.startswith("/refunds/"):
                if not self.authorised():
                    return self.reply(401, {"errors": [{"code": "unauthorized", "detail": "The secret key is not right."}]})
                refund = state["refunds"].get(path.rsplit("/", 1)[-1])
                if refund is None:
                    return self.reply(404, {"errors": [{"detail": "No such refund."}]})
                return self.reply(200, {"data": {"id": path.rsplit("/", 1)[-1], "type": "refund", "attributes": {
                    "status": "succeeded", "amount": refund["amount"], "payment_id": refund["payment_id"]}}})
            self.reply(404, {"errors": [{"detail": "unknown"}]})

        def do_POST(self):
            path = urlparse(self.path).path
            if path.startswith("/pay/"):
                parts = path.split("/")
                sid, action = parts[2], parts[3] if len(parts) > 3 else ""
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
                channel = parse_qs(raw).get("channel", ["gcash"])[0]
                if channel not in {value for value, _ in CHANNELS}:
                    channel = "gcash"
                with lock:
                    session = state["sessions"].get(sid)
                    if session and action == "authorize" and session["status"] == "active" and not self.paid(session):
                        session["payments"] = [{"id": f"pay_{sid}", "type": "payment", "attributes": {
                            "status": "paid", "amount": session["amount"], "source": {"type": channel}, "paid_at": int(time.time())}}]
                        return self.redirect(session["attrs"]["success_url"])
                if session and action == "fail":
                    back = html.escape(session["attrs"].get("cancel_url", "#"), quote=True)
                    again = html.escape(f"/pay/{sid}", quote=True)
                    return self.reply(200, _page(f'<h1 id="dp-failed">Payment failed</h1><p>You chose to fail this practice payment. Nothing was charged.</p>'
                                                 f'<p><a href="{again}">Try again</a> &middot; <a href="{back}">Back to the shop</a></p>'), "text/html")
                return self.reply(200, _page("<h1>This payment page is closed</h1><p>It can no longer be paid.</p>"), "text/html")
            if not self.authorised():
                return self.reply(401, {"errors": [{"code": "unauthorized", "detail": "The secret key is not right."}]})
            payload = self.read_json()
            if path == "/v2/checkout_sessions":
                attrs = (payload.get("data") or {}).get("attributes") or {}
                items = attrs.get("line_items") or []
                if not items or not attrs.get("success_url") or not attrs.get("cancel_url"):
                    return self.reply(400, {"errors": [{"code": "parameter_invalid", "detail": "line_items, success_url and cancel_url are required."}]})
                with lock:
                    state["n"] += 1
                    sid = f"cs_demo{state['n']:05d}"
                    state["sessions"][sid] = {"attrs": attrs, "status": "active", "payments": [], "amount": items[0]["amount"]}
                return self.reply(200, {"data": {"id": sid, "type": "checkout_session", "attributes": {"checkout_url": f"http://{host}:{port}/pay/{sid}", "status": "active"}}})
            if path.startswith("/v1/checkout_sessions/") and path.endswith("/expire"):
                sid = path.split("/")[3]
                with lock:
                    session = state["sessions"].get(sid)
                    if session is None:
                        return self.reply(404, {"errors": [{"detail": "No such checkout session."}]})
                    if session["status"] == "expired":
                        return self.reply(400, {"errors": [{"code": "bad", "detail": "The session is already expired"}]})
                    if self.paid(session):
                        return self.reply(400, {"errors": [{"code": "bad", "detail": "The session already has a paid payment"}]})
                    session["status"] = "expired"
                return self.reply(200, {})
            if path == "/v1/refunds":
                attrs = (payload.get("data") or {}).get("attributes") or {}
                with lock:
                    rid = f"ref_demo{len(state['refunds']) + 1:05d}"
                    state["refunds"][rid] = {"payment_id": attrs.get("payment_id", ""), "amount": attrs.get("amount", 0), "notes": attrs.get("notes", "")}
                return self.reply(200, {"data": {"id": rid, "type": "refund", "attributes": {"status": "pending", "amount": attrs.get("amount", 0), "payment_id": attrs.get("payment_id", "")}}})
            self.reply(404, {"errors": [{"detail": "unknown"}]})

    server = ThreadingHTTPServer((host, port), Handler)
    port = server.server_address[1]            # the port really bound (the page addresses must use it, also when 0 = "any free port" was asked for)
    server.demo_state = state
    return server


def start_in_background(host="127.0.0.1", port=8801, key="sk_test_demo"):
    """Start the practice gateway on a background thread; returns the server (call .shutdown() to stop it)."""
    server = make_server(host, port, key)
    thread = threading.Thread(target=server.serve_forever, name="demo-paymongo", daemon=True)
    thread.start()
    return server
