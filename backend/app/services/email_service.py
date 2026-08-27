"""
Отправка писем через SMTP с HTML-шаблоном (Jinja2). Два сценария аутентификации SMTP,
переключаются флагом SMTP_SEND_AS_USER:

  A) Сервисный ящик (по умолчанию, рекомендуется). Письмо уходит от сервисного адреса,
     но со значением Reply-To = e-mail заявителя — согласующий отвечает "как обычно",
     ответ попадёт инициатору. Это самый надёжный вариант: не нужно хранить/передавать
     доменный пароль пользователя на бэкенд, не будет проблем с MFA/Modern Auth в Exchange.

  B) От имени текущего пользователя. Требует доменный логин/пароль пользователя ИМЕННО
     в момент отправки (передаются вместе с запросом на submit, используются один раз и
     не сохраняются). Работает только если SMTP-релей клиники разрешает аутентификацию
     обычных доменных пользователей (NTLM/Basic) — на многих инфраструктурах с Exchange
     Online это заблокировано в пользу OAuth2, тогда нужно использовать вариант A.
"""
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import get_settings

settings = get_settings()
logger = logging.getLogger("price_form.email")

# Без явного timeout smtplib.SMTP(...) может зависнуть НАВСЕГДА, если хост недоступен
# (закрытый firewall, опечатка в SMTP_HOST, временная недоступность релея) — это подвесит
# HTTP-запрос, обрабатывающий заявку, на неопределённое время. 10 секунд достаточно для
# внутрикорпоративного релея; при необходимости вынести в настройки.
SMTP_TIMEOUT_SECONDS = 10

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(["html"]))


def render_template(name: str, **context) -> str:
    return _env.get_template(name).render(**context)


def _from_header_and_envelope() -> tuple[str, str]:
    """Разделяем заголовок From и SMTP MAIL FROM.

    SMTP_FROM часто задают как «Имя <user@domain>». Если эту строку целиком
    отдать в smtplib.sendmail(), Exchange отвечает 550 5.7.60
    «Client does not have permissions to send as this sender»: конверт не
    совпадает с ящиком, под которым прошла AUTH. В MAIL FROM нужен только адрес.
    """
    name, addr = parseaddr(settings.smtp_from or "")
    envelope = (addr or settings.smtp_user or "").strip() or (settings.smtp_from or "")
    header = formataddr((name, envelope)) if name else envelope
    return header, envelope


def send_email(
    to: list[str],
    subject: str,
    html_body: str,
    reply_to: str | None = None,
    user_credentials: tuple[str, str] | None = None,
) -> None:
    from_header, envelope_from = _from_header_and_envelope()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_header
    msg["To"] = ", ".join(to)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_send_as_user and user_credentials:
            server.login(user_credentials[0], user_credentials[1])
        elif settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(envelope_from, to, msg.as_string())


def try_send_email(to: list[str], subject: str, html_body: str, **kwargs) -> bool:
    """Обёртка для мест, где сбой почты НЕ должен ронять уже совершённое действие
    (заявка уже сохранена в БД / решение уже зафиксировано) — например, фоновая
    отправка после коммита в requests.py и approvals.py. Возвращает False и пишет
    в лог вместо необработанного исключения, которое иначе превратилось бы в 500
    уже после того, как пользователь получил бы успешный ответ."""
    try:
        send_email(to, subject, html_body, **kwargs)
        return True
    except Exception:
        logger.exception("Не удалось отправить письмо %r получателям %r", subject, to)
        return False


def send_email_with_attachment(
    to: list[str],
    subject: str,
    html_body: str,
    attachment_bytes: bytes,
    attachment_filename: str,
    reply_to: str | None = None,
) -> None:
    from_header, envelope_from = _from_header_and_envelope()
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = from_header
    msg["To"] = ", ".join(to)
    if reply_to:
        msg["Reply-To"] = reply_to

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    from email.mime.base import MIMEBase
    from email import encoders

    part = MIMEBase("application", "octet-stream")
    part.set_payload(attachment_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{attachment_filename}"')
    msg.attach(part)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(envelope_from, to, msg.as_string())


def try_send_email_with_attachment(to: list[str], subject: str, html_body: str, **kwargs) -> bool:
    """См. try_send_email — тот же принцип для письма с вложением (финальное
    уведомление с файлом импорта после полного согласования)."""
    try:
        send_email_with_attachment(to, subject, html_body, **kwargs)
        return True
    except Exception:
        logger.exception("Не удалось отправить письмо с вложением %r получателям %r", subject, to)
        return False
