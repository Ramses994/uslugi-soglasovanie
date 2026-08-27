"""Неизменяемый журнал решений по заявке.

Новые строки только добавляются (approve / reject / rework / resubmit).
Старые не правятся и не удаляются — это аудит, а не текущий снимок согласования
(снимок по-прежнему в JSON-поле ServiceRequest.approvals).
"""
from sqlalchemy.orm import Session

from ..models import ApprovalHistory


def record(
    db: Session,
    *,
    request_id: str,
    approver_login: str,
    approver_name: str,
    approver_email: str,
    decision: str,
    comment: str | None = None,
    status_before: str | None = None,
    status_after: str | None = None,
    approver_id: str | None = None,
) -> ApprovalHistory:
    row = ApprovalHistory(
        request_id=request_id,
        approver_id=approver_id,
        approver_login=approver_login,
        approver_name=approver_name,
        approver_email=approver_email,
        decision=decision,
        comment=comment,
        status_before=status_before,
        status_after=status_after,
    )
    db.add(row)
    return row


def list_for_request(db: Session, request_id: str) -> list[ApprovalHistory]:
    return (
        db.query(ApprovalHistory)
        .filter(ApprovalHistory.request_id == request_id)
        .order_by(ApprovalHistory.created_at.asc(), ApprovalHistory.id.asc())
        .all()
    )


def serialize(row: ApprovalHistory) -> dict:
    return {
        "id": row.id,
        "approver_id": row.approver_id,
        "approver_login": row.approver_login,
        "approver_name": row.approver_name,
        "approver_email": row.approver_email,
        "decision": row.decision,
        "comment": row.comment,
        "created_at": row.created_at,
        "status_before": row.status_before,
        "status_after": row.status_after,
    }
