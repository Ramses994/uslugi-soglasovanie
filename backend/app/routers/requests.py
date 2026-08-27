import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ServiceRequest, RequestStatus
from ..schemas import ResubmitIn, ServiceRequestIn, UserProfile
from ..services.approval_history import list_for_request, record as record_history, serialize as serialize_history
from ..services.email_service import render_template, try_send_email
from ..security import make_approval_token
from ..config import get_settings
from .auth import get_current_user, require_admin

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


def _payload_dict(body: ServiceRequestIn) -> dict:
    return json.loads(body.model_dump_json(exclude={"submission_comment"}))


def _notify_approvers(
    request_id: str,
    initiator_name,
    initiator_position,
    initiator_department,
    initiator_email: str,
    body: ServiceRequestIn,
    approver_ids: list[str],
    approvers_map: dict[str, dict],
    version: int = 1,
    submission_comment: str | None = None,
) -> None:
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
            rework_url=f"{settings.app_base_url}/api/approvals/{token}/rework",
            app_url=settings.app_base_url,
            version=version,
            submission_comment=submission_comment or "",
        )
        try_send_email(
            to=[approver["email"]],
            subject=f"[Согласование прейскуранта] {body.name} — требуется решение ({approver['role']}, версия {version})",
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
        payload=_payload_dict(body),
        approvals=[{"approver_id": aid, "decision": None, "comment": None, "decided_at": None} for aid in body.approver_ids],
        version=1,
        submission_comment=body.submission_comment,
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
        entity.version, entity.submission_comment,
    )

    return {"id": entity.id, "status": entity.status, "version": entity.version}


_OWNER_EDITABLE_STATUSES = {RequestStatus.rejected, RequestStatus.rework}
_RESUBMIT_OWNER_STATUSES = {RequestStatus.rejected, RequestStatus.rework}
_RESUBMIT_ADMIN_STATUSES = {RequestStatus.rejected, RequestStatus.rework, RequestStatus.approved}


def _can_view(user: UserProfile, row: ServiceRequest) -> bool:
    return user.is_admin or row.initiator_login.lower() == user.login.lower()


def _get_visible_request(db: Session, request_id: str, user: UserProfile) -> ServiceRequest:
    row = db.get(ServiceRequest, request_id)
    if not row:
        raise HTTPException(404, "Заявка не найдена")
    if not _can_view(user, row):
        raise HTTPException(403, "Недостаточно прав для просмотра этой заявки")
    return row


def _list_item(r: ServiceRequest) -> dict:
    payload = r.payload or {}
    return {
        "id": r.id,
        "name": payload.get("name"),
        "request_type": r.request_type,
        "status": r.status,
        "initiator_name": r.initiator_name,
        "initiator_login": r.initiator_login,
        "created_at": r.created_at,
        "approvals": r.approvals,
        "version": r.version or 1,
    }


def _detail(r: ServiceRequest, db: Session) -> dict:
    return {
        "id": r.id,
        "status": r.status,
        "request_type": r.request_type,
        "payload": r.payload,
        "approvals": r.approvals,
        "initiator_login": r.initiator_login,
        "initiator_name": r.initiator_name,
        "initiator_email": r.initiator_email,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "version": r.version or 1,
        "submission_comment": r.submission_comment,
        "history": [serialize_history(h) for h in list_for_request(db, r.id)],
    }


