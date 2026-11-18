"""Генерация файлов импорта в МИС Медиалог (два отдельных .xls).

SERV_FIELDS.xls — карточка услуги (IMP_SERV_FIELDS).
Price_{N}_Branches.xls — цены по филиалам (категории FM_PRICE_CATEGORY).
"""
from __future__ import annotations

import io
import json
from datetime import date, datetime
from pathlib import Path

import xlwt
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

# Порядок заголовков — эталон Downloads/SERV_FIELDS.xls (IMP_SERV_FIELDS, Cystatin C / 11635).
SERV_FIELDS_COLUMNS = [
    "CODE", "INSURANCE_TYPE", "LABEL", "SHORT_LABEL", "DESCRIPTION",
    "SERVGRP_CODE", "TAXE_CODE", "DEP_CODE", "STATE", "SEX", "SERV_TYPE", "SERV_EXT_TYPE",
    "PERIODICAL", "PRICE_FOR_CHANGE", "PRICE_TYPE", "INVOICE_TYPE", "ORG_CODE", "OMS_CODE",
    "MINZDRAV_CODE", "CODE_AN", "CODE_AN2", "CODE_AN3", "NAME_ALTER",
    "EXAM_GR_CODE", "EXAM_CODE", "EXAM_NAME", "EXAM_DUREE", "MODELS_CODE",
]
SERV_FIELDS_SHEET = "Импорт_услуги"
PRICE_BRANCHES_SHEET = "Import"
# Совместимость со старыми тестами / двухлистовым xlsx.
SERV_COLUMNS = [c for c in SERV_FIELDS_COLUMNS if c not in ("NAME_ALTER", "CODE_AN", "CODE_AN2", "CODE_AN3")]
PRICE_BRANCHES_COLUMNS = ["FM_SERV_CODE", "PRICE", "FM_PRICE_CATEGORY_CODE"]
PRICE_COLUMNS = ["FM_SERV_CODE", "PRICE", "DATE_FROM"]  # legacy двухлистовый xlsx

# Аналитические коды из рабочей выгрузки 11635 (в форме полей пока нет).
DEFAULT_CODE_AN = "web"
DEFAULT_CODE_AN2 = "web"
DEFAULT_CODE_AN3 = "Анализы"

TEXT_COLUMNS = {
    "CODE", "INSURANCE_TYPE", "LABEL", "SHORT_LABEL", "NAME_ALTER", "DESCRIPTION",
    "SERVGRP_CODE", "TAXE_CODE", "DEP_CODE", "STATE", "SEX", "SERV_TYPE", "SERV_EXT_TYPE",
    "PRICE_TYPE", "INVOICE_TYPE", "ORG_CODE", "OMS_CODE",
    "MINZDRAV_CODE", "CODE_AN", "CODE_AN2", "CODE_AN3",
    "EXAM_GR_CODE", "EXAM_CODE", "EXAM_NAME", "EXAM_DUREE", "MODELS_CODE",
    "FM_SERV_CODE", "FM_PRICE_CATEGORY_CODE",
}
# PERIODICAL / PRICE_FOR_CHANGE в эталоне — число 0, не текст.


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


def _load_branches() -> list[dict]:
    settings = get_settings()
    path = Path(settings.branches_json)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _load_org_code_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for item in _load_branches():
        name = str((item or {}).get("name") or "").strip()
        if not name:
            continue
        code = str((item or {}).get("mis_org_code") or name).strip() or name
        out[name] = code
        out[name.lower()] = code
    return out


def _load_price_category_map() -> dict[str, str]:
    """CODE в FM_PRICE_CATEGORY = читаемое имя филиала (не FM_PRICE_CATEGORY_ID)."""
    out: dict[str, str] = {}
    for item in _load_branches():
        name = str((item or {}).get("name") or "").strip()
        if not name:
            continue
        # Явный CODE, иначе mis_org_code / name — в этой инсталляции совпадают с LABEL.
        code = (
            str((item or {}).get("mis_price_category_code") or "").strip()
            or str((item or {}).get("mis_org_code") or "").strip()
            or name
        )
        out[name] = code
        out[name.lower()] = code
    return out


def org_code_for(branch_name: str, mapping: dict[str, str] | None = None) -> str:
    name = (branch_name or "").strip()
    mapping = mapping if mapping is not None else _load_org_code_map()
    return mapping.get(name) or mapping.get(name.lower()) or name


