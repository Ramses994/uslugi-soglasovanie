"""Генерация xlsx-шаблона массовой загрузки заявок."""
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation


COLUMN_SPECS = [
    ("request_type", "Тип заявки: new_service / complex_service / delete / change / promo"),
    ("name", "Наименование услуги"),
    ("mis_code", "Код в МИС (обязателен для delete и change)"),
    ("service_type", "Тип услуги: simple / complex / combined / program / package / prn"),
    ("price_undefined", "Цена не определена: да / нет"),
    ("all_branches", "Одинаковая цена во всех филиалах: да / нет"),
    ("business_description", "Деловое обоснование (суть услуги)"),
    ("simple_justification", "Обоснование для delete/change"),
    ("change_description", "Что меняется (только change)"),
    ("affected_branches", "Филиалы через ; (для delete/change)"),
    ("included_services", "Состав комплекса: Название|код; Название|код"),
    ("description", "Описание"),
    ("sex_limit", "all / male / female"),
    ("age_from", "Возраст от"),
    ("age_to", "Возраст до"),
    ("is_lab_service", "Лабораторная услуга: да / нет"),
    ("erlu_code", "Код ЕРЛУ"),
    ("erlu_name", "Наименование ЕРЛУ"),
    ("direction_name", "Направление исследования"),
    ("biomaterial_type", "Тип биоматериала"),
]


def _yes_no_dv(formula: str, sqref: str) -> DataValidation:
    dv = DataValidation(type="list", formula1=formula, allow_blank=True)
    dv.sqref = sqref
    return dv


def build_template_workbook(branch_names: list[str]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    headers = [c[0] for c in COLUMN_SPECS] + [f"price_{name}" for name in branch_names]
    hints = [c[1] for c in COLUMN_SPECS] + [f"Цена, ₽ — {name}" for name in branch_names]
    header_fill = PatternFill("solid", fgColor="0F3D73")
    header_font = Font(color="FFFFFF", bold=True)
    hint_font = Font(italic=True, color="666666", size=9)
    for col, (h, hint) in enumerate(zip(headers, hints), start=1):
        cell = ws.cell(1, col, h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(wrap_text=True)
        hint_cell = ws.cell(2, col, hint)
        hint_cell.font = hint_font
        ws.column_dimensions[get_column_letter(col)].width = max(18, min(36, len(h) + 4))
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    last = 500
    ws.add_data_validation(_yes_no_dv(
        '"new_service,complex_service,delete,change,promo"', f"A3:A{last}"
    ))
    ws.add_data_validation(_yes_no_dv(
        '"simple,complex,combined,program,package,prn"', f"D3:D{last}"
    ))
    ws.add_data_validation(_yes_no_dv('"да,нет"', f"E3:E{last}"))
    ws.add_data_validation(_yes_no_dv('"да,нет"', f"F3:F{last}"))
    ws.add_data_validation(_yes_no_dv('"all,male,female"', f"M3:M{last}"))
    ws.add_data_validation(_yes_no_dv('"да,нет"', f"P3:P{last}"))

    instr = wb.create_sheet("Инструкция")
    instr["A1"] = "Как заполнять шаблон"
    instr["A1"].font = Font(bold=True, size=14)
    lines = [
        "Строка 1 — технические имена колонок, строка 2 — подсказки. Данные начинаются со строки 3.",
        "Колонку согласующих в файле нет: список выбирается один раз на экране предпросмотра и применяется ко всей пачке.",
        "Фоломова и Соколовская добавляются в цепочку каждой заявки автоматически.",
        "Для новой/комплексной/акции нужны наименование и обоснование; цены — либо колонки price_*, либо «да» в price_undefined.",
        "Для delete/change: mis_code, affected_branches, simple_justification; для change ещё change_description.",
        "Состав комплекса: «Консультация терапевта|01.01.001; Анализ крови|».",
        "Файл только заполняет предпросмотр. Заявки создаются кнопкой «Отправить выбранные».",
    ]
    for i, line in enumerate(lines, start=3):
        instr[f"A{i}"] = line
        instr.column_dimensions["A"].width = 120

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
