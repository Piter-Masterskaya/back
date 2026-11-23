"""
Эксперимент: парсер афиши БДТ (источник 4).

Скачивает страницу афиши месяца, находит спектакли и печатает их в JSON.

    python parser.py             # текущий месяц
    python parser.py --month 12  # афиша на декабрь

Зависимости: pip install -r requirements.txt
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

URL = "https://bdt.spb.ru/afisha/"
SITE = "https://bdt.spb.ru"
SOURCE = "bdt_4"
# Момент, когда страница была скачана. Один на весь запуск, поэтому
# считаем его один раз при импорте модуля.
COLLECTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Event:
    """Одно мероприятие (один сеанс). Общий формат для обоих сайтов."""
    title: str
    starts_at: datetime
    venue: str | None
    description: str | None
    url: str                # страница спектакля на сайте театра
    ticket_url: str | None  # ссылка на покупку билетов, если есть
    tickets: str            # link_available | unknown | none
    source: str = SOURCE    # id источника, часть ключа события
    collected_at: str = COLLECTED_AT  # когда скачана страница (ISO 8601)


def clean(text):
    """Убирает неразрывные пробелы, переносы и лишние пробелы."""
    if text is None:
        return ""
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def make_dt(month, day, hour, minute):
    """Собирает дату. Года на сайтах нет, поэтому его нужно угадать."""
    now = datetime.now()
    dt = datetime(now.year, month, day, hour, minute)
    # Дата давно в прошлом (например, "08 янв" в ноябре) - значит,
    # это следующий год. Запас 60 дней нужен, чтобы только что
    # прошедшие сеансы не уезжали на год вперёд.
    if dt < now - timedelta(days=60):
        dt = dt.replace(year=now.year + 1)
    return dt


def get_soup(url):
    """Скачивает страницу и возвращает её как дерево HTML."""
    headers = {"User-Agent": "Mozilla/5.0"}
    r = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
    # Ошибка HTTP (403, 404, 500) остановит запуск с понятным текстом.
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")


def parse_bdt_block(block, day, month):
    """Разбирает один спектакль внутри строки-даты. None - если пропуск."""
    text = clean(block.get_text(" "))

    title_link = block.select_one("a[href^='/spektakli/']")
    time_match = re.search(r"Начало в\s*(\d{1,2}):(\d{2})", text)
    if title_link is None or time_match is None or day is None:
        return None

    # Перенесённые и отменённые спектакли помечены "ВНИМАНИЕ: ...".
    # Обычное "Внимание! ..." (про возраст) не подойдёт: нет двоеточия.
    if re.search(r"ВНИМАНИЕ:\s*(Перенос|Отмена)", text):
        return None

    ticket = None
    for a in block.find_all("a", href=True):
        if "ticketland" in a["href"]:
            ticket = a["href"]
            break
    if ticket:
        status = "link_available"
    else:
        status = "none"

    venue = None
    place = block.select_one(".cell-place")
    if place is not None:
        venue = clean(place.get_text(" "))

    parts = []
    for span in block.select(".cell-name span.text-secondary"):
        parts.append(clean(span.get_text(" ")))
    description = " ".join(parts)
    if description == "":
        description = None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2))
    starts_at = make_dt(month, day, hour, minute)
    url = SITE + title_link["href"]
    title = clean(title_link.get_text())
    return Event(title, starts_at, venue, description, url, ticket, status)


def parse_bdt(soup):
    """БДТ: афиша - строки-даты, в каждой один или несколько спектаклей."""
    events = []
    day = None
    month = None

    for row in soup.select("div.afisha-row"):
        day_tag = row.select_one(".cell-date .day")  # "19/11"
        if day_tag is not None:
            m = re.search(r"(\d{1,2})/(\d{1,2})", day_tag.get_text())
            if m is not None:
                # Запоминаем дату: у следующей строки она может
                # отсутствовать.
                day = int(m.group(1))
                month = int(m.group(2))

        for block in row.select(".cell-data > .d-flex"):
            event = parse_bdt_block(block, day, month)
            if event is not None:
                events.append(event)
    return events


def print_events(events):
    """Печатает события в JSON. Пустой результат считаем ошибкой."""
    # Пустой результат почти всегда значит, что вёрстка сайта изменилась.
    if len(events) == 0:
        raise SystemExit(
            "Событий не найдено: возможно, изменилась вёрстка сайта."
        )

    # Принудительно выводим в UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")

    result = []
    for event in events:
        result.append(asdict(event))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", type=int, help="номер месяца")
    args = ap.parse_args()

    url = URL
    if args.month:
        url = url + "?month=" + str(args.month)
    print_events(parse_bdt(get_soup(url)))
