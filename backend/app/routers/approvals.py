"""
Обработка кликов по ссылкам "Согласовать / Отклонить / На доработку" прямо из письма
(без логина — токен уже содержит и request_id, и approver_id, подписан приложением
и живёт 14 дней, см. security.make_approval_token / read_approval_token).

Как только все согласующие подтвердили заявку — статус переводится в approved
и генерируется файл импорта в МИС (medialog_export.build_import_workbook), который
уходит письмом администратору МИС.

Reject и rework оба переводят заявку в status=rework, чтобы инициатор мог править
и отправить повторно. В журнале истории решение всё равно различается (reject vs rework).

«Согласовать» срабатывает одним GET. «Отклонить» / «На доработку» по GET сначала
показывают HTML-форму комментария (почтовики вырезают JS из писем, поэтому
prompt/modal на фронтенде из письма открыть нельзя), а решение фиксируется POST-ом.
"""
import copy
import json
import logging
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ServiceRequest, RequestStatus
from ..schemas import ServiceRequestIn
from ..security import read_approval_token
from ..services.approval_history import record as record_history
from ..services.approval_chain import (
    current_approver_id,
    load_branch_names,
    needs_folomova_price,
    next_pending_index,
    notify_approver,
)
from ..services.email_service import render_template, try_send_email
from ..services.mis_notify import send_mis_import_email
from ..config import get_settings

router = APIRouter(prefix="/api/approvals", tags=["approvals"])
settings = get_settings()
logger = logging.getLogger("price_form.approvals")

_ACTIONS = ("approve", "reject", "rework")
_COMMENT_ACTIONS = ("reject", "rework")
_ACTION_TITLES = {
    "approve": ("Спасибо, решение зафиксировано", "согласовано"),
    "reject": ("Заявка отклонена и отправлена на доработку", "отклонено"),
    "rework": ("Заявка отправлена на доработку", "на доработку"),
}


def _load_approvers() -> dict[str, dict]:
    path = Path(settings.approvers_json)
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return {a["id"]: a for a in items}


def _confirmation_page(title: str, message: str, ok: bool = True) -> HTMLResponse:
    color = "#1b8a4c" if ok else "#c0392b"
    return HTMLResponse(f"""
    <!doctype html><html lang="ru"><head><meta charset="utf-8">
    <title>{escape(title)}</title></head>
    <body style="font-family:Segoe UI,Arial,sans-serif;background:#f2f4f7;display:flex;
                 align-items:center;justify-content:center;height:100vh;margin:0;">
      <div style="background:#fff;padding:32px 40px;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);max-width:480px;text-align:center;">
        <h2 style="color:{color};margin-top:0;">{escape(title)}</h2>
        <p style="color:#333;font-size:14px;">{message}</p>
      </div>
    </body></html>
    """)


def _comment_form_page(token: str, action: str, service_name: str) -> HTMLResponse:
    if action == "reject":
        title, prompt, placeholder, submit_label, accent = (
            "Отклонить заявку",
            "Причина отклонения",
            "Например: цена не соответствует бюджету",
            "Отклонить",
            "#c0392b",
        )
    else:
        title, prompt, placeholder, submit_label, accent = (
            "Отправить на доработку",
            "Что нужно доработать",
            "Например: уточните показания и цены по филиалам",
            "На доработку",
            "#b45309",
        )
    html = render_template(
        "approval_comment_form.html",
        title=title,
        prompt=prompt,
        placeholder=placeholder,
        submit_label=submit_label,
        accent=accent,
        service_name=service_name,
        form_url=f"/api/approvals/{token}/{action}",
    )
    return HTMLResponse(html)


def _notify_after_decision(
    status_value: str, request_id: str, service_name: str, initiator_email: str,
    approver_name: str, approver_role: str, decision: str,
    comment: str | None = None,
) -> None:
    """Фон для reject/rework: статус уже в БД. Письмо в МИС уходит синхронно
    в _apply_decision, чтобы не потерять вложение при сбое фоновой задачи."""
    html = render_template(
        "email_decision.html",
        status=status_value,
        service_name=service_name,
        request_id=request_id,
        approver_name=approver_name,
        approver_role=approver_role,
        decision=decision,
        comment=comment,
    )
    try_send_email(to=[initiator_email], subject=f"[Обновление] {service_name}", html_body=html)


