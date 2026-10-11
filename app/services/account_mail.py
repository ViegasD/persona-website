"""SMTP delivery called by the durable outbox dispatcher."""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from app.core.settings import get_settings


def mail_is_configured() -> bool:
    settings = get_settings()
    return bool(settings.smtp_host and settings.smtp_from)


async def send_account_email(payload: dict) -> None:
    settings = get_settings()
    if not mail_is_configured():
        raise RuntimeError("Account SMTP is not configured")
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = payload["to"]
    message["Subject"] = payload["subject"]
    message.set_content(payload["body"])

    def send():
        client = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
        with client(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.smtp_starttls and not settings.smtp_ssl:
                smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password or "")
            smtp.send_message(message)

    await asyncio.to_thread(send)
