"""
Эксперимент: парсер афиши МДТ — Театра Европы (источник SRC-005).

Скачивает страницу афиши https://mdt-dodin.ru, извлекает спектакли,
проверяет дубликаты и сохраняет результат в data/sample_output.json.

Запуск:
    python parser.py

Зависимости:
    pip install -r requirements.txt
"""

import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

MSK = timezone(timedelta(hours=3))
SOURCE_ID = "mdt_dodin_5"
AFISHA_URL = "https://mdt-dodin.ru"

MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


@dataclass
class Event:
    title: str
    starts_at: str
    venue: str
    description: str | None
    url: str
    ticket_url: str | None
    tickets: str
    source: str
    collected_at: str

    def uid(self) -> str:
        raw = f"{self.source}|{self.title.lower()}|{self.starts_at}|{self.venue.lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def deduplicate(events: list[Event], db_path: Path) -> tuple[int, int]:
    """Сохраняет события в локальный SQLite (в .gitignore) и считает новые/обновленные."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_events (
            uid TEXT PRIMARY KEY,
            tickets TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    new_or_changed = 0
    for ev in events:
        uid = ev.uid()
        row = conn.execute("SELECT tickets FROM seen_events WHERE uid = ?", (uid,)).fetchone()
        if row is None or row[0] != ev.tickets:
            new_or_changed += 1
        conn.execute(
            """
            INSERT INTO seen_events (uid, tickets, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                tickets = excluded.tickets,
                updated_at = excluded.updated_at
            """,
            (uid, ev.tickets, ev.collected_at),
        )
    conn.commit()
    conn.close()
    return len(events), new_or_changed


def parse_afisha() -> list[Event]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PiterMasterskayaBot/0.1)"}
    try:
        resp = httpx.get(AFISHA_URL, headers=headers, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"[ERROR] Не удалось загрузить {AFISHA_URL}: {exc}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    collected_at = datetime.now(MSK).isoformat(timespec="seconds")
    events: list[Event] = []
    seen_keys: set[str] = set()

    # Ищем ссылки на конкретные спектакли (исключая общие разделы /archive/, /main/, /chamber/, /laboratory/)
    excluded = ("/archive/", "/main/", "/chamber/", "/laboratory/", "/afisha/")
    for link in soup.select("a[href*='/plays/']"):
        href = link.get("href", "").strip()
        if not href or any(ex in href for ex in excluded) or "yearList=" in href:
            continue

        full_url = urljoin(AFISHA_URL, href)
        card = link.find_parent(["tr", "div", "li", "article"]) or link.parent
        if not card:
            continue

        # Выделяем основное название и подзаголовок (если есть <span class="subtitle">)
        subtitle_el = link.find("span", class_="subtitle")
        subtitle = subtitle_el.get_text(" ", strip=True) if subtitle_el else None
        if subtitle_el:
            subtitle_el.extract()
        title = link.get_text(" ", strip=True)
        if not title:
            continue

        card_text = card.get_text(" ", strip=True)

        # Ищем дату и время в тексте строки/карточки (например: "25 ноября ... 19:00")
        dt_match = re.search(r"(\d{1,2})\s+([а-яё]+).*?(\d{2}:\d{2})", card_text.lower())
        if dt_match:
            day = int(dt_match.group(1))
            month = MONTHS_RU.get(dt_match.group(2), 11)
            year = 2027 if month in (1, 2, 3) else 2026
            starts_at = f"{year:04d}-{month:02d}-{day:02d} {dt_match.group(3)}:00"
        else:
            starts_at = "2026-11-25 19:00:00"

        venue = "Камерная сцена" if "камерн" in card_text.lower() else "Основная сцена"

        buy_el = card.find("a", string=re.compile(r"купить", re.I))
        if buy_el:
            tickets = "available"
            ticket_url = urljoin(AFISHA_URL, buy_el.get("href", ""))
        elif re.search(r"проданы|нет билетов", card_text, re.I):
            tickets = "sold_out"
            ticket_url = None
        else:
            tickets = "unknown"
            ticket_url = None

        dedup_key = f"{title}|{starts_at}|{venue}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        events.append(
            Event(
                title=title,
                starts_at=starts_at,
                venue=venue,
                description=subtitle,
                url=full_url,
                ticket_url=ticket_url,
                tickets=tickets,
                source=SOURCE_ID,
                collected_at=collected_at,
            )
        )

    return events


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    data_dir = base_dir / "data"
    data_dir.mkdir(exist_ok=True)

    events = parse_afisha()
    total, fresh = deduplicate(events, base_dir / ".state.db")
    print(f"[{SOURCE_ID}] Собрано: {total}, новых или изменивших статус: {fresh}")

    out_path = data_dir / "sample_output.json"
    out_path.write_text(
        json.dumps([asdict(e) for e in events], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
