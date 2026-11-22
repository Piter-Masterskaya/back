"""
Источник 4: Санкт-Петербургская филармония (https://www.philharmonia.spb.ru/afisha/).

Скачивает афишу за месяц и печатает события в JSON.

    python parser.py              # текущий месяц
    python parser.py 12           # декабрь текущего года
    python parser.py 3 2027       # март 2027 года

Зависимости: pip install -r requirements.txt
"""
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

URL = "https://www.philharmonia.spb.ru/afisha/"
BASE = "https://www.philharmonia.spb.ru"

# На сайте месяц написан полным словом в родительном падеже: "14 мая 2026".
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
    # Именительный падеж - на случай изменения вёрстки.
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11,
    "декабрь": 12,
}


@dataclass
class Event:
    """Одно мероприятие (один сеанс). Общий формат для обоих сайтов."""
    title: str
    starts_at: datetime
    venue: str | None
    description: str | None
    url: str                # страница концерта на сайте филармонии
    ticket_url: str | None  # ссылки нет: покупка открывает JS-виджет
    tickets: str            # link_available | unknown | none


def clean(text):
    """Убирает неразрывные пробелы, переносы и лишние пробелы."""
    if text is None:
        return ""
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def get_soup(url):
    """Скачивает страницу и возвращает её как дерево HTML."""
    headers = {"User-Agent": "Mozilla/5.0"}
    r = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
    # Ошибка HTTP (403, 404, 500) остановит запуск с понятным текстом.
    r.raise_for_status()
    if r.charset_encoding is None:
        # Сервер может не отдавать charset в заголовке, а страница
        # свёрстана в windows-1251 (см. meta в <head>).
        r.encoding = "windows-1251"
    return BeautifulSoup(r.text, "lxml")


def parse_dt(item, year=None):
    """Собирает дату и время начала из блока ._date_from.

    Год берётся из вёрстки; если его там нет — подставляется год
    запрошенного месяца (переданный из main).
    """
    day_t = item.select_one("._date_from .date_day")
    mon_t = item.select_one("._date_from .date_dw_m")
    yr_t = item.select_one("._date_from .date_y")
    time_t = item.select_one("._date_from .date_h")
    if day_t is None or mon_t is None or time_t is None:
        return None
    month = MONTHS.get(mon_t.get_text(strip=True).lower())
    match = re.match(r"^(\d{1,2}):(\d{2})$", clean(time_t.get_text()))
    if month is None or match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if yr_t is not None and yr_t.get_text(strip=True).isdigit():
        year = int(yr_t.get_text(strip=True))
    if year is None:
        return None
    return datetime(year, month, int(day_t.get_text(strip=True)), hour, minute)


def get_description(item):
    """Склеивает подзаголовок, исполнителей, программу и организаторов."""
    parts = []
    preview = item.select_one("._preview")
    if preview is not None:
        parts.append(clean(preview.get_text(" ")))
    ansambles = item.select_one("._ansambles")
    if ansambles is not None:
        parts.append(clean(ansambles.get_text(" ")))
    pers = item.select_one("._persmain")
    if pers is not None:
        parts.append(clean(pers.get_text(" ")))
    program = item.select_one("._programm")
    if program is not None:
        parts.append(clean(program.get_text(" ")))
    organisers = item.select_one("._organisers_items")
    if organisers is not None:
        text = clean(organisers.get_text(" "))
        parts.append(re.sub(r"^Организаторы:\s*", "", text))
    # Для многодневных событий рядом с началом есть блок ._date_to.
    date_to = item.select_one("._date_to")
    if date_to is not None:
        parts.append("Окончание: " + clean(date_to.get_text(" ")))

    parts = [p for p in parts if p != ""]
    if len(parts) == 0:
        return None
    return " ".join(parts)


