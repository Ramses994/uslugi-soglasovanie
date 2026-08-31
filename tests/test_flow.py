"""
Регресс-тесты для сценариев, которые проверялись вручную при разработке
(раздел 12 ТЗ.md). Каждый тест — это ровно тот сценарий, который был пройден
руками, только теперь автоматически и с падением при регрессии.

Запуск:
    pip install -r requirements-dev.txt
    pytest tests/ -v
"""
import io
import re
import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.services.dictionaries import store  # noqa: E402
from conftest import SAMPLE_REQUEST_PAYLOAD  # noqa: E402


def _extract_action_url(html: str, action: str) -> str:
    m = re.search(rf'href="([^"]*?/api/approvals/[^"]+?/{action})"', html)
    assert m, f"Не нашли ссылку '{action}' в письме:\n{html[:500]}"
    return m.group(1)


def _post_decision(client, url: str, comment: str = ""):
    """Reject/rework фиксируются POST-ом формы комментария, не GET-ом из письма."""
    return client.post(url, data={"comment": comment})


# ---------------------------------------------------------------------------
# 12.1 — загрузка справочников из реальных файлов (ЕРЛУ-КДЛ, номенклатура Минздрава)
# ---------------------------------------------------------------------------

class TestDictionaries:
    def test_erlu_loaded_from_real_file(self):
        # Регресс-тест на баг из раздела 7.2/12.1: если кто-то случайно вернёт
        # read_only=True в load_minzdrav или сломает парсинг load_erlu,
        # количество загруженных строк упадёт до 0 или станет подозрительно малым.
        assert len(store.erlu) > 1000, "ЕРЛУ-КДЛ должен содержать больше 1000 анализов"

    def test_minzdrav_loaded_from_real_file(self):
        assert len(store.minzdrav) > 10000, (
            "Справочник Минздрава должен содержать больше 10000 записей — "
            "0 записей означает регрессию бага с битым <dimension> в XLSX (раздел 7.2 ТЗ.md)"
        )

    def test_search_erlu_finds_known_analysis(self):
        results = store.search_erlu("клинический анализ крови")
        codes = {r.erlu_code for r in results}
        assert "01.00.002" in codes
        match = next(r for r in results if r.erlu_code == "01.00.002")
        assert match.lab_code == "1.0.D1.202"

    def test_search_minzdrav_finds_known_code(self):
        results = store.search_minzdrav("офтальмометрия")
        assert results and results[0].code == "A03.26.009"

    def test_best_minzdrav_match_for_service_name(self):
        match = store.best_minzdrav_match("Приём врача-терапевта повторный")
        assert match is not None
        assert match.code == "B01.047.002"


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------

