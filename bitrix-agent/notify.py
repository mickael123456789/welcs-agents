"""Отправка отчёта на почту через SMTP (по умолчанию Gmail)."""

from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _md_to_html(text: str) -> str:
    import html
    safe = html.escape(text)
    return (
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "font-size:15px;line-height:1.5;white-space:pre-wrap;max-width:720px\">"
        f"{safe}</div>"
    )


def send_email(report: str, cfg: dict, subject: str) -> None:
    host = cfg.get("SMTP_HOST", "smtp.gmail.com")
    port = int(cfg.get("SMTP_PORT", 587))
    user = cfg["SMTP_USER"]
    password = cfg["SMTP_PASSWORD"]
    to_addr = cfg.get("REPORT_TO", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(report, "plain", "utf-8"))
    msg.attach(MIMEText(_md_to_html(report), "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(user, [a.strip() for a in to_addr.split(",")], msg.as_string())
