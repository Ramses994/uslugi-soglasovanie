"""Текстовое описание текущего этапа согласования и «дней ожидания»."""
from __future__ import annotations

from datetime import datetime, timedelta

from .approval_chain import current_approver_id, is_resolved_decision


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def stage_waiting_since(entity) -> datetime | None:
    """С какого момента ждём решение на текущем этапе."""
    chain = list(entity.approval_chain or [])
    idx = int(entity.current_stage_index or 0)
    approvals = list(entity.approvals or [])
    by_id = {a.get("approver_id"): a for a in approvals}
    # предыдущий закрытый этап
    for i in range(idx - 1, -1, -1):
        if i >= len(chain):
            continue
        prev = by_id.get(chain[i]) or {}
        if is_resolved_decision(prev.get("decision")):
            dt = _parse_dt(prev.get("decided_at"))
            if dt:
                return dt
    return entity.created_at


def working_days_between(start: datetime, end: datetime) -> int:
    """Число рабочих дней (пн–пт) между start и end, не считая день start."""
    if end < start:
        return 0
    days = 0
    cur = start.date() + timedelta(days=1)
    last = end.date()
    while cur <= last:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def stage_summary(entity, approvers_map: dict | None = None) -> dict:
    """Краткое описание этапа для списка/карточки/отчёта."""
    approvers_map = approvers_map or {}
    chain = list(entity.approval_chain or [])
    idx = int(entity.current_stage_index or 0)
    status = entity.status.value if hasattr(entity.status, "value") else str(entity.status)
    total = len(chain) or max(len(entity.approvals or []), 1)
    current_id = current_approver_id(entity) if status == "pending" and chain else None
    current_name = None
    if current_id:
        current_name = (approvers_map.get(current_id) or {}).get("name") or current_id

    if status == "approved":
        label = "Согласовано всеми"
    elif status in ("rejected", "rework"):
        # кто отклонил / отправил на доработку
        culprit = None
        for a in entity.approvals or []:
            if a.get("decision") in ("reject", "rework"):
                aid = a.get("approver_id")
                culprit = (approvers_map.get(aid) or {}).get("name") or aid
                break
        stage_no = idx + 1 if chain else 1
        who = f" ({culprit})" if culprit else ""
        verb = "Отклонено" if status == "rejected" else "На доработку"
        label = f"{verb} на этапе {stage_no}{who}"
    elif status == "pending" and chain and current_name:
        label = f"Этап {idx + 1} из {total}: ожидает {current_name}"
    elif status == "pending":
        label = f"Этап {idx + 1} из {total}: на согласовании"
    else:
        label = status

    waiting_since = stage_waiting_since(entity) if status == "pending" else None
    now = datetime.utcnow()
    waiting_days = working_days_between(waiting_since, now) if waiting_since else 0

    return {
        "label": label,
        "stage_index": idx,
        "stage_number": idx + 1 if chain else 1,
        "stage_total": total,
        "current_approver_id": current_id,
        "current_approver_name": current_name,
        "waiting_since": waiting_since.isoformat() if waiting_since else None,
        "waiting_working_days": waiting_days,
    }
