import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ServiceRequest, RequestStatus
from ..schemas import ServiceRequestIn, UserProfile
from ..services.email_service import render_template, try_send_email
from ..security import make_approval_token
from ..config import get_settings
from .auth import get_current_user

router = APIRouter(prefix="/api/requests", tags=["requests"])
settings = get_settings()

_SERVICE_TYPE_LABELS = {
    "simple": "Простая услуга",
    "complex": "Сложная услуга",
    "combined": "Комплексная услуга",
    "program": "Медпрограмма",
    "package": "Пакет услуг",
    "prn": "ПРН",
}


def _load_approvers() -> dict[str, dict]:
    path = Path(settings.approvers_json)
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return {a["id"]: a for a in items}


def _notify_approvers(request_id: str, initiator_name, initiator_position, initiator_department,
                       initiator_email: str, body: ServiceRequestIn, approver_ids: list[str],
                       approvers_map: dict[str, dict]) -> None:
    """Выполняется в фоне (BackgroundTasks), ПОСЛЕ того как ответ 201 уже ушёл клиенту.
    Заявка к этому моменту уже закоммичена в БД — сбой отправки конкретному согласующему
    (недоступен SMTP, опечатка в адресе и т.п.) не должен превращать успешную подачу
    заявки в ошибку 500 и не должен блокировать HTTP-запрос на время SMTP-таймаута."""
    for aid in approver_ids:
        approver = approvers_map[aid]
        token = make_approval_token(request_id, aid)
        html = render_template(
            "email_request.html",
            request_id=request_id,
            initiator_name=initiator_name,
            initiator_position=initiator_position,
            initiator_department=initiator_department,
            data=body,
            service_type_label=_SERVICE_TYPE_LABELS.get(body.service_type.value, body.service_type.value),
            approve_url=f"{settings.app_base_url}/api/approvals/{token}/approve",
            reject_url=f"{settings.app_base_url}/api/approvals/{token}/reject",
            app_url=settings.app_base_url,
        )
        try_send_email(
            to=[approver["email"]],
            subject=f"[Согласование прейскуранта] {body.name} — требуется решение ({approver['role']})",
            html_body=html,
            reply_to=initiator_email,
        )


@router.post("", status_code=201)
def create_request(
    body: ServiceRequestIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
):
    approvers_map = _load_approvers()
    missing = [aid for aid in body.approver_ids if aid not in approvers_map]
    if missing:
        raise HTTPException(400, f"Неизвестные согласующие: {missing}")

    entity = ServiceRequest(
        request_type=body.request_type,
        status=RequestStatus.pending,
        initiator_login=user.login,
        initiator_name=user.display_name,
        initiator_email=user.email,
        initiator_department=user.department,
        initiator_position=user.position,
        payload=json.loads(body.model_dump_json()),
        approvals=[{"approver_id": aid, "decision": None, "comment": None, "decided_at": None} for aid in body.approver_ids],
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)

    # Письма уходят ПОСЛЕ ответа клиенту (см. _notify_approvers) — подача заявки
    # подтверждается сразу же и не зависит от доступности/скорости SMTP.
    background_tasks.add_task(
        _notify_approvers,
        entity.id, entity.initiator_name, entity.initiator_position, entity.initiator_department,
        entity.initiator_email, body, body.approver_ids, approvers_map,
    )

    return {"id": entity.id, "status": entity.status}


@router.get("")
def list_requests(db: Session = Depends(get_db), user: UserProfile = Depends(get_current_user)):
    items = db.query(ServiceRequest).order_by(ServiceRequest.created_at.desc()).limit(200).all()
    return [
        {
            "id": r.id,
            "name": r.payload.get("name"),
            "status": r.status,
            "initiator_name": r.initiator_name,
            "created_at": r.created_at,
            "approvals": r.approvals,
        }
        for r in items
    ]


@router.get("/{request_id}")
def get_request(request_id: str, db: Session = Depends(get_db), user: UserProfile = Depends(get_current_user)):
    r = db.get(ServiceRequest, request_id)
    if not r:
        raise HTTPException(404, "Заявка не найдена")
    return {
        "id": r.id,
        "status": r.status,
        "payload": r.payload,
        "approvals": r.approvals,
        "initiator_name": r.initiator_name,
        "initiator_email": r.initiator_email,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }
