"""
Загрузка и поиск по двум справочникам:

1. ЕРЛУ-КДЛ (файл .xls, реальная структура — 5 колонок):
   код ЕРЛУ | наименование ЕРЛУ | код лаборатории | наименование лаборатории | Составное исследование
   Пример строки: '01.00.001' | 'СОЭ (по Вестергрену)' | '1.0.A1.202' | 'СОЭ (венозная кровь)' | 'Нет'
   Строки-заголовки разделов (например '01.' / 'ГЕМАТОЛОГИЯ') не имеют кода лаборатории —
   пропускаются при построении поискового индекса, но пригодятся для группировки в UI.

2. Номенклатура медицинских услуг Минздрава, НСИ OID 1.2.643.5.1.13.13.11.1070
   (файл .xlsx, лист 'Справочник', колонки ID | S_CODE | NAME | REL | DATEOUT):
   S_CODE — код вида A03.26.009, NAME — наименование, REL=1 — актуальная запись,
   DATEOUT — дата упразднения (если заполнена, запись неактуальна).

Оба справочника грузятся в память при старте приложения и обновляются по требованию
(POST /api/dictionaries/reload) — без перезапуска сервиса, если файлы заменили на диске
(например, выгрузили новую версию ЕРЛУ-КДЛ или НСИ Минздрава).

Поиск — простой substring/fuzzy по нормализованной строке (без внешних индексов вроде
Elasticsearch: справочники по объёму — тысячи-десятки тысяч строк, укладываются в память
и линейный скан с кэшем на 200мс более чем достаточен для автокомплита).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import openpyxl
import xlrd

from ..config import get_settings

settings = get_settings()


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


@dataclass
class ErluEntry:
    erlu_code: str
    erlu_name: str
    lab_code: str
    lab_name: str
    is_composite: bool
    section: str = ""  # ближайший вышестоящий заголовок раздела, например 'ГЕМАТОЛОГИЯ'


@dataclass
class MinzdravEntry:
    code: str
    name: str
    active: bool = True


@dataclass
class DictionaryStore:
    erlu: list[ErluEntry] = field(default_factory=list)
    minzdrav: list[MinzdravEntry] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ---------- загрузка ----------
    def load_erlu(self, path: str) -> int:
        wb = xlrd.open_workbook(path)
        sheet = wb.sheet_by_index(0)
        entries: list[ErluEntry] = []
        current_section = ""
        for r in range(1, sheet.nrows):  # строка 0 — заголовки колонок
            row = [sheet.cell_value(r, c) for c in range(min(5, sheet.ncols))]
            erlu_code, erlu_name, lab_code, lab_name, composite = (row + ["", "", "", "", ""])[:5]
            erlu_code = str(erlu_code).strip()
            erlu_name = str(erlu_name).strip()
            if not erlu_code:
                continue
            # Строки-заголовки разделов: код вида '01.' и пустой код лаборатории
            if not str(lab_code).strip():
                current_section = erlu_name or current_section
                continue
            entries.append(
                ErluEntry(
                    erlu_code=erlu_code,
                    erlu_name=erlu_name,
                    lab_code=str(lab_code).strip(),
                    lab_name=str(lab_name).strip(),
                    is_composite=_normalize(str(composite)) in ("да", "yes", "1", "true"),
                    section=current_section,
                )
            )
        with self.lock:
            self.erlu = entries
        return len(entries)

    def load_minzdrav(self, path: str) -> int:
        # read_only=True НЕ используем: у части выгрузок НСИ Минздрава в XML прописан
        # некорректный <dimension> (например, "A1:A1" при факте в 10000+ строк), из-за чего
        # ReadOnlyWorksheet.iter_rows останавливается после первой ячейки. Обычная загрузка
        # игнорирует битый dimension и читает по фактическому содержимому листа.
        wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
        ws = wb[wb.sheetnames[0]]
        entries: list[MinzdravEntry] = []
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)  # ('ID','S_CODE','NAME','REL','DATEOUT')
        next(rows, None)  # строка с русским описанием колонок — тоже пропускаем
        for row in rows:
            if not row or len(row) < 3:
                continue
            _id, code, name = row[0], row[1], row[2]
            rel = row[3] if len(row) > 3 else 1
            dateout = row[4] if len(row) > 4 else None
            if not code or not name:
                continue
            active = (rel in (1, 1.0, "1", True)) and not dateout
            entries.append(MinzdravEntry(code=str(code).strip(), name=str(name).strip(), active=bool(active)))
        with self.lock:
            self.minzdrav = entries
        return len(entries)

    def reload_all(self) -> dict:
        n_erlu = self.load_erlu(settings.dict_erlu_kdl_path)
        n_mz = self.load_minzdrav(settings.dict_minzdrav_path)
        return {"erlu_loaded": n_erlu, "minzdrav_loaded": n_mz}

    # ---------- поиск ----------
    def search_erlu(self, query: str, limit: int = 20) -> list[ErluEntry]:
        q = _normalize(query)
        if not q:
            return self.erlu[:limit]
        scored = []
        for e in self.erlu:
            name_n = _normalize(e.erlu_name)
            if q in name_n or q in e.erlu_code.lower():
                score = 1.0 if name_n.startswith(q) else 0.8
            else:
                score = SequenceMatcher(None, q, name_n).ratio()
                if score < 0.35:
                    continue
            scored.append((score, e))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [e for _, e in scored[:limit]]

    @staticmethod
    def _score_minzdrav(q: str, e: "MinzdravEntry") -> float:
        """Общая формула релевантности — вынесена отдельно, чтобы search_minzdrav
        (список для ручного автокомплита) и best_minzdrav_match_scored (авто-подстановка
        кода по полю "Наименование услуги", см. routers/dictionaries.py GET /minzdrav/suggest)
        считали её одинаково: иначе поле "уже выбрано автоматически" и список ручного
        поиска могли бы по-разному ранжировать один и тот же текст."""
        name_n = _normalize(e.name)
        if q == e.code.lower():
            return 2.0
        if q in name_n:
            return 1.0 if name_n.startswith(q) else 0.75
        if name_n and name_n in q:
            # название услуги в заявке часто ДЛИННЕЕ канонического имени Минздрава
            # (например, "Приём врача-офтальмолога первичный" содержит "Офтальмометрия" не будет,
            # но "Офтальмометрия" может быть подстрокой более длинного пользовательского ввода) —
            # без этой ветки такие совпадения находил бы только медленный SequenceMatcher.
            return 0.7
        return SequenceMatcher(None, q, name_n).ratio()

    def search_minzdrav(self, query: str, limit: int = 20, active_only: bool = True) -> list[MinzdravEntry]:
        q = _normalize(query)
        pool = [e for e in self.minzdrav if (e.active or not active_only)]
        if not q:
            return pool[:limit]
        scored = []
        for e in pool:
            score = self._score_minzdrav(q, e)
            if score < 0.4:
                continue
            scored.append((score, e))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [e for _, e in scored[:limit]]

    def best_minzdrav_match(self, service_name: str) -> MinzdravEntry | None:
        results = self.search_minzdrav(service_name, limit=1)
        return results[0] if results else None

    def best_minzdrav_match_scored(self, service_name: str, min_score: float = 0.7) -> tuple[float, "MinzdravEntry"] | None:
        """Для автоматической подстановки кода Минздрава по мере ввода поля
        "Наименование услуги" (без клика по выпадающему списку, см. app.js
        _doMinzdravAutoSuggest).

        ВАЖНО: сознательно НЕ использует SequenceMatcher-фоллбэк из _score_minzdrav —
        только дешёвую проверку подстрокой. Две причины, обе найдены при живой
        проверке формы (см. ТЗ.md, раздел 12.9), не только в теории:
        1) Производительность: полный SequenceMatcher.ratio() по ~11000 записям
           справочника Минздрава занимает ~0.5-0.6с. Это уже терпимо для ОДНОГО
           запроса при клике "искать", но при автоподсказке НА КАЖДЫЙ debounce
           (то есть почти на каждое слово при вводе) плюс параллельный запрос
           обычного автокомплита (search_minzdrav, тот же скан) — два таких скана
           одновременно конкурируют за GIL и почти удваивают время ответа обоих
           (~1.2с вместо ~0.6с, воспроизведено и замерено через curl). Заметно
           тормозит форму при вводе.
        2) Корректность важнее полноты здесь: авто-подстановка происходит БЕЗ
           подтверждения пользователя, поэтому нечёткое (fuzzy) совпадение —
           это риск подставить похожий, но неверный код. Настоящий fuzzy-поиск
           остаётся в search_minzdrav — там результат виден в списке, и человек
           выбирает сам.
        """
        q = _normalize(service_name)
        if not q:
            return None
        best: tuple[float, MinzdravEntry] | None = None
        for e in self.minzdrav:
            if not e.active:
                continue
            name_n = _normalize(e.name)
            if q == e.code.lower():
                score = 2.0
            elif q in name_n:
                score = 1.0 if name_n.startswith(q) else 0.85
            elif name_n and name_n in q:
                score = 0.75
            else:
                continue  # без SequenceMatcher — см. пояснение выше
            if best is None or score > best[0]:
                best = (score, e)
        if best and best[0] >= min_score:
            return best
        return None


store = DictionaryStore()


def init_dictionaries():
    if settings.dict_refresh_on_start and Path(settings.dict_erlu_kdl_path).exists() and Path(settings.dict_minzdrav_path).exists():
        store.reload_all()
