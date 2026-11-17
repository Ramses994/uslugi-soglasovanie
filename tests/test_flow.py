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
    m = re.search(rf'href="([^"]*?/api/approvals/[^"/]+/{action})"', html)
    assert m, f"Не нашли ссылку '{action}' в письме:\n{html[:500]}"
    return m.group(1)


def _html_to(sent_emails: list[dict], recipient: str, last: bool = True) -> str:
    matches = [m for m in sent_emails if recipient in m["to"]]
    assert matches, f"Нет письма для {recipient}"
    return (matches[-1] if last else matches[0])["html_body"]


def _approve_as(client, recipient: str):
    url = _extract_action_url(_html_to(client.sent_emails, recipient), "approve")
    return client.get(url)


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

        assert len(logged_in_client.sent_emails) == 1
        recipients = {tuple(m["to"]) for m in logged_in_client.sent_emails}
        assert ("chief.doctor@clinic.local",) in recipients
        assert ("mis.admin@clinic.local",) not in recipients

    def test_full_approval_changes_status_and_generates_import_file(self, logged_in_client):
        r = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD)
        request_id = r.json()["id"]

        emails = logged_in_client.sent_emails
        approve_chief = _extract_action_url(_html_to(emails, "chief.doctor@clinic.local"), "approve")

        r1 = logged_in_client.get(approve_chief)
        assert r1.status_code == 200
        status_after_1 = logged_in_client.get(f"/api/requests/{request_id}").json()["status"]
        assert status_after_1 == "pending"

        approve_mis = _extract_action_url(_html_to(logged_in_client.sent_emails, "mis.admin@clinic.local"), "approve")
        r2 = logged_in_client.get(approve_mis)
        assert r2.status_code == 200
        status_after_2 = logged_in_client.get(f"/api/requests/{request_id}").json()["status"]
        assert status_after_2 == "approved"

        attachment_mails = [m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]
        assert len(attachment_mails) == 1
        recips = attachment_mails[0]["to"]
        assert "mis.admin@clinic.local" in recips
        assert "ivanov@clinic.local" in recips
        assert logged_in_client.get(f"/api/requests/{request_id}").json()["mis_notified_at"]
        wb = openpyxl.load_workbook(io.BytesIO(attachment_mails[0]["attachment_bytes"]))
        ws = wb.active
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        assert header[:3] == ["CODE", "INSURANCE_TYPE", "LABEL"]
        assert "EXAM_GR_CODE" in header
        assert header[-1] == "MODELS_CODE"
        assert "Импорт прейскурантов" in wb.sheetnames
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        # 2 филиала x (ДМС+ОМС, т.к. price_list='both') = 4 строки
        assert len(rows) == 4
        assert all(row[0] for row in rows)
        assert all(row[2] == "Приём врача-терапевта повторный" for row in rows)
        assert all(row[6] == "НДС-0" for row in rows)
        assert all(row[7] == "000" for row in rows)
        assert all(row[18] == "B01.047.002" for row in rows)  # MINZDRAV_CODE

    def test_repeat_click_on_same_link_is_idempotent(self, logged_in_client):
        r = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD)
        request_id = r.json()["id"]
        approve_chief = _extract_action_url(_html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "approve")

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
        reject_chief = _extract_action_url(_html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "reject")

        r_reject = _post_decision(logged_in_client, reject_chief)
        assert r_reject.status_code == 200

        status = logged_in_client.get(f"/api/requests/{request_id}").json()["status"]
        assert status == "rework"

    def test_reject_get_opens_comment_form_without_changing_status(self, logged_in_client):
        r = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD)
        request_id = r.json()["id"]
        reject_chief = _extract_action_url(_html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "reject")

        page = logged_in_client.get(reject_chief)
        assert page.status_code == 200
        assert "<form" in page.text.lower()
        assert 'name="comment"' in page.text
        assert logged_in_client.get(f"/api/requests/{request_id}").json()["status"] == "pending"

    def test_submit_email_has_three_action_links_and_version(self, logged_in_client):
        payload = {**SAMPLE_REQUEST_PAYLOAD, "submission_comment": "Просим согласовать до пятницы"}
        logged_in_client.post("/api/requests", json=payload)
        html = _html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local")
        _extract_action_url(html, "approve")
        _extract_action_url(html, "reject")
        _extract_action_url(html, "rework")
        assert "версия 1" in html.lower()
        assert "Просим согласовать до пятницы" in html


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
        bad_payload = {**SAMPLE_REQUEST_PAYLOAD, "prices": [], "all_branches": False, "price_undefined": False}
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
        payload = {
            **SAMPLE_REQUEST_PAYLOAD,
            "request_type": "delete",
            "mis_code": "01.01.001",
            "affected_branches": ["Куркино"],
            "simple_justification": "Услуга больше не оказывается",
        }
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
        reject_url = _extract_action_url(_html_to(client.sent_emails, "chief.doctor@clinic.local"), "reject")
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
        _approve_as(logged_in_client, "chief.doctor@clinic.local")
        _approve_as(logged_in_client, "mis.admin@clinic.local")

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
            _html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "rework"
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
            _html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "reject"
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
        rework_url = _extract_action_url(_html_to(emails, "chief.doctor@clinic.local"), "rework")
        _post_decision(logged_in_client, rework_url)
        r = logged_in_client.get(rework_url)
        assert r.status_code == 200
        assert "уже зафиксировано" in r.text.lower() or "уже не на согласовании" in r.text.lower()
        assert logged_in_client.get(f"/api/requests/{request_id}").json()["status"] == "rework"

    def test_resubmit_from_rework_increments_version_and_resends_mail(self, logged_in_client):
        created = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()
        request_id = created["id"]
        assert created["version"] == 1
        rework_url = _extract_action_url(
            _html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "rework"
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
        assert len(logged_in_client.sent_emails) == before + 1

        detail = logged_in_client.get(f"/api/requests/{request_id}").json()
        assert detail["version"] == 2
        assert detail["submission_comment"] == "Цену поправили"
        assert all(a["decision"] is None for a in detail["approvals"])
        assert any(h["decision"] == "resubmit" for h in detail["history"])
        html = _html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local")
        assert "версия 2" in html.lower()
        assert "Цену поправили" in html

    def test_employee_cannot_resubmit_pending(self, client):
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        request_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        assert client.post(f"/api/requests/{request_id}/resubmit", json={}).status_code == 403

    def test_employee_cannot_resubmit_someone_elses_rework(self, client):
        client.post("/api/auth/login", json={"login": "ivanov", "password": "any"})
        request_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        rework_url = _extract_action_url(_html_to(client.sent_emails, "chief.doctor@clinic.local"), "rework")
        _post_decision(client, rework_url)
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        assert client.post(f"/api/requests/{request_id}/resubmit", json={}).status_code == 403

    def test_admin_can_resubmit_approved(self, logged_in_client):
        request_id = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        _approve_as(logged_in_client, "chief.doctor@clinic.local")
        _approve_as(logged_in_client, "mis.admin@clinic.local")
        assert logged_in_client.get(f"/api/requests/{request_id}").json()["status"] == "approved"

        r = logged_in_client.post(f"/api/requests/{request_id}/resubmit", json={})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"
        assert r.json()["version"] == 2

    def test_owner_cannot_resubmit_approved(self, client):
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        request_id = client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        emails = client.sent_emails
        _approve_as(client, "chief.doctor@clinic.local")
        _approve_as(client, "mis.admin@clinic.local")
        assert client.get(f"/api/requests/{request_id}").json()["status"] == "approved"
        assert client.post(f"/api/requests/{request_id}/resubmit", json={}).status_code == 403


def _add_chain_anchors(client):
    client.post("/api/admin/approvers", json={
        "id": "nfolomova", "name": "Фоломова", "email": "nfolomova@clinic.local",
        "role": "Маркетинг", "required_for": ["all"], "order": 1,
    })
    client.post("/api/admin/approvers", json={
        "id": "osokolovskaya", "name": "Соколовская", "email": "osokolovskaya@clinic.local",
        "role": "ГД", "required_for": ["all"], "order": 999,
    })


class TestSequentialChain:
    def test_folomova_first_sokolovskaya_last_even_if_not_selected(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        payload = {**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]}
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 201, r.text
        assert r.json()["approval_chain"] == ["nfolomova", "chief_doctor", "osokolovskaya"]
        assert len(logged_in_client.sent_emails) == 1
        assert "nfolomova@clinic.local" in logged_in_client.sent_emails[0]["to"]

    def test_out_of_turn_approve_is_idempotent(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        logged_in_client.post("/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]})
        rid = logged_in_client.get("/api/requests").json()[0]["id"]
        from app.security import make_approval_token
        url = f"/api/approvals/{make_approval_token(rid, 'osokolovskaya')}/approve"
        page = logged_in_client.get(url)
        assert page.status_code == 200
        assert "не ваша очередь" in page.text.lower()
        assert logged_in_client.get(f"/api/requests/{rid}").json()["status"] == "pending"

    def test_resubmit_skips_already_approved(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        created = logged_in_client.post(
            "/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]}
        ).json()
        rid = created["id"]
        _approve_as(logged_in_client, "nfolomova@clinic.local")
        rework_url = _extract_action_url(
            _html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "rework"
        )
        _post_decision(logged_in_client, rework_url, "правьте описание")
        before = len(logged_in_client.sent_emails)
        r = logged_in_client.post(f"/api/requests/{rid}/resubmit", json={"submission_comment": "готово"})
        assert r.status_code == 200, r.text
        assert len(logged_in_client.sent_emails) == before + 1
        detail = logged_in_client.get(f"/api/requests/{rid}").json()
        by_id = {a["approver_id"]: a["decision"] for a in detail["approvals"]}
        assert by_id["nfolomova"] == "approve"
        assert by_id["chief_doctor"] is None
        assert "chief.doctor@clinic.local" in logged_in_client.sent_emails[-1]["to"]

    def test_folomova_price_form_then_next_stage(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        payload = {
            **SAMPLE_REQUEST_PAYLOAD,
            "approver_ids": ["chief_doctor"],
            "price_undefined": True,
            "prices": [],
            "all_branches": False,
        }
        rid = logged_in_client.post("/api/requests", json=payload).json()["id"]
        html = _html_to(logged_in_client.sent_emails, "nfolomova@clinic.local")
        price_url = _extract_action_url(html, "approve-with-price")
        page = logged_in_client.get(price_url)
        assert page.status_code == 200
        assert "<form" in page.text.lower()
        posted = logged_in_client.post(price_url, data={"price_Куркино": "2500", "price_Каширка": "2500"})
        assert posted.status_code == 200
        detail = logged_in_client.get(f"/api/requests/{rid}").json()
        assert detail["status"] == "pending"
        assert detail["payload"]["price_undefined"] is False
        assert detail["payload"]["prices"]
        assert "chief.doctor@clinic.local" in logged_in_client.sent_emails[-1]["to"]

    def test_full_chain_sends_one_mis_mail(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        rid = logged_in_client.post(
            "/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]}
        ).json()["id"]
        _approve_as(logged_in_client, "nfolomova@clinic.local")
        _approve_as(logged_in_client, "chief.doctor@clinic.local")
        before = [m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]
        assert before == []
        _approve_as(logged_in_client, "osokolovskaya@clinic.local")
        attachments = [m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]
        assert len(attachments) == 1
        recips = attachments[0]["to"]
        assert "mis.admin@clinic.local" in recips
        assert "ivanov@clinic.local" in recips
        detail = logged_in_client.get(f"/api/requests/{rid}").json()
        assert detail["status"] == "approved"
        assert detail["mis_notified_at"]

    def test_mis_notify_is_idempotent_and_admin_can_force_resend(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        rid = logged_in_client.post(
            "/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]}
        ).json()["id"]
        _approve_as(logged_in_client, "nfolomova@clinic.local")
        _approve_as(logged_in_client, "chief.doctor@clinic.local")
        _approve_as(logged_in_client, "osokolovskaya@clinic.local")
        assert len([m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]) == 1

        skipped = logged_in_client.post(f"/api/admin/requests/{rid}/notify-mis")
        assert skipped.status_code == 200, skipped.text
        assert skipped.json()["skipped"] is True
        assert len([m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]) == 1

        forced = logged_in_client.post(f"/api/admin/requests/{rid}/notify-mis?force=true")
        assert forced.status_code == 200, forced.text
        assert forced.json()["skipped"] is False
        assert len([m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]) == 2

    def test_rework_resubmit_sends_mis_mail_only_after_final_round(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        rid = logged_in_client.post(
            "/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]}
        ).json()["id"]
        _approve_as(logged_in_client, "nfolomova@clinic.local")
        rework_url = _extract_action_url(
            _html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "rework"
        )
        _post_decision(logged_in_client, rework_url, "правьте")
        assert [m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"] == []
        logged_in_client.post(f"/api/requests/{rid}/resubmit", json={"submission_comment": "готово"})
        _approve_as(logged_in_client, "chief.doctor@clinic.local")
        assert [m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"] == []
        _approve_as(logged_in_client, "osokolovskaya@clinic.local")
        assert len([m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]) == 1
        assert logged_in_client.get(f"/api/requests/{rid}").json()["mis_notified_at"]

    def test_old_parallel_then_resubmit_still_sends_mis_mail(self, logged_in_client):
        """Заявка без approval_chain (старая параллельная логика) после доработки
        получает цепочку, и финальное письмо в МИС всё равно уходит."""
        from app.database import get_db
        from app.main import app as fastapi_app
        from app.models import ServiceRequest

        created = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()
        rid = created["id"]
        gen = fastapi_app.dependency_overrides[get_db]()
        db = next(gen)
        row = db.get(ServiceRequest, rid)
        row.approval_chain = []
        row.current_stage_index = 0
        db.add(row)
        db.commit()
        db.close()
        try:
            next(gen)
        except StopIteration:
            pass

        rework_url = _extract_action_url(
            _html_to(logged_in_client.sent_emails, "chief.doctor@clinic.local"), "rework"
        )
        _post_decision(logged_in_client, rework_url, "на старой логике")
        _add_chain_anchors(logged_in_client)
        logged_in_client.post(f"/api/requests/{rid}/resubmit", json={"submission_comment": "на новой цепочке"})
        chain = logged_in_client.get(f"/api/requests/{rid}").json()["approval_chain"]
        assert chain[0] == "nfolomova"
        assert chain[-1] == "osokolovskaya"
        _approve_as(logged_in_client, "nfolomova@clinic.local")
        _approve_as(logged_in_client, "chief.doctor@clinic.local")
        _approve_as(logged_in_client, "mis.admin@clinic.local")
        _approve_as(logged_in_client, "osokolovskaya@clinic.local")
        attachments = [m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]
        assert len(attachments) == 1
        assert logged_in_client.get(f"/api/requests/{rid}").json()["status"] == "approved"

    def test_empty_chain_parallel_all_approve_sends_mis_mail(self, logged_in_client):
        from app.database import get_db
        from app.main import app as fastapi_app
        from app.models import ServiceRequest

        rid = logged_in_client.post("/api/requests", json=SAMPLE_REQUEST_PAYLOAD).json()["id"]
        gen = fastapi_app.dependency_overrides[get_db]()
        db = next(gen)
        row = db.get(ServiceRequest, rid)
        row.approval_chain = []
        db.add(row)
        db.commit()
        db.close()
        try:
            next(gen)
        except StopIteration:
            pass

        _approve_as(logged_in_client, "chief.doctor@clinic.local")
        assert logged_in_client.get(f"/api/requests/{rid}").json()["status"] == "pending"
        from app.security import make_approval_token
        logged_in_client.get(f"/api/approvals/{make_approval_token(rid, 'mis_admin')}/approve")
        assert logged_in_client.get(f"/api/requests/{rid}").json()["status"] == "approved"
        assert len([m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]) == 1


class TestPrompt10Fields:
    def test_price_undefined_without_prices_is_ok(self, logged_in_client):
        payload = {**SAMPLE_REQUEST_PAYLOAD, "price_undefined": True, "prices": [], "all_branches": False}
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 201, r.text

    def test_change_requires_description_and_branches(self, logged_in_client):
        payload = {**SAMPLE_REQUEST_PAYLOAD, "request_type": "change", "mis_code": "01.01.001"}
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 422
        payload.update({
            "change_description": "Цена",
            "affected_branches": ["Куркино"],
            "simple_justification": "Прайс",
        })
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 201, r.text

    def test_complex_requires_included_services(self, logged_in_client):
        payload = {**SAMPLE_REQUEST_PAYLOAD, "request_type": "complex_service", "included_services": []}
        assert logged_in_client.post("/api/requests", json=payload).status_code == 422
        payload["included_services"] = [{"name": "Приём терапевта", "code": "01.01.001"}]
        r = logged_in_client.post("/api/requests", json=payload)
        assert r.status_code == 201, r.text
        detail = logged_in_client.get(f"/api/requests/{r.json()['id']}").json()
        assert detail["payload"]["included_services"][0]["name"] == "Приём терапевта"


class TestExcelImportBulk:
    def test_template_and_preview_and_bulk(self, logged_in_client):
        tpl = logged_in_client.get("/api/requests/template.xlsx")
        assert tpl.status_code == 200
        assert tpl.headers["content-type"].startswith("application/vnd.openxmlformats")

        from openpyxl import load_workbook
        from io import BytesIO
        wb = load_workbook(BytesIO(tpl.content))
        ws = wb.active
        ws["A3"] = "new_service"
        ws["B3"] = "Услуга из Excel для массовой загрузки"
        ws["D3"] = "simple"
        ws["E3"] = "нет"
        ws["G3"] = "Обоснование из файла массовой загрузки услуг"
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        price_col = headers.index("price_Куркино") + 1
        ws.cell(3, price_col, 1200)
        buf = BytesIO()
        wb.save(buf)

        preview = logged_in_client.post(
            "/api/requests/import-preview",
            files={"file": ("t.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert preview.status_code == 200, preview.text
        results = preview.json()["results"]
        assert len(results) == 1
        assert results[0]["errors"] == []

        bulk = logged_in_client.post("/api/requests/bulk", json={
            "items": [results[0]["data"]],
            "approver_ids": ["chief_doctor"],
        })
        assert bulk.status_code == 200, bulk.text
        assert len(bulk.json()["created"]) == 1
        assert bulk.json()["errors"] == []


class TestMedialogExport12:
    def _req(self, **overrides):
        from app.schemas import ServiceRequestIn
        payload = {
            **SAMPLE_REQUEST_PAYLOAD,
            "price_list": "dms",
            "all_branches": False,
            "prices": [{"branch": "Куркино", "price": 1500}, {"branch": "Каширка", "price": 1500}],
            "mis_code": "90001",
            "short_name": "",
            **overrides,
        }
        return ServiceRequestIn.model_validate(payload)

    def _wb(self, req):
        from app.services.medialog_export import build_import_workbook
        return openpyxl.load_workbook(io.BytesIO(build_import_workbook(req, generated_code_hint=req.mis_code or "90001")))

    def test_serv_and_price_sheets_share_code(self):
        from app.services.medialog_export import PRICE_COLUMNS, SERV_COLUMNS
        req = self._req()
        wb = self._wb(req)
        assert [c.value for c in next(wb["Импорт услуг"].iter_rows(min_row=1, max_row=1))] == SERV_COLUMNS
        assert [c.value for c in next(wb["Импорт прейскурантов"].iter_rows(min_row=1, max_row=1))] == PRICE_COLUMNS
        serv_rows = list(wb["Импорт услуг"].iter_rows(min_row=2, values_only=True))
        price_rows = list(wb["Импорт прейскурантов"].iter_rows(min_row=2, values_only=True))
        assert {r[0] for r in serv_rows} == {"90001"}
        assert {r[0] for r in price_rows} == {"90001"}
        assert len(price_rows) == 2
        assert all(r[1] == 1500 for r in price_rows)

    def test_serv_ext_type_mapping(self):
        from app.services.medialog_export import serv_ext_type
        expected = {
            "simple": "S", "complex": "D", "combined": "P", "program": "C", "package": "X", "prn": "S",
        }
        for value, code in expected.items():
            extra = {}
            if value == "combined":
                extra["request_type"] = "complex_service"
                extra["included_services"] = [{"name": "Приём", "code": "1"}]
            req = self._req(service_type=value, **extra)
            assert serv_ext_type(req) == code
            row = list(self._wb(req)["Импорт услуг"].iter_rows(min_row=2, max_row=2, values_only=True))[0]
            assert row[11] == code

    def test_short_label_filled_and_trimmed(self):
        from app.services.medialog_export import SHORT_LABEL_MAX, make_short_label
        long_name = ("Комплексный анализ крови на аминокислоты сорок восемь показателей ВЭЖХ-МС " * 4)[:255]
        assert make_short_label("Bio-Oss") == "Bio-Oss"
        short = make_short_label(long_name)
        assert short
        assert len(short) <= SHORT_LABEL_MAX
        assert "…" not in short
        req = self._req(name=long_name, short_name=None)
        row = list(self._wb(req)["Импорт услуг"].iter_rows(min_row=2, max_row=2, values_only=True))[0]
        assert row[3] == short

    def test_description_from_business_justification(self):
        req = self._req(
            description=None,
            simple_justification=None,
            business={**SAMPLE_REQUEST_PAYLOAD["business"], "description": "Обоснование из заявки для МИС"},
        )
        row = list(self._wb(req)["Импорт услуг"].iter_rows(min_row=2, max_row=2, values_only=True))[0]
        assert row[4] == "Обоснование из заявки для МИС"

    def test_defaults_taxe_dep_exam_text_and_cyrillic_minzdrav(self):
        req = self._req(minzdrav={"code": "A03.26.009", "name": "Офтальмометрия"})
        ws = self._wb(req)["Импорт услуг"]
        row = list(ws.iter_rows(min_row=2, max_row=2, values_only=True))[0]
        assert row[6] == "НДС-0"
        assert row[7] == "000"
        assert row[16] == "Куркино"
        assert row[18].startswith("А")
        exam_cell = ws.cell(2, 20)
        assert exam_cell.value in (None, "")
        assert exam_cell.number_format == "@"

    def test_regression_payloads_from_production_cases(self):
        cases = [
            {"name": "Остеопатический материал Bio-Oss 1гр.", "prices": [{"branch": "Куркино", "price": 22000}]},
            {"name": "Экспресс-тест на ОРВИ (5 инфекций)", "prices": [{"branch": "Куркино", "price": 1900}]},
            {"name": "Комплексный анализ крови на аминокислоты (48 показателей), ВЭЖХ-МС", "prices": [{"branch": "Куркино", "price": 12000}]},
            {"name": "Стероидный профиль крови, ВЭЖХ-МС", "prices": [{"branch": "Куркино", "price": 1}]},
            {"name": "Комплексный анализ крови на аминокислоты (12 показателей), ВЭЖХ-МС", "prices": [{"branch": "Куркино", "price": 5000}]},
        ]
        for case in cases:
            req = self._req(mis_code="11000", **case)
            wb = self._wb(req)
            serv = list(wb["Импорт услуг"].iter_rows(min_row=2, values_only=True))
            prices = list(wb["Импорт прейскурантов"].iter_rows(min_row=2, values_only=True))
            assert serv and prices
            assert all(r[0] == "11000" for r in serv)
            assert all(r[0] == "11000" for r in prices)
            assert all(r[1] == "D" for r in serv)
            assert serv[0][2] == case["name"]


class TestSkipStageAndReports:
    def test_admin_skips_middle_stage_and_notifies(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        rid = logged_in_client.post(
            "/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]}
        ).json()["id"]
        _approve_as(logged_in_client, "nfolomova@clinic.local")
        before = len(logged_in_client.sent_emails)
        r = logged_in_client.post(
            f"/api/requests/{rid}/skip-stage",
            json={"comment": "Фоломова уже согласовала, Камаев в отпуске"},
        )
        assert r.status_code == 200, r.text
        detail = logged_in_client.get(f"/api/requests/{rid}").json()
        assert detail["status"] == "pending"
        by_id = {a["approver_id"]: a for a in detail["approvals"]}
        assert by_id["chief_doctor"]["decision"] == "skipped"
        assert by_id["chief_doctor"]["decided_by"] == "ivanov"
        assert "osokolovskaya" in detail["stage_label"].lower() or detail["stage_summary"]["current_approver_id"] == "osokolovskaya"
        assert len(logged_in_client.sent_emails) > before
        skip_mails = [m for m in logged_in_client.sent_emails[before:] if "Пропуск этапа" in m["subject"]]
        assert skip_mails
        assert any("отпуске" in m["html_body"] for m in skip_mails)
        # инициатор + админы формы (не только следующий согласующий)
        skip_recipients = set()
        for m in skip_mails:
            skip_recipients.update(m["to"] if isinstance(m["to"], (list, tuple)) else [m["to"]])
        assert "ivanov@clinic.local" in skip_recipients
        next_mails = [m for m in logged_in_client.sent_emails[before:] if "требуется решение" in m["subject"].lower()]
        assert next_mails  # письмо следующему согласующему — отдельно от уведомления о пропуске

    def test_non_admin_cannot_skip_stage(self, client):
        client.post("/api/auth/login", json={"login": "ivanov", "password": "any"})
        _add_chain_anchors(client)
        rid = client.post("/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]}).json()["id"]
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        assert client.post(f"/api/requests/{rid}/skip-stage", json={"comment": "нет прав"}).status_code == 403

    def test_skip_last_stage_sends_mis_mail_once(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        rid = logged_in_client.post(
            "/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]}
        ).json()["id"]
        _approve_as(logged_in_client, "nfolomova@clinic.local")
        _approve_as(logged_in_client, "chief.doctor@clinic.local")
        attachments_before = [m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]
        assert attachments_before == []
        r = logged_in_client.post(
            f"/api/requests/{rid}/skip-stage",
            json={"comment": "Соколовская в отпуске, пропускаем финальный этап"},
        )
        assert r.status_code == 200, r.text
        detail = logged_in_client.get(f"/api/requests/{rid}").json()
        assert detail["status"] == "approved"
        assert detail["mis_notified_at"]
        attachments = [m for m in logged_in_client.sent_emails if m["kind"] == "mail+attachment"]
        assert len(attachments) == 1
        # повторный skip на approved запрещён
        assert logged_in_client.post(
            f"/api/requests/{rid}/skip-stage", json={"comment": "ещё раз"}
        ).status_code == 400

    def test_stage_label_in_list(self, logged_in_client):
        _add_chain_anchors(logged_in_client)
        logged_in_client.post("/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]})
        items = logged_in_client.get("/api/requests").json()
        assert items
        assert "этап" in (items[0].get("stage_label") or "").lower()
        assert items[0]["stage_summary"]["current_approver_id"] == "nfolomova"

    def test_admin_report_marks_stuck_by_working_days(self, logged_in_client):
        from datetime import datetime, timedelta
        from app.database import get_db
        from app.main import app as fastapi_app
        from app.models import ServiceRequest
        from app.services.weekly_reports import build_admin_report

        _add_chain_anchors(logged_in_client)
        rid = logged_in_client.post(
            "/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]}
        ).json()["id"]
        gen = fastapi_app.dependency_overrides[get_db]()
        db = next(gen)
        row = db.get(ServiceRequest, rid)
        row.created_at = datetime(2026, 11, 9, 10, 0, 0)  # понедельник
        db.add(row)
        db.commit()
        now = datetime(2026, 11, 13, 10, 0, 0)  # пятница — 4 рабочих дня после пн
        report = build_admin_report(db, now=now, stuck_days=3)
        db.close()
        try:
            next(gen)
        except StopIteration:
            pass
        assert any(r["id"] == rid and r["stuck"] for r in report["pending"])
        assert report["stuck"]

    def test_initiator_report_is_personal(self, client):
        from app.database import get_db
        from app.main import app as fastapi_app
        from app.services.weekly_reports import build_initiator_reports

        client.post("/api/auth/login", json={"login": "ivanov", "password": "any"})
        _add_chain_anchors(client)
        client.post("/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"]})
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"login": "petrova", "password": "any"})
        client.post("/api/requests", json={**SAMPLE_REQUEST_PAYLOAD, "approver_ids": ["chief_doctor"], "name": "Услуга Петровой"})
        gen = fastapi_app.dependency_overrides[get_db]()
        db = next(gen)
        reports = build_initiator_reports(db)
        db.close()
        try:
            next(gen)
        except StopIteration:
            pass
        assert "ivanov@clinic.local" in reports
        assert "petrova@clinic.local" in reports
        ivanov_names = {r["name"] for r in reports["ivanov@clinic.local"]["pending"]}
        petrova_names = {r["name"] for r in reports["petrova@clinic.local"]["pending"]}
        assert "Услуга Петровой" not in ivanov_names
        assert "Услуга Петровой" in petrova_names

    def test_weekly_report_idempotent_same_week(self, logged_in_client, monkeypatch):
        from datetime import datetime
        from app.database import get_db
        from app.main import app as fastapi_app
        from app.services import weekly_reports

        sent = []

        def fake_send(to, subject, html_body, **kwargs):
            sent.append({"to": to, "subject": subject})
            return True

        monkeypatch.setattr(weekly_reports, "try_send_email", fake_send)
        gen = fastapi_app.dependency_overrides[get_db]()
        db = next(gen)
        now = datetime(2026, 11, 13, 7, 0, 0)
        first = weekly_reports.run_friday_reports(db, force=False, now=now)
        second = weekly_reports.run_friday_reports(db, force=False, now=now)
        db.close()
        try:
            next(gen)
        except StopIteration:
            pass
        assert first["admin"]["skipped"] is False
        assert second["admin"]["skipped"] is True
        assert second["initiators"]["skipped"] is True



