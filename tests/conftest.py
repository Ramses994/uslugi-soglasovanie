"""
Общие фикстуры тестов.

Изоляция БД: каждый тест получает свою SQLite-базу во временной директории
(через dependency_overrides для get_db) — тесты никогда не трогают
data/requests.db, которым пользуется приложение при обычном запуске.

Изоляция почты: routers.requests.try_send_email и
routers.approvals.try_send_email / try_send_email_with_attachment
подменяются фейками, которые складывают письма в список client.sent_emails
вместо реальной отправки — тесты работают без доступного SMTP-сервера
(ровно так, как выполнялось ручное smoke-тестирование при разработке,
см. раздел 12 ТЗ.md).

Аутентификация: LDAP_MOCK=true по умолчанию (см. .env.example) — реальный AD
не требуется, любой логин/пароль принимается (backend/app/auth/ldap_auth.py).

Изоляция справочников approvers.json/branches.json (роль «Администратор
формы», routers/admin.py): в отличие от БД, admin_store читает пути прямо из
settings — Depends() тут ни при чём. Поскольку get_settings() кэширован
(@lru_cache), все модули держат ОДИН И ТОТ ЖЕ объект Settings, поэтому
monkeypatch.setattr(settings, "approvers_json", ...) в фикстуре ниже
подменяет путь сразу во всех роутерах — без этого тесты CRUD дописывали бы
записи в реальные data/approvers.json и data/branches.json.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.services.dictionaries import store as dictionaries_store  # noqa: E402
import app.routers.approvals as approvals_router  # noqa: E402

# Справочники (ЕРЛУ-КДЛ, номенклатура Минздрава) обычно грузятся в память на
# FastAPI-событии "startup" (см. app/main.py::on_startup). TestDictionaries-тесты
# обращаются к store напрямую, без поднятия TestClient/приложения — поэтому грузим
# их здесь один раз при сборе тестов, а не полагаемся на порядок выполнения тестов.
dictionaries_store.reload_all()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db

    # Изолированные копии approvers.json/branches.json — см. docstring вверху файла.
    settings = get_settings()
    approvers_path = tmp_path / "approvers.json"
    shutil.copy(ROOT / "data" / "approvers.json", approvers_path)
    branches_path = tmp_path / "branches.json"
    branches_path.write_text(
        json.dumps([
            {"id": "kurkino", "name": "Куркино", "mis_org_code": "Куркино", "mis_price_category_code": "Куркино"},
            {"id": "kashirka", "name": "Каширка", "mis_org_code": "Каширка", "mis_price_category_code": "Каширка"},
            {"id": "kurkino-2", "name": "Куркино 2 (взр.)", "mis_org_code": "Куркино 2 (взр.)", "mis_price_category_code": "Куркино 2 (взр.)"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "approvers_json", str(approvers_path))
    monkeypatch.setattr(settings, "branches_json", str(branches_path))
    # Не полагаемся на наличие/содержимое .env в окружении, где гоняются тесты —
    # роль администратора для тестового пользователя "ivanov" задаём явно здесь.
    monkeypatch.setattr(settings, "form_admin_logins", "ivanov")
    monkeypatch.setattr(settings, "ldap_directory_mail_suffix", "@clinic.local")
    monkeypatch.setattr(settings, "mis_notify_emails", "")

    sent = []

    def fake_send(to, subject, html_body, **kwargs):
        sent.append({"kind": "mail", "to": to, "subject": subject, "html_body": html_body})
        return True

    def fake_send_with_attachment(to, subject, html_body, attachment_bytes, attachment_filename, **kwargs):
        sent.append({
            "kind": "mail+attachment", "to": to, "subject": subject, "html_body": html_body,
            "attachment_filename": attachment_filename, "attachment_size": len(attachment_bytes),
            "attachment_bytes": attachment_bytes,
        })
        return True

    monkeypatch.setattr(approvals_router, "try_send_email", fake_send)
    import app.services.approval_chain as approval_chain
    import app.services.email_service as email_service
    monkeypatch.setattr(approval_chain, "try_send_email", fake_send)
    monkeypatch.setattr(email_service, "try_send_email", fake_send)
    monkeypatch.setattr(email_service, "try_send_email_with_attachment", fake_send_with_attachment)

    with TestClient(fastapi_app) as test_client:
        test_client.sent_emails = sent
        yield test_client

    fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def logged_in_client(client):
    r = client.post("/api/auth/login", json={"login": "ivanov", "password": "any"})
    assert r.status_code == 200, r.text
    return client


SAMPLE_REQUEST_PAYLOAD = {
    "request_type": "new_service",
    "name": "Приём врача-терапевта повторный",
    "service_type": "simple",
    "price_list": "both",
    "all_branches": True,
    "prices": [{"branch": "Куркино", "price": 1500}, {"branch": "Каширка", "price": 1500}],
    "sex_limit": "all",
    "is_lab_service": False,
    "minzdrav": {"code": "B01.047.002", "name": "Прием врача-терапевта повторный"},
    "business": {"description": "Повторный приём терапевта — расширение прейскуранта ДМС и ОМС."},
    "approver_ids": ["chief_doctor", "mis_admin"],
}
