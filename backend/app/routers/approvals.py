"""
Обработка кликов по ссылкам "Согласовать / Отклонить" прямо из письма (без логина —
токен уже содержит и request_id, и approver_id, подписан приложением и живёт 14 дней,
см. security.make_approval_token / read_approval_token).

Как только все согласующие подтвердили заявку — статус переводится в approved
и генерируется файл импорта в МИС (medialog_export.build_import_workbook), который
уходит письмом администратору МИС.
"""
import copy
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ServiceRequest, RequestStatus
from ..schemas import ServiceRequestIn
from ..security import read_approval_token
from ..services.email_service import render_template, try_send_email, try_send_email_with_attachment
from ..services.medialog_export import build_import_workbook
from ..config import get_settings

router = APIRouter(prefix="/api/approvals", tags=["approvals"])
settings = get_settings()


def _load_approvers() -> dict[str, dict]:
    path = Path(settings.approvers_json)
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return {a["id"]: a for a in items}


def _confirmation_page(title: str, message: str, ok: bool = True) -> HTMLResponse:
    color = "#1b8a4c" if ok else "#c0392b"
    return HTMLResponse(f"""
    <!doctype html><html lang="ru"><head><meta charset="utf-8">
    <title>{title}</title></head>
    <body style="font-family:Segoe UI,Arial,sans-serif;background:#f2f4f7;display:flex;
                 align-items:center;justify-content:center;height:100vh;margin:0;">
      <div style="background:#fff;padding:32px 40px;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);max-width:480px;text-align:center;">
        <h2 style="color:{color};margin-top:0;">{title}</h2>
        <p style="color:#333;font-size:14px;">{message}</p>
      </div>
    </body></html>
    """)


def _notify_after_decision(
    status_value: str, request_id: str, service_name: str, initiator_email: str,
    approver_name: str, approver_role: str, decision: str,
    mis_admin_email: str | None, import_xlsx: bytes | None,
) -> None:
    """Выполняется в фоне, ПОСЛЕ того как согласующий уже увидел страницу
    подтверждения — сбой/задержка SMTP здесь не заставляет согласующего смотреть
    на зависшую вкладку браузера, а статус заявки в БД уже обновлён к этому моменту."""
    html = render_template(
        "email_decision.html",
        status=status_value,
        service_name=service_name,
        request_id=request_id,
        approver_name=approver_name,
        approver_role=approver_role,
        decision=decision,
        comment=None,
    )
    if status_value == RequestStatus.approved.value and import_xlsx is not None:
        recipients = [initiator_email] + ([mis_admin_email] if mis_admin_email else [])
        try_send_email_with_attachment(
            to=recipients,
            subject=f"[Согласовано] {service_name} — файл для импорта в Медиалог",
            html_body=html,
            attachment_bytes=import_xlsx,
            attachment_filename=f"import_{request_id[:8]}.xlsx",
        )
    else:
        try_send_email(to=[initiator_email], subject=f"[Обновление] {service_name}", html_body=html)


@router.get("/{token}/{action}")
def decide(token: str, action: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # ВАЖНО: db приходит через Depends(get_db), как и во всех остальных роутерах — НЕ через
    # прямое SessionLocal(). Раньше это было единственное место в проекте, обращавшееся к
    # SessionLocal() напрямую, из-за чего FastAPI dependency_overrides (ключевой механизм
    # подмены БД на тестовую в tests/conftest.py) на этот эндпоинт не действовал: тесты
    # создавали заявку через один движок БД, а approvals.decide() искал её в другом
    # (реальном, dev-файле data/requests.db) и получал "заявка не найдена". Приведено
    # к общему паттерну — заодно эндпоинт стал по-настоящему тестируемым в изоляции.
    if action not in ("approve", "reject"):
        raise HTTPException(400, "Некорректное действие")

    data = read_approval_token(token)
    if not data:
        return _confirmation_page("Ссылка недействительна", "Ссылка устарела (срок 14 дней) или повреждена.", ok=False)

    entity = db.get(ServiceRequest, data["request_id"])
    if not entity:
        return _confirmation_page("Заявка не найдена", "Возможно, она была удалена.", ok=False)

    approvers_map = _load_approvers()
    approver = approvers_map.get(data["approver_id"])
    if not approver:
        return _confirmation_page("Согласующий не найден", "Обратитесь к администратору формы.", ok=False)

    # ВАЖНО: копируем, а не берём entity.approvals напрямую. Column(JSON) в SQLAlchemy
    # отслеживает изменения по идентичности объекта, снятой при загрузке строки; если
    # мутировать тот же список in-place и присвоить его же обратно, история атрибута
    # не увидит разницы и UPDATE в БД не улетит. deepcopy гарантирует новый объект.
    approvals = copy.deepcopy(entity.approvals) or []
    already = next((a for a in approvals if a["approver_id"] == data["approver_id"] and a["decision"]), None)
    if already:
        return _confirmation_page(
            "Решение уже зафиксировано",
            f"Вы уже отметили эту заявку как «{already['decision']}» {already['decided_at']}.",
        )

    for a in approvals:
        if a["approver_id"] == data["approver_id"]:
            a["decision"] = action
            a["decided_at"] = datetime.utcnow().isoformat()
    entity.approvals = approvals

    if action == "reject":
        entity.status = RequestStatus.rejected
    elif all(a["decision"] == "approve" for a in approvals):
        entity.status = RequestStatus.approved
    else:
        entity.status = RequestStatus.pending

    db.add(entity)
    db.commit()

    # уведомляем инициатора (и, если согласовано, администратора МИС) в фоне —
    # см. _notify_after_decision. Все данные, нужные для письма, снимаются здесь,
    # пока сущность ещё привязана к сессии, и передаются в фоновую задачу как простые
    # значения (не сам ORM-объект — сессия из Depends(get_db) закроется сразу после ответа).
    req_in = ServiceRequestIn.model_validate(entity.payload)
    import_xlsx = build_import_workbook(req_in) if entity.status == RequestStatus.approved else None
    mis_admin_email = approvers_map.get("mis_admin", {}).get("email")

    background_tasks.add_task(
        _notify_after_decision,
        entity.status.value, entity.id, req_in.name, entity.initiator_email,
        approver["name"], approver["role"], action, mis_admin_email, import_xlsx,
    )

    return _confirmation_page(
        "Спасибо, решение зафиксировано" if action == "approve" else "Заявка отклонена",
        f"Услуга «{req_in.name}» отмечена вами как «{'согласовано' if action == 'approve' else 'отклонено'}».",
    )
