"""Email backends for hosts that cannot use SMTP.

Render's FREE web services block outbound SMTP (ports 25, 465 and 587), so Django's normal Gmail backend cannot
connect there. AppsScriptRelayBackend sends the same messages over HTTPS (port 443, never blocked) through a small
Google Apps Script web app that belongs to the shop's own Google account -- the email is then sent BY Gmail itself
(a real Gmail sender, signed by Google), at no cost. The script to paste into Google is apps_script/mail_relay.gs.

Turn it on with these environment variables (see README.md, "Customer emails"):
    EMAIL_BACKEND=accounts.email_backends.AppsScriptRelayBackend
    EMAIL_RELAY_URL=<the web app's /exec link>
    EMAIL_RELAY_SECRET=<the same secret typed into the script>
"""
import base64
import json
from email.utils import parseaddr

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class EmailRelayError(Exception):
    """The relay refused the email or did not answer properly. The text is written to be shown to the owner."""


class AppsScriptRelayBackend(BaseEmailBackend):
    """Sends each message by POSTing it to the Apps Script web app. Never puts the secret in an error."""

    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages or []:
            try:
                self._send_one(message)
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
        return sent

    def _send_one(self, message):
        url = getattr(settings, "EMAIL_RELAY_URL", "")
        secret = getattr(settings, "EMAIL_RELAY_SECRET", "")
        if not url or not secret:
            raise EmailRelayError("The mail relay is not set up: EMAIL_RELAY_URL and EMAIL_RELAY_SECRET are missing.")
        recipients = list(message.to)
        if not recipients:
            raise EmailRelayError("This email has no recipient.")

        html = next(
            (content for content, mimetype in getattr(message, "alternatives", []) if mimetype == "text/html"), ""
        )
        # Images the HTML refers to as cid:... (the logo), found in the assembled message and sent along as base64
        # so the script can attach them.
        inline = {}
        for part in message.message().walk():
            content_id = str(part.get("Content-ID") or "").strip("<> ")
            if content_id and part.get_content_maintype() == "image":
                inline[content_id] = base64.b64encode(part.get_payload(decode=True)).decode("ascii")

        payload = {
            "secret": secret,
            "to": ",".join(recipients),
            "subject": " ".join(str(message.subject).split()),   # one line, whatever was passed in
            "text": message.body or "",
            "html": html,
            "name": parseaddr(message.from_email or settings.DEFAULT_FROM_EMAIL)[0] or "Arabela",
            "inline": inline,
        }
        try:
            # text/plain keeps this a "simple" request; Apps Script answers with a redirect that requests follows.
            response = requests.post(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "text/plain;charset=utf-8"},
                timeout=getattr(settings, "EMAIL_RELAY_TIMEOUT", 25),
            )
        except requests.ConnectionError as exc:      # includes a connection that never opened in time
            raise EmailRelayError(f"Could not reach the mail relay: {type(exc).__name__}.")
        except requests.Timeout:
            raise EmailRelayError("The mail relay took too long to answer. Please try again in a minute.")
        except requests.RequestException as exc:
            raise EmailRelayError(f"Could not reach the mail relay: {type(exc).__name__}.")

        try:
            answer = response.json()
        except ValueError:
            raise EmailRelayError(
                "The mail relay did not answer properly. In Google Apps Script, deploy it as a Web app "
                "(Execute as: Me, Who has access: Anyone) and use the newest /exec link."
            )
        if not isinstance(answer, dict) or not answer.get("ok"):
            reason = str(answer.get("error", "")) if isinstance(answer, dict) else ""
            if "secret" in reason.lower():
                reason = "the secret in the script does not match EMAIL_RELAY_SECRET"
            raise EmailRelayError(f"The mail relay refused the email: {reason or 'no reason given'}.")
