"""
CRUD для справочников, редактируемых ролью «Администратор формы»: согласующие
(approvers.json) и филиалы (branches.json). Все эндпоинты защищены
Depends(require_admin) — обычный сотрудник получит 403.

Роль назначается через список логинов в .env (FORM_ADMIN_LOGINS) — см.
config.Settings.admin_logins() и комментарий там же про AD-группу для прода.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

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
