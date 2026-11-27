import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://parf-muzey.ru"
TIMETABLE_URL = f"{BASE_URL}/parftimetable.htm"
DEFAULT_VENUE = "Петербургский музей парфюмерии (Санкт-Петербург)"

DATA_DIR = Path(__file__).parent / "data"
SEEN_FILE = DATA_DIR / "seen_ids.json"
OUTPUT_FILE = DATA_DIR / "sample_output.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Стоп-фразы, обозначающие недоступные или служебные дни вместо открытых мероприятий
STOP_PHRASES = (
    "занято",
    "музей в спячке",
    "отпуск",
    "свободно для заказа экскурсии",
)


class PerfumeEventItem(BaseModel):
    external_id: str
    source: str = "parf_muzey_12"
    title: str
    date_time: str
    venue: str
    description: Optional[str] = None
    url: str
    tickets_info: str


def make_external_id(date_str: str, time_str: str, title: str) -> str:
    raw = f"{date_str}|{time_str}|{title.lower().strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_seen_ids() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen_ids(seen_ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(
        json.dumps(sorted(seen_ids), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_timetable_html() -> str:
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        response = client.get(TIMETABLE_URL, timeout=15.0)
        response.raise_for_status()
        # Принудительное декодирование из windows-1251 во избежание кракозябр
        return response.content.decode("cp1251", errors="replace")


def detect_venue(text: str) -> str:
    lowered = text.lower()
    if "петропавловк" in lowered:
        return "Петропавловская крепость (выездная лекция)"
    if "юсуповск" in lowered:
        return "Юсуповский дворец (выездная лекция)"
    if "петроконгресс" in lowered or "лодейнопольская" in lowered:
        return 'БЦ "Петроконгресс", Лодейнопольская ул., 5'
    if "доме лумберга" in lowered:
        return "Дом Лумберга"
    return DEFAULT_VENUE


def detect_tickets_info(text: str, price_col: str) -> str:
    lowered = text.lower()
    if "вход свободный" in lowered:
        return "Вход свободный (бесплатно)"
    if "можно присоединиться" in lowered:
        price = f"Стоимость: {price_col} руб. " if price_col else ""
        return (
            f"{price}Есть свободные места, запись по SMS (+7-911-742-45-02) "
            "или Telegram (@Elinaarsenyeva)"
        ).strip()
    if "оплата наличными на входе" in lowered:
        if price_col:
            return f"Вход по билетам, оплата наличными на входе ({price_col} руб.)"
        return "Оплата наличными на входе"
    if price_col:
        return f"Стоимость: {price_col} руб. Предварительное бронирование обязательно"
    return "Предварительное бронирование по SMS/Telegram (стоимость по тарифу)"


def parse_timetable(html: str) -> list[PerfumeEventItem]:
    soup = BeautifulSoup(html, "lxml")
    events: list[PerfumeEventItem] = []

    current_year = "2026"
    last_month = 11

    month_pattern = (
        r"(?:январь|февраль|март|апрель|май|июнь|июль|август|"
        r"сентябрь|октябрь|ноябрь|декабрь)\s+(20\d{2})"
    )

    for row in soup.find_all("tr"):
        if not isinstance(row, Tag):
            continue

        row_text = " ".join(row.get_text(" ").split())

        # Обновляем год из заголовка таблицы (например, "Ноябрь 2026", "Декабрь 2026")
        year_match = re.search(month_pattern, row_text, re.I)
        if year_match:
            current_year = year_match.group(1)
            continue

        cells = [cell for cell in row.find_all(["td", "th"]) if isinstance(cell, Tag)]
        if len(cells) < 3:
            continue

        date_cell = " ".join(cells[0].get_text(" ").split())
        date_match = re.match(r"^(\d{2})\.(\d{2})\b", date_cell)
        if not date_match:
            continue

        day, month_str = date_match.group(1), date_match.group(2)
        month_num = int(month_str)

        # Обработка перехода через Новый год (с декабря на январь)
        if last_month == 12 and month_num == 1:
            current_year = str(int(current_year) + 1)
        last_month = month_num

        full_date = f"{day}.{month_str}.{current_year}"
        time_cell = " ".join(cells[1].get_text(" ").split())
        content_cell = cells[2]
        content_text = " ".join(content_cell.get_text(" ").split())
        price_cell = " ".join(cells[3].get_text(" ").split()) if len(cells) > 3 else ""

        # Отсекаем строки-заглушки ("ЗАНЯТО", "МУЗЕЙ В СПЯЧКЕ", слоты под заказ)
        lowered_content = content_text.lower()
        if any(stop in lowered_content for stop in STOP_PHRASES):
            continue

        # Извлекаем ссылку на внешнюю афишу/билеты, если есть внутри ячейки
        link_tag = content_cell.find("a", href=True)
        raw_href = link_tag.get("href") if isinstance(link_tag, Tag) else None
        event_url = urljoin(BASE_URL, raw_href) if isinstance(raw_href, str) else TIMETABLE_URL

        # Разделяем заголовок и описание по переносам строк
        raw_lines = [
            line.strip()
            for line in content_cell.get_text("\n").splitlines()
            if line.strip()
        ]
        title = raw_lines[0] if raw_lines else content_text[:80]
        description = " ".join(raw_lines[1:]) if len(raw_lines) > 1 else content_text

        date_time_str = f"{full_date} {time_cell}".strip() if time_cell else full_date

        events.append(
            PerfumeEventItem(
                external_id=make_external_id(full_date, time_cell, title),
                title=title,
                date_time=date_time_str,
                venue=detect_venue(content_text),
                description=description,
                url=event_url,
                tickets_info=detect_tickets_info(content_text, price_cell),
            )
        )

    return events


def run() -> None:
    seen_ids = load_seen_ids()
    try:
        html = fetch_timetable_html()
    except httpx.HTTPError as exc:
        logger.error("Ошибка загрузки расписания %s: %s", TIMETABLE_URL, exc)
        return

    parsed_events = parse_timetable(html)
    if not parsed_events:
        logger.warning("Не удалось извлечь ни одного мероприятия из таблицы расписания.")
        return

    new_events: list[PerfumeEventItem] = []
    for event in parsed_events:
        if event.external_id in seen_ids:
            logger.info(
                "Пропуск уже известного мероприятия: %s (%s)",
                event.title,
                event.external_id,
            )
            continue
        seen_ids.add(event.external_id)
        new_events.append(event)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps([e.model_dump() for e in new_events], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    save_seen_ids(seen_ids)
    logger.info(
        "Готово. Извлечено всего: %d, новых сохранено: %d.",
        len(parsed_events),
        len(new_events),
    )


if __name__ == "__main__":
    run()
