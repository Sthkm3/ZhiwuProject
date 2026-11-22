"""寄送驗證碼。改寫自 RhyculatorServer/Verify_email/main.py 的 send_email()，改成通用 SMTP。

寄信是容器主動連出去（smtp.gmail.com:465 等），不經過 cloudflared tunnel，不用額外開 port。
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import config
from .i18n import t


def send_code(to_email: str, code: str, lang: str) -> None:
    # 本地開發沒有設定 SMTP 帳密時，直接把驗證碼印在 log
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        print(f"[DEV] verify code for {to_email}: {code}", flush=True)
        return

    msg = EmailMessage()
    msg["Subject"] = t(lang, "mail.subject", code=code)
    # Gmail：MAIL_FROM 要先在「以這個地址寄送郵件」加入別名，否則 Gmail 會改回帳號本身
    msg["From"] = formataddr(("吱鼯計畫", config.MAIL_FROM))
    msg["To"] = to_email
    msg["Message-ID"] = make_msgid(domain=config.MAIL_FROM.rpartition("@")[2] or None)
    msg.set_content(t(lang, "mail.body", code=code, url=config.PUBLIC_URL))

    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        smtp = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=15, context=ctx)
    else:
        smtp = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15)
        smtp.starttls(context=ctx)
    with smtp:
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.send_message(msg)