def _prepare_decision(token: str, action: str, db: Session):
    """Общая проверка токена/заявки до показа формы или фиксации решения.
    Возвращает (контекст, None) либо (None, HTML-ответ с ошибкой/уже решено)."""
    if action not in _ACTIONS:
        raise HTTPException(400, "Некорректное действие")

    data = read_approval_token(token)
    if not data:
        return None, _confirmation_page("Ссылка недействительна", "Ссылка устарела (срок 14 дней) или повреждена.", ok=False)

    entity = db.get(ServiceRequest, data["request_id"])
    if not entity:
        return None, _confirmation_page("Заявка не найдена", "Возможно, она была удалена.", ok=False)

    approvers_map = _load_approvers()
    approver = approvers_map.get(data["approver_id"])
    if not approver:
        return None, _confirmation_page("Согласующий не найден", "Обратитесь к администратору формы.", ok=False)

    # ВАЖНО: копируем, а не берём entity.approvals напрямую. Column(JSON) в SQLAlchemy
    # отслеживает изменения по идентичности объекта, снятой при загрузке строки; если
    # мутировать тот же список in-place и присвоить его же обратно, история атрибута
    # не увидит разницы и UPDATE в БД не улетит. deepcopy гарантирует новый объект.
    approvals = copy.deepcopy(entity.approvals) or []
    already = next((a for a in approvals if a["approver_id"] == data["approver_id"] and a["decision"]), None)
    if already:
        return None, _confirmation_page(
            "Решение уже зафиксировано",
            f"Вы уже отметили эту заявку как «{escape(str(already['decision']))}» {escape(str(already['decided_at'] or ''))}.",
        )

    if entity.status != RequestStatus.pending:
        return None, _confirmation_page(
            "Заявка уже не на согласовании",
            f"Текущий статус: «{escape(entity.status.value)}». Новое решение по этой ссылке не принимается.",
            ok=False,
        )

    current = current_approver_id(entity)
    if entity.approval_chain and current and current != data["approver_id"]:
        return None, _confirmation_page(
            "Сейчас не ваша очередь",
            "Решение уже зафиксировано на этом этапе или заявка ожидает другого согласующего.",
            ok=False,
        )

    return {
        "data": data,
        "entity": entity,
        "approver": approver,
        "approvers_map": approvers_map,
        "approvals": approvals,
    }, None


def _apply_decision(ctx: dict, action: str, note: str | None, background_tasks: BackgroundTasks, db: Session) -> HTMLResponse:
    entity = ctx["entity"]
    data = ctx["data"]
    approver = ctx["approver"]
    approvals = ctx["approvals"]
    status_before = entity.status.value

    for a in approvals:
        if a["approver_id"] == data["approver_id"]:
            a["decision"] = action
            a["comment"] = note
            a["decided_at"] = datetime.utcnow().isoformat()
    entity.approvals = approvals

    if action in ("reject", "rework"):
        entity.status = RequestStatus.rework
    else:
        chain = list(entity.approval_chain or [])
        nxt = next_pending_index(approvals, chain, int(entity.current_stage_index or 0)) if chain else None
        if not chain:
            entity.status = RequestStatus.approved if all(a["decision"] == "approve" for a in approvals) else RequestStatus.pending
        elif nxt is None:
            entity.status = RequestStatus.approved
            entity.current_stage_index = max(len(chain) - 1, 0)
        else:
            entity.status = RequestStatus.pending
            entity.current_stage_index = nxt

    record_history(
        db,
        request_id=entity.id,
        approver_id=data["approver_id"],
        approver_login=approver.get("login") or data["approver_id"],
        approver_name=approver["name"],
        approver_email=approver["email"],
        decision=action,
        comment=note,
        status_before=status_before,
        status_after=entity.status.value,
    )
    db.add(entity)
    db.commit()

    payload = entity.payload or {}
    req_in = None
    try:
        req_in = ServiceRequestIn.model_validate(payload)
        service_name = req_in.name
    except Exception:
        logger.exception("Payload заявки %s не проходит схему после решения — письмо в МИС всё равно попытаемся отправить", entity.id)
        service_name = str(payload.get("name") or "Заявка")

    if entity.status == RequestStatus.approved:
        send_mis_import_email(
            db, entity, ctx["approvers_map"],
            force=False,
            approver_name=approver["name"],
            approver_role=approver["role"],
        )
    else:
        background_tasks.add_task(
            _notify_after_decision,
            entity.status.value, entity.id, service_name, entity.initiator_email,
            approver["name"], approver["role"], action, note,
        )

    if entity.status == RequestStatus.pending and action == "approve":
        nxt_id = current_approver_id(entity)
        if nxt_id and req_in is not None:
            background_tasks.add_task(
                notify_approver,
                entity.id, entity.initiator_name, entity.initiator_position, entity.initiator_department,
                entity.initiator_email, req_in, nxt_id, ctx["approvers_map"],
                entity.version or 1, entity.submission_comment,
                bool(payload.get("price_undefined")),
            )

    title, verb = _ACTION_TITLES[action]
    extra = "<br>Комментарий сохранён." if note else ""
    if entity.status == RequestStatus.pending and action == "approve":
        title = "Согласовано, заявка передана на следующий этап"
    elif entity.status == RequestStatus.approved:
        title = "Заявка полностью согласована"
    return _confirmation_page(
        title,
        f"Услуга «{escape(service_name)}» отмечена вами как «{verb}».{extra}",
    )


