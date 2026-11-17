"""
Конфигурация приложения. Все параметры читаются из переменных окружения
(файл .env в корне проекта) через pydantic-settings.

ВАЖНО про относительные пути (dict_*_path, approvers_json, database_url,
и сам .env): в .env.example они записаны как "./data/...", что интуитивно
читается как "относительно корня проекта". Но relative paths в Python
резолвятся относительно ТЕКУЩЕЙ РАБОЧЕЙ ДИРЕКТОРИИ ПРОЦЕССА, а не
расположения файла .env или кода. README рекомендует запуск командой
`cd backend && uvicorn app.main:app` — в этом случае cwd = .../backend,
а не корень проекта, и "./data/requests.db" резолвился бы в
".../backend/data/requests.db", которой не существует → SQLite падает с
"unable to open database file" (воспроизведено при smoke-тесте с реальным
uvicorn, а не TestClient — TestClient не выявил проблему, т.к. тестовый
скрипт запускался из корня проекта).

Решение: PROJECT_ROOT вычисляется от расположения ЭТОГО файла
(backend/app/config.py → на два уровня выше — корень проекта), а не от
cwd. Все относительные пути из .env резолвятся относительно PROJECT_ROOT
после загрузки, поэтому приложение работает одинаково независимо от того,
из какой директории его запустили (cd backend && uvicorn ..., или
uvicorn backend.app.main:app из корня, или из Docker с произвольным WORKDIR).
Абсолютные пути в .env (например, сетевой путь к общей папке со
справочниками) по-прежнему поддерживаются и не трогаются.
"""
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> app -> backend -> <корень проекта>
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_path(value: str) -> str:
    path = Path(value)
    return value if path.is_absolute() else str((PROJECT_ROOT / path).resolve())


def _resolve_sqlite_url(url: str) -> str:
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith("sqlite:////"):
        rel = url[len(prefix):]
        return f"{prefix}{(PROJECT_ROOT / rel).resolve()}"
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),  # абсолютный путь — не зависит от cwd
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    app_secret_key: str = "dev-secret"
    app_base_url: str = "http://localhost:8000"
    jwt_expire_minutes: int = 480

    # LDAP
    ldap_mock: bool = True
    ldap_server: str = "ldap://dc01.clinic.local"
    ldap_domain: str = "CLINIC"  # NetBIOS-имя домена (для формата DOMAIN\\login)
    # Суффикс UPN для построения "login@suffix" из голого логина без домена —
    # РАНЬШЕ это было захардкожено как f"{ldap_domain}.local", что ломается на
    # любом боевом AD, чей DNS-домен не заканчивается на .local (например,
    # medkvadrat.ru). Обычно совпадает с DNS-именем домена AD, но если в AD
    # настроен отдельный альтернативный суффикс UPN — укажите именно его.
    ldap_upn_suffix: str = "clinic.local"
    # Принудительно использовать LDAPS (порт 636) даже если ldap_server начинается
    # с "ldap://". Многие AD, начиная примерно с Server 2019/патчей 2020+, по
    # умолчанию требуют LDAP signing/channel binding для simple bind — обычный
    # незашифрованный bind по ldap:// в таком случае отклоняется как "неверные
    # учётные данные" ДАЖЕ ПРИ ПРАВИЛЬНОМ ПАРОЛЕ, что легко спутать с опечаткой в
    # пароле. Самый надёжный вариант — использовать ldap_server=ldaps://... (тогда
    # схема детектируется автоматически) или явно выставить этот флаг.
    ldap_use_ssl: bool = False
    ldap_base_dn: str = "DC=clinic,DC=local"
    ldap_user_search_base: str = "OU=Users,DC=clinic,DC=local"
    # Сервисная read-only учётка — нужна не только как fallback при чтении
    # атрибутов (см. ldap_auth.authenticate), но и для поиска по каталогу
    # (подбор согласующих на вкладке «Администрирование», см. ldap_auth.search_directory) —
    # без неё поиск по каталогу в боевом режиме недоступен.
    ldap_service_user: str = ""
    ldap_service_password: str = ""
    # Ограничить поиск по каталогу учётками с почтой в этом домене (например,
    # "@medkvadrat.ru"), чтобы не показывать в подборе согласующих служебные/
    # системные учётки без корпоративной почты. Пусто — не фильтровать.
    ldap_directory_mail_suffix: str = ""

    # SMTP
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "Прейскурант МЕДИАЛОГ <noreply@clinic.local>"
    smtp_send_as_user: bool = False

    # Справочники
    dict_erlu_kdl_path: str = "./data/ERLU-KDL_13.10.25.xls"
    dict_minzdrav_path: str = "./data/minzdrav_nsi_1.2.643.5.1.13.13.11.1070.xlsx"
    dict_refresh_on_start: bool = True

    approvers_json: str = "./data/approvers.json"

    # Справочник филиалов — редактируется через роль «Администратор формы» (routers/admin.py),
    # хранится как JSON-файл того же вида, что approvers.json (список {id, name}), а не
    # только в .env, чтобы его можно было менять без переменных окружения/рестарта.
    # branches_seed используется один раз — только если branches.json ещё не существует
    # (первый запуск); дальше единственный источник истины — сам файл.
    # В реальном контуре список филиалов можно вместо этого подгружать из МИС (таблица
    # FM_DEP / внутренние мед. учреждения) через отдельный read-only коннектор к БД Медиалог.
    branches_json: str = "./data/branches.json"
    service_groups_json: str = "./data/service_groups.json"
    branches_seed: list[str] = ["Куркино", "Каширка", "Куркино 2 (взр.)"]

    database_url: str = "sqlite:///./data/requests.db"

    # Роль «Администратор формы»: логины через запятую (регистр не важен), например
    # "ivanov,petrova". В боевом контуре стоит заменить на проверку членства в AD-группе
    # (например, CN=PriceFormAdmins) — см. раздел 10 ТЗ.md; строка-список — самый простой
    # вариант для MVP и для локальной разработки/демо без реального AD.
    form_admin_logins: str = ""
    # Явные адреса для письма с файлом импорта в МИС (через запятую). Нужны, когда
    # администратор формы есть в FORM_ADMIN_LOGINS, но его нет в approvers.json.
    mis_notify_emails: str = ""
    # Порог «застрявшей» заявки для еженедельного отчёта админу (рабочие дни).
    weekly_report_stuck_working_days: int = 3

    @model_validator(mode="after")
    def _resolve_relative_paths(self):
        self.dict_erlu_kdl_path = _resolve_path(self.dict_erlu_kdl_path)
        self.dict_minzdrav_path = _resolve_path(self.dict_minzdrav_path)
        self.approvers_json = _resolve_path(self.approvers_json)
        self.branches_json = _resolve_path(self.branches_json)
        self.service_groups_json = _resolve_path(self.service_groups_json)
        self.database_url = _resolve_sqlite_url(self.database_url)
        return self

    def admin_logins(self) -> set[str]:
        return {x.strip().lower() for x in self.form_admin_logins.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # На случай sqlite по относительному пути — каталог должен существовать
    # ДО того, как SQLAlchemy попытается создать файл БД.
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.replace("sqlite:///", "", 1))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
