from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema() -> None:
    """create_all создаёт новые таблицы, но не добавляет колонки в уже существующие.
    На боевом SQLite (data/requests.db) после промпта №6 нужны version и
    submission_comment — без ALTER старый журнал заявок остался бы без них."""
    from . import models as _models  # noqa: F401 — регистрирует таблицы на Base

    Base.metadata.create_all(bind=engine)
    if not str(settings.database_url).startswith("sqlite"):
        return
    insp = inspect(engine)
    if "service_requests" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("service_requests")}
    with engine.begin() as conn:
        if "version" not in cols:
            conn.execute(text(
                "ALTER TABLE service_requests ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
            ))
        if "submission_comment" not in cols:
            conn.execute(text(
                "ALTER TABLE service_requests ADD COLUMN submission_comment TEXT"
            ))
