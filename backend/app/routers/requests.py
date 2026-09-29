import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ServiceRequest, RequestStatus
from ..schemas import ResubmitIn, ServiceRequestIn, UserProfile
from ..services.approval_chain import (
    build_approval_chain,
    empty_approvals,
    first_pending_index,
    merge_approvals_keeping_approves,
    notify_approver,
)
from ..services.approval_history import list_for_request, record as record_history, serialize as serialize_history
from ..services.xlsx_import import parse_xlsx_requests
from ..services.xlsx_template import build_template_workbook
from ..config import get_settings
from .auth import get_current_user, require_admin

router = APIRouter(prefix="/api/requests", tags=["requests"])
settings = get_settings()


def _load_approvers() -> dict[str, dict]:
    path = Path(settings.approvers_json)
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return {a["id"]: a for a in items}


def _payload_dict(body: ServiceRequestIn) -> dict:
    return json.loads(body.model_dump_json(exclude={"submission_comment"}))


def _queue_current_stage_email(
    background_tasks: BackgroundTasks,
    entity: ServiceRequest,
    body: ServiceRequestIn,
    approvers_map: dict[str, dict],
) -> None:
    chain = list(entity.approval_chain or [])
    idx = int(entity.current_stage_index or 0)
    if not chain or idx < 0 or idx >= len(chain):
        return
    aid = chain[idx]
    payload = entity.payload or {}
    background_tasks.add_task(
        notify_approver,
        entity.id, entity.initiator_name, entity.initiator_position, entity.initiator_department,
        entity.initiator_email, body, aid, approvers_map,
        entity.version, entity.submission_comment,
        bool(payload.get("price_undefined")),
    )


def _prepare_chain(body: ServiceRequestIn, approvers_map: dict[str, dict]) -> list[str]:
    missing = [aid for aid in body.approver_ids if aid not in approvers_map]
    if missing:
        raise HTTPException(400, f"Неизвестные согласующие: {missing}")
    chain = build_approval_chain(body.approver_ids, approvers_map)
    if not chain:
        raise HTTPException(400, "Не удалось построить цепочку согласования")
    return chain


class BulkCreateIn(BaseModel):
    items: list[ServiceRequestIn]
    approver_ids: list[str] = []


@router.post("", status_code=201)
def create_request(
    body: ServiceRequestIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
):
    approvers_map = _load_approvers()
    chain = _prepare_chain(body, approvers_map)

    entity = ServiceRequest(
        request_type=body.request_type,
        status=RequestStatus.pending,
        initiator_login=user.login,
        initiator_name=user.display_name,
        initiator_email=user.email,
        initiator_department=user.department,
        initiator_position=user.position,
        payload=_payload_dict(body),
        approvals=empty_approvals(chain),
        approval_chain=chain,
        current_stage_index=0,
        version=1,
        submission_comment=body.submission_comment,
    )
    db.add(entity)
    db.commit()
    db.refresh(entity)

    _queue_current_stage_email(background_tasks, entity, body, approvers_map)
    return {
        "id": entity.id,
        "status": entity.status,
        "version": entity.version,
        "approval_chain": chain,
        "current_stage_index": 0,
    }


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
        "approval_chain": r.approval_chain or [],
        "current_stage_index": r.current_stage_index or 0,
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
        "approval_chain": r.approval_chain or [],
        "current_stage_index": r.current_stage_index or 0,
        "mis_notified_at": r.mis_notified_at.isoformat() if r.mis_notified_at else None,
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


@router.get("/template.xlsx")
def download_template(user: UserProfile = Depends(get_current_user)):
    from ..services.approval_chain import load_branch_names

    content = build_template_workbook(load_branch_names())
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=service_requests_template.xlsx"},
    )


@router.post("/import-preview")
async def import_preview(
    file: UploadFile = File(...),
    user: UserProfile = Depends(get_current_user),
):
    raw = await file.read()
    try:
        rows = parse_xlsx_requests(raw)
    except Exception as exc:
        raise HTTPException(400, f"Не удалось прочитать Excel: {exc}")
    results = []
    for i, data in enumerate(rows, start=3):
        try:
            parsed = ServiceRequestIn.model_validate(data)
            results.append({"row": i, "data": parsed.model_dump(mode="json"), "errors": []})
        except ValidationError as exc:
            msgs = []
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", []) if p != "body")
                msgs.append(f"{loc}: {err.get('msg')}" if loc else err.get("msg", "ошибка"))
            results.append({"row": i, "data": data, "errors": msgs})
    return {"results": results}


@router.post("/bulk")
def create_requests_bulk(
    body: BulkCreateIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: UserProfile = Depends(get_current_user),
):
    created = []
    errors = []
    for i, item in enumerate(body.items):
        try:
            if body.approver_ids:
                item.approver_ids = list(body.approver_ids)
            res = create_request(item, background_tasks, db, user)
            created.append(res["id"])
        except HTTPException as exc:
            errors.append({"index": i, "error": exc.detail})
        except Exception as exc:
            errors.append({"index": i, "error": str(exc)})
    return {"created": created, "errors": errors}


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
    Цепочка пересчитывается; у кого уже есть approve — этап пропускается."""
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
    chain = _prepare_chain(payload, approvers_map)

    status_before = row.status.value
    comment = body.submission_comment if body else None
    if comment is not None:
        row.submission_comment = comment

    reset_all = status_before == RequestStatus.approved.value
    approvals = merge_approvals_keeping_approves(chain, row.approvals, reset_all=reset_all)
    idx = first_pending_index(approvals, chain)
    if idx >= len(chain):
        idx = 0
        approvals = empty_approvals(chain)

    row.approvals = approvals
    row.approval_chain = chain
    row.current_stage_index = idx
    row.version = (row.version or 1) + 1
    row.status = RequestStatus.pending
    row.mis_notified_at = None
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

    _queue_current_stage_email(background_tasks, row, payload, approvers_map)
    return {
        "id": row.id,
        "status": row.status,
        "version": row.version,
        "approval_chain": chain,
        "current_stage_index": idx,
    }


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
