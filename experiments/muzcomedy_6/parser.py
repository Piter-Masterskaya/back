"""
Эксперимент: парсер афиши Театра музыкальной комедии (источник SRC-006).

Парсит HTML-страницу афиши https://muzcomedy.ru, извлекает спектакли,
даты, статус билетов, проверяет дубликаты и сохраняет в data/sample_output.json.

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
SOURCE_ID = "muzcomedy_6"
PLAYBILL_URL = "https://muzcomedy.ru"

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
        if row is None or row != ev.tickets:
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


def normalize_ru_datetime(raw_dt: str, fallback_year: int = 2026) -> str:
    m = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?.*?(\d{2}:\d{2})", raw_dt.lower())
    if not m:
        return raw_dt.strip()
    day = int(m.group(1))
    month = MONTHS_RU.get(m.group(2), 1)
    year = int(m.group(3)) if m.group(3) else fallback_year
    if not m.group(3) and month in (1, 2, 3) and fallback_year == 2026:
        year = 2027
    return f"{year:04d}-{month:02d}-{day:02d} {m.group(4)}:00"


def extract_title(card, href: str) -> str:
    # 1. Ищем заголовок в родительских блоках карточки
    curr = card
    for _ in range(4):
        if not curr:
            break
        for tag in curr.find_all(["h1", "h2", "h3", "h4", "strong"]):
            txt = tag.get_text(" ", strip=True)
            if txt and len(txt) > 2 and not re.match(r"^\d{1,2}\s+[а-яё]+", txt.lower()):
                return txt
        curr = curr.parent

    # 2. Запасной вариант: извлекаем slug из URL (например monospektakl_maksima_averina -> Monospektakl maksima averina)
    slug = href.rstrip("/").split("/")[-1]
    slug = re.sub(r"\d+$", "", slug).replace("_", " ").strip()
    return slug.capitalize() if slug else "Спектакль Театра музыкальной комедии"


def parse_playbill() -> list[Event]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PiterMasterskayaBot/0.1)"}
    try:
        resp = httpx.get(PLAYBILL_URL, headers=headers, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"[ERROR] Не удалось загрузить {PLAYBILL_URL}: {exc}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    collected_at = datetime.now(MSK).isoformat(timespec="seconds")
    events: list[Event] = []
    seen_keys: set[str] = set()

    for link in soup.select("a[href*='/playbill/playbill/']"):
        href = link.get("href", "").strip()
        if not href:
            continue
        full_url = urljoin("https://muzcomedy.ru", href)

        card = link.find_parent(["div", "li", "article", "tr"]) or link.parent
        if not card:
            continue

        card_text = card.get_text("\n", strip=True)
        year_match = re.search(r"/playbill/playbill/(\d{4})/", href)
        url_year = int(year_match.group(1)) if year_match else 2026

        date_matches = re.findall(r"\d{1,2}\s+[а-яё]+(?:\s+\d{4})?,\s*\d{2}:\d{2}", card_text.lower())
        if not date_matches:
            continue

        title = extract_title(card, href)
        buy_link = card.find("a", string=re.compile(r"купить\s+билет", re.I))
        sold_out = re.search(r"все\s+билеты\s+проданы", card_text, re.I)

        if buy_link:
            tickets = "available"
            ticket_url = urljoin("https://muzcomedy.ru", buy_link.get("href", ""))
        elif sold_out:
            tickets = "sold_out"
            ticket_url = None
        else:
            tickets = "unknown"
            ticket_url = None

        for raw_dt in date_matches:
            starts_at = normalize_ru_datetime(raw_dt, fallback_year=url_year)
            dedup_key = f"{full_url}|{starts_at}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            events.append(
                Event(
                    title=title,
                    starts_at=starts_at,
                    venue="Театр музыкальной комедии",
                    description=None,
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

    events = parse_playbill()
    total, fresh = deduplicate(events, base_dir / ".state.db")
    print(f"[{SOURCE_ID}] Собрано: {total}, новых или изменивших статус: {fresh}")

    out_path = data_dir / "sample_output.json"
    out_path.write_text(
        json.dumps([asdict(e) for e in events], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
