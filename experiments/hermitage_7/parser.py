
"""Экспериментальный парсер афиши Эрмитажного театра."""

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag


URL = "https://hermitagetheater.ru/afisha"
SOURCE = "hermitage_7"
DATA_DIR = Path(__file__).resolve().parent / "data"
TIMEZONE = ZoneInfo("Europe/Moscow")

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
DATE_RE = re.compile(
    r"^(\d{1,2})\s+([а-яё]+)\s+(20\d{2})(?:\s*,\s*[а-яё]+)?$",
    re.I,
)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
TICKETS_RE = re.compile(r"осталось\s+билетов\s*:?\s*(\d+)", re.I)
ORDER_RE = re.compile(r"^/order/(\d+)/?$")
REPERTOIRE_RE = re.compile(r"^/repertoire/\d+(?:/|$)")


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


def text_nodes(tag: Tag) -> list[str]:
    """Возвращает текстовые узлы элемента."""
    return [
        value for node in tag.descendants
        if isinstance(node, NavigableString)
        if (value := clean(str(node)))
    ]


def site_link(anchor: Tag) -> str | None:
    """Возвращает абсолютную ссылку на сайт театра."""
    href = anchor.get("href")
    if not isinstance(href, str) or not href.strip():
        return None
    link = urljoin(URL, href.strip())
    parsed = urlparse(link)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.hostname not in (
        "hermitagetheater.ru", "www.hermitagetheater.ru"
    ):
        return None
    return link


def find_card(anchor: Tag) -> Tag | None:
    """Находит блок сеанса вокруг ссылки на постановку."""
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name in ("body", "html"):
            break
        texts = text_nodes(parent)
        times = [text for text in texts if TIME_RE.fullmatch(text)]
        counts = TICKETS_RE.findall(" ".join(texts))
        if len(times) != 1 or len(counts) != 1:
            continue

        pages = {
            link for item in parent.select("a[href]")
            if (link := site_link(item))
            if REPERTOIRE_RE.match(urlparse(link).path)
        }
        if len(pages) == 1:
            return parent
    return None


def find_cards(soup: BeautifulSoup) -> list[Tag]:
    """Собирает карточки сеансов без повторов."""
    result = []
    seen = set()
    for anchor in soup.select("a[href]"):
        link = site_link(anchor)
        if not link or not REPERTOIRE_RE.match(urlparse(link).path):
            continue
        card = find_card(anchor)
        if card is not None and id(card) not in seen:
            result.append(card)
            seen.add(id(card))
    return result


def find_day(card: Tag) -> datetime | None:
    """Ищет дату внутри карточки или в предыдущем заголовке."""
    nodes = list(card.descendants) + list(card.previous_elements)
    for node in nodes:
        if not isinstance(node, NavigableString):
            continue
        match = DATE_RE.fullmatch(clean(str(node)))
        if match is None:
            continue
        day = int(match.group(1))
        month = MONTHS.get(match.group(2).lower())
        year = int(match.group(3))
        if month is None:
            continue
        try:
            return datetime(year, month, day)
        except ValueError:
            continue
    return None


def parse_card(card: Tag, collected_at: str) -> Event | None:
    """Создаёт событие из карточки афиши."""
    title = None
    page_url = None
    ticket_url = None
    for anchor in card.select("a[href]"):
        link = site_link(anchor)
        if link is None:
            continue
        path = urlparse(link).path
        label = clean(anchor.get_text(" "))
        if REPERTOIRE_RE.match(path) and label:
            if label.casefold() not in {"подробнее", "расписание"}:
                if title is None:
                    title = label
                    page_url = link
        elif ORDER_RE.fullmatch(path):
            ticket_url = link

    if not title or not page_url or len(title) > 250:
        return None
    day = find_day(card)
    if day is None:
        return None

    texts = text_nodes(card)
    times = [text for text in texts if TIME_RE.fullmatch(text)]
    if len(times) != 1:
        return None
    hour, minute = map(int, times[0].split(":"))
    starts_at = day.replace(
        hour=hour, minute=minute
    ).strftime("%Y-%m-%d %H:%M:%S")

    venue = next(
        (
            text for text in texts
            if "Эрмитажный театр" in text and len(text) <= 200
        ),
        None,
    )
    description = "Премьера" if "Премьера" in texts else None
    count_match = TICKETS_RE.search(" ".join(texts))
    remaining = int(count_match.group(1)) if count_match else None
    if remaining == 0:
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
        url=page_url,
        ticket_url=ticket_url,
        tickets=tickets,
        source=SOURCE,
        collected_at=collected_at,
    )


def event_key(event: Event) -> str:
    """Создаёт ключ сеанса для поиска повторов."""
    path = urlparse(event.ticket_url or "").path
    match = ORDER_RE.fullmatch(path)
    if match:
        return f"{event.source}:order:{match.group(1)}"

    base = f"{event.source}|{event.url}|{event.starts_at}"
    return "fallback:" + hashlib.sha256(
        base.encode("utf-8")
    ).hexdigest()[:20]


def parse_html(
    html: str,
    collected_at: str | None = None,
) -> list[Event]:
    """Разбирает HTML и возвращает сеансы без дублей."""
    collected_at = collected_at or datetime.now(TIMEZONE).isoformat(
        timespec="seconds"
    )
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select(
        "script, style, noscript, nav, footer, .splide, .afisha-slide"
    ):
        tag.decompose()

    found = {}
    for card in find_cards(soup):
        event = parse_card(card, collected_at)
        if event is not None:
            found[event_key(event)] = event
    return list(found.values())


def fetch_html() -> str:
    """Скачивает HTML афиши."""
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        response = client.get(
            URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type:
            raise ValueError("Сайт вернул не HTML")
        return response.text


def compare_and_save(events: list[Event]) -> tuple[int, int, int, int]:
    """Сравнивает сеансы и сохраняет результат в JSON."""
    if not events:
        raise ValueError("Сеансы не найдены: проверьте HTML сайта")

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

    records = [asdict(event) for event in events]
    output_text = json.dumps(
        records, ensure_ascii=False, indent=2
    ) + "\n"
    snapshot_text = json.dumps(
        new, ensure_ascii=False, indent=2
    ) + "\n"

    (DATA_DIR / "sample_output.json").write_text(
        output_text, encoding="utf-8"
    )
    snapshot.write_text(snapshot_text, encoding="utf-8")
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
        events = parse_html(html)
        added, changed, unchanged, missing = compare_and_save(events)
        print(
            f"Эрмитажный театр: {len(events)} сеансов; "
            f"новых: {added}, изменено: {changed}, "
            f"без изменений: {unchanged}, "
            f"не найдены повторно: {missing}"
        )
        print(f"Данные: {DATA_DIR / 'sample_output.json'}")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise SystemExit(f"Ошибка парсера: {exc}") from exc


if __name__ == "__main__":
    main()
