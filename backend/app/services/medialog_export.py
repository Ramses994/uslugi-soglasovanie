"""
Генерация файла в формате, который МИС Медиалог понимает через
'Услуги / Импорт прейскуранта услуг' (см. документацию Медиалог, раздел
"Обязательные параметры" / "Дополнительные параметры" для импорта услуг).

Это ключевая экономия времени администратора МИС: вместо переноса заявки
из письма в карточку услуги вручную, после того как заявка согласована
всеми участниками, администратор скачивает готовый .xlsx с колонками
в точности как ожидает импортёр Медиалог и подгружает его через штатный
пункт меню — без ручного набора полей.

Колонки (обязательные + дополнительные), как в документации Медиалог:
  CODE, INSURANCE_TYPE, LABEL, SHORT_LABEL, DESCRIPTION, SERVGRP_CODE,
  TAXE_CODE, DEP_CODE, STATE, SEX, SERV_TYPE, SERV_EXT_TYPE, PERIODICAL,
  PRICE_FOR_CHANGE, PRICE_TYPE, INVOICE_TYPE,
  ORG_CODE, DEVISE_CODE, OMS_CODE, MINZDRAV_CODE
Плюс по одной строке на филиал/цену (ORG_CODE меняется), т.к. импортёр
Медиалог сопоставляет цену с конкретным филиалом через ORG_CODE.
"""
import io
from openpyxl import Workbook

from ..schemas import ServiceRequestIn

_SEX_MAP = {"all": "A", "male": "M", "female": "F"}
_SERV_EXT_TYPE_MAP = {
    "simple": "S",
    "complex": "D",
    "combined": "P",
    "program": "C",
    "package": "X",
    "prn": "S",  # ПРН в Медиалог технически оформляется как простая услуга + тип направления
}
_PRICE_LIST_MAP = {"dms": "D", "oms": "O", "both": "D"}  # при 'both' создаём отдельно строку под ОМС ниже

COLUMNS = [
    "CODE", "INSURANCE_TYPE", "LABEL", "SHORT_LABEL", "DESCRIPTION", "SERVGRP_CODE",
    "TAXE_CODE", "DEP_CODE", "STATE", "SEX", "SERV_TYPE", "SERV_EXT_TYPE", "PERIODICAL",
    "PRICE_FOR_CHANGE", "PRICE_TYPE", "INVOICE_TYPE",
    "ORG_CODE", "DEVISE_CODE", "OMS_CODE", "MINZDRAV_CODE",
    # служебные колонки для администратора (не читаются импортёром, но полезны для проверки)
    "_ЦЕНА_РУБ", "_ФИЛИАЛ",
]


def build_import_workbook(req: ServiceRequestIn, generated_code_hint: str = "") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Импорт услуг"
    ws.append(COLUMNS)

    insurance_types = ["D", "O"] if req.price_list.value == "both" else [_PRICE_LIST_MAP[req.price_list.value]]
    # Если заявитель/админ указал код МИС — он идёт в CODE; иначе подсказка генератора
    # (часто пустая: код присваивает администратор МИС при импорте).
    code = (req.mis_code or "").strip() or generated_code_hint

    for bp in req.prices:
        for ins in insurance_types:
            ws.append([
                code,                                      # CODE — код услуги в МИС, если указан; иначе подсказка/пусто
                ins,                                        # INSURANCE_TYPE
                req.name,                                   # LABEL
                req.short_name or "",                        # SHORT_LABEL
                req.description or "",                       # DESCRIPTION
                req.service_group or "",                      # SERVGRP_CODE
                "",                                          # TAXE_CODE — заполняется администратором по учётной политике
                req.department_code or "",                    # DEP_CODE
                "A",                                         # STATE — активная
                _SEX_MAP[req.sex_limit.value],                # SEX
                "S",                                         # SERV_TYPE — услуга (не товар/аванс/скидка)
                _SERV_EXT_TYPE_MAP[req.service_type.value],   # SERV_EXT_TYPE
                "1" if False else "0",                        # PERIODICAL — по умолчанию нет, правится вручную при необходимости
                "0",                                          # PRICE_FOR_CHANGE
                "S",                                          # PRICE_TYPE — по прейскуранту
                "S",                                          # INVOICE_TYPE — по услуге
                bp.branch,                                    # ORG_CODE (передаём название филиала; администратор сопоставит с кодом)
                "",                                          # DEVISE_CODE
                "",                                          # OMS_CODE
                req.minzdrav.code if req.minzdrav else "",     # MINZDRAV_CODE
                bp.price,                                     # _ЦЕНА_РУБ
                bp.branch,                                     # _ФИЛИАЛ
            ])

    for col in ws.columns:
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
