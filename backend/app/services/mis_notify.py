"""Финальное уведомление в МИС: текстовое письмо без вложений + ссылка на карточку."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import RequestStatus, ServiceRequest
from ..schemas import ServiceRequestIn
from ..services import email_service
from ..services.email_service import render_template
from ..services.medialog_export import build_import_workbook, resolve_service_code

logger = logging.getLogger("price_form.mis_notify")


def mis_recipients(initiator_email: str, approvers_map: dict) -> list[str]:
    """Кому уходит уведомление после полного согласования.

    Нельзя опираться только на запись mis_admin в approvers.json — в боевом
    каталоге её нет. FORM_ADMIN_LOGINS тоже часто не совпадает с id согласующего
    (админа убрали из маршрута, а письма в МИС он по-прежнему должен получать).
    """
    settings = get_settings()
    seen: list[str] = []

    def add(addr: str | None) -> None:
        if addr and addr not in seen:
            seen.append(addr)

    add(initiator_email)
    add((approvers_map.get("mis_admin") or {}).get("email"))

    by_login = {}
    for aid, info in (approvers_map or {}).items():
        email = (info or {}).get("email")
        by_login[str(aid).lower()] = email
        login = str((info or {}).get("login") or "").lower()
        if login:
            by_login[login] = email

    suffix = (settings.ldap_directory_mail_suffix or "").strip()
    if suffix and not suffix.startswith("@"):
        suffix = "@" + suffix

    for login in settings.admin_logins():
        add(by_login.get(login))
        if suffix:
            add(f"{login}{suffix}")

    for raw in (settings.mis_notify_emails or "").split(","):
        add(raw.strip())
    return seen


def _service_name(payload: dict | None) -> str:
    return str((payload or {}).get("name") or "Заявка")


def build_workbook_safe(payload: dict | None, *, generated_code_hint: str = "") -> bytes | None:
    try:
        req_in = ServiceRequestIn.model_validate(payload or {})
        return build_import_workbook(req_in, generated_code_hint=generated_code_hint)
    except Exception:
        logger.exception("Не удалось собрать файл импорта МИС из payload")
        return None


def build_workbook_for_entity(db: Session, entity: ServiceRequest) -> bytes | None:
    try:
        req_in = ServiceRequestIn.model_validate(entity.payload or {})
        code = resolve_service_code(db, entity, req_in)
        return build_import_workbook(
            req_in,
            generated_code_hint=code,
            date_from=entity.updated_at,
        )
    except Exception:
        logger.exception("Не удалось собрать файл импорта МИС из заявки %s", getattr(entity, "id", "?"))
        return None


def send_mis_import_email(
    db: Session,
    entity: ServiceRequest,
    approvers_map: dict,
    *,
    force: bool = False,
    approver_name: str = "",
    approver_role: str = "",
) -> dict:
    """Текстовое письмо без вложений: услуга согласована, скачать файлы в форме."""
    if entity.status != RequestStatus.approved:
        return {"ok": False, "skipped": True, "recipients": [], "error": "Заявка не в статусе approved"}
    if entity.mis_notified_at and not force:
        return {
            "ok": True,
            "skipped": True,
            "recipients": [],
            "error": None,
            "mis_notified_at": entity.mis_notified_at.isoformat() if entity.mis_notified_at else None,
        }

    settings = get_settings()
    payload = entity.payload or {}
    name = _service_name(payload)
    # Код услуги выделяем заранее — чтобы к моменту скачивания он уже был в БД.
    try:
        req_in = ServiceRequestIn.model_validate(payload)
        resolve_service_code(db, entity, req_in)
        db.add(entity)
        db.commit()
    except Exception:
        logger.exception("Не удалось закрепить CODE за заявкой %s перед письмом", entity.id)

    recipients = mis_recipients(entity.initiator_email, approvers_map)
    card_url = f"{settings.app_base_url.rstrip('/')}/#requests"
    html = render_template(
        "email_decision.html",
        status=RequestStatus.approved.value,
        service_name=name,
        request_id=entity.id,
        approver_name=approver_name or "согласование завершено",
        approver_role=approver_role or "",
        decision="approve",
        comment=None,
        app_url=settings.app_base_url,
        card_url=card_url,
        download_hint=True,
    )
    if not recipients:
        logger.error("Нет получателей письма МИС для заявки %s", entity.id)
        return {"ok": False, "skipped": False, "recipients": [], "error": "Пустой список получателей"}

    ok = email_service.try_send_email(
        to=recipients,
        subject=f"[Согласовано] {name} — заведите в МИС Медиалог",
        html_body=html,
    )
    if ok:
        entity.mis_notified_at = datetime.utcnow()
        db.add(entity)
        db.commit()
        logger.info("Письмо МИС (без вложения) отправлено по заявке %s на %s", entity.id, recipients)
    else:
        logger.error("SMTP не принял письмо МИС по заявке %s получателям %s", entity.id, recipients)
    return {"ok": ok, "skipped": False, "recipients": recipients, "error": None if ok else "SMTP"}
