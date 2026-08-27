"""
Аутентификация в Active Directory / LDAP.

Схема:
1. Пользователь присылает логин+пароль на POST /api/auth/login (форма логина
   выполняет запрос по HTTPS, пароль никогда не пишется в лог и не сохраняется).
2. Мы делаем LDAP simple bind СРАЗУ под учёткой пользователя (user@domain или
   DOMAIN\\login) — это и есть проверка пароля. AD сам вернёт ошибку, если
   пароль неверный, учётка заблокирована/просрочена и т.д.
3. Тем же соединением (или отдельным поиском под сервисной учёткой read-only,
   если политика AD запрещает пользователю читать чужие атрибуты) вычитываем
   атрибуты: sAMAccountName, displayName, mail, title, department.
4. Соединение закрывается, пароль пользователя не персистится нигде — ни в БД,
   ни в токене. Токен сессии несёт только логин и уже извлечённые атрибуты.

Для локальной разработки/демонстрации без боевого AD включается LDAP_MOCK=true —
тогда любой пароль принимается, а профиль строится по таблице LDAP_MOCK_USERS.
"""
from dataclasses import dataclass
from typing import Optional

from ldap3 import Server, Connection, ALL, SUBTREE, NTLM
from ldap3.core.exceptions import LDAPBindError, LDAPException
from ldap3.utils.conv import escape_filter_chars

from ..config import get_settings

settings = get_settings()


def _make_server() -> Server:
    """Собирает Server с учётом LDAPS. ldap3 сам разбирает схему из строки вида
    'ldaps://host:636', но LDAP_USE_SSL позволяет форсировать TLS, даже если в
    LDAP_SERVER схему не указали (просто hostname или 'ldap://...').
    Зачем это вообще нужно — см. комментарий у Settings.ldap_use_ssl в config.py:
    боевой AD нередко отклоняет незашифрованный simple bind как неверный пароль,
    хотя пароль верный."""
    use_ssl = settings.ldap_use_ssl or settings.ldap_server.strip().lower().startswith("ldaps://")
    return Server(settings.ldap_server, use_ssl=use_ssl, get_info=ALL)


@dataclass
class LdapUser:
    login: str
    display_name: str
    email: str
    department: Optional[str]
    position: Optional[str]


class LdapAuthError(Exception):
    pass


# ---- MOCK-режим для разработки/демо ----
_MOCK_USERS = {
    "ivanov": LdapUser("ivanov", "Иванов Иван Иванович", "ivanov@clinic.local", "Регистратура", "Администратор"),
    "petrova": LdapUser("petrova", "Петрова Анна Сергеевна", "petrova@clinic.local", "Отдел развития", "Продукт-менеджер"),
}


def _mock_authenticate(login: str, password: str) -> LdapUser:
    if not password:
        raise LdapAuthError("Пустой пароль")
    login_key = login.lower().split("\\")[-1]
    if login_key in _MOCK_USERS:
        return _MOCK_USERS[login_key]
    # любой другой логин в mock-режиме тоже пускаем, чтобы форму можно было
    # проверить с произвольным ФИО во время демо
    return LdapUser(
        login=login_key,
        display_name=login_key.capitalize(),
        email=f"{login_key}@clinic.local",
        department="Демо-отдел",
        position="Сотрудник",
    )


def authenticate(login: str, password: str) -> LdapUser:
    """Проверяет логин/пароль в AD и возвращает атрибуты профиля.
    Бросает LdapAuthError при неверных данных."""
    if settings.ldap_mock:
        return _mock_authenticate(login, password)

    # РАНЬШЕ здесь было захардкожено f"{login}@{settings.ldap_domain}.local" —
    # ломалось на любом домене, чей DNS-суффикс не .local (например, medkvadrat.ru
    # для домена MEDKVADRAT). Теперь суффикс берётся из настройки, см. config.py.
    user_principal = login if "@" in login or "\\" in login else f"{login}@{settings.ldap_upn_suffix}"

    server = _make_server()
    try:
        # Шаг 1: bind под самим пользователем = проверка пароля
        user_conn = Connection(
            server,
            user=user_principal,
            password=password,
            authentication=NTLM if "\\" in login else "SIMPLE",
            auto_bind=True,
        )
    except LDAPBindError as exc:
        raise LdapAuthError("Неверный логин или пароль") from exc
    except LDAPException as exc:
        raise LdapAuthError(f"Ошибка соединения с AD: {exc}") from exc

    try:
        # Шаг 2: поиск атрибутов. Если у пользователя нет прав на self-read
        # (редко, но бывает при жёстких GPO), переоткрываем соединение под
        # сервисной read-only учёткой.
        search_conn = user_conn
        sam_account = login.split("\\")[-1].split("@")[0]
        search_filter = f"(sAMAccountName={sam_account})"

        found = search_conn.search(
            search_base=settings.ldap_user_search_base,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["sAMAccountName", "displayName", "mail", "title", "department"],
        )

        if not found or not search_conn.entries:
            if settings.ldap_service_user and settings.ldap_service_password:
                search_conn = Connection(
                    server,
                    user=settings.ldap_service_user,
                    password=settings.ldap_service_password,
                    auto_bind=True,
                )
                search_conn.search(
                    search_base=settings.ldap_user_search_base,
                    search_filter=search_filter,
                    search_scope=SUBTREE,
                    attributes=["sAMAccountName", "displayName", "mail", "title", "department"],
                )
            if not search_conn.entries:
                raise LdapAuthError("Пользователь аутентифицирован, но профиль в AD не найден")

        entry = search_conn.entries[0]
        return LdapUser(
            login=str(entry.sAMAccountName) if entry.sAMAccountName else sam_account,
            display_name=str(entry.displayName) if entry.displayName else sam_account,
            email=str(entry.mail) if entry.mail else f"{sam_account}@{settings.ldap_upn_suffix}",
            department=str(entry.department) if entry.department else None,
            position=str(entry.title) if entry.title else None,
        )
    finally:
        user_conn.unbind()