def _price_form_page(token: str, service_name: str) -> HTMLResponse:
    html = render_template(
        "approval_price_form.html",
        service_name=service_name,
        form_url=f"/api/approvals/{token}/approve-with-price",
        branches=load_branch_names(),
    )
    return HTMLResponse(html)


@router.get("/{token}/approve-with-price")
def approve_price_get(token: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    ctx, err = _prepare_decision(token, "approve", db)
    if err:
        return err
    if not needs_folomova_price(ctx["entity"], ctx["data"]["approver_id"]):
        return RedirectResponse(f"/api/approvals/{token}/approve")
    req_in = ServiceRequestIn.model_validate(ctx["entity"].payload)
    return _price_form_page(token, req_in.name)


@router.post("/{token}/approve-with-price")
async def approve_price_post(
    token: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    ctx, err = _prepare_decision(token, "approve", db)
    if err:
        return err
    form = await request.form()
    prices = []
    for key, val in form.items():
        if not str(key).startswith("price_") or val in (None, ""):
            continue
        branch = str(key)[len("price_"):]
        try:
            price = float(str(val).replace(",", "."))
        except ValueError:
            continue
        if price > 0:
            prices.append({"branch": branch, "price": price})
    if not prices:
        req_in = ServiceRequestIn.model_validate(ctx["entity"].payload)
        return _confirmation_page("Укажите цену", "Нужна хотя бы одна цена по филиалу.", ok=False)
    payload = copy.deepcopy(ctx["entity"].payload or {})
    payload["prices"] = prices
    payload["price_undefined"] = False
    ctx["entity"].payload = payload
    return _apply_decision(ctx, "approve", None, background_tasks, db)


@router.get("/{token}/{action}")
def decide_get(
    token: str,
    action: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    comment: str | None = Query(None),
):
    ctx, err = _prepare_decision(token, action, db)
    if err:
        return err
    if action == "approve" and needs_folomova_price(ctx["entity"], ctx["data"]["approver_id"]):
        return RedirectResponse(f"/api/approvals/{token}/approve-with-price")
    if action in _COMMENT_ACTIONS:
        req_in = ServiceRequestIn.model_validate(ctx["entity"].payload)
        return _comment_form_page(token, action, req_in.name)
    note = (comment or "").strip() or None
    return _apply_decision(ctx, action, note, background_tasks, db)


@router.post("/{token}/{action}")
def decide_post(
    token: str,
    action: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    comment: str | None = Form(None),
    reason: str | None = Form(None),
):
    ctx, err = _prepare_decision(token, action, db)
    if err:
        return err
    note = (comment or reason or "").strip() or None
    return _apply_decision(ctx, action, note, background_tasks, db)
