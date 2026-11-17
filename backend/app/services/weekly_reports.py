"""Еженедельные отчёты по заявкам (пятница) — админам и инициаторам."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ReportRun, RequestStatus, ServiceRequest
from ..services.email_service import render_template, try_send_email
from ..services.mis_notify import mis_recipients
from ..services.stage_status import stage_summary

logger = logging.getLogger("price_form.weekly_reports")


def _load_approvers() -> dict[str, dict]:
    settings = get_settings()
    path = Path(settings.approvers_json)
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return {a["id"]: a for a in items}


def iso_week_key(when: datetime | None = None) -> str:
    when = when or datetime.utcnow()
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def already_sent(db: Session, kind: str, period_key: str) -> bool:
    return (
        db.query(ReportRun)
        .filter(ReportRun.kind == kind, ReportRun.period_key == period_key)
        .first()
        is not None
    )


def mark_sent(db: Session, kind: str, period_key: str) -> None:
    db.add(ReportRun(kind=kind, period_key=period_key, sent_at=datetime.utcnow()))
    db.commit()


def admin_emails(approvers_map: dict) -> list[str]:
    """Адреса админов формы — без инициатора конкретной заявки."""
    return [a for a in mis_recipients("", approvers_map) if a]


def build_admin_report(db: Session, *, now: datetime | None = None, stuck_days: int | None = None) -> dict:
    settings = get_settings()
    now = now or datetime.utcnow()
    stuck_days = stuck_days if stuck_days is not None else settings.weekly_report_stuck_working_days
    approvers_map = _load_approvers()
    pending = db.query(ServiceRequest).filter(ServiceRequest.status == RequestStatus.pending).all()
    week_ago = now - timedelta(days=7)
    approved_week = (
        db.query(ServiceRequest)
        .filter(ServiceRequest.status == RequestStatus.approved, ServiceRequest.updated_at >= week_ago)
        .count()
    )
    rework_week = (
        db.query(ServiceRequest)
        .filter(ServiceRequest.status.in_([RequestStatus.rework, RequestStatus.rejected]), ServiceRequest.updated_at >= week_ago)
        .count()
    )

    rows = []
    stuck = []
    for entity in pending:
        summary = stage_summary(entity, approvers_map)
        item = {
            "id": entity.id,
            "name": (entity.payload or {}).get("name") or "Заявка",
            "initiator_name": entity.initiator_name,
            "initiator_email": entity.initiator_email,
            "stage_label": summary["label"],
            "waiting_working_days": summary["waiting_working_days"],
            "stuck": summary["waiting_working_days"] >= stuck_days,
        }
        rows.append(item)
        if item["stuck"]:
            stuck.append(item)
    rows.sort(key=lambda r: (-r["waiting_working_days"], r["name"] or ""))
    return {
        "generated_at": now,
        "stuck_days": stuck_days,
        "pending": rows,
        "stuck": stuck,
        "approved_week": approved_week,
        "rework_week": rework_week,
        "app_url": settings.app_base_url,
    }


def build_initiator_reports(db: Session, *, now: datetime | None = None) -> dict[str, dict]:
    """email -> {initiator_name, pending, resolved_week}."""
    settings = get_settings()
    now = now or datetime.utcnow()
    week_ago = now - timedelta(days=7)
    approvers_map = _load_approvers()
    by_email: dict[str, dict] = defaultdict(lambda: {
        "initiator_name": "",
        "pending": [],
        "resolved_week": [],
        "app_url": settings.app_base_url,
    })

    pending = db.query(ServiceRequest).filter(ServiceRequest.status == RequestStatus.pending).all()
    for entity in pending:
        email = (entity.initiator_email or "").strip()
        if not email:
            continue
        summary = stage_summary(entity, approvers_map)
        bucket = by_email[email]
        bucket["initiator_name"] = entity.initiator_name
        bucket["pending"].append({
            "id": entity.id,
            "name": (entity.payload or {}).get("name") or "Заявка",
            "stage_label": summary["label"],
            "waiting_working_days": summary["waiting_working_days"],
        })

    resolved = (
        db.query(ServiceRequest)
        .filter(
            ServiceRequest.status.in_([RequestStatus.approved, RequestStatus.rework, RequestStatus.rejected]),
            ServiceRequest.updated_at >= week_ago,
        )
        .all()
    )
    for entity in resolved:
        email = (entity.initiator_email or "").strip()
        if not email:
            continue
        summary = stage_summary(entity, approvers_map)
        bucket = by_email[email]
        bucket["initiator_name"] = entity.initiator_name or bucket["initiator_name"]
        bucket["resolved_week"].append({
            "id": entity.id,
            "name": (entity.payload or {}).get("name") or "Заявка",
            "status": entity.status.value,
            "stage_label": summary["label"],
        })
    return dict(by_email)


def send_admin_weekly_report(db: Session, *, force: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    period = iso_week_key(now)
    kind = "admin_weekly"
    if already_sent(db, kind, period) and not force:
        return {"ok": True, "skipped": True, "period": period, "sent": 0}
    report = build_admin_report(db, now=now)
    recipients = admin_emails(_load_approvers())
    if not recipients:
        logger.error("Нет получателей еженедельного отчёта админу")
        return {"ok": False, "skipped": False, "period": period, "sent": 0, "error": "no recipients"}
    html = render_template("email_weekly_admin.html", **report)
    ok = try_send_email(
        to=recipients,
        subject=f"[Согласование услуг] Еженедельный отчёт админу — {period}",
        html_body=html,
    )
    if ok:
        mark_sent(db, kind, period)
    return {"ok": ok, "skipped": False, "period": period, "sent": 1 if ok else 0, "recipients": recipients}


def send_initiator_weekly_reports(db: Session, *, force: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    period = iso_week_key(now)
    kind = "initiator_weekly"
    if already_sent(db, kind, period) and not force:
        return {"ok": True, "skipped": True, "period": period, "sent": 0}
    reports = build_initiator_reports(db, now=now)
    sent = 0
    for email, payload in reports.items():
        if not payload["pending"] and not payload["resolved_week"]:
            continue
        html = render_template(
            "email_weekly_initiator.html",
            initiator_name=payload["initiator_name"],
            pending=payload["pending"],
            resolved_week=payload["resolved_week"],
            app_url=payload["app_url"],
            period=period,
        )
        if try_send_email(
            to=[email],
            subject=f"[Согласование услуг] Ваши заявки за неделю — {period}",
            html_body=html,
        ):
            sent += 1
    mark_sent(db, kind, period)
    return {"ok": True, "skipped": False, "period": period, "sent": sent}


def run_friday_reports(db: Session, *, force: bool = False, now: datetime | None = None) -> dict:
    admin = send_admin_weekly_report(db, force=force, now=now)
    initiators = send_initiator_weekly_reports(db, force=force, now=now)
    return {"admin": admin, "initiators": initiators}
