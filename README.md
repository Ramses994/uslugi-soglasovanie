# Форма согласования услуг — МИС «Медиалог»

Веб-сервис для подачи и согласования заявок на добавление услуг/продуктов
в прейскурант МИС «Медиалог» (8.10.04.R). Полное ТЗ и архитектурное
описание — см. **ТЗ.md** в корне проекта.

## Быстрый старт (dev, без реального AD)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # LDAP_MOCK=true уже выставлен по умолчанию
# при необходимости поправьте .env (SMTP, справочники, список согласующих)

cd backend
uvicorn app.main:app --reload --port 8000
```

Откройте http://localhost:8000 — фронтенд раздаётся тем же приложением.
Интернет для работы формы не нужен: Tailwind и Alpine.js — локальные файлы
в `frontend/vendor/` (см. `frontend/vendor/README.md`), а не CDN — на CDN
форма была бы полностью пустой белой страницей в сети без выхода наружу
(типичная ситуация для внутренней сети клиники), это воспроизведено и
описано в разделе 12.7 ТЗ.md.
Логин: любой (например `ivanov` / любой пароль) — в mock-режиме LDAP
любые креды принимаются, профиль строится автоматически.

Готовые тестовые пользователи с предзаполненным профилем: `ivanov`, `petrova`
(см. `backend/app/auth/ldap_auth.py`, `_MOCK_USERS`).

## Подключение к боевому AD

1. В `.env` выставить `LDAP_MOCK=false`.
2. Заполнить `LDAP_SERVER`, `LDAP_DOMAIN`, `LDAP_BASE_DN`, `LDAP_USER_SEARCH_BASE`.
3. При необходимости — сервисную read-only учётку `LDAP_SERVICE_USER` /
   `LDAP_SERVICE_PASSWORD` (нужна, только если обычным пользователям запрещено
   читать свои же атрибуты — редкий случай, но встречается при жёстких GPO).

## Деплой на Linux-сервере (Docker)

```bash
cp .env.example .env      # заполнить боевые LDAP_*/SMTP_*, выставить LDAP_MOCK=false
docker compose up -d --build
curl http://localhost/api/health   # {"status":"ok"}
```

`data/` монтируется с хоста (`docker-compose.yml`) — справочники, `approvers.json`,
`branches.json` и `requests.db` переживают пересборку образа. По умолчанию сервис
слушает 80 порт хоста; если он занят — см. раздел 13.1 ТЗ.md.

## SMTP

По умолчанию используется сервисный ящик + `Reply-To` на инициатора
(`SMTP_SEND_AS_USER=false`). Заполните `SMTP_HOST/PORT/USER/PASSWORD/FROM`.

## Справочники

Файлы уже лежат в `data/`:
- `data/ERLU-KDL_13.10.25.xls` — справочник лабораторных исследований (ЕРЛУ-КДЛ);
- `data/minzdrav_nsi_1.2.643.5.1.13.13.11.1070.xlsx` — номенклатура медуслуг Минздрава (НСИ).

Обновление: замените файл по тому же пути и вызовите
`POST /api/dictionaries/reload` (авторизованным пользователем) — перезапуск
сервиса не требуется.

**Если в вашей копии репозитория этих файлов нет** (например, `.xls`/`.xlsx`
отфильтровались `.gitignore`'ом при коммите, или скопирована только часть
архива) — автокомплит ЕРЛУ и Минздрава работать не будет, а лог при старте
покажет 0 загруженных записей. Проверить: `ls -la data/*.xls data/*.xlsx`
и сверить размер с `README`/поставкой (ЕРЛУ — ~670 КБ, Минздрав — ~460 КБ).
Файлы бинарные, есть смысл проверить `.gitignore` репозитория на правила
вида `*.xls*`.

## Согласующие и филиалы

- `data/approvers.json` — список согласующих (имя, e-mail, роль).
- `data/branches.json` — список филиалов; создаётся автоматически при первом
  запуске из `config.Settings.branches_seed`, если файла ещё нет.
- Оба редактируются через UI (вкладка «⚙ Администрирование», видна только
  пользователям из `FORM_ADMIN_LOGINS`) или напрямую через `/api/admin/approvers`
  и `/api/admin/branches` (см. раздел 10, 12.6 ТЗ.md). В проде стоит заменить
  проверку по списку логинов на членство в AD-группе — см. `config.py`.

## Роль «Администратор формы»

Задаётся через `.env`: `FORM_ADMIN_LOGINS=ivanov,petrova` (логины через
запятую, регистр не важен). Пользователь с таким логином видит вкладку
«⚙ Администрирование» и может добавлять/удалять согласующих и филиалы —
изменения сразу видны в форме «Новая заявка» без перезапуска сервиса.

## Тесты

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

14 тестов в `tests/test_flow.py` (изолированная SQLite на каждый тест,
почта подменяется фейками — см. `tests/conftest.py`), покрывают ровно то,
что описано в разделе 12 ТЗ.md: загрузку справочников из реальных файлов,
логин, полный цикл заявка→согласование→статус→файл импорта, идемпотентность
повторного клика по ссылке, отклонение заявки, валидацию обязательных полей.

## Структура проекта

```
.
├── ТЗ.md
├── README.md
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── data/
│   ├── ERLU-KDL_13.10.25.xls
│   ├── minzdrav_nsi_1.2.643.5.1.13.13.11.1070.xlsx
│   ├── forma_zayavka_obrazec.xlsx      # исходная форма-заявка (для сверки полей)
│   └── approvers.json
├── tests/
│   ├── conftest.py
│   └── test_flow.py
├── backend/
│   └── app/
│       ├── main.py
│       ├── config.py
│       ├── database.py
│       ├── models.py
│       ├── schemas.py
│       ├── security.py
│       ├── auth/ldap_auth.py
│       ├── services/
│       │   ├── dictionaries.py
│       │   ├── email_service.py
│       │   └── medialog_export.py
│       ├── routers/
│       │   ├── auth.py
│       │   ├── dictionaries.py
│       │   ├── requests.py
│       │   ├── approvals.py
│       │   └── admin.py
│       └── templates/
│           ├── email_request.html
│           └── email_decision.html
└── frontend/
    ├── index.html
    ├── app.js
    └── vendor/            # офлайн-копии Tailwind/Alpine.js — не CDN, см. vendor/README.md
        ├── tailwind.css
        ├── alpine.min.js
        └── README.md
```
