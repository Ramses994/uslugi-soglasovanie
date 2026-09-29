"""
CRUD для справочников, редактируемых ролью «Администратор формы»: согласующие
(approvers.json) и филиалы (branches.json). Все эндпоинты защищены
Depends(require_admin) — обычный сотрудник получит 403.

Роль назначается через список логинов в .env (FORM_ADMIN_LOGINS) — см.
config.Settings.admin_logins() и комментарий там же про AD-группу для прода.
"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth.ldap_auth import search_directory, LdapAuthError
from ..config import get_settings
from ..database import get_db
from ..models import ServiceRequest
from ..services import admin_store
from ..services.mis_notify import send_mis_import_email
from .auth import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])
settings = get_settings()


class ApproverIn(BaseModel):
    id: str
    name: str
    email: str
    role: str
    required_for: list[str] = ["all"]
    order: int = 50


class BranchIn(BaseModel):
    id: str
    name: str


# ---------------------------------------------------------------------------
# Поиск по корпоративному каталогу (AD) — для подбора согласующих без ручного
# набора email. В боевом режиме ходит в AD под сервисной учёткой (см.
# ldap_auth.search_directory), в LDAP_MOCK — по фиксированному демо-набору.
# ---------------------------------------------------------------------------

@router.get("/directory/search")
def search_corporate_directory(q: str = Query("", description="Часть ФИО, логина или email"),
                                limit: int = 15, user=Depends(require_admin)):
    try:
        results = search_directory(q, limit=limit)
    except LdapAuthError as exc:
        raise HTTPException(400, str(exc))
    return [
        {"login": u.login, "name": u.display_name, "email": u.email, "department": u.department, "position": u.position}
        for u in results
    ]


# ---------------------------------------------------------------------------
# Согласующие
# ---------------------------------------------------------------------------

@router.get("/approvers")
def list_approvers(user=Depends(require_admin)):
    return admin_store.list_items(settings.approvers_json)


@router.post("/approvers", status_code=201)
def create_approver(body: ApproverIn, user=Depends(require_admin)):
    try:
        return admin_store.add_item(settings.approvers_json, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/approvers/{approver_id}")
def update_approver(approver_id: str, body: ApproverIn, user=Depends(require_admin)):
    try:
        return admin_store.update_item(settings.approvers_json, approver_id, body.model_dump())
    except KeyError:
        raise HTTPException(404, "Согласующий не найден")


@router.delete("/approvers/{approver_id}", status_code=204)
def delete_approver(approver_id: str, user=Depends(require_admin)):
    try:
        admin_store.delete_item(settings.approvers_json, approver_id)
    except KeyError:
        raise HTTPException(404, "Согласующий не найден")


# ---------------------------------------------------------------------------
# Филиалы
# ---------------------------------------------------------------------------

@router.get("/branches")
def list_branches(user=Depends(require_admin)):
    return admin_store.list_items(settings.branches_json)


@router.post("/branches", status_code=201)
def create_branch(body: BranchIn, user=Depends(require_admin)):
    try:
        return admin_store.add_item(settings.branches_json, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/branches/{branch_id}")
def update_branch(branch_id: str, body: BranchIn, user=Depends(require_admin)):
    try:
        return admin_store.update_item(settings.branches_json, branch_id, body.model_dump())
    except KeyError:
        raise HTTPException(404, "Филиал не найден")


@router.delete("/branches/{branch_id}", status_code=204)
def delete_branch(branch_id: str, user=Depends(require_admin)):
    try:
        admin_store.delete_item(settings.branches_json, branch_id)
    except KeyError:
        raise HTTPException(404, "Филиал не найден")


@router.post("/requests/{request_id}/notify-mis")
def notify_mis_import(
    request_id: str,
    force: bool = Query(False, description="Повторно отправить, даже если письмо уже уходило"),
    db: Session = Depends(get_db),
    user=Depends(require_admin),
):
    """Дослать файл импорта в МИС по уже согласованной заявке без пересоздания."""
    entity = db.get(ServiceRequest, request_id)
    if not entity:
        raise HTTPException(404, "Заявка не найдена")
    path = Path(settings.approvers_json)
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    approvers_map = {a["id"]: a for a in items}
    result = send_mis_import_email(db, entity, approvers_map, force=force)
    if result.get("skipped") and result.get("error"):
        raise HTTPException(400, result["error"])
    if not result.get("ok"):
        raise HTTPException(502, result.get("error") or "Не удалось отправить письмо в МИС")
    return result