class TestAuth:
    def test_login_mock_and_me(self, client):
        r = client.post("/api/auth/login", json={"login": "ivanov", "password": "any"})
        assert r.status_code == 200
        profile = r.json()
        assert profile["login"] == "ivanov"
        assert profile["email"] == "ivanov@clinic.local"

        r_me = client.get("/api/auth/me")
        assert r_me.status_code == 200
        assert r_me.json()["login"] == "ivanov"

    def test_me_without_session_is_401(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 12.2 — полный цикл: заявка → согласование → статус
# ---------------------------------------------------------------------------

class TestApprovalFlow:
    def test_submit_sends_email_to_each_approver(self, logged_in_client):
        r = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD)
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "pending"

        assert len(logged_in_client.sent_emails) == 2
        recipients = {tuple(m["to"]) for m in logged_in_client.sent_emails}
        assert ("chief.doctor@clinic.local",) in recipients
        assert ("mis.admin@clinic.local",) in recipients

    def test_full_approval_changes_status_and_generates_import_file(self, logged_in_client):
        r = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD)
        request_id = r.json()["id"]

        emails = logged_in_client.sent_emails
        approve_chief = _extract_action_url(_html_of(emails, "chief.doctor@clinic.local"), "approve")
        approve_mis = _extract_action_url(_html_of(emails, "mis.admin@clinic.local"), "approve")

        # первое согласование — статус ещё pending, ждём второго
        r1 = logged_in_client.get(approve_chief)
        assert r1.status_code == 200
        status_after_1 = logged_in_client.get(f"/api/requests/{request_id}").json()["status"]
        assert status_after_1 == "pending"

        # второе согласование — все согласовали, статус approved + письмо с файлом импорта
        r2 = logged_in_client.get(approve_mis)
        assert r2.status_code == 200
        status_after_2 = logged_in_client.get(f"/api/requests/{request_id}").json()["status"]
        assert status_after_2 == "approved"

        attachment_mails = [m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]
        assert len(attachment_mails) == 1
        wb = openpyxl.load_workbook(io.BytesIO(attachment_mails[0]["attachment_bytes"]))
        ws = wb.active
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        assert header[:3] == ["CODE", "INSURANCE_TYPE", "LABEL"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        # 2 филиала x (ДМС+ОМС, т.к. price_list='both') = 4 строки
        assert len(rows) == 4
        assert all(row[2] == "Приём врача-терапевта повторный" for row in rows)
        assert all(row[19] == "B01.047.002" for row in rows)  # MINZDRAV_CODE

    def test_repeat_click_on_same_link_is_idempotent(self, logged_in_client):
        r = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD)
        request_id = r.json()["id"]
        approve_chief = _extract_action_url(_html_of(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "approve")

        logged_in_client.get(approve_chief)
        mails_after_first_click = len(logged_in_client.sent_emails)

        r_repeat = logged_in_client.get(approve_chief)
        assert r_repeat.status_code == 200
        assert "уже зафиксировано" in r_repeat.text.lower() or "уже отметили" in r_repeat.text.lower()
        # повторный клик не должен рассылать повторных уведомлений
        assert len(logged_in_client.sent_emails) == mails_after_first_click

        status = logged_in_client.get(f"/api/requests/{request_id}").json()["status"]
        assert status == "pending"  # второй согласующий ещё не решил

    def test_reject_stops_the_request(self, logged_in_client):
        r = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD)
        request_id = r.json()["id"]
        reject_chief = _extract_action_url(_html_of(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "reject")

        r_reject = _post_decision(logged_in_client, reject_chief)
        assert r_reject.status_code == 200

        status = logged_in_client.get(f"/api/requests/{request_id}").json()["status"]
        assert status == "rework"

    def test_reject_get_opens_comment_form_without_changing_status(self, logged_in_client):
        r = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD)
        request_id = r.json()["id"]
        reject_chief = _extract_action_url(_html_of(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "reject")

        page = logged_in_client.get(reject_chief)
        assert page.status_code == 200
        assert "<form" in page.text.lower()
        assert 'name="comment"' in page.text
        assert logged_in_client.get(f"/api/requests/{request_id}").json()["status"] == "pending"

    def test_submit_email_has_three_action_links_and_version(self, logged_in_client):
        payload = {**SAMPLE_REQUEST_PAYLOAD, "submission_comment": "Просим согласовать до пятницы"}
        logged_in_client.post("/api/requests", json=payload)
        html = _html_of(logged_in_client.sent_emails, "chief.doctor@clinic.local")
        _extract_action_url(html, "approve")
        _extract_action_url(html, "reject")
        _extract_action_url(html, "rework")
        assert "версия 1" in html.lower()
        assert "Просим согласовать до пятницы" in html


def _html_of(sent_emails: list[dict], recipient: str) -> str:
    """Находит письмо, адресованное recipient, и возвращает его HTML-тело
    (conftest.py::fake_send / fake_send_with_attachment сохраняют его как есть,
    без реальной отправки — см. docstring в conftest.py)."""
    match = next(m for m in sent_emails if recipient in m["to"])
    return match["html_body"]


# ---------------------------------------------------------------------------
# Валидация
# ---------------------------------------------------------------------------

class TestValidation:
    def test_lab_service_without_lab_block_is_rejected(self, logged_in_client):
        payload = dict(SAMPLE_REQUEST_PAYLOAD)
        payload["is_lab_service"] = True
        payload["lab"] = None
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 422

    def test_unknown_approver_returns_400(self, logged_in_client):
        payload = dict(SAMPLE_REQUEST_PAYLOAD)
        payload["approver_ids"] = ["does_not_exist"]
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 400

    def test_age_from_greater_than_age_to_is_rejected(self, logged_in_client):
        payload = dict(SAMPLE_REQUEST_PAYLOAD)
        payload["age_from"] = 50
        payload["age_to"] = 10
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Роль «Администратор формы» (FORM_ADMIN_LOGINS в .env.example содержит "ivanov",
# тестовый mock-пользователь "petrova" в него не входит — используем обоих,
# чтобы проверить и допуск, и отказ)
# ---------------------------------------------------------------------------

class TestAdminRole:
    def test_regular_user_is_not_admin_and_gets_403(self, client):
        r = client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        assert r.json()["is_admin"] is False

        assert client.get("/api/admin/approvers").status_code == 403
        assert client.get("/api/admin/branches").status_code == 403
        assert client.post("/api/dictionaries/reload").status_code == 403

    def test_admin_user_flag_is_true(self, logged_in_client):
        # logged_in_client логинится как "ivanov" — он в FORM_ADMIN_LOGINS
        me = logged_in_client.get("/api/auth/me").json()
        assert me["is_admin"] is True

    def test_admin_can_crud_approvers(self, logged_in_client):
        new_approver = {"id": "test_approver", "name": "Тест Тестов", "email": "test@clinic.local",
                         "role": "Тестовая роль", "required_for": ["all"]}
        r = logged_in_client.post("/api/admin/approvers", json=new_approver)
        assert r.status_code == 201

        listed = logged_in_client.get("/api/admin/approvers").json()
        assert any(a["id"] == "test_approver" for a in listed)

        # новый согласующий сразу виден и в обычном (не только admin) списке для формы
        public_listed = logged_in_client.get("/api/dictionaries/approvers").json()
        assert any(a["id"] == "test_approver" for a in public_listed)

        r_dup = logged_in_client.post("/api/admin/approvers", json=new_approver)
        assert r_dup.status_code == 400  # дубликат id

        r_update = logged_in_client.put("/api/admin/approvers/test_approver",
                                         json={**new_approver, "role": "Обновлённая роль"})
        assert r_update.status_code == 200
        assert r_update.json()["role"] == "Обновлённая роль"

        r_delete = logged_in_client.delete("/api/admin/approvers/test_approver")
        assert r_delete.status_code == 204
        assert logged_in_client.delete("/api/admin/approvers/test_approver").status_code == 404

    def test_admin_can_crud_branches(self, logged_in_client):
        new_branch = {"id": "test_branch", "name": "Тестовый филиал"}
        r = logged_in_client.post("/api/admin/branches", json=new_branch)
        assert r.status_code == 201

        public_branches = logged_in_client.get("/api/dictionaries/branches").json()
        assert "Тестовый филиал" in public_branches

        logged_in_client.delete("/api/admin/branches/test_branch")
        public_branches_after = logged_in_client.get("/api/dictionaries/branches").json()
        assert "Тестовый филиал" not in public_branches_after


# ---------------------------------------------------------------------------
# 12.9 — подбор согласующих из каталога AD, авто-подстановка кода Минздрава,
# читаемые ошибки валидации (см. ТЗ.md, раздел 12.9)
# ---------------------------------------------------------------------------

class TestDirectorySearch:
    def test_regular_user_cannot_search_directory(self, client):
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        assert client.get("/api/admin/directory/search?q=иванов").status_code == 403

    def test_admin_can_search_directory_by_name(self, logged_in_client):
        # LDAP_MOCK=true -> ldap_auth._mock_search_directory ищет по фиксированному
        # демо-набору (_MOCK_USERS + _MOCK_DIRECTORY_EXTRA), без реального AD.
        r = logged_in_client.get("/api/admin/directory/search?q=иванов")
        assert r.status_code == 200
        results = r.json()
        assert any(u["login"] == "ivanov" for u in results)
        assert all(u["email"] for u in results)  # без email согласующего не добавить

    def test_admin_can_search_directory_by_login_substring(self, logged_in_client):
        r = logged_in_client.get("/api/admin/directory/search?q=petr")
        assert r.status_code == 200
        assert any(u["login"] == "petrova" for u in r.json())

    def test_directory_search_empty_query_returns_pool(self, logged_in_client):
        r = logged_in_client.get("/api/admin/directory/search?q=")
        assert r.status_code == 200
        assert len(r.json()) >= 2  # хотя бы ivanov + petrova


class TestMinzdravAutoSuggest:
    def test_short_name_returns_no_suggestion(self, logged_in_client):
        r = logged_in_client.get("/api/dictionaries/minzdrav/suggest?name=узи")
        assert r.status_code == 200
        assert r.json() is None  # короче 4 символов — не пытаемся угадывать

    def test_exact_name_match_returns_confident_suggestion(self, logged_in_client):
        # Берём реальное наименование из справочника (первую АКТИВНУЮ запись),
        # чтобы не зависеть от конкретных кодов — устойчиво к обновлению файла
        # НСИ Минздрава. best_minzdrav_match_scored игнорирует неактивные записи.
        sample = next(e for e in store.minzdrav if e.active)
        r = logged_in_client.get(f"/api/dictionaries/minzdrav/suggest?name={sample.name}")
        assert r.status_code == 200
        body = r.json()
        assert body is not None
        assert body["code"] == sample.code
        assert body["score"] >= 0.55

    def test_gibberish_name_returns_no_suggestion(self, logged_in_client):
        r = logged_in_client.get("/api/dictionaries/minzdrav/suggest?name=asdfqwertyzxcv0000")
        assert r.status_code == 200
        assert r.json() is None


class TestValidationErrorFormat:
    def test_422_detail_is_a_list_of_field_errors_not_opaque_objects(self, logged_in_client):
        # Регресс-тест на баг из раздела 12.9: фронтенд раньше показывал
        # "[object Object],[object Object]" для точно такого ответа — сам бэкенд
        # тут ни при чём (FastAPI и раньше отдавал detail корректно), но полезно
        # зафиксировать форму ответа, на которую опирается formatApiErrorDetail()
        # в app.js, чтобы будущий рефакторинг API не сломал фронтенд молча.
        bad_payload = {**SAMPLE_REQUEST_PAYLOAD, "prices": []}  # нарушает min_length=1
        r = logged_in_client.post("/api/requests", json=bad_payload)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert isinstance(detail, list)
        assert all("msg" in item and "loc" in item for item in detail)

    def test_blank_date_strings_are_accepted_as_optional(self, logged_in_client):
        # Регресс-тест на баг, найденный на боевом сервере 192.168.2.104: обычный
        # <input type="date"> без выбранной даты отдаёт "" (не null), а pydantic
        # v2 не приводит "" к None для типа date сам по себе — заявка без дат
        # (которые НЕ обязательны) отклонялась с "input is too short". См. schemas.py
        # (_blank_date_to_none) и ТЗ.md.
        payload = {**SAMPLE_REQUEST_PAYLOAD, "date_start": "", "date_end": ""}
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 201, r.text

    def test_missing_business_justification_is_422(self, logged_in_client):
        payload = {k: v for k, v in SAMPLE_REQUEST_PAYLOAD.items() if k != "business"}
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 422

    def test_blank_business_description_is_422(self, logged_in_client):
        payload = {**SAMPLE_REQUEST_PAYLOAD, "business": {"description": "   "}}
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 422

    def test_delete_without_mis_code_is_422(self, logged_in_client):
        payload = {**SAMPLE_REQUEST_PAYLOAD, "request_type": "delete", "mis_code": None}
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 422
        assert "МИС" in r.text or "mis_code" in r.text.lower() or "удален" in r.text.lower()

    def test_delete_with_mis_code_is_201(self, logged_in_client):
        payload = {**SAMPLE_REQUEST_PAYLOAD, "request_type": "delete", "mis_code": "01.01.001"}
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 201, r.text
        detail = logged_in_client.get(f"/api/requests/{r.json()['id']}").json()
        assert detail["request_type"] == "delete"
        assert detail["payload"]["mis_code"] == "01.01.001"


# ---------------------------------------------------------------------------
# RBAC списка/карточки заявок, фильтры, DELETE/PATCH, код МИС в экспорте
# ---------------------------------------------------------------------------

class TestRequestsRbac:
    def test_employee_sees_only_own_requests(self, client):
        client.post("/api/auth/login", json={"login": "ivanov", "password": "any"})
        admin_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]

        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        own_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]

        listed = client.get("/api/requests").json()
        ids = {item["id"] for item in listed}
        assert own_id in ids
        assert admin_id not in ids
        assert client.get(f"/api/requests/{admin_id}").status_code == 403
        assert client.get(f"/api/requests/{own_id}").status_code == 200

        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"login": "ivanov", "password": "any"})
        admin_listed = {item["id"] for item in client.get("/api/requests").json()}
        assert admin_id in admin_listed and own_id in admin_listed

    def test_employee_cannot_delete_request(self, client):
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        request_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        assert client.delete(f"/api/requests/{request_id}").status_code == 403
        assert client.get(f"/api/requests/{request_id}").status_code == 200

    def test_admin_can_delete_request(self, logged_in_client):
        request_id = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        assert logged_in_client.delete(f"/api/requests/{request_id}").status_code == 204
        assert logged_in_client.get(f"/api/requests/{request_id}").status_code == 404
        assert logged_in_client.delete(f"/api/requests/{request_id}").status_code == 404

    def test_employee_cannot_patch_pending(self, client):
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        request_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        payload = {**SAMPLE_REQUEST_PAYLOAD, "name": "Изменённое наименование услуги"}
        assert client.patch(f"/api/requests/{request_id}", json=payload).status_code == 403

    def test_admin_can_patch_pending(self, logged_in_client):
        request_id = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        payload = {**SAMPLE_REQUEST_PAYLOAD, "name": "Изменённое наименование услуги"}
        r = logged_in_client.patch(f"/api/requests/{request_id}", json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["payload"]["name"] == "Изменённое наименование услуги"

    def test_employee_can_patch_rejected(self, client):
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        request_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        reject_url = _extract_action_url(_html_of(client.sent_emails, "chief.doctor@clinic.local"), "reject")
        assert _post_decision(client, reject_url).status_code == 200
        assert client.get(f"/api/requests/{request_id}").json()["status"] == "rework"

        payload = {**SAMPLE_REQUEST_PAYLOAD, "name": "Услуга после доработки инициатором"}
        r = client.patch(f"/api/requests/{request_id}", json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["payload"]["name"] == "Услуга после доработки инициатором"


class TestRequestFilters:
    def test_filter_by_status_and_type_and_search(self, logged_in_client):
        logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD)
        promo = {**SAMPLE_REQUEST_PAYLOAD, "request_type": "promo", "name": "Акция УЗИ щитовидной железы"}
        promo_id = logged_in_client.post("/api/requests", json=promo).json()["id"]

        by_type = logged_in_client.get("/api/requests?request_type=promo").json()
        assert {item["id"] for item in by_type} == {promo_id}

        by_search = logged_in_client.get("/api/requests?q=щитовидной").json()
        assert {item["id"] for item in by_search} == {promo_id}
        assert logged_in_client.get("/api/requests?q=zzzznotfound").json() == []

        pending = logged_in_client.get("/api/requests?status=pending").json()
        assert all(item["status"] == "pending" for item in pending)
        assert len(pending) >= 2


class TestMisCodeExport:
    def test_mis_code_goes_to_import_code_column(self, logged_in_client):
        from app.schemas import ServiceRequestIn
        from app.services.medialog_export import build_import_workbook

        req = ServiceRequestIn.model_validate({**SAMPLE_REQUEST_PAYLOAD, "mis_code": "01.01.001"})
        wb = openpyxl.load_workbook(io.BytesIO(build_import_workbook(req)))
        rows = list(wb.active.iter_rows(min_row=2, values_only=True))
        assert rows
        assert all(row[0] == "01.01.001" for row in rows)


class TestSmtpEnvelope:
    def test_display_name_in_smtp_from_is_not_used_as_envelope(self, monkeypatch):
        # Регресс на 550 5.7.60 Exchange: MAIL FROM = «Имя <addr>» целиком.
        import app.services.email_service as email_service

        monkeypatch.setattr(
            email_service.settings,
            "smtp_from",
            "Согласование услуг МИС <svc-clinic-apps@medkvadrat.local>",
        )
        monkeypatch.setattr(email_service.settings, "smtp_user", "svc-clinic-apps@medkvadrat.local")
        header, envelope = email_service._from_header_and_envelope()
        assert envelope == "svc-clinic-apps@medkvadrat.local"
        assert "svc-clinic-apps@medkvadrat.local" in header


class TestApprovalHistoryAndResubmit:
    def test_full_approval_writes_history_rows(self, logged_in_client):
        request_id = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        emails = logged_in_client.sent_emails
        logged_in_client.get(_extract_action_url(_html_of(emails, "chief.doctor@clinic.local"), "approve"))
        logged_in_client.get(_extract_action_url(_html_of(emails, "mis.admin@clinic.local"), "approve"))

        detail = logged_in_client.get(f"/api/requests/{request_id}").json()
        assert detail["status"] == "approved"
        assert detail["version"] == 1
        decisions = [h["decision"] for h in detail["history"]]
        assert decisions == ["approve", "approve"]
        assert all(h["status_before"] == "pending" for h in detail["history"])
        assert detail["history"][-1]["status_after"] == "approved"

        via_endpoint = logged_in_client.get(f"/api/requests/{request_id}/history").json()
        assert len(via_endpoint) == 2

    def test_rework_sets_status_and_keeps_comment(self, logged_in_client):
        request_id = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        rework_url = _extract_action_url(
            _html_of(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "rework"
        )
        form = logged_in_client.get(rework_url)
        assert form.status_code == 200
        assert "<form" in form.text.lower()
        r = _post_decision(logged_in_client, rework_url, "Нужно уточнить цену")
        assert r.status_code == 200
        assert "доработк" in r.text.lower()

        detail = logged_in_client.get(f"/api/requests/{request_id}").json()
        assert detail["status"] == "rework"
        assert detail["approvals"][0]["decision"] == "rework"
        assert any(h["decision"] == "rework" and h["comment"] == "Нужно уточнить цену" for h in detail["history"])

    def test_reject_is_recorded_as_reject_but_status_is_rework(self, logged_in_client):
        request_id = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        reject_url = _extract_action_url(
            _html_of(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "reject"
        )
        _post_decision(logged_in_client, reject_url, "Недостаточно обоснования")
        detail = logged_in_client.get(f"/api/requests/{request_id}").json()
        assert detail["status"] == "rework"
        assert any(h["decision"] == "reject" and h["status_after"] == "rework" for h in detail["history"])
        assert any(h["comment"] == "Недостаточно обоснования" for h in detail["history"])

        initiator_mail = next(
            m for m in reversed(logged_in_client.sent_emails)
            if "ivanov@clinic.local" in m["to"] and m["kind"] == "mail"
        )
        assert "Недостаточно обоснования" in initiator_mail["html_body"]
        assert "border-left" in initiator_mail["html_body"]

    def test_cannot_decide_after_rework(self, logged_in_client):
        request_id = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        emails = logged_in_client.sent_emails
        rework_url = _extract_action_url(_html_of(emails, "chief.doctor@clinic.local"), "rework")
        approve_mis = _extract_action_url(_html_of(emails, "mis.admin@clinic.local"), "approve")
        _post_decision(logged_in_client, rework_url)
        r = logged_in_client.get(approve_mis)
        assert r.status_code == 200
        assert "уже не на согласовании" in r.text.lower()
        assert logged_in_client.get(f"/api/requests/{request_id}").json()["status"] == "rework"

    def test_resubmit_from_rework_increments_version_and_resends_mail(self, logged_in_client):
        created = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()
        request_id = created["id"]
        assert created["version"] == 1
        rework_url = _extract_action_url(
            _html_of(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "rework"
        )
        _post_decision(logged_in_client, rework_url)
        before = len(logged_in_client.sent_emails)

        r = logged_in_client.post(
            f"/api/requests/{request_id}/resubmit",
            json={"submission_comment": "Цену поправили"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"
        assert r.json()["version"] == 2
        assert len(logged_in_client.sent_emails) == before + 2

        detail = logged_in_client.get(f"/api/requests/{request_id}").json()
        assert detail["version"] == 2
        assert detail["submission_comment"] == "Цену поправили"
        assert all(a["decision"] is None for a in detail["approvals"])
        assert any(h["decision"] == "resubmit" for h in detail["history"])
        html = _html_of(logged_in_client.sent_emails[-2:], "chief.doctor@clinic.local")
        assert "версия 2" in html.lower()
        assert "Цену поправили" in html

    def test_employee_cannot_resubmit_pending(self, client):
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        request_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        assert client.post(f"/api/requests/{request_id}/resubmit", json={}).status_code == 403

    def test_employee_cannot_resubmit_someone_elses_rework(self, client):
        client.post("/api/auth/login", json={"login": "ivanov", "password": "any"})
        request_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        rework_url = _extract_action_url(_html_of(client.sent_emails, "chief.doctor@clinic.local"), "rework")
        _post_decision(client, rework_url)
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        assert client.post(f"/api/requests/{request_id}/resubmit", json={}).status_code == 403

    def test_admin_can_resubmit_approved(self, logged_in_client):
        request_id = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        emails = logged_in_client.sent_emails
        logged_in_client.get(_extract_action_url(_html_of(emails, "chief.doctor@clinic.local"), "approve"))
        logged_in_client.get(_extract_action_url(_html_of(emails, "mis.admin@clinic.local"), "approve"))
        assert logged_in_client.get(f"/api/requests/{request_id}").json()["status"] == "approved"

        r = logged_in_client.post(f"/api/requests/{request_id}/resubmit", json={})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"
        assert r.json()["version"] == 2

    def test_owner_cannot_resubmit_approved(self, client):
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        request_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        emails = client.sent_emails
        client.get(_extract_action_url(_html_of(emails, "chief.doctor@clinic.local"), "approve"))
        client.get(_extract_action_url(_html_of(emails, "mis.admin@clinic.local"), "approve"))
        assert client.get(f"/api/requests/{request_id}").json()["status"] == "approved"
        assert client.post(f"/api/requests/{request_id}/resubmit", json={}).status_code == 403

