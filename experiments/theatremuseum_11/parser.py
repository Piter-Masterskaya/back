import hashlib
import json
import logging
import random
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://theatremuseum.ru"
AFISHA_URL = f"{BASE_URL}/event"
DATA_DIR = Path(__file__).parent / "data"
SEEN_FILE = DATA_DIR / "seen_ids.json"
OUTPUT_FILE = DATA_DIR / "sample_output.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class EventItem(BaseModel):
    external_id: str
    source: str = "theatremuseum_11"
    title: str
    date_time: str
    venue: Optional[str] = None
    description: Optional[str] = None
    url: str
    tickets_info: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


def make_external_id(url: str, date_time: str) -> str:
    """Формирует стабильный хеш события по URL и дате проведения."""
    raw = f"{url.strip()}|{date_time.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_seen_ids() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen_ids(seen_ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(seen_ids), ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_page(client: httpx.Client, url: str, params: dict[str, str | int]) -> str:
    response = client.get(url, params=params, timeout=15.0)
    response.raise_for_status()
    return response.text


def parse_event_cards(html: str) -> tuple[list[EventItem], int]:
    soup = BeautifulSoup(html, "lxml")
    items: list[EventItem] = []

    event_links = soup.select("a[href*='/event/']")
    visited_urls: set[str] = set()

    for link in event_links:
        raw_href = link.get("href")
        if not isinstance(raw_href, str) or raw_href.rstrip("/") == "/event":
            continue

        full_url: str = urljoin(BASE_URL, raw_href)
        if full_url in visited_urls:
            continue

        card = link.find_parent(["div", "article", "li"])
        if not isinstance(card, Tag):
            continue

        text_lines = [line.strip() for line in card.get_text("\n").splitlines() if line.strip()]
        if len(text_lines) < 3:
            continue

        visited_urls.add(full_url)

        date_match = next(
            (line for line in text_lines if re.search(r"\d{2}\.\d{2}\.\d{4}", line)),
            "Дата не указана",
        )

        # Теги вида #Пушкинская карта (могут состоять из нескольких слов до следующего #)
        tags = [
            f"#{tag.strip()}"
            for line in text_lines
            if "#" in line
            for tag in line.split("#")[1:]
            if tag.strip()
        ]

        title_el = card.find(["h2", "h3", "h4"])
        title_source = title_el if isinstance(title_el, Tag) else link
        title = " ".join(title_source.get_text(strip=True).split())
        if not title or title.lower() == "подробнее":
            title = text_lines[0]

        # Известные площадки музея — проверяем по точному вхождению названий филиалов
        known_venues = (
            "Театральный музей",
            "Шереметевский дворец — Музей музыки",
            "Музей-квартира Н. А. Римского-Корсакова",
            "Музей-квартира актеров Самойловых",
            "Дом-музей Ф. И. Шаляпина",
            "Театр в Театральном музее",
        )
        venue = next(
            (line for line in text_lines if any(v.lower() in line.lower() for v in known_venues) and len(line) < 70),
            "Музей театрального и музыкального искусства",
        )

        desc_candidates = [
            line
            for line in text_lines
            if line not in {title, date_match, venue, "подробнее"} and "#" not in line
        ]
        description = max(desc_candidates, key=len) if desc_candidates else None

        has_pushkin = any("пушкинская карта" in t.lower() for t in tags)
        has_buy_btn = bool(card.find(string=re.compile(r"купить\s+билет", re.I)))
        if has_pushkin and has_buy_btn:
            tickets_info = "Онлайн-покупка доступна, действует Пушкинская карта"
        elif has_pushkin:
            tickets_info = "Доступно по Пушкинской карте (детали на странице события)"
        elif has_buy_btn:
            tickets_info = "Онлайн-покупка билетов доступна"
        else:
            tickets_info = "Уточняется на детальной странице события"

        items.append(
            EventItem(
                external_id=make_external_id(full_url, date_match),
                title=title,
                date_time=date_match,
                venue=venue,
                description=description,
                url=full_url,
                tickets_info=tickets_info,
                tags=tags,
            )
        )

    page_numbers = [
        int(a.get_text(strip=True))
        for a in soup.select("a[href*='page='], a[href*='list=']")
        if a.get_text(strip=True).isdigit()
    ]
    max_page = max(page_numbers) if page_numbers else 1

    return items, max_page


def enrich_event_details(client: httpx.Client, item: EventItem) -> EventItem:
    """Добирает точное время, цену и статус билетов со страницы конкретного события."""
    try:
        html = fetch_page(client, item.url, {})
        soup = BeautifulSoup(html, "lxml")
        page_text = " ".join(soup.get_text(" ").split())

        ticket_link = next(
            (
                a
                for a in soup.find_all("a", href=True)
                if re.search(r"купить\s+билет", a.get_text(" ", strip=True), re.I)
            ),
            None,
        )
        price_match = re.search(r"(\d{2,5}\s*(?:₽|руб\.?))", page_text, re.I)

        details: list[str] = []
        if price_match:
            details.append(f"Стоимость: {price_match.group(1)}")
        if ticket_link is not None:
            ticket_href = ticket_link.get("href")
            if isinstance(ticket_href, str):
                details.append(f"Билеты: {urljoin(BASE_URL, ticket_href)}")
        if "билетов нет" in page_text.lower() or "продажа закрыта" in page_text.lower():
            details.append("Билетов нет")

        if details:
            item.tickets_info = "; ".join(details)
    except httpx.HTTPError as exc:
        logger.warning("Не удалось загрузить детальную страницу %s: %s", item.url, exc)
    return item


def run(max_pages_limit: int = 2, fetch_details: bool = True) -> None:
    today = date.today()
    end_date = today + timedelta(days=30)
    date_range = f"{today.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}"

    seen_ids = load_seen_ids()
    new_events: list[EventItem] = []

    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        page = 1
        while page <= max_pages_limit:
            params: dict[str, str | int] = {"data_find": date_range, "page": page}
            logger.info("Запрос страницы %s с фильтром %s", page, date_range)

            try:
                html = fetch_page(client, AFISHA_URL, params)
            except httpx.HTTPError as exc:
                logger.error("Ошибка загрузки списка мероприятий (page=%s): %s", page, exc)
                break

            parsed_items, max_page = parse_event_cards(html)
            if not parsed_items:
                logger.warning("На странице %s не найдено ни одного мероприятия (возможно, изменилась вёрстка).", page)
                break

            for item in parsed_items:
                if item.external_id in seen_ids:
                    logger.info("Пропуск уже известного мероприятия: %s (%s)", item.title, item.external_id)
                    continue

                if fetch_details:
                    time.sleep(random.uniform(1.0, 2.0))
                    item = enrich_event_details(client, item)

                seen_ids.add(item.external_id)
                new_events.append(item)

            if page >= max_page:
                break
            page += 1
            time.sleep(random.uniform(1.5, 3.0))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps([e.model_dump() for e in new_events], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    save_seen_ids(seen_ids)
    logger.info("Готово. Новых мероприятий сохранено: %d. Файл: %s", len(new_events), OUTPUT_FILE)


if __name__ == "__main__":
    run()
