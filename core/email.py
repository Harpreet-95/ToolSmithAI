import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from core.config import (
    ENABLE_REAL_EMAIL,
    FRONTEND_BASE_URL,
    MAX_STEP_RETRIES,
    RETRY_BACKOFF_SECONDS,
    SMTP_FROM_EMAIL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_TLS,
    SMTP_USERNAME,
)


def send_verification_email(email: str, token: str, user_id: str | None = None) -> None:
    url = f"{FRONTEND_BASE_URL}/verify-email?token={token}"
    print(
        f"\n[DEV] Verification email for {email}\n"
        f"      Open this link to verify your account:\n"
        f"      {url}\n"
    )
    result = send_real_email(
        to=email,
        subject="Verify your ToolSmith account",
        body=(
            f"Click the link below to verify your email address:\n\n"
            f"{url}\n\n"
            f"This link expires in 24 hours."
        ),
        user_id=user_id,
        email_type="verification",
    )
    if not result["sent"] and ENABLE_REAL_EMAIL:
        print(f"[WARN] Verification email not delivered to {email}: {result.get('reason', 'unknown')}")


def send_real_email(
    to: str,
    subject: str,
    body: str,
    *,
    user_id: str | None = None,
    report_id: int | None = None,
    email_type: str = "report",
) -> dict:
    """Send an email via SMTP. Returns a result dict — never raises.

    Retries up to MAX_STEP_RETRIES times on SMTP failure. All attempts are
    recorded in email_logs. Logging failures never block delivery.
    """
    log_id: int | None = None
    try:
        from data.email_log_service import create_email_log
        log_id = create_email_log(
            recipient_email=to,
            subject=subject,
            email_type=email_type,
            user_id=user_id,
            report_id=report_id,
        )
    except Exception:
        pass

    def _update_log(
        status: str,
        attempt: int,
        error: str | None = None,
        sent_at: str | None = None,
    ) -> None:
        if log_id is None:
            return
        try:
            from data.email_log_service import update_email_log_status
            update_email_log_status(log_id, status, attempt, error, sent_at)
        except Exception:
            pass

    if not ENABLE_REAL_EMAIL:
        reason = (
            "Email delivery is disabled. "
            "Set ENABLE_REAL_EMAIL=true in .env to enable."
        )
        _update_log("simulated", 0, reason)
        return {"sent": False, "reason": reason}

    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not SMTP_FROM_EMAIL:
        reason = (
            "SMTP not configured. "
            "Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM_EMAIL in .env."
        )
        _update_log("failed", 0, reason)
        return {"sent": False, "reason": reason}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = to
    msg.attach(MIMEText(body, "plain"))

    last_error: str = ""
    max_attempts = MAX_STEP_RETRIES + 1

    for attempt in range(1, max_attempts + 1):
        try:
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

            _update_log("sent", attempt, sent_at=datetime.now(timezone.utc).isoformat())
            return {"sent": True, "to": to, "subject": subject}

        except Exception as exc:
            last_error = str(exc)
            if attempt < max_attempts:
                _update_log("pending", attempt, last_error)
                time.sleep(RETRY_BACKOFF_SECONDS)

    _update_log("failed", max_attempts, last_error)
    return {"sent": False, "reason": last_error}
