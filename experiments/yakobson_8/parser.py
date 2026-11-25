
"""Экспериментальный парсер афиши Театра балета Якобсона."""

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag


URL = "https://www.yacobsonballet.ru/afisha"
SOURCE = "yakobson_8"
DATA_DIR = Path(__file__).resolve().parent / "data"
TIMEZONE = ZoneInfo("Europe/Moscow")

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
DATE_RE = re.compile(
    r"^(\d{1,2})\s+([а-яё]+)(?:\s+(20\d{2}))?$", re.I
)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
EVENT_RE = re.compile(r"^/events/[^/]+/(\d+)/?$")
WEEKDAYS = {"пн", "вт", "ср", "чт", "пт", "сб", "вс"}

TITLE_SELECTORS = (
    '[itemprop="name"], .event-title, .event__title, '
    '.afisha-title, .afisha__title, .performance-title, '
    '.performance__title, h2, h3, h4'
)
VENUE_SELECTORS = (
    '[itemprop="location"], .venue, .event-venue, '
    '.event__venue, .place, .event-place, .event__place, '
    '.hall, .afisha__place'
)


@dataclass(frozen=True)
class Event:
    """Данные одного сеанса в формате команды."""

    title: str
    starts_at: str
    venue: str | None
    description: str | None
    url: str
    ticket_url: str | None
    tickets: str
    source: str
    collected_at: str


def clean(text: str) -> str:
    """Убирает невидимые символы и лишние пробелы."""
    text = re.sub(r"[\ufeff\u200b\u200e]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def text_of(tag: Tag) -> str:
    """Возвращает очищенный текст элемента."""
    return clean(tag.get_text(" ", strip=True))


def matching_tags(root: Tag, pattern: re.Pattern) -> list[Tag]:
    """Находит самые вложенные элементы с нужным текстом."""
    result = []
    for tag in root.find_all(True):
        if not pattern.fullmatch(text_of(tag)):
            continue
        if any(
            pattern.fullmatch(text_of(child))
            for child in tag.find_all(True)
        ):
            continue
        result.append(tag)
    return result


def content_parts(card: Tag) -> list[str]:
    """Собирает текст карточки без даты и кнопок."""
    parts = []
    for value in card.stripped_strings:
        text = clean(str(value))
        lower = text.lower().rstrip(".")
        if not text or lower in WEEKDAYS:
            continue
        if DATE_RE.fullmatch(text) or TIME_RE.fullmatch(text):
            continue
        if lower.startswith(("купить", "заказать билет")):
            continue
        if lower == "билеты":
            continue
        parts.append(text)
    return parts


def field_text(card: Tag, selectors: str) -> str | None:
    """Находит текст поля по CSS-селекторам."""
    for tag in card.select(selectors):
        value = text_of(tag)
        if value and not DATE_RE.fullmatch(value):
            if not TIME_RE.fullmatch(value):
                return value
    return None


def title_and_venue(card: Tag) -> tuple[str, str] | None:
    """Определяет название и место проведения."""
    title = field_text(card, TITLE_SELECTORS)
    venue = field_text(card, VENUE_SELECTORS)
    parts = content_parts(card)

    if title is None or venue is None:
        if len(parts) == 2:
            title = title or parts[0]
            venue = venue or parts[1]
        elif len(parts) == 3:
            title = title or parts[1]
            venue = venue or parts[2]

    if not title or not venue or title == venue:
        return None
    if len(title) > 250 or len(venue) > 250:
        return None
    return title, venue


def find_cards(soup: BeautifulSoup) -> list[Tag]:
    """Находит независимые блоки сеансов."""
    cards = []
    seen = set()
    body = soup.body or soup

    for date_tag in matching_tags(body, DATE_RE):
        parent = date_tag.parent
        while isinstance(parent, Tag) and parent is not body:
            if len(matching_tags(parent, DATE_RE)) != 1:
                break
            if len(matching_tags(parent, TIME_RE)) == 1:
                if title_and_venue(parent) is not None:
                    if id(parent) not in seen:
                        cards.append(parent)
                        seen.add(id(parent))
                    break
            parent = parent.parent
    return cards


def parse_day(value: str, today: date) -> date:
    """Получает дату сеанса, определяя год при необходимости."""
    match = DATE_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"Некорректная дата: {value}")

    day = int(match.group(1))
    month_name = match.group(2).lower()
    if month_name not in MONTHS:
        raise ValueError(f"Неизвестный месяц: {month_name}")

    month = MONTHS[month_name]
    year = int(match.group(3)) if match.group(3) else today.year
    result = date(year, month, day)
    if not match.group(3) and result < today - timedelta(days=45):
        result = date(year + 1, month, day)
    return result


def is_site_url(url: str) -> bool:
    """Проверяет адрес сайта театра."""
    host = urlparse(url).hostname or ""
    return host == "yacobsonballet.ru" or host.endswith(
        ".yacobsonballet.ru"
    )


