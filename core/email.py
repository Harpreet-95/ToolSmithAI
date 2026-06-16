import base64
import smtplib
import time
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from core.config import (
    EMAIL_PROVIDER,
    ENABLE_REAL_EMAIL,
    FRONTEND_BASE_URL,
    MAX_STEP_RETRIES,
    RESEND_API_KEY,
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
        subject="Verify your ToolSmithAI account",
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


def send_admin_invite_email(
    recipient_email: str,
    invite_token: str,
    created_by_user_id: str | None = None,
) -> None:
    register_url = f"{FRONTEND_BASE_URL}/register-admin?email={recipient_email}&token={invite_token}"
    print(
        f"\n[DEV] Admin invite email for {recipient_email}\n"
        f"      Token: {invite_token}\n"
        f"      Register URL: {register_url}\n"
    )
    plain_body = (
        "You have been invited to join ToolSmithAI as an Admin.\n\n"
        "Use the invite token below to complete your registration.\n\n"
        f"Invite Token:\n{invite_token}\n\n"
        f"Register here:\n{register_url}\n\n"
        "Important:\n"
        "- This invite expires in 72 hours.\n"
        "- This token is single-use. Once registered, it cannot be reused.\n\n"
        "If you did not expect this invite, you can safely ignore this email.\n\n"
        "— The ToolSmithAI Team"
    )
    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ToolSmithAI Admin Invite</title>
</head>
<body style="margin:0;padding:0;background:#0f1117;font-family:'Inter',Arial,sans-serif;color:#e2e8f0;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f1117;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background:#1a1d2e;border-radius:12px;overflow:hidden;border:1px solid #2d3148;">
          <tr>
            <td style="background:linear-gradient(135deg,#6366f1,#4f46e5);padding:32px 40px;text-align:center;">
              <div style="margin-bottom:10px;">
                <img src="{FRONTEND_BASE_URL}/toolsmith-logo-transparent.png" alt="ToolSmithAI" width="38" height="38" style="display:inline-block;vertical-align:middle;border:0;margin-right:8px;" />
                <span style="display:inline-block;vertical-align:middle;font-size:1.5rem;font-weight:800;color:#ffffff;letter-spacing:0.02em;">ToolSmithAI</span>
              </div>
              <div style="font-size:0.85rem;color:#c7d2fe;margin-top:4px;letter-spacing:0.05em;text-transform:uppercase;font-weight:600;">Admin Invitation</div>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 40px;">
              <p style="margin:0 0 16px;font-size:1rem;color:#e2e8f0;">You've been invited to join <strong style="color:#a5b4fc;">ToolSmithAI</strong> as an <strong style="color:#a5b4fc;">Admin</strong>.</p>
              <p style="margin:0 0 24px;font-size:0.9rem;color:#94a3b8;">Use the invite token below to complete your registration. This token is <strong style="color:#f1f5f9;">single-use</strong> and <strong style="color:#f1f5f9;">expires in 72 hours</strong>.</p>
              <div style="background:#0f1117;border:1px solid #4f46e5;border-radius:8px;padding:16px 20px;margin:0 0 24px;">
                <div style="font-size:0.7rem;font-weight:700;color:#6366f1;letter-spacing:0.1em;text-transform:uppercase;margin-bottom:8px;">Invite Token</div>
                <div style="font-family:'Courier New',monospace;font-size:0.78rem;color:#e2e8f0;word-break:break-all;line-height:1.6;">{invite_token}</div>
              </div>
              <div style="text-align:center;margin:0 0 28px;">
                <a href="{register_url}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#4f46e5);color:#ffffff;text-decoration:none;font-size:0.9rem;font-weight:700;padding:14px 32px;border-radius:8px;letter-spacing:0.02em;">Register as Admin</a>
              </div>
              <p style="margin:0 0 8px;font-size:0.8rem;color:#64748b;">Or copy and use this URL directly:</p>
              <div style="background:#0f1117;border-radius:6px;padding:10px 14px;margin:0 0 24px;">
                <a href="{register_url}" style="font-family:'Courier New',monospace;font-size:0.72rem;color:#6366f1;word-break:break-all;">{register_url}</a>
              </div>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="background:#1e2035;border-radius:8px;padding:14px 18px;border-left:3px solid #f59e0b;">
                    <div style="font-size:0.78rem;color:#fbbf24;font-weight:700;margin-bottom:4px;">Security Notice</div>
                    <div style="font-size:0.78rem;color:#94a3b8;">This token is single-use and will be invalidated immediately after registration. If you did not expect this invitation, ignore this email.</div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="background:#13152a;padding:20px 40px;border-top:1px solid #2d3148;text-align:center;">
              <div style="font-size:0.72rem;color:#475569;">ToolSmithAI &middot; This is an automated message. Do not reply.</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    result = send_real_email(
        to=recipient_email,
        subject="You're Invited to ToolSmithAI Admin Access",
        body=plain_body,
        html_body=html_body,
        user_id=created_by_user_id,
        report_id=None,
        email_type="admin_invite",
    )
    if not result["sent"] and ENABLE_REAL_EMAIL:
        print(f"[WARN] Admin invite email not delivered to {recipient_email}: {result.get('reason', 'unknown')}")


def send_real_email(
    to: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
    user_id: str | None = None,
    report_id: int | None = None,
    email_type: str = "report",
) -> dict:
    """Send an email via the configured provider. Returns a result dict — never raises.

    Routes to Resend (EMAIL_PROVIDER=resend) or SMTP (default). Retries up to
    MAX_STEP_RETRIES times on failure. All attempts are recorded in email_logs.
    Logging failures never block delivery.
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

    if EMAIL_PROVIDER == "resend":
        if not RESEND_API_KEY:
            reason = (
                "Resend not configured. "
                "Set RESEND_API_KEY in .env."
            )
            _update_log("failed", 0, reason)
            return {"sent": False, "reason": reason}

        import resend as _resend  # noqa: PLC0415
        _resend.api_key = RESEND_API_KEY

        last_error: str = ""
        max_attempts = MAX_STEP_RETRIES + 1

        for attempt in range(1, max_attempts + 1):
            try:
                params: dict = {
                    "from": SMTP_FROM_EMAIL,
                    "to": [to],
                    "subject": subject,
                    "text": body,
                }
                if html_body:
                    params["html"] = html_body
                if attachment_bytes and attachment_filename:
                    params["attachments"] = [
                        {
                            "filename": attachment_filename,
                            "content": base64.b64encode(attachment_bytes).decode("ascii"),
                        }
                    ]
                _resend.Emails.send(params)

                _update_log("sent", attempt, sent_at=datetime.now(timezone.utc).isoformat())
                return {"sent": True, "to": to, "subject": subject}

            except Exception as exc:
                last_error = str(exc)
                if attempt < max_attempts:
                    _update_log("pending", attempt, last_error)
                    time.sleep(RETRY_BACKOFF_SECONDS)

        _update_log("failed", max_attempts, last_error)
        return {"sent": False, "reason": last_error}

    # SMTP path (default)
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not SMTP_FROM_EMAIL:
        reason = (
            "SMTP not configured. "
            "Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, and SMTP_FROM_EMAIL in .env."
        )
        _update_log("failed", 0, reason)
        return {"sent": False, "reason": reason}

    if attachment_bytes and attachment_filename:
        outer = MIMEMultipart("mixed")
        outer["Subject"] = subject
        outer["From"] = SMTP_FROM_EMAIL
        outer["To"] = to
        inner = MIMEMultipart("alternative")
        inner.attach(MIMEText(body, "plain"))
        if html_body:
            inner.attach(MIMEText(html_body, "html"))
        outer.attach(inner)
        part = MIMEApplication(attachment_bytes, Name=attachment_filename)
        part["Content-Disposition"] = f'attachment; filename="{attachment_filename}"'
        outer.attach(part)
        msg = outer
    else:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM_EMAIL
        msg["To"] = to
        msg.attach(MIMEText(body, "plain"))
        if html_body:
            msg.attach(MIMEText(html_body, "html"))

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
