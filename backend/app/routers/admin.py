"""
CRUD для справочников, редактируемых ролью «Администратор формы»: согласующие
(approvers.json) и филиалы (branches.json). Все эндпоинты защищены
Depends(require_admin) — обычный сотрудник получит 403.

Роль назначается через список логинов в .env (FORM_ADMIN_LOGINS) — см.
config.Settings.admin_logins() и комментарий там же про AD-группу для прода.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..auth.ldap_auth import search_directory, LdapAuthError
from ..config import get_settings
from ..services import admin_store
from .auth import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])
settings = get_settings()


class ApproverIn(BaseModel):
    id: str
    name: str
    email: str
    role: str
    required_for: list[str] = ["all"]


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
