import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from core.config import (
    ENABLE_REAL_EMAIL,
    SMTP_FROM_EMAIL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_TLS,
    SMTP_USERNAME,
)

_VERIFICATION_BASE_URL = "http://localhost:5173/verify-email"


def send_verification_email(email: str, token: str) -> None:
    url = f"{_VERIFICATION_BASE_URL}?token={token}"
    print(
        f"\n[DEV] Verification email for {email}\n"
        f"      Open this link to verify your account:\n"
        f"      {url}\n"
    )
    if ENABLE_REAL_EMAIL:
        result = send_real_email(
            to=email,
            subject="Verify your ToolSmith account",
            body=(
                f"Click the link below to verify your email address:\n\n"
                f"{url}\n\n"
                f"This link expires in 24 hours."
            ),
        )
        if not result["sent"]:
            print(f"[WARN] Verification email not delivered to {email}: {result.get('reason', 'unknown')}")


def send_real_email(to: str, subject: str, body: str) -> dict:
    """Send an email via SMTP. Returns a result dict — never raises."""
    if not ENABLE_REAL_EMAIL:
        return {
            "sent": False,
            "reason": (
                "Email delivery is disabled. "
                "Set ENABLE_REAL_EMAIL=true in .env to enable."
            ),
        }

    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not SMTP_FROM_EMAIL:
        return {
            "sent": False,
            "reason": (
                "SMTP not configured. "
                "Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM_EMAIL in .env."
            ),
        }

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM_EMAIL
        msg["To"] = to
        msg.attach(MIMEText(body, "plain"))

        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM_EMAIL, to, msg.as_string())
        elif SMTP_USE_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM_EMAIL, to, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.sendmail(SMTP_FROM_EMAIL, to, msg.as_string())

        return {"sent": True, "to": to, "subject": subject}

    except Exception as exc:
        return {"sent": False, "reason": str(exc)}
