"""
Pydantic-схемы. Структура ServiceRequestIn сознательно повторяет карточку
"Услуга [новая запись]" МИС Медиалог (см. скриншот в ТЗ) + деловое обоснование
из формы "Форма заявка на изменения прейскуранта.xlsx" (лист "Внесение новой услуги"),
которую заявители заполняли раньше вручную.

Поля сгруппированы так же, как в чек-листе раздела 3 ТЗ.
"""
import re
from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Внутрикорпоративные адреса нередко живут в зоне .local/.internal, которую
# pydantic.EmailStr (через email_validator, check_deliverability=True по умолчанию)
# считает «special-use» и отклоняет, хотя для внутренней AD-инфраструктуры это
# совершенно нормальный, реально работающий адрес. Поэтому используем свой
# лёгкий формат-валидатор вместо EmailStr.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_format(value: str) -> str:
    if not _EMAIL_RE.match(value):
        raise ValueError("Некорректный формат email")
    return value


class ServiceType(str, Enum):
    simple = "simple"          # Простая услуга (SERV_EXT_TYPE=S)
    complex = "complex"        # Сложная услуга (D)
    combined = "combined"      # Комплексная услуга (P)
    program = "program"        # Медпрограмма (C)
    package = "package"        # Пакет услуг (X)
    prn = "prn"                # ПРН (тип направления / профильная услуга)


class Sex(str, Enum):
    all = "all"        # A
    male = "male"       # M
    female = "female"     # F


class PriceListType(str, Enum):
    dms = "dms"   # ДМС
    oms = "oms"   # ОМС
    both = "both"


class BranchPrice(BaseModel):
    branch: str
    price: float = Field(gt=0, description="Цена в рублях")


class LabDetails(BaseModel):
    """Обязательный блок для типа 'Лабораторные исследования / Анализы'."""
    erlu_code: str = Field(..., description="Код ЕРЛУ (справочник ЕРЛУ-КДЛ, колонка 'код ЕРЛУ')")
    erlu_name: str = Field(..., description="Точное наименование анализа как в ЕРЛУ/ЛИС")
    lab_code: Optional[str] = Field(None, description="Код лаборатории (колонка 'код лаборатории')")
    lab_name: Optional[str] = Field(None, description="Наименование исследования в лаборатории")
    direction_name: str = Field(..., description="Наименование направления исследования (тип направления в Медиалог)")
    biomaterial_type: str = Field(..., description="Тип биоматериала (кровь венозная, моча, мазок и т.п.)")
    is_composite: bool = Field(False, description="Составное исследование (колонка 'Составное исследование' = Да)")


class MinzdravMatch(BaseModel):
    code: str
    name: str
    match_score: Optional[float] = None
    manual_override: bool = False


class BusinessJustification(BaseModel):
    """Деловое обоснование — перенесено из старой формы Excel
    (лист 'Внесение новой услуги'), чтобы согласующие видели не только
    технические параметры МИС, но и бизнес-контекст."""
    service_kind: Optional[str] = Field(None, description="Вид услуги: консультация/манипуляция/операция/исследование")
    duration_minutes: Optional[int] = Field(None, ge=0)
    staff_involved: Optional[str] = Field(None, description="Персонал, задействованный для оказания услуги (должности)")
    equipment: Optional[str] = None
    consumables: Optional[str] = None
    medications: Optional[str] = None
    description: Optional[str] = Field(None, description="Суть услуги, позиционирование, продвижение")
    comment: Optional[str] = None


class ServiceRequestIn(BaseModel):
    # --- служебные / деловые ---
    request_type: str = Field("new_service", description="new_service | complex_service | delete | promo")
    business: Optional[BusinessJustification] = None

    # --- обязательные поля карточки услуги МИС ---
    name: str = Field(..., min_length=3, max_length=255, description="Наименование (как в МИС)")
    mis_code: Optional[str] = Field(None, max_length=64, description="Код услуги в МИС Медиалог (поле CODE при импорте)")
    service_type: ServiceType
    price_list: PriceListType = PriceListType.both
    prices: list[BranchPrice] = Field(..., min_length=1)
    all_branches: bool = False

    # --- опциональные поля карточки услуги ---
    short_name: Optional[str] = Field(None, max_length=100)
    alt_name: Optional[str] = None
    description: Optional[str] = None
    treatment_standard: Optional[str] = None
    date_start: Optional[date] = None
    date_end: Optional[date] = None

    # Фронтенд — обычный <input type="date"> на Alpine x-model без модификатора:
    # когда поле не заполнено, в форму приходит "" (пустая строка), а не null/
    # отсутствие ключа. Pydantic v2 НЕ приводит "" к None для типа date (в отличие
    # от x-model.number, который сам подставляет null) — "" валится как "input is
    # too short", хотя даты явно необязательны (см. ТЗ.md, раздел 12.9/12.10:
    # баг воспроизведён на боевом сервере 192.168.2.104 при попытке отправить
    # заявку без дат). Приводим "" -> None здесь же, на бэкенде, а не только на
    # фронтенде — так поле остаётся необязательным и при прямых вызовах API.
    @field_validator("date_start", "date_end", mode="before")
    @classmethod
    def _blank_date_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("mis_code", mode="before")
    @classmethod
    def _blank_mis_code_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v.strip() if isinstance(v, str) else v
    sex_limit: Sex = Sex.all
    age_from: Optional[float] = Field(None, ge=0)
    age_to: Optional[float] = Field(None, ge=0)
    repeat_control_per_day: bool = False
    service_group: Optional[str] = Field(None, description="Группа услуг (SERVGRP_CODE)")
    department_code: Optional[str] = Field(None, description="Отделение (DEP_CODE)")

    # --- лаборатория (условно-обязательный блок) ---
    is_lab_service: bool = False
    lab: Optional[LabDetails] = None

    # --- сопоставление с номенклатурой Минздрава ---
    minzdrav: Optional[MinzdravMatch] = None

    # --- маршрутизация согласования ---
    approver_ids: list[str] = Field(..., min_length=1)

    @model_validator(mode="after")
    def check_lab_block(self):
        if self.is_lab_service and self.lab is None:
            raise ValueError("Для лабораторной услуги обязателен блок 'lab' (ЕРЛУ-КДЛ)")
        if self.age_from is not None and self.age_to is not None and self.age_from > self.age_to:
            raise ValueError("'age_from' не может быть больше 'age_to'")
        # Деловое обоснование — вложенный объект business (не плоская строка).
        # Обязательна «суть услуги» (description): без неё согласующие не видят,
        # зачем услуга нужна, и заявка не должна уходить на согласование.
        desc = (self.business.description if self.business else None) or ""
        if not str(desc).strip():
            raise ValueError("Укажите деловое обоснование (суть услуги)")
        return self


class ApprovalDecision(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject)$")
    comment: Optional[str] = None


class UserProfile(BaseModel):
    login: str
    display_name: str
    email: str
    department: Optional[str] = None
    position: Optional[str] = None
    is_admin: bool = Field(False, description="Роль «Администратор формы» — редактирование согласующих/филиалов")

    _check_email = field_validator("email")(_validate_email_format)
