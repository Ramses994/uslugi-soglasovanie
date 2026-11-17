import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from ..services import admin_store
from ..services.dictionaries import store
from ..config import get_settings
from .auth import get_current_user, require_admin

router = APIRouter(prefix="/api/dictionaries", tags=["dictionaries"])
settings = get_settings()


@router.get("/erlu")
def search_erlu(q: str = Query("", description="Строка поиска по коду/наименованию ЕРЛУ"), limit: int = 20,
                 user=Depends(get_current_user)):
    results = store.search_erlu(q, limit=limit)
    return [
        {
            "erlu_code": e.erlu_code,
            "erlu_name": e.erlu_name,
            "lab_code": e.lab_code,
            "lab_name": e.lab_name,
            "is_composite": e.is_composite,
            "section": e.section,
        }
        for e in results
    ]


@router.get("/minzdrav")
def search_minzdrav(q: str = Query("", description="Строка поиска по коду/наименованию Минздрава"), limit: int = 20,
                     user=Depends(get_current_user)):
    results = store.search_minzdrav(q, limit=limit)
    return [{"code": e.code, "name": e.name, "active": e.active} for e in results]


@router.get("/minzdrav/suggest")
def suggest_minzdrav(name: str = Query("", description="Текст поля «Наименование услуги» — для авто-подстановки кода"),
                      user=Depends(get_current_user)):
    """Фоновая авто-подсказка кода Минздрава по мере ввода наименования услуги —
    в отличие от /minzdrav (обычный поиск для ручного автокомплита), тут порог
    строже и в ответе только один лучший кандидат: подставлять код без участия
    пользователя можно только при уверенном совпадении, см. min_score в
    DictionaryStore.best_minzdrav_match_scored."""
    if not name or len(name.strip()) < 4:
        return None
    match = store.best_minzdrav_match_scored(name)
    if not match:
        return None
    score, entry = match
    return {"code": entry.code, "name": entry.name, "score": round(score, 3)}


@router.get("/branches")
def branches(user=Depends(get_current_user)):
    # Имена филиалов читаются из branches.json (редактируется через роль
    # «Администратор формы», см. routers/admin.py) — для формы «Новая заявка»
    # нужен только плоский список названий, полные объекты {id, name} — в /api/admin/branches.
    return [b["name"] for b in admin_store.list_items(settings.branches_json)]


@router.get("/service-groups")
def service_groups(user=Depends(get_current_user)):
    path = Path(settings.service_groups_json)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/approvers")
def approvers(user=Depends(get_current_user)):
    path = Path(settings.approvers_json)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


@router.post("/reload")
def reload_dictionaries(user=Depends(require_admin)):
    return store.reload_all()
