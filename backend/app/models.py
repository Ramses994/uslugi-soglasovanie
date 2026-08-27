"""
SQLAlchemy-модели журнала заявок. Хранение в БД нужно для:
  - истории (кто/когда подал, кто/когда согласовал),
  - обработки кликов "Согласовать/Отклонить" из письма (нужен статус, который меняется),
  - повторной генерации файла импорта в МИС после того, как все согласовали.
Сама заявка (JSON) хранится целиком в поле payload, чтобы не городить десятки колонок
под структуру, которая совпадает с формой (см. schemas.py) — это OLTP-журнал, а не источник
истины для номенклатуры (источник истины — сама МИС Медиалог).
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, String, DateTime, Enum, JSON, Text
from .database import Base


class RequestStatus(str, enum.Enum):
    draft = "draft"
    pending = "pending"           # отправлена, ждёт согласований
    approved = "approved"         # согласована всеми
    rejected = "rejected"         # отклонена хотя бы одним согласующим
    rework = "rework"             # отправлена на доработку инициатору
    imported = "imported"         # администратор МИС подтвердил внесение в Медиалог


class ServiceRequest(Base):
    __tablename__ = "service_requests"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_type = Column(String(32), nullable=False, default="new_service")  # new_service | complex_service | delete | promo
    status = Column(Enum(RequestStatus), nullable=False, default=RequestStatus.pending)

    initiator_login = Column(String(128), nullable=False)
    initiator_name = Column(String(256), nullable=False)
    initiator_email = Column(String(256), nullable=False)
    initiator_department = Column(String(256), nullable=True)
    initiator_position = Column(String(256), nullable=True)

    payload = Column(JSON, nullable=False)          # содержимое формы, см. schemas.ServiceRequestIn
    approvals = Column(JSON, nullable=False, default=list)  # [{approver_id, decision, comment, decided_at}]

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    comment_log = Column(Text, nullable=True)
