"""CLI для фоновых задач (как `python -m pricemonitor daily` у PriceMonitor).

Запуск из корня репозитория / контейнера:
  PYTHONPATH=backend python -m app.cli weekly-reports
  PYTHONPATH=backend python -m app.cli weekly-reports --force
"""
from __future__ import annotations

import argparse
import json
import sys

from .database import SessionLocal, ensure_schema
from .services.weekly_reports import run_friday_reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Фоновые задачи формы согласования")
    sub = parser.add_subparsers(dest="command", required=True)

    weekly = sub.add_parser("weekly-reports", help="Еженедельные отчёты админу и инициаторам")
    weekly.add_argument("--force", action="store_true", help="Повторно отправить за текущую ISO-неделю")

    args = parser.parse_args(argv)
    ensure_schema()
    db = SessionLocal()
    try:
        if args.command == "weekly-reports":
            result = run_friday_reports(db, force=args.force)
            print(json.dumps(result, ensure_ascii=False, default=str))
            admin_ok = result.get("admin", {}).get("ok", False)
            return 0 if admin_ok else 1
        parser.error(f"Неизвестная команда: {args.command}")
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
