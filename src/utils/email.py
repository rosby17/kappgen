from typing import Optional
import httpx
from src.config import BREVO_API_KEY, BREVO_SENDER_EMAIL, BREVO_SENDER_NAME, FRONTEND_BASE_URL

SUPPORTED_LOCALES = {"fr", "en"}
EMAIL_LOGO_URL = f"{FRONTEND_BASE_URL}/assets/logo/logo-kappgen-horizontale-blanc.png"
EMAIL_ACCENT = "#00c2ff"


def detect_locale(accept_language: Optional[str]) -> str:
    # Accept-Language looks like "en-US,en;q=0.9,fr;q=0.8" — take the
    # highest-priority tag whose base language we actually support.
    if not accept_language:
        return "fr"
    for tag in accept_language.split(","):
        lang = tag.split(";")[0].strip().split("-")[0].lower()
        if lang in SUPPORTED_LOCALES:
            return lang
    return "fr"


def send_brevo_email(to_email: str, subject: str, html_content: str, text_content: str) -> None:
    if not BREVO_API_KEY or not BREVO_SENDER_EMAIL:
        raise RuntimeError("Brevo n'est pas configuré pour KappGen.")

    response = httpx.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json={
            "sender": {"email": BREVO_SENDER_EMAIL, "name": BREVO_SENDER_NAME or "KappGen"},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_content,
            "textContent": text_content,
        },
        timeout=15.0,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Brevo a refusé l'envoi ({response.status_code}).")


def email_shell(preheader: str, body_html: str) -> str:
    # Table-based layout — Gmail/Outlook strip <style> blocks and flexbox, so
    # every visual rule here must be an inline attribute/style for it to render.
    return f"""
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">{preheader}</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#070b12;padding:40px 16px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
      <tr>
        <td align="center">
          <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%">
            <tr>
              <td style="padding-bottom:28px">
                <img src="{EMAIL_LOGO_URL}" alt="KappGen" width="180" style="width:180px;max-width:100%;height:auto;display:block" />
              </td>
            </tr>
            <tr>
              <td style="background:#161b22;border:1px solid #2b374d;border-radius:16px;padding:36px">
                {body_html}
              </td>
            </tr>
            <tr>
              <td style="padding-top:28px;text-align:center">
                <p style="color:#5b6779;font-size:12px;line-height:1.6;margin:0">
                  KappGen — génère des vidéos courtes automatiquement.<br />
                  <a href="{FRONTEND_BASE_URL}" style="color:{EMAIL_ACCENT};text-decoration:none">kappgen.com</a>
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
    """
