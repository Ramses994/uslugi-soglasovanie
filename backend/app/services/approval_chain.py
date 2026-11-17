"""Последовательная цепочка согласования.

Фоломова всегда первая (цена), Соколовская всегда последняя. Остальные выбранные
инициатором — в середине, по полю order. Если кого-то из крайних нет в каталоге
(локальный dummy без боевых id) — крайняя точка просто пропускается.
"""
from __future__ import annotations

from pathlib import Path

from ..config import get_settings
from ..security import make_approval_token
from ..services.email_service import render_template, try_send_email

FOLOMOVA_ID = "nfolomova"
SOKOLOVSKAYA_ID = "osokolovskaya"

_SERVICE_TYPE_LABELS = {
    "simple": "Простая услуга",
    "complex": "Сложная услуга",
    "combined": "Комплексная услуга",
    "program": "Медпрограмма",
    "package": "Пакет услуг",
    "prn": "ПРН",
}


def build_approval_chain(selected_ids: list[str], approvers_map: dict[str, dict]) -> list[str]:
    selected = [aid for aid in selected_ids if aid in approvers_map]
    middle = [
        aid for aid in selected
        if aid not in (FOLOMOVA_ID, SOKOLOVSKAYA_ID)
    ]
    middle.sort(key=lambda aid: (int(approvers_map[aid].get("order") or 50), aid))
    chain: list[str] = []
    if FOLOMOVA_ID in approvers_map:
        chain.append(FOLOMOVA_ID)
    for aid in middle:
        if aid not in chain:
            chain.append(aid)
    if SOKOLOVSKAYA_ID in approvers_map and SOKOLOVSKAYA_ID not in chain:
        chain.append(SOKOLOVSKAYA_ID)
    if not chain:
        chain = list(dict.fromkeys(selected))
    return chain


def empty_slot(approver_id: str) -> dict:
    return {"approver_id": approver_id, "decision": None, "comment": None, "decided_at": None}


def empty_approvals(chain: list[str]) -> list[dict]:
    return [empty_slot(aid) for aid in chain]


def merge_approvals_keeping_approves(
    chain: list[str],
    old_approvals: list[dict] | None,
    reset_all: bool = False,
) -> list[dict]:
    """При повторной отправке после доработки не требуем повторного approve
    у тех, кто уже согласовал. Reject/rework и пустые слоты сбрасываются.
    Полный сброс — только если админ заново запускает уже approved заявку."""
    old = {a["approver_id"]: a for a in (old_approvals or [])}
    out = []
    for aid in chain:
        prev = old.get(aid)
        if (not reset_all) and prev and prev.get("decision") == "approve":
            out.append({
                "approver_id": aid,
                "decision": "approve",
                "comment": prev.get("comment"),
                "decided_at": prev.get("decided_at"),
            })
        else:
            out.append(empty_slot(aid))
    return out


def is_resolved_decision(decision: str | None) -> bool:
    """Этап закрыт: обычный approve или пропуск админом (skipped)."""
    return decision in ("approve", "skipped")


def first_pending_index(approvals: list[dict], chain: list[str]) -> int:
    by_id = {a["approver_id"]: a for a in (approvals or [])}
    for i, aid in enumerate(chain):
        if not is_resolved_decision((by_id.get(aid) or {}).get("decision")):
            return i
    return len(chain)


def next_pending_index(approvals: list[dict], chain: list[str], after_index: int) -> int | None:
    by_id = {a["approver_id"]: a for a in (approvals or [])}
    for i in range(after_index + 1, len(chain)):
        aid = chain[i]
        if not is_resolved_decision((by_id.get(aid) or {}).get("decision")):
            return i
    return None


def apply_chain_progress(entity, approvals: list[dict]) -> None:
    """После положительного решения (approve/skipped) сдвигает этап или закрывает заявку."""
    from ..models import RequestStatus

    chain = list(entity.approval_chain or [])
    if not chain:
        entity.status = (
            RequestStatus.approved
            if approvals and all(is_resolved_decision(a.get("decision")) for a in approvals)
            else RequestStatus.pending
        )
        return
    nxt = next_pending_index(approvals, chain, int(entity.current_stage_index or 0))
    if nxt is None:
        entity.status = RequestStatus.approved
        entity.current_stage_index = max(len(chain) - 1, 0)
    else:
        entity.status = RequestStatus.pending
        entity.current_stage_index = nxt


def current_approver_id(entity) -> str | None:
    chain = list(entity.approval_chain or [])
    if not chain:
        return None
    idx = int(entity.current_stage_index or 0)
    if 0 <= idx < len(chain):
        return chain[idx]
    return None


def needs_folomova_price(entity, approver_id: str, payload: dict | None = None) -> bool:
    data = payload if payload is not None else (entity.payload or {})
    return (
        approver_id == FOLOMOVA_ID
        and bool(data.get("price_undefined"))
        and current_approver_id(entity) == FOLOMOVA_ID
    )


def notify_approver(
    request_id: str,
    initiator_name,
    initiator_position,
    initiator_department,
    initiator_email: str,
    body,
    approver_id: str,
    approvers_map: dict[str, dict],
    version: int = 1,
    submission_comment: str | None = None,
    price_undefined: bool = False,
) -> None:
    settings = get_settings()
    approver = approvers_map[approver_id]
    token = make_approval_token(request_id, approver_id)
    approve_path = "approve-with-price" if (approver_id == FOLOMOVA_ID and price_undefined) else "approve"
    st = body.service_type.value if hasattr(body.service_type, "value") else body.service_type
    html = render_template(
        "email_request.html",
        request_id=request_id,
        initiator_name=initiator_name,
        initiator_position=initiator_position,
        initiator_department=initiator_department,
        data=body,
        service_type_label=_SERVICE_TYPE_LABELS.get(st, st),
        approve_url=f"{settings.app_base_url}/api/approvals/{token}/{approve_path}",
        reject_url=f"{settings.app_base_url}/api/approvals/{token}/reject",
        rework_url=f"{settings.app_base_url}/api/approvals/{token}/rework",
        app_url=settings.app_base_url,
        version=version,
        submission_comment=submission_comment or "",
        price_undefined=price_undefined,
    )
    try_send_email(
        to=[approver["email"]],
        subject=f"[Согласование прейскуранта] {body.name} — требуется решение ({approver['role']}, версия {version})",
        html_body=html,
        reply_to=initiator_email,
    )


def load_branch_names() -> list[str]:
    settings = get_settings()
    path = Path(settings.branches_json)
    if not path.exists():
        return []
    import json
    items = json.loads(path.read_text(encoding="utf-8"))
    names = []
    for b in items:
        if isinstance(b, str):
            names.append(b)
        else:
            names.append(b.get("name") or b.get("id") or "")
    return [n for n in names if n]
