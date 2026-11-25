"""Проверки парсера Якобсона на локальных HTML-примерах."""

import hashlib
import importlib.util
import json
import sys
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path


PARSER_PATH = Path(__file__).resolve().parents[1] / "parser.py"
spec = importlib.util.spec_from_file_location("yakobson_parser", PARSER_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


HTML = """
<html><body>
  <nav><h3>25 ноября</h3><span>12:00</span></nav>
  <main>
    <div class="event-card">
      <div class="date"><span>05 Декабря</span></div>
      <time>13:00</time>
      <span>Сб</span>
      <span>Балет в 3-х актах</span>
      <h3><a href="/events/shchelkunchik--/457">Щелкунчик</a></h3>
      <p class="venue">Большой драматический театр</p>
      <a href="https://tickets.example/order/457">Купить билеты</a>
    </div>
    <div class="event-card">
      <span>04 Января</span>
      <time>19:00</time>
      <span>Пн</span>
      <span>Балет в 3-х актах</span>
      <h3>Щелкунчик</h3>
      <p>Александринский театр</p>
      <button>Купить билеты</button>
    </div>
  </main>
</body></html>
"""


def test_cards_are_independent():
    """Каждый сеанс получает свои дату, название и площадку."""
    events = module.parse_html(HTML, today=date(2026, 11, 25))
    assert len(events) == 2
    assert events[0].title == "Щелкунчик"
    assert events[0].venue == "Большой драматический театр"
    assert events[0].starts_at == "2026-12-05 13:00:00"
    assert events[0].description == "Балет в 3-х актах"
    assert events[1].starts_at == "2027-01-04 19:00:00"
    assert events[1].venue == "Александринский театр"


def test_event_key_and_ticket_link():
    """Ключ сеанса стабилен, ссылки и статусы сохраняются."""
    events = module.parse_html(HTML, today=date(2026, 11, 25))
    first, second = events

    expected = hashlib.sha256(
        f"{first.source}|457|{first.starts_at}".encode("utf-8")
    ).hexdigest()[:20]
    assert module.event_key(first) == expected
    assert module.event_key(first) != module.event_key(second)
    assert module.event_key(replace(first, collected_at="later")) == expected

    assert first.url.endswith("/events/shchelkunchik--/457")
    assert first.ticket_url == "https://tickets.example/order/457"
    assert first.tickets == "link_available"
    assert second.url == module.URL
    assert second.ticket_url is None
    assert second.tickets == "unknown"


def test_ignores_unrelated_html():
    """Посторонний текст не превращается в сеансы."""
    assert module.parse_html("<html><body>Нет афиши</body></html>") == []
    events = module.parse_html(HTML, today=date(2026, 11, 25))
    assert all(event.title != "25 ноября" for event in events)


def test_not_mixing_two_dates_in_one_wrapper():
    """Общий контейнер нескольких дат не считается сеансом."""
    html = """
    <main>
      <h3>05 декабря</h3><span>13:00</span>
      <h3>06 декабря</h3><span>19:00</span>
      <h4>Щелкунчик</h4><p>Александринский театр</p>
    </main>
    """
    assert module.parse_html(html, today=date(2026, 11, 25)) == []


def test_compare_and_save(tmp_path, monkeypatch):
    """Повторные запуски различают известные и изменённые сеансы."""
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    events = module.parse_html(
        HTML,
        today=date(2026, 11, 25),
        collected_at="2026-11-25T12:00:00+03:00",
    )

    assert module.compare_and_save(events) == (2, 0, 0)
    later = [replace(event, collected_at="2026-11-25T13:00:00+03:00")
             for event in events]
    assert module.compare_and_save(later) == (0, 0, 2)

    changed = [replace(later[0], description="Изменение"), later[1]]
    assert module.compare_and_save(changed) == (0, 1, 1)

    records = json.loads(
        (tmp_path / "sample_output.json").read_text(encoding="utf-8")
    )
    assert records[0] == asdict(changed[0])
    assert list(records[0]) == [
        "title", "starts_at", "venue", "description", "url",
        "ticket_url", "tickets", "source", "collected_at",
    ]

    state = json.loads(
        (tmp_path / "previous_snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert "collected_at" not in state[module.event_key(changed[0])]
