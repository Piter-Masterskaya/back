"""
Эксперимент: парсер афиши Базилики Св. Екатерины (источник 18).

Особенность источника: расписание публикуется картинками (постерами),
текстового списка событий на странице нет.

Стратегия прототипа: следим за самими постерами. Новый постер или
изменённая картинка (по SHA-256) - сигнал, что расписание изменилось.
Извлечение текста постеров (OCR) - следующий шаг.

Запуск:
    python parser.py

Зависимости: pip install -r requirements.txt
"""
import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

URL = 'https://bazilika-catherine.ru/concerts/'
SITE = 'https://bazilika-catherine.ru'
SOURCE = 'bazilika_catherine_18'
VENUE = 'Базилика Св. Екатерины Александрийской'

OUTPUT_DIR = Path('data')
POSTERS_DIR = OUTPUT_DIR / 'posters'

# Постеры афиши высокие (910px), баннеры партнёров - 200px.
MIN_POSTER_HEIGHT = 400

# Момент, когда страница была скачана. Один на весь запуск.
COLLECTED_AT = datetime.now().astimezone().isoformat(timespec='seconds')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (compatible; CulturalEventsBot/1.0; '
        '+https://example.com/bot)'
    )
}


@dataclass
class Poster:
    """Один постер с расписанием. Единица события - постер, а не сеанс."""
    image_url: str
    image_hash: str          # SHA-256 содержимого - id постера для сравнения
    venue: str
    url: str                 # страница-источник постера
    ticket_url: str | None
    tickets: str             # у постера статус билетов неизвестен
    source: str = SOURCE
    collected_at: str = COLLECTED_AT


def section_widgets(soup):
    """
    Виджеты секции КОНЦЕРТЫ.

    Порядок секций на странице фиксирован: заголовок КОНЦЕРТЫ ->
    постеры -> заголовок ЭКСКУРСИИ -> фото храма. Секция афиши -
    это срез виджетов между двумя заголовками: фото храма и
    баннеры партнёров в него не попадают.
    """
    widgets = soup.select('div.ul-widget')

    def index_by_text(text):
        return next(
            (i for i, w in enumerate(widgets) if text in w.get_text()),
            None,
        )

    start = index_by_text('КОНЦЕРТЫ')
    if start is None:
        return []
    end = index_by_text('ЭКСКУРСИИ')
    return widgets[start:end]


def is_poster(widget):
    """Постер: виджет uImage с высокой картинкой и оригинальным <img src>."""
    if widget.get('type') != 'uImage':
        return False
    box = widget.select_one('div[height]')
    img = widget.select_one('img.ul-image__image')
    if not box or not img or not img.get('src'):
        return False
    height = box.get('height', '')
    return height.isdigit() and int(height) >= MIN_POSTER_HEIGHT


def find_posters(soup):
    """
    Находит постеры афиши.

    Берём именно <img src> (оригинал без масштабирования), а не
    srcset: у него стабильное имя файла и стабильный хэш.
    """
    urls = [
        urljoin(SITE, widget.select_one('img.ul-image__image')['src'])
        for widget in section_widgets(soup)
        if is_poster(widget)
    ]
    # dict.fromkeys убирает дубликаты, сохраняя порядок
    return list(dict.fromkeys(urls))


def download_poster(url):
    """Скачивает картинку и возвращает её SHA-256 и содержимое."""
    r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
    r.raise_for_status()
    digest = hashlib.sha256(r.content).hexdigest()
    return digest, r.content


def collect_posters(soup):
    """Скачивает постеры, сохраняет их и возвращает список записей."""
    POSTERS_DIR.mkdir(parents=True, exist_ok=True)

    posters = []
    for image_url in find_posters(soup):
        try:
            digest, content = download_poster(image_url)
        except httpx.RequestError as e:
            # Один неудачный постер не должен ломать весь сбор.
            print(f'[WARNING] Постер не скачался: {e}', file=sys.stderr)
            continue

        # Хэш в имени файла: при замене картинки появится новый файл,
        # старый останется на диске как история изменений.
        ext = image_url.rsplit('.', 1)[-1].lower()[:4]
        path = POSTERS_DIR / f'{digest[:12]}.{ext}'
        if not path.exists():
            path.write_bytes(content)

        posters.append(Poster(
            image_url=image_url,
            image_hash=f'sha256:{digest}',
            venue=VENUE,
            url=URL,
            ticket_url=None,
            tickets='unknown',
        ))

    return posters


def save_posters(posters):
    """Сохраняет постеры в JSON. Пустой результат считаем ошибкой."""
    if len(posters) == 0:
        raise SystemExit('Постеры не найдены: возможно, изменилась вёрстка.')

    output_path = OUTPUT_DIR / 'example_output.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(
            [asdict(p) for p in posters],
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f'[INFO] Постеров: {len(posters)}, файл: {output_path}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Парсер афиши Базилики Св. Екатерины'
    )
    ap.add_argument('--url', type=str, default=URL, help='URL афиши')
    args = ap.parse_args()

    try:
        r = httpx.get(
            args.url,
            headers=HEADERS,
            timeout=15,
            follow_redirects=True,
        )
        r.raise_for_status()
    except httpx.RequestError as e:
        raise SystemExit(f'Не удалось загрузить страницу: {e}')

    save_posters(collect_posters(BeautifulSoup(r.text, 'lxml')))
