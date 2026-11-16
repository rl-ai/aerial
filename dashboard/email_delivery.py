#!/usr/bin/env python3
"""Email delivery for the Aerial exec report and alerts.

Reads Gmail creds from the profile's secrets dir (default
~/.aerial/secrets/gmail, business.{user,pass}; Gmail app-password,
smtp.gmail.com:587). Sends the daily board HTML plus a text summary
to the profile's recipient_email. Used by the board cron and the
notification engine's ping path.

All deployment values come from config/deployment.yaml (see
deployment_profile.py) with AERIAL_* env-var overrides. The generic
OSS default has no recipient — delivery is a no-op returning False.
"""
from __future__ import annotations

import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import sys as _sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from deployment_profile import value as _pv          # noqa: E402

SECRETS = Path(_pv("email.secrets_dir")).expanduser()
DEFAULT_TO = _pv("email.recipient_email") or ""
SMTP_HOST = _pv("email.smtp_host")
SMTP_PORT = int(_pv("email.smtp_port"))


def _cred(name: str) -> str:
    return (SECRETS / f"business.{name}").read_text(encoding="utf-8").strip()


def send_report(subject: str, text_body: str,
                html_attachment: Path | None = None,
                to: str = DEFAULT_TO,
                html_body: str | None = None) -> bool:
    """Send the exec report. Returns True on success; never raises.

    html_body: inline styled HTML (multipart/alternative with the plain
    text) — the format iPhone Mail renders natively."""
    try:
        if html_body:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = _cred("user")
            msg["To"] = to
            msg.attach(MIMEText(text_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        else:
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = _cred("user")
            msg["To"] = to
            msg.attach(MIMEText(text_body, "plain", "utf-8"))
        if html_attachment and Path(html_attachment).exists():
            att = MIMEApplication(Path(html_attachment).read_bytes(),
                                  _subtype="html")
            att.add_header("Content-Disposition", "attachment",
                           filename=Path(html_attachment).name)
            msg.attach(att)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(_cred("user"), _cred("pass"))
            s.send_message(msg)
        return True
    except Exception as exc:
        from deployment_profile import pvalue
        log = pvalue("email_log")
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            fh.write(f"{Path(html_attachment or 'send').name}: FAIL {exc}\n")
        return False


def send_alert(text: str, to: str = DEFAULT_TO) -> bool:
    return send_report("Aerial alert", text, None, to)


if __name__ == "__main__":
    import sys
    body = sys.argv[1] if len(sys.argv) > 1 else "Aerial email delivery test."
    ok = send_report("Aerial — email delivery test", body)
    print("sent" if ok else "FAILED (see email_log in the deployment profile)")
