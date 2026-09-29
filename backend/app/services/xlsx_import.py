"""Разбор загруженного xlsx в сырые словари для ServiceRequestIn (без записи в БД)."""
from io import BytesIO
from typing import Any

from openpyxl import load_workbook

from .xlsx_template import COLUMN_SPECS


def _cell(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _as_bool(v) -> bool:
    s = _cell(v).lower()
    return s in ("да", "yes", "true", "1")


def _as_float(v) -> float | None:
    if v is None or _cell(v) == "":
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def _split_list(v: str) -> list[str]:
    if not v:
        return []
    parts = [p.strip() for p in v.replace(",", ";").split(";")]
    return [p for p in parts if p]


def parse_xlsx_requests(content: bytes) -> list[dict[str, Any]]:
    wb = load_workbook(BytesIO(content), data_only=True)
    ws = wb[wb.sheetnames[0]]
    header_row = [(_cell(c.value) or "") for c in next(ws.iter_rows(min_row=1, max_row=1))]
    known = {c[0] for c in COLUMN_SPECS}

    rows = []
    for row in ws.iter_rows(min_row=3, values_only=False):
        values = {}
        empty = True
        for idx, cell in enumerate(row):
            if idx >= len(header_row):
                break
            key = header_row[idx]
            if not key:
                continue
            val = cell.value
            if val is not None and _cell(val) != "":
                empty = False
            values[key] = val
        if empty:
            continue
        rows.append(_row_to_payload(values, known))
    return rows


def _row_to_payload(values: dict, known: set[str]) -> dict:
    get = lambda k: values.get(k)
    request_type = _cell(get("request_type")) or "new_service"
    prices = []
    for key, val in values.items():
        if key.startswith("price_") and key not in known:
            branch = key[len("price_"):]
            price = _as_float(val)
            if price:
                prices.append({"branch": branch, "price": price})
    included = []
    for chunk in _split_list(_cell(get("included_services"))):
        if "|" in chunk:
            name, code = chunk.split("|", 1)
        else:
            name, code = chunk, ""
        if name.strip():
            included.append({"name": name.strip(), "code": code.strip() or None})

    lab = None
    is_lab = _as_bool(get("is_lab_service"))
    if is_lab:
        lab = {
            "erlu_code": _cell(get("erlu_code")) or "—",
            "erlu_name": _cell(get("erlu_name")) or _cell(get("name")),
            "lab_code": None,
            "lab_name": None,
            "direction_name": _cell(get("direction_name")) or "—",
            "biomaterial_type": _cell(get("biomaterial_type")) or "Другое",
            "is_composite": False,
        }

    payload = {
        "request_type": request_type,
        "name": _cell(get("name")),
        "mis_code": _cell(get("mis_code")) or None,
        "service_type": _cell(get("service_type")) or "simple",
        "price_undefined": _as_bool(get("price_undefined")),
        "all_branches": _as_bool(get("all_branches")),
        "prices": prices,
        "business": {"description": _cell(get("business_description")) or None},
        "simple_justification": _cell(get("simple_justification")) or None,
        "change_description": _cell(get("change_description")) or None,
        "affected_branches": _split_list(_cell(get("affected_branches"))),
        "included_services": included,
        "description": _cell(get("description")) or None,
        "sex_limit": _cell(get("sex_limit")) or "all",
        "age_from": _as_float(get("age_from")),
        "age_to": _as_float(get("age_to")),
        "is_lab_service": is_lab,
        "lab": lab,
        "approver_ids": [],
    }
    return payload
