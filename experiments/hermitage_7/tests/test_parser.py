
"""Проверки парсера на локальных HTML-примерах."""

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest


PARSER_PATH = Path(__file__).resolve().parents[1] / "parser.py"
spec = importlib.util.spec_from_file_location(
    "hermitage_parser", PARSER_PATH
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

HTML = """
<html><body>
  <div class="afisha-slide">
    <span>18:00</span>
    <a href="/repertoire/999/ad">Реклама</a>
    <p>осталось билетов: 20</p>
  </div>
  <main>
    <h3>25 ноября 2026, среда</h3>
    <div class="show">
      <span>19:00</span>
      <a href="/repertoire/598/spyaschaya-krasavica">Спящая красавица</a>
      <p>Эрмитажный театр / Основной зал</p>
      <span>осталось билетов: 0</span>
      <a href="/order/6091">Купить</a>
    </div>
    <h3>28 ноября 2026, суббота</h3>
    <section>
      <div class="show">
        <span>15:00</span>
        <a href="/repertoire/641/shchelkunchik">Щелкунчик</a>
        <div>Эрмитажный театр / Основной зал</div>
        <span>осталось билетов: 1</span>
        <a href="/order/6001">Купить</a>
      </div>
      <div class="show">
        <span>19:30</span>
        <a href="/repertoire/641/shchelkunchik">Щелкунчик</a>
        <a href="/repertoire/641/shchelkunchik">Подробнее</a>
        <div>Эрмитажный театр / Основной зал</div>
        <span>Премьера</span>
        <span>осталось билетов: 0</span>
        <a href="/order/6002">Купить</a>
      </div>
    </section>
  </main>
</body></html>
"""


def get_events(collected_at="2026-11-25T12:00:00+03:00"):
    """Разбирает общий пример HTML."""
    return module.parse_html(HTML, collected_at=collected_at)


def test_independent_sessions():
    """Сеансы одного спектакля не объединяются."""
    events = get_events()
    assert len(events) == 3
    assert events[0].title == "Спящая красавица"
    assert events[0].starts_at == "2026-11-25 19:00:00"
    assert events[1].starts_at == "2026-11-28 15:00:00"
    assert events[2].starts_at == "2026-11-28 19:30:00"
    assert events[2].description == "Премьера"
    assert events[2].title == "Щелкунчик"


def test_tickets_and_links():
    """Проверяет ссылки, площадку и статусы билетов."""
    events = get_events()
    assert events[0].tickets == "none"
    assert events[1].tickets == "link_available"
    assert events[1].ticket_url.endswith("/order/6001")
    assert events[1].url.endswith("/repertoire/641/shchelkunchik")
    assert events[1].venue == "Эрмитажный театр / Основной зал"


def test_advertising_and_duplicates():
    """Рекламный слайдер и повторная ссылка не создают сеанс."""
    events = get_events()
    assert len(events) == 3
    assert all(event.title != "Реклама" for event in events)
    assert len({module.event_key(event) for event in events}) == 3


def test_fallback_key_without_order():
    """Без ссылки заказа используется запасной ключ."""
    html = HTML.replace('<a href="/order/6001">Купить</a>', '')
    events = module.parse_html(html)
    event = next(e for e in events if e.starts_at.endswith("15:00:00"))
    assert event.ticket_url is None
    assert event.tickets == "unknown"
    assert module.event_key(event).startswith("fallback:")


def test_shared_format_and_snapshot(tmp_path, monkeypatch):
    """Сохраняет девять полей и проверяет повторные запуски."""
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    events = get_events()

    assert module.compare_and_save(events) == (3, 0, 0, 0)

    again = get_events("2026-11-25T13:00:00+03:00")
    assert module.compare_and_save(again) == (0, 0, 3, 0)

    changed = list(again)
    changed[1] = replace(changed[1], description="Новый текст")
    assert module.compare_and_save(changed) == (0, 1, 2, 0)

    records = module.json.loads(
        (tmp_path / "sample_output.json").read_text(encoding="utf-8")
    )
    assert list(records[0]) == [
        "title", "starts_at", "venue", "description", "url",
        "ticket_url", "tickets", "source", "collected_at",
    ]


def test_rejects_very_small_result(tmp_path, monkeypatch):
    """При частичном разборе прежнее состояние не затирается."""
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    events = get_events()
    module.compare_and_save(events)

    snapshot = tmp_path / "previous_snapshot.json"
    before = snapshot.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="слишком мало сеансов"):
        module.compare_and_save(events[:1])

    assert snapshot.read_text(encoding="utf-8") == before


def test_stable_order_id_after_rescheduling():
    """Перенос времени сохраняет ID заказа."""
    event = get_events()[1]
    moved = replace(event, starts_at="2026-11-29 15:00:00")
    assert module.event_key(event) == module.event_key(moved)


def test_missing_event(tmp_path, monkeypatch):
    """Определяет сеанс, пропавший из текущей выборки."""
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    events = get_events()

    assert module.compare_and_save(events) == (3, 0, 0, 0)
    assert module.compare_and_save(events[:2]) == (0, 0, 2, 1)
