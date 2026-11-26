"""
Эксперимент: парсер афиши Филармонии джазовой музыки (источник 19).

Скачивает страницу афиши, находит концерты и сохраняет их в JSON.

Запуск:
    python parser.py

Зависимости: pip install -r requirements.txt
"""
import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

URL = 'https://jazz-hall.ru/afisha/'
SITE = 'https://jazz-hall.ru'
SOURCE = 'jazz_hall_19'
# Время событий на сайте московское.
TZ = ZoneInfo('Europe/Moscow')
OUTPUT_DIR = Path('data')

# Момент, когда страница была скачана. Один на весь запуск.
COLLECTED_AT = datetime.now().astimezone().isoformat(timespec='seconds')


@dataclass
class Event:
    """Одно мероприятие (один сеанс). Общий формат для всех источников."""
    title: str
    starts_at: str
    venue: str | None
    description: str | None
    url: str
    ticket_url: str | None
    tickets: str
    source: str = SOURCE
    collected_at: str = COLLECTED_AT


HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (compatible; CulturalEventsBot/1.0; '
        '+https://example.com/bot)'
    )
}


def clean(text):
    """Убирает неразрывные пробелы, переносы и лишние пробелы."""
    if text is None:
        return ''
    text = text.replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def parse_tab_month_year(tab):
    """Месяц и год из data-date='DD.MM.YYYY' вкладки месяцев."""
    parts = (tab.get('data-date') or '').split('.')
    try:
        return int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return None, None


def get_soup(url):
    """Скачивает страницу и возвращает её как дерево HTML."""
    try:
        r = httpx.get(
            url,
            headers=HEADERS,
            timeout=15,
            follow_redirects=True,
        )
        r.raise_for_status()
        return BeautifulSoup(r.text, 'lxml')
    except httpx.RequestError as e:
        raise SystemExit(f'Не удалось загрузить страницу: {e}')


def parse_card(card, year, month):
    """Разбирает одну карточку афиши. None - если карточку пропускаем."""
    date_p = card.select_one('div.afisha__list-item-date p:first-child')
    content = card.select_one('div.afisha__list-item-content')
    title_link = card.select_one('div.afisha__list-item-content div > a')
    if not all([date_p, content, title_link]):
        return None

    day_match = re.search(r'(\d+)', clean(date_p.get_text()))
    # Первый <p> контента - время сеанса.
    time_match = re.search(r'(\d{1,2}):(\d{2})', clean(content.p.get_text()))
    if not day_match or not time_match:
        return None
    day = int(day_match.group(1))
    hour, minute = (int(x) for x in time_match.groups())

    starts_at = datetime(year, month, day, hour, minute, tzinfo=TZ)

    # Все <p> контента: первый - время, последний - зал,
    # всё между ними - описание. Порядок не съезжает, даже если
    # описания у концерта нет.
    texts = [clean(p.get_text()) for p in content.find_all('p')]
    texts = [
        t for t in texts
        if t and not re.match(r'^\d{1,2}:\d{2}$', t)
    ]
    venue = texts[-1] if texts else None
    description = ' '.join(texts[:-1]) if len(texts) > 1 else None

    # Прямой ссылки на покупку нет: кнопка - это JS-виджет profi.ticket.
    # Наличие кнопки считаем признаком доступных билетов.
    ticket_button = card.select_one('div.profticket-button')
    tickets = 'link_available' if ticket_button else 'none'

    return Event(
        title=clean(title_link.get_text()),
        starts_at=starts_at.astimezone(ZoneInfo('UTC')).isoformat(),
        venue=venue,
        description=description,
        url=urljoin(SITE, title_link['href']),
        ticket_url=None,
        tickets=tickets,
    )


def parse_jazz_hall(soup):
    """Афиша: вкладка месяца задаёт месяц и год всех карточек."""
    active_label = soup.select_one('label.afisha__tabs-label-active')
    month, year = None, None
    if active_label and active_label.parent:
        month, year = parse_tab_month_year(active_label.parent)

    now = datetime.now(tz=TZ)
    if month is None or year is None:
        # Фолбэк: если вкладка не найдена, считаем текущий месяц.
        month, year = now.month, now.year

    events = []
    for card in soup.select('div.afisha__list-item'):
        try:
            event = parse_card(card, year, month)
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            # Сломанная карточка не должна ронять весь сбор.
            print(f'[WARNING] Пропущена карточка: {e}', file=sys.stderr)
            continue
        if event is not None:
            events.append(event)
    return events


def save_events(events):
    """Сохраняет события в JSON. Пустой результат считаем ошибкой."""
    if len(events) == 0:
        raise SystemExit('Событий не найдено: возможно, изменилась вёрстка.')

    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / 'example_output.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(
            [asdict(e) for e in events],
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    print(f'[INFO] Событий: {len(events)}, файл: {output_path}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Парсер афиши Jazz Hall')
    ap.add_argument('--url', type=str, default=URL, help='URL афиши')
    args = ap.parse_args()

    save_events(parse_jazz_hall(get_soup(args.url)))