def event_links(card: Tag) -> tuple[str, str | None]:
    """Находит страницу постановки и ссылку на покупку."""
    page_url = URL
    ticket_url = None

    for anchor in card.select("a[href]"):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        href = href.strip()
        if not href or href.startswith(("#", "javascript:")):
            continue

        full_url = urljoin(URL, href)
        if urlparse(full_url).scheme not in ("http", "https"):
            continue
        if is_site_url(full_url):
            if urlparse(full_url).path.startswith("/events/"):
                page_url = full_url
        if "купить" in text_of(anchor).lower():
            ticket_url = full_url
    return page_url, ticket_url


def parse_card(
    card: Tag,
    today: date,
    collected_at: str,
) -> Event | None:
    """Создаёт событие из карточки афиши."""
    dates = matching_tags(card, DATE_RE)
    times = matching_tags(card, TIME_RE)
    fields = title_and_venue(card)
    if len(dates) != 1 or len(times) != 1 or fields is None:
        return None

    title, venue = fields
    day = parse_day(text_of(dates[0]), today)
    hour, minute = map(int, text_of(times[0]).split(":"))
    starts_at = datetime(
        day.year, day.month, day.day, hour, minute,
        tzinfo=TIMEZONE,
    ).strftime("%Y-%m-%d %H:%M:%S")

    url, ticket_url = event_links(card)
    description = "; ".join(
        part for part in content_parts(card)
        if part not in {title, venue}
    ) or None

    text = text_of(card).lower()
    if "все билеты проданы" in text:
        tickets = "none"
    elif ticket_url:
        tickets = "link_available"
    else:
        tickets = "unknown"

    return Event(
        title=title,
        starts_at=starts_at,
        venue=venue,
        description=description,
        url=url,
        ticket_url=ticket_url,
        tickets=tickets,
        source=SOURCE,
        collected_at=collected_at,
    )


def event_key(event: Event) -> str:
    """Формирует ключ известного сеанса в рамках источника."""
    match = EVENT_RE.fullmatch(urlparse(event.url).path)
    if match and is_site_url(event.url):
        return f"{event.source}:session:{match.group(1)}"

    base = (
        f"{event.source}|{event.title.casefold()}|"
        f"{event.starts_at}|{(event.venue or '').casefold()}"
    )
    return "fallback:" + hashlib.sha256(
        base.encode("utf-8")
    ).hexdigest()[:20]


def parse_html(
    html: str,
    today: date | None = None,
    collected_at: str | None = None,
) -> list[Event]:
    """Разбирает HTML и возвращает сеансы без повторов."""
    now = datetime.now(TIMEZONE)
    today = today or now.date()
    collected_at = collected_at or now.isoformat(timespec="seconds")
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.select("script, style, noscript, nav, footer"):
        tag.decompose()

    found = {}
    for card in find_cards(soup):
        try:
            event = parse_card(card, today, collected_at)
        except (ValueError, KeyError):
            continue
        if event is not None:
            found[event_key(event)] = event
    return list(found.values())


def fetch_html() -> str:
    """Скачивает HTML афиши."""
    headers = {"User-Agent": "Mozilla/5.0"}
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        response = client.get(URL, headers=headers)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type:
            raise ValueError("Сайт вернул не HTML")
        return response.text


def compare_and_save(events: list[Event]) -> tuple[int, int, int, int]:
    """Сравнивает сеансы и сохраняет результат в JSON."""
    if not events:
        raise ValueError("Сеансы не найдены")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = DATA_DIR / "previous_snapshot.json"
    old = {}
    if snapshot.exists():
        old = json.loads(snapshot.read_text(encoding="utf-8"))
        if not isinstance(old, dict):
            raise ValueError("Некорректный формат snapshot")

    new = {}
    for event in events:
        record = asdict(event)
        record.pop("collected_at")
        new[event_key(event)] = record

    if old and len(new) < len(old) / 2:
        raise ValueError("Найдено слишком мало сеансов: проверьте HTML")

    added = sum(key not in old for key in new)
    changed = sum(
        key in old and old[key] != record
        for key, record in new.items()
    )
    unchanged = len(new) - added - changed
    missing = len(old.keys() - new.keys())

    output = [asdict(event) for event in events]
    output_text = json.dumps(
        output, ensure_ascii=False, indent=2
    ) + "\n"
    state_text = json.dumps(
        new, ensure_ascii=False, indent=2
    ) + "\n"

    (DATA_DIR / "sample_output.json").write_text(
        output_text, encoding="utf-8"
    )
    snapshot.write_text(state_text, encoding="utf-8")
    return added, changed, unchanged, missing


def main() -> None:
    """Запускает парсер и выводит статистику."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-file", type=Path)
    args = parser.parse_args()
    try:
        html = (
            args.html_file.read_text(encoding="utf-8")
            if args.html_file else fetch_html()
        )
        collected_at = datetime.now(TIMEZONE).isoformat(
            timespec="seconds"
        )
        events = parse_html(html, collected_at=collected_at)
        added, changed, unchanged, missing = compare_and_save(events)
        print(
            f"Театр Якобсона: {len(events)} сеансов; "
            f"новых: {added}, изменено: {changed}, "
            f"без изменений: {unchanged}, "
            f"не найдены повторно: {missing}"
        )
        print(f"Данные: {DATA_DIR / 'sample_output.json'}")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise SystemExit(f"Ошибка парсера: {exc}") from exc


if __name__ == "__main__":
    main()