@router.get("")
def list_requests(
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
    status: Optional[RequestStatus] = Query(None),
    request_type: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    """Администратор формы видит все заявки; сотрудник — только свои.
    Фильтры status / request_type / q (поиск по наименованию и ФИО инициатора)
    доступны обоим, но всегда поверх уже ограниченной выборки."""
    query = db.query(ServiceRequest)
    if not user.is_admin:
        query = query.filter(ServiceRequest.initiator_login == user.login)
    if status is not None:
        query = query.filter(ServiceRequest.status == status)
    if request_type:
        query = query.filter(ServiceRequest.request_type == request_type)
    rows = query.order_by(ServiceRequest.created_at.desc()).limit(200).all()
    items = [_list_item(r) for r in rows]
    needle = (q or "").strip().lower()
    if needle:
        items = [
            i for i in items
            if needle in (i.get("name") or "").lower()
            or needle in (i.get("initiator_name") or "").lower()
        ]
    return items


@router.get("/{request_id}/history")
def get_request_history(
    request_id: str,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
):
    row = _get_visible_request(db, request_id, user)
    return [serialize_history(h) for h in list_for_request(db, row.id)]


@router.post("/{request_id}/resubmit")
def resubmit_request(
    request_id: str,
    background_tasks: BackgroundTasks,
    body: Optional[ResubmitIn] = None,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
):
    """Повторно отправить заявку на согласование после доработки.
    Сотрудник — свою заявку в статусе rework/rejected.
    Админ — также согласованную (approved), если нужно запустить цикл заново.
    Решения в JSON сбрасываются, version увеличивается, письма уходят заново."""
    row = db.get(ServiceRequest, request_id)
    if not row:
        raise HTTPException(404, "Заявка не найдена")
    allowed = _RESUBMIT_ADMIN_STATUSES if user.is_admin else _RESUBMIT_OWNER_STATUSES
    if not user.is_admin and row.initiator_login.lower() != user.login.lower():
        raise HTTPException(403, "Вы не владелец этой заявки")
    if row.status not in allowed:
        raise HTTPException(403, "Заявку нельзя повторно отправить в текущем статусе")

    approvers_map = _load_approvers()
    payload = ServiceRequestIn.model_validate({**(row.payload or {}), "submission_comment": None})
    missing = [aid for aid in payload.approver_ids if aid not in approvers_map]
    if missing:
        raise HTTPException(400, f"Неизвестные согласующие: {missing}")

    status_before = row.status.value
    comment = body.submission_comment if body else None
    if comment is not None:
        row.submission_comment = comment

    approvals = copy.deepcopy(row.approvals) or [
        {"approver_id": aid, "decision": None, "comment": None, "decided_at": None}
        for aid in payload.approver_ids
    ]
    for a in approvals:
        a["decision"] = None
        a["comment"] = None
        a["decided_at"] = None
    row.approvals = approvals
    row.version = (row.version or 1) + 1
    row.status = RequestStatus.pending
    row.updated_at = datetime.utcnow()

    record_history(
        db,
        request_id=row.id,
        approver_id=None,
        approver_login=user.login,
        approver_name=user.display_name,
        approver_email=user.email,
        decision="resubmit",
        comment=row.submission_comment,
        status_before=status_before,
        status_after=row.status.value,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    background_tasks.add_task(
        _notify_approvers,
        row.id, row.initiator_name, row.initiator_position, row.initiator_department,
        row.initiator_email, payload, payload.approver_ids, approvers_map,
        row.version, row.submission_comment,
    )
    return {"id": row.id, "status": row.status, "version": row.version}


@router.get("/{request_id}")
def get_request(request_id: str, db: Session = Depends(get_db), user: UserProfile = Depends(get_current_user)):
    return _detail(_get_visible_request(db, request_id, user), db)


@router.patch("/{request_id}")
def update_request(
    request_id: str,
    body: ServiceRequestIn,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
):
    """Админ может править заявку в любом статусе. Сотрудник — только свою
    и только если она отклонена / отправлена на доработку (rejected или rework).
    После отправки на согласование (pending) правка сотруднику закрыта."""
    row = db.get(ServiceRequest, request_id)
    if not row:
        raise HTTPException(404, "Заявка не найдена")
    if not user.is_admin:
        if row.initiator_login.lower() != user.login.lower():
            raise HTTPException(403, "Вы не владелец этой заявки")
        if row.status not in _OWNER_EDITABLE_STATUSES:
            raise HTTPException(403, "Заявку нельзя редактировать в текущем статусе")
    row.payload = _payload_dict(body)
    row.request_type = body.request_type
    if body.submission_comment is not None:
        row.submission_comment = body.submission_comment
    row.updated_at = datetime.utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _detail(row, db)


@router.delete("/{request_id}", status_code=204)
def delete_request(
    request_id: str,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(require_admin),
):
    row = db.get(ServiceRequest, request_id)
    if not row:
        raise HTTPException(404, "Заявка не найдена")
    db.delete(row)
    db.commit()
    return None
