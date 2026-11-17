"""Генерация xlsx под «Услуги → Импорт → Импорт услуг» в МИС Медиалог.

Эталон колонок — успешно загруженный файл `11000_услуга_импорт_текстовые_коды.xls`
(1 обработана / 1 создана). Цены в FM_SERV нет: второй лист — заготовка
«Импорт прейскурантов» (FM_SERVPRICE), порядок импорта указан в письме.
"""
from __future__ import annotations

import io
import json
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import numbers
from sqlalchemy.orm import Session

from ..config import get_settings
from ..schemas import ServiceRequestIn

_SEX_MAP = {"all": "A", "male": "M", "female": "F"}
_SERV_EXT_TYPE_MAP = {
    "simple": "S",
    "complex": "D",
    "combined": "P",
    "program": "C",
    "package": "X",
    "prn": "S",
}
_PRICE_LIST_MAP = {"dms": "D", "oms": "O", "both": "D"}

DEFAULT_TAXE_CODE = "НДС-0"
DEFAULT_DEP_CODE = "000"
SHORT_LABEL_MAX = 100
DESCRIPTION_MAX = 2000
GENERATED_CODE_START = 11603

# 24 колонки успешного ручного импорта (без DEVISE_CODE и без служебных _ЦЕНА/_ФИЛИАЛ).
SERV_COLUMNS = [
    "CODE", "INSURANCE_TYPE", "LABEL", "SHORT_LABEL", "DESCRIPTION", "SERVGRP_CODE",
    "TAXE_CODE", "DEP_CODE", "STATE", "SEX", "SERV_TYPE", "SERV_EXT_TYPE", "PERIODICAL",
    "PRICE_FOR_CHANGE", "PRICE_TYPE", "INVOICE_TYPE", "ORG_CODE", "OMS_CODE",
    "MINZDRAV_CODE", "EXAM_GR_CODE", "EXAM_CODE", "EXAM_NAME", "EXAM_DUREE", "MODELS_CODE",
]
PRICE_COLUMNS = ["FM_SERV_CODE", "PRICE", "DATE_FROM"]


def make_short_label(label: str, short_name: str | None = None) -> str:
    if short_name and str(short_name).strip():
        return str(short_name).strip()[:SHORT_LABEL_MAX]
    text = " ".join((label or "").split())
    if len(text) <= SHORT_LABEL_MAX:
        return text
    cut = text[:SHORT_LABEL_MAX]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip()
    return cut


def description_from_request(req: ServiceRequestIn) -> str:
    parts = [
        (req.description or "").strip(),
        (req.simple_justification or "").strip(),
        ((req.business.description if req.business else None) or "").strip(),
    ]
    text = next((p for p in parts if p), "")
    return text[:DESCRIPTION_MAX]


def cyrillic_minzdrav_code(code: str | None) -> str:
    raw = (code or "").strip()
    if not raw:
        return ""
    if raw[0] in "Aa":
        return "А" + raw[1:]
    return raw


def insurance_types_for(req: ServiceRequestIn) -> list[str]:
    if req.price_list.value == "both":
        return ["D", "O"]
    return [_PRICE_LIST_MAP[req.price_list.value]]


def _load_org_code_map() -> dict[str, str]:
    settings = get_settings()
    path = Path(settings.branches_json)
    if not path.exists():
        return {}
    items = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for item in items:
        name = str((item or {}).get("name") or "").strip()
        if not name:
            continue
        code = str((item or {}).get("mis_org_code") or name).strip() or name
        out[name] = code
        out[name.lower()] = code
    return out


def org_code_for(branch_name: str, mapping: dict[str, str] | None = None) -> str:
    name = (branch_name or "").strip()
    mapping = mapping if mapping is not None else _load_org_code_map()
    return mapping.get(name) or mapping.get(name.lower()) or name


def serv_ext_type(req: ServiceRequestIn) -> str:
    return _SERV_EXT_TYPE_MAP.get(req.service_type.value, "S")


def allocate_generated_code(db: Session) -> str:
    from ..models import MisCodeCounter

    row = db.get(MisCodeCounter, 1)
    if row is None:
        row = MisCodeCounter(id=1, next_value=GENERATED_CODE_START)
        db.add(row)
        db.flush()
    code = str(row.next_value)
    row.next_value = int(row.next_value) + 1
    db.add(row)
    return code


def resolve_service_code(db: Session | None, entity, req: ServiceRequestIn) -> str:
    if req.mis_code:
        return str(req.mis_code).strip()
    stored = getattr(entity, "generated_mis_code", None) if entity is not None else None
    if stored:
        return str(stored).strip()
    if db is None or entity is None:
        return ""
    code = allocate_generated_code(db)
    entity.generated_mis_code = code
    db.add(entity)
    return code


def _as_text(value) -> str:
    if value is None:
        return ""
    return str(value)


def build_import_workbook(
    req: ServiceRequestIn,
    generated_code_hint: str = "",
    *,
    date_from: date | datetime | None = None,
) -> bytes:
    mapping = _load_org_code_map()
    code = (req.mis_code or "").strip() or (generated_code_hint or "").strip()
    short = make_short_label(req.name, req.short_name)
    description = description_from_request(req)
    taxe = DEFAULT_TAXE_CODE
    dep = (req.department_code or "").strip() or DEFAULT_DEP_CODE
    grp = (req.service_group or "").strip()
    minz = cyrillic_minzdrav_code(req.minzdrav.code if req.minzdrav else None)
    ins_types = insurance_types_for(req)
    ext = serv_ext_type(req)
    when = date_from.date() if isinstance(date_from, datetime) else (date_from or date.today())
    date_str = when.isoformat()

    wb = Workbook()
    ws = wb.active
    ws.title = "Импорт услуг"
    ws.append(SERV_COLUMNS)

    exam_idx = [SERV_COLUMNS.index(c) + 1 for c in SERV_COLUMNS if c.startswith("EXAM") or c == "MODELS_CODE"]
    code_col = 1

    for bp in req.prices:
        org = org_code_for(bp.branch, mapping)
        for ins in ins_types:
            row = [
                _as_text(code),
                ins,
                req.name,
                short,
                description,
                grp,
                taxe,
                dep,
                "A",
                _SEX_MAP[req.sex_limit.value],
                "S",
                ext,
                "0",
                "0",
                "S",
                "S",
                org,
                "",
                minz,
                "",
                "",
                "",
                "",
                "",
            ]
            ws.append(row)
            r = ws.max_row
            ws.cell(r, code_col).number_format = numbers.FORMAT_TEXT
            for col in exam_idx:
                cell = ws.cell(r, col)
                cell.value = ""
                cell.number_format = numbers.FORMAT_TEXT

    prices_ws = wb.create_sheet("Импорт прейскурантов")
    prices_ws.append(PRICE_COLUMNS)
    for bp in req.prices:
        prices_ws.append([_as_text(code), bp.price, date_str])
        prices_ws.cell(prices_ws.max_row, 1).number_format = numbers.FORMAT_TEXT

    for sheet in (ws, prices_ws):
        for col in sheet.columns:
            max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
            sheet.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
