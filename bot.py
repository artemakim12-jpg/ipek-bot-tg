import asyncio
import json
import logging
import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import quote
from calendar import monthrange

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    CallbackQuery, Message
)

TG_TOKEN = os.getenv("TG_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

TARGET_GROUP = "И-25-1"
TARGET_COURSE = "2 курс"
CLOUD_URL = "https://cloud.pilot-ipek.ru/s/7HDT8FZGBd7J5cs"

BASE_URL = "https://www.pilot-ipek.ru"
API_URL = f"{BASE_URL}/api/get_list"

CHECK_INTERVAL = 300
DAYS_AHEAD = 7
CACHE_FILE = "schedule_cache.json"

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}
MONTHS_NOM_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
MONTH_NUM_BY_NAME = {name: num for num, name in MONTHS_NOM_RU.items()}
DATES_PER_PAGE = 3
MONTHS_PER_PAGE = 6

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

last_cloud_signature = None
loop = None
user_state = {}
schedule_snapshot = {}


def load_cache():
    global schedule_snapshot, last_cloud_signature
    if not os.path.exists(CACHE_FILE):
        logger.info("Файл кэша не найден — начинаем с нуля.")
        return
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        schedule_snapshot = data.get("schedule", {})
        last_cloud_signature = data.get("cloud_signature")
        logger.info(f"Кэш загружен: {len(schedule_snapshot)} дней.")
    except Exception as e:
        logger.error(f"Ошибка загрузки кэша: {e}")


def save_cache():
    try:
        data = {"schedule": schedule_snapshot, "cloud_signature": last_cloud_signature}
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения кэша: {e}")


def get_last_day_of_month(month_name, year=None):
    if year is None:
        year = datetime.now().year
    m = MONTH_NUM_BY_NAME.get(month_name)
    return monthrange(year, m)[1] if m else 31


def extract_days_from_text(t):
    return [int(x) for x in re.findall(r"\b(\d{1,2})\b", t)]


def extract_month_from_text(t):
    tl = t.lower()
    for n, name in MONTHS_RU.items():
        if name in tl:
            return n
    return None