def get_tickets(item):
    """Возвращает статус билетов по блоку ._sale."""
    sale = item.select_one("._sale")
    if sale is None:
        return "unknown"
    text = clean(sale.get_text(" "))
    if "проданы" in text or "не продаются" in text:
        return "none"
    # Покупка - это кнопка, открывающая JS-виджет (button.__buy),
    # отдельной ссылки на билетный шоп у события нет.
    if sale.select_one("button.__buy") is not None or "Купить билет" in text:
        return "link_available"
    return "unknown"


def parse_philharmonia(soup, year=None):
    """Филармония: список событий, на каждую дату отдельный Event."""
    events = []

    # Все события лежат в div.it-afisha-list, каждое - в div._item.
    # Рядом лежат служебные блоки (сепараторы, спонсоры): у них нет даты
    # или заголовка, поэтому отсеиваются проверкой ниже.
    for item in soup.select("div.it-afisha-list div._item"):
        title_tag = item.select_one("._title a")
        if title_tag is None:
            continue
        starts_at = parse_dt(item, year)
        if starts_at is None:
            continue

        title = clean(title_tag.get_text(" "))
        url = urljoin(BASE, title_tag["href"])

        hall = item.select_one("._hall a")
        venue = clean(hall.get_text()) if hall is not None else None
        if venue == "":
            venue = None

        events.append(
            Event(
                title, starts_at, venue, get_description(item),
                url, None, get_tickets(item)
            )
        )
    return events


def next_page_url(soup):
    """URL следующей страницы из кнопки "ещё" (атрибут ajax у div.more)."""
    more = soup.select_one("div.more[ajax]")
    if more is None:
        return None
    return urljoin(BASE, more["ajax"])


def parse_all(url, year=None, max_pages=50):
    """Обходит страницы афиши, пока есть кнопка "ещё" или не упёрлись в лимит.

    Одинаковые (url, starts_at) со соседних страниц склеиваются.
    """
    events = []
    seen = set()
    for page in range(1, max_pages + 1):
        soup = get_soup(url)
        batch = parse_philharmonia(soup, year)
        new = [e for e in batch if (e.url, e.starts_at) not in seen]
        for e in new:
            seen.add((e.url, e.starts_at))
        events.extend(new)
        url = next_page_url(soup)
        if url is None:
            break
    return events


def build_url(year, month):
    """URL афиши за месяц: на сайте есть серверный фильтр ?year=&month=.

    Параметр year - это год сезона (сезон длится с сентября по июнь),
    поэтому январь-август относятся к сезону предыдущего года:
    март 2027 запрашивается как ?year=2026&month=3.
    """
    season_year = year if month >= 9 else year - 1
    return f"{URL}?year={season_year}&month={month}"


def parse_args(argv):
    """Разбирает аргументы [месяц [год]]; по умолчанию текущий месяц."""
    now = datetime.now()
    try:
        month = int(argv[1]) if len(argv) > 1 else now.month
        year = int(argv[2]) if len(argv) > 2 else now.year
    except ValueError:
        month, year = 0, 0
    if not (1 <= month <= 12 and 2000 <= year <= 2100):
        raise SystemExit(
            "Использование: python 2.py [месяц 1-12 [год]]\n"
            "Примеры: python 2.py | python 2.py 12 | python 2.py 3 2027"
        )
    return year, month


def print_events(events):
    """Печатает события в JSON. Пустой результат считаем ошибкой."""
    if len(events) == 0:
        raise SystemExit(
            "Событий не найдено: проверьте месяц/год; если они верные - "
            "возможно, изменилась вёрстка сайта."
        )

    sys.stdout.reconfigure(encoding="utf-8")

    result = []
    for event in events:
        result.append(asdict(event))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    year, month = parse_args(sys.argv)
    # Фильтр на случай, если сайт отдаст соседний месяц.
    events = [
        e for e in parse_all(build_url(year, month), year=year)
        if e.starts_at.year == year and e.starts_at.month == month
    ]
    print_events(events)