def price_category_for(branch_name: str, mapping: dict[str, str] | None = None) -> str:
    name = (branch_name or "").strip()
    mapping = mapping if mapping is not None else _load_price_category_map()
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


def _xls_text_style():
    return xlwt.easyxf(num_format_str="@")


def _write_xls_sheet(headers: list[str], rows: list[list], sheet_name: str = "Sheet1") -> bytes:
    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet(sheet_name[:31] or "Sheet1")
    text_style = _xls_text_style()
    for c, header in enumerate(headers):
        ws.write(0, c, header, text_style)
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row):
            header = headers[c] if c < len(headers) else ""
            if header in TEXT_COLUMNS or value == "" or value is None:
                ws.write(r, c, _as_text(value), text_style)
            else:
                ws.write(r, c, value)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_serv_fields_row(req: ServiceRequestIn, code: str) -> list:
    org_map = _load_org_code_map()
    first_branch = req.prices[0].branch if req.prices else ""
    ins = insurance_types_for(req)[0]
    name_alter = (req.alt_name or "").strip() or (req.name or "").strip()
    return [
        _as_text(code),
        ins,
        req.name,
        make_short_label(req.name, req.short_name),
        description_from_request(req),
        _as_text(req.service_group or ""),
        DEFAULT_TAXE_CODE,
        _as_text((req.department_code or "").strip() or DEFAULT_DEP_CODE),
        "A",
        _SEX_MAP[req.sex_limit.value],
        "S",
        serv_ext_type(req),
        0,  # PERIODICAL — число, как в эталоне SERV_FIELDS.xls
        0,  # PRICE_FOR_CHANGE
        "S",
        "S",
        org_code_for(first_branch, org_map) if first_branch else "",
        "",
        cyrillic_minzdrav_code(req.minzdrav.code if req.minzdrav else None),
        DEFAULT_CODE_AN,
        DEFAULT_CODE_AN2,
        DEFAULT_CODE_AN3,
        name_alter,
        "",  # EXAM_* / MODELS — пустые текстовые
        "",
        "",
        "",
        "",
    ]


def build_serv_fields_xls(req: ServiceRequestIn, generated_code_hint: str = "") -> bytes:
    """Одна строка на услугу заявки → SERV_FIELDS.xls."""
    code = (req.mis_code or "").strip() or (generated_code_hint or "").strip()
    return _write_xls_sheet(
        SERV_FIELDS_COLUMNS,
        [build_serv_fields_row(req, code)],
        SERV_FIELDS_SHEET,
    )


def build_price_branch_rows(req: ServiceRequestIn, code: str) -> list[list]:
    cat_map = _load_price_category_map()
    rows = []
    for bp in req.prices:
        cat = price_category_for(bp.branch, cat_map)
        rows.append([_as_text(code), bp.price, _as_text(cat)])
    return rows


def price_branches_filename(branch_count: int) -> str:
    return f"Price_{int(branch_count)}_Branches.xls"


def build_price_branches_xls(req: ServiceRequestIn, generated_code_hint: str = "") -> tuple[bytes, str]:
    """Цены по филиалам → Price_{N}_Branches.xls (CODE категории = имя филиала)."""
    code = (req.mis_code or "").strip() or (generated_code_hint or "").strip()
    rows = build_price_branch_rows(req, code)
    payload = _write_xls_sheet(PRICE_BRANCHES_COLUMNS, rows, PRICE_BRANCHES_SHEET)
    return payload, price_branches_filename(len(rows))


def build_import_workbook(
    req: ServiceRequestIn,
    generated_code_hint: str = "",
    *,
    date_from: date | datetime | None = None,
) -> bytes:
    """Legacy: один xlsx с двумя листами (для старых тестов/совместимости)."""
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
    mapping = _load_org_code_map()

    wb = Workbook()
    ws = wb.active
    ws.title = "Импорт услуг"
    ws.append(SERV_COLUMNS)
    exam_idx = [SERV_COLUMNS.index(c) + 1 for c in SERV_COLUMNS if c.startswith("EXAM") or c == "MODELS_CODE"]
    for bp in req.prices:
        org = org_code_for(bp.branch, mapping)
        for ins in ins_types:
            ws.append([
                _as_text(code), ins, req.name, short, description, grp, taxe, dep,
                "A", _SEX_MAP[req.sex_limit.value], "S", ext, "0", "0", "S", "S",
                org, "", minz, "", "", "", "", "",
            ])
            r = ws.max_row
            ws.cell(r, 1).number_format = numbers.FORMAT_TEXT
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
