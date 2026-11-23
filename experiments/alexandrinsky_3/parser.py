"""
Эксперимент: парсер афиши Александринского театра (источник 3).

Скачивает страницу афиши, находит спектакли и печатает их в JSON.

    python parser.py

Зависимости: pip install -r requirements.txt
"""
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

import httpx
from bs4 import BeautifulSoup

URL = "https://alexandrinsky.ru/afisha-i-bilety/"
SOURCE = "alexandrinsky_3"
# Момент, когда страница была скачана. Один на весь запуск, поэтому
# считаем его один раз при импорте модуля.
COLLECTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")


# На сайте месяц написан словом: "19 ноя 18:00".
# Берём первые 3 буквы.
MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11,
    "дек": 12,
}


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


def count_events(tag):
    """Считает заголовки спектаклей (h4 со ссылкой) внутри тега."""
    count = 0
    for h4 in tag.find_all("h4"):
        if h4.find("a", href=re.compile("/afisha-i-bilety/")):
            count += 1
    return count


def find_card(h4):
    """
    Находит карточку спектакля вокруг заголовка.
    Идём вверх по родителям, пока внутри нет других спектаклей.
    """
    card = h4
    while card.parent is not None:
        if count_events(card.parent) != 1:
            break
        card = card.parent
    return card


def is_inside(text_node, card):
    """Проверяет, что кусок текста лежит внутри карточки."""
    for parent in text_node.parents:
        if parent is card:
            return True
    return False


def get_alex_venue(h4, card, title):
    """Собирает сцену и зал из текста карточки перед заголовком."""
    before = []
    # Идём назад от заголовка и останавливаемся на границе карточки.
    for node in h4.find_all_previous(string=True):
        if not is_inside(node, card):
            break
        before.append(clean(node))
    before.reverse()

    parts = []
    for text in before:
        if text == "" or text == title:
            continue
        if "Премьера" in text:
            continue
        if re.match(r"^\d+\+$", text):  # возраст, например "12+"
            continue
        parts.append(text)

    if len(parts) == 0:
        return None
    return ", ".join(parts)


def get_alex_description(card):
    """Склеивает абзацы карточки в описание."""
    parts = []
    for p in card.find_all("p"):
        text = clean(p.get_text(" "))
        if text == "":
            continue
        # Пропускаем приписку про "Пушкинскую карту".
        if text.lstrip("* ").startswith("Билеты на"):
            continue
        parts.append(text)

    if len(parts) == 0:
        return None
    return " ".join(parts)


def get_alex_sessions(card, date_re):
    """Возвращает словарь: дата сеанса -> ссылка на сеанс (или None)."""
    # Короткий и полный списки дат дублируют друг друга, поэтому
    # храним даты в словаре. Ссылка с ID есть не у всех сеансов.
    sessions = {}
    for li in card.find_all("li"):
        text = clean(li.get_text())
        if not date_re.match(text):
            continue
        link = li.find("a", href=re.compile("ID_EVENT_SB"))
        if link is not None:
            sessions[text] = link["href"]
        elif text not in sessions:
            sessions[text] = None  # уже найденную ссылку не затираем
    return sessions


def parse_alex(soup):
    """Александринский: в карточке список дат, на каждую дату свой Event."""
    date_re = re.compile(
        r"^(\d{1,2})\s+([а-яё]{3})\s+(\d{1,2}):(\d{2})$",
        re.I
    )  # "19 ноя 18:00"
    events = []

    # Ищем карточки по заголовку h4 со ссылкой на спектакль,
    # потому что CSS-классы карточек мы заранее не знаем.
    for h4 in soup.find_all("h4"):
        link = h4.find("a", href=True)
        if link is None or "/afisha-i-bilety/" not in link["href"]:
            continue
        title = clean(link.get_text())
        url = link["href"]

        card = find_card(h4)
        venue = get_alex_venue(h4, card, title)
        description = get_alex_description(card)
        sessions = get_alex_sessions(card, date_re)

        for text, ticket in sessions.items():
            day, month_name, hour, minute = date_re.match(text).groups()
            month = MONTHS.get(month_name.lower())
            if month is None:  # неизвестный месяц пропускаем
                continue
            starts_at = make_dt(month, int(day), int(hour), int(minute))
            if ticket:
                status = "link_available"
            else:
                status = "unknown"  # на афише нет данных о билетах
            events.append(
                Event(
                    title, starts_at, venue, description, url, ticket, status
                )
            )
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
    print_events(parse_alex(get_soup(URL)))
