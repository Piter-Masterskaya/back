"""
Локальный эксперимент: OCR постеров афиши Базилики (источник 18).

Запускается ТОЛЬКО локально на Windows, где установлен Tesseract
(установщик UB Mannheim, путь по умолчанию C:\\Program Files\\Tesseract-OCR).
В DevContainer/CI этот скрипт не запускается: там Tesseract ставится
отдельно через apt-get и этот fallback не нужен.

Цель эксперимента: посмотреть глазами, что Tesseract вытаскивает
из постеров, и решить, есть ли смысл в извлечении дат и названий.

Запуск:
    python ocr_experiment.py
"""
import shutil
from pathlib import Path

import pytesseract
from PIL import Image, ImageOps

POSTERS_DIR = Path('data/posters')

# Постеры у Базилики смешанные: русские названия плюс даты,
# иногда латиница. rus+eng покрывает оба случая.
LANG = 'rus+eng'


if shutil.which('tesseract') is None:
    # Fallback для локальной Windows, где Tesseract не в PATH.
    pytesseract.pytesseract.tesseract_cmd = (
        r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    )


def load_grayscale(path):
    """
    Готовит картинку к распознаванию: переводит в ч/б.

    Текст на постерах часто светлый на тёмном фоне - OCR лучше
    работает с тёмным текстом на светлом, поэтому инвертируем.
    """
    img = Image.open(path)
    gray = ImageOps.grayscale(img)

    if gray.getpixel((5, 5)) < 128:
        gray = ImageOps.invert(gray)
    return gray


def main():
    for path in sorted(POSTERS_DIR.glob('*')):
        print(f'=== {path.name} ===')
        text = pytesseract.image_to_string(
            load_grayscale(path),
            lang=LANG,
            config='--psm 6',
        )
        print(text.strip())
        print()


if __name__ == '__main__':
    main()
