"""
Простое JSON-хранилище для справочников, которые редактируются через роль
«Администратор формы» (approvers.json, branches.json). Не отдельная таблица
БД — записей единицы/десятки, транзакционность и конкурентная запись не
критичны для MVP; при росте требований легко перенести в SQLAlchemy-модели
по тому же паттерну, что ServiceRequest в models.py.

Каждая запись — dict с обязательным полем "id" (стабильный идентификатор,
не меняется при редактировании, в отличие от "name").
"""
import json
import re
import threading
from pathlib import Path

_lock = threading.Lock()  # оба файла маленькие, но пишем последовательно на всякий случай


def _read(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _write(path: str, items: list[dict]) -> None:
    Path(path).write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def list_items(path: str) -> list[dict]:
    return _read(path)


def add_item(path: str, item: dict) -> dict:
    with _lock:
        items = _read(path)
        if any(i["id"] == item["id"] for i in items):
            raise ValueError(f"Запись с id={item['id']!r} уже существует")
        items.append(item)
        _write(path, items)
        return item


def update_item(path: str, item_id: str, patch: dict) -> dict:
    with _lock:
        items = _read(path)
        for i in items:
            if i["id"] == item_id:
                i.update(patch)
                _write(path, items)
                return i
        raise KeyError(item_id)


def delete_item(path: str, item_id: str) -> None:
    with _lock:
        items = _read(path)
        filtered = [i for i in items if i["id"] != item_id]
        if len(filtered) == len(items):
            raise KeyError(item_id)
        _write(path, filtered)


def _slugify(name: str) -> str:
    # ВАЖНО: не ограничиваться [a-zA-Z0-9] — весь домен на русском (филиалы,
    # ФИО согласующих), и такой фильтр вырезал бы все буквы, оставляя только
    # цифры/пунктуацию. Это реальный баг, пойманный на смоке: "Куркино" и
    # "Каширка" оба схлопывались в один и тот же id "item" (пустая строка после
    # вырезания кириллицы → фолбэк), что ломает уникальность id в CRUD-хранилище.
    # \w в Python re по умолчанию unicode-aware и матчит кириллицу, поэтому
    # slug получается человекочитаемым и уникальным ("куркино", "каширка", ...).
    slug = re.sub(r"[^\w]+", "-", name.strip().lower()).strip("-")
    return slug or "item"


def ensure_seed(path: str, seed_names: list[str]) -> None:
    """Создаёт файл со списком {id, name} из seed_names, но только если файла
    ещё нет — не перезаписывает то, что уже отредактировал администратор."""
    with _lock:
        if Path(path).exists():
            return
        items = [{"id": _slugify(name), "name": name} for name in seed_names]
        _write(path, items)