def search_directory(query: str, limit: int = 20) -> list[LdapUser]:
    """Поиск по каталогу AD для подбора согласующих во вкладке «Администрирование»
    (см. routers/admin.py, GET /api/admin/directory/search) — админ ищет коллегу по
    части ФИО/логина/почты вместо того, чтобы вручную набирать email. Требует
    сервисной read-only учётки (LDAP_SERVICE_USER/LDAP_SERVICE_PASSWORD) — bind под
    самим админом здесь не подходит, т.к. обычному пользователю AD может быть
    запрещено читать чужие карточки (только свою)."""
    if settings.ldap_mock:
        return _mock_search_directory(query, limit)

    if not (settings.ldap_service_user and settings.ldap_service_password):
        raise LdapAuthError(
            "Поиск по каталогу AD недоступен: не заполнены LDAP_SERVICE_USER/"
            "LDAP_SERVICE_PASSWORD в .env (нужна сервисная read-only учётка)"
        )

    server = _make_server()
    try:
        conn = Connection(server, user=settings.ldap_service_user, password=settings.ldap_service_password, auto_bind=True)
    except LDAPBindError as exc:
        raise LdapAuthError("Сервисная учётка LDAP_SERVICE_USER: неверный логин/пароль") from exc
    except LDAPException as exc:
        raise LdapAuthError(f"Ошибка соединения с AD: {exc}") from exc

    try:
        q = escape_filter_chars((query or "").strip())
        name_filter = f"(|(displayName=*{q}*)(sAMAccountName=*{q}*)(mail=*{q}*))" if q else ""
        mail_filter = f"(mail=*{settings.ldap_directory_mail_suffix})" if settings.ldap_directory_mail_suffix else "(mail=*)"
        search_filter = f"(&(objectClass=user){mail_filter}{name_filter})"

        conn.search(
            search_base=settings.ldap_user_search_base,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["sAMAccountName", "displayName", "mail", "title", "department"],
            size_limit=limit,
        )
        return [
            LdapUser(
                login=str(entry.sAMAccountName) if entry.sAMAccountName else "",
                display_name=str(entry.displayName) if entry.displayName else str(entry.sAMAccountName),
                email=str(entry.mail) if entry.mail else "",
                department=str(entry.department) if entry.department else None,
                position=str(entry.title) if entry.title else None,
            )
            for entry in conn.entries
            if entry.mail  # без почты согласующего добавить нельзя — email обязателен в approvers.json
        ]
    except LDAPException as exc:
        raise LdapAuthError(f"Ошибка поиска в AD: {exc}") from exc
    finally:
        conn.unbind()


# Небольшой доп. набор для демонстрации поиска по каталогу в LDAP_MOCK — без
# него в mock-режиме подбор согласующих искал бы только по 2 тестовым логинам
# (ivanov/petrova) и выглядел бы пустым почти при любом запросе.
_MOCK_DIRECTORY_EXTRA = [
    LdapUser("chief_doctor", "Смирнова Елена Викторовна", "smirnova@clinic.local", "Руководство", "Главный врач"),
    LdapUser("commercial_director", "Козлов Артём Павлович", "kozlov@clinic.local", "Коммерческий отдел", "Коммерческий директор"),
    LdapUser("lab_head", "Фёдорова Ольга Игоревна", "fedorova@clinic.local", "КДЛ", "Заведующий лабораторией"),
]


def _mock_search_directory(query: str, limit: int) -> list[LdapUser]:
    q = (query or "").strip().lower()
    pool = list(_MOCK_USERS.values()) + _MOCK_DIRECTORY_EXTRA
    if not q:
        return pool[:limit]
    return [u for u in pool if q in u.display_name.lower() or q in u.login.lower() or q in u.email.lower()][:limit]
