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
def get_cloud_signature():
    h = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(CLOUD_URL, headers=h, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Облако недоступно: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    parts = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        n = cells[0].get_text(" ", strip=True)
        d = cells[-1].get_text(" ", strip=True)
        if n and d:
            parts.append(f"{n}|{d}")
    if not parts:
        parts.append(soup.get_text(" ", strip=True)[:2000])
    return "\n".join(parts)

def check_cloud_and_notify():
    global last_cloud_signature
    sig = get_cloud_signature()
    if not sig:
        return
    if last_cloud_signature is None:
        last_cloud_signature = sig
        save_cache()
        return
    if sig != last_cloud_signature:
        last_cloud_signature = sig
        save_cache()
        msg = (
            "🔔 Новые материалы в дистанционном обучении!\n\n"
            f"📚 Группа {TARGET_GROUP} · {TARGET_COURSE}\n"
            f"🔗 {CLOUD_URL}\n\n"
            f"Откройте: {TARGET_COURSE} → {TARGET_GROUP}"
        )
        try:
            asyncio.run_coroutine_threadsafe(
                bot.send_message(ADMIN_CHAT_ID, msg, disable_web_page_preview=True),
                loop,
            )
            logger.info("Уведомление об облаке отправлено.")
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")
    else:
        logger.info("Облако без изменений.")


def get_api_data():
    try:
        r = requests.get(API_URL, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API недоступно: {e}")
        return {}


def get_all_date_links():
    data = get_api_data()
    out = []
    for mk, dates in data.items():
        if not isinstance(dates, list):
            continue
        if mk in ("Текущий месяц", "Расписание звонков"):
            continue
        for dt in dates:
            out.append((dt, f"{BASE_URL}/raspo/{quote(dt)}"))
    return out


def get_months_with_dates():
    data = get_api_data()
    out = []
    for n in range(1, 13):
        name = MONTHS_NOM_RU[n]
        ds = data.get(name, [])
        if isinstance(ds, list) and ds:
            out.append((name, ds))
    return out


def find_link_for_date(target):
    day = target.day
    mn = MONTHS_RU[target.month]
    for text, url in get_all_date_links():
        tl = text.lower()
        if mn not in tl:
            continue
        if re.search(rf"(?<!\d)0?{day}(?!\d)", tl):
            return text, url
    return None, None


def get_latest_available_date():
    latest = None
    for dt, _ in get_all_date_links():
        days = extract_days_from_text(dt)
        mn = extract_month_from_text(dt)
        if not days or not mn:
            continue
        for d in days:
            try:
                dt2 = datetime(datetime.now().year, mn, d)
            except ValueError:
                continue
            if latest is None or dt2 > latest:
                latest = dt2
    return latest


def get_day_from_text(t):
    m = re.match(r"^(\d{1,2})", t.strip())
    return int(m.group(1)) if m else None


def filter_dates_by_range(dates, s, e):
    out = []
    for d in dates:
        day = get_day_from_text(d)
        if day is None:
            continue
        if e == 0:
            if s <= day <= 31:
                out.append(d)
        else:
            if s <= day <= e:
                out.append(d)
    out.sort(key=lambda x: get_day_from_text(x) or 0)
    return out


def get_available_ranges(month_name, dates):
    last = get_last_day_of_month(month_name)
    ranges = [
        ("1–10", filter_dates_by_range(dates, 1, 10)),
        ("11–20", filter_dates_by_range(dates, 11, 20)),
        (f"21–{last}", filter_dates_by_range(dates, 21, 0)),
    ]
    return [(l, i) for l, i in ranges if i]


def filter_by_range_label(dates, label):
    if label == "1–10":
        return filter_dates_by_range(dates, 1, 10)
    if label == "11–20":
        return filter_dates_by_range(dates, 11, 20)
    if label.startswith("21–"):
        return filter_dates_by_range(dates, 21, 0)
    return []


def clean_cell_text(cell):
    ps = cell.find_all("p")
    if not ps:
        t = cell.get_text(" ", strip=True)
        return t if t else "—"
    parts = [p.get_text(" ", strip=True) for p in ps]
    parts = [p for p in parts if p and p.strip() != "–"]
    if not parts:
        return "—"
    return " / ".join(parts)


def parse_all_tables_from_url(url):
    h = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=h, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Не открыть {url}: {e}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for w in soup.find_all("div", class_="table-wrapper"):
        table = w.find("table")
        if not table:
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        hc = rows[0].find_all(["td", "th"])
        col = None
        for i, c in enumerate(hc):
            if TARGET_GROUP in c.get_text(strip=True):
                col = i
                break
        if col is None:
            continue
        tt = w.find_previous(["p", "h1", "h2", "h3"])
        title = tt.get_text(" ", strip=True) if tt else ""
        lessons = []
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= col:
                continue
            tc = cells[0].get_text(" ", strip=True)
            if "пара" not in tc.lower():
                continue
            lessons.append((tc, clean_cell_text(cells[col])))
        if lessons:
            out.append({"title": title, "lessons": lessons})
    return out


def parse_schedule_for_date(target):
    _, url = find_link_for_date(target)
    if not url:
        return []
    tables = parse_all_tables_from_url(url)
    day = target.day
    mn = MONTHS_RU[target.month]
    flex = r"[\s\u00A0\u2009\u202F]+"
    pat = re.compile(rf"\b0?{day}{flex}{mn}{flex}\d{{4}}\b", re.IGNORECASE)
    return [t for t in tables if pat.search(t["title"])]


def parse_schedule_for_date_text(dt_text):
    days = extract_days_from_text(dt_text)
    mn = extract_month_from_text(dt_text)
    if not days or not mn:
        return []
    url = None
    for text, u in get_all_date_links():
        if text.lower() == dt_text.lower():
            url = u
            break
    if not url:
        return []
    tables = parse_all_tables_from_url(url)
    if not tables:
        return []
    flex = r"[\s\u00A0\u2009\u202F]+"
    mn_name = MONTHS_RU[mn]
    pats = [re.compile(rf"\b0?{d}{flex}{mn_name}{flex}\d{{4}}\b", re.IGNORECASE) for d in days]
    matched = []
    for t in tables:
        for p in pats:
            if p.search(t["title"]):
                matched.append(t)
                break
    return matched if matched else tables


def make_snapshot_for_day(target):
    tables = parse_schedule_for_date(target)
    if not tables:
        return []
    lessons = tables[0]["lessons"]
    result = []
    for ti, content in lessons:
        t = re.sub(r"\s+", " ", ti).strip()
        c = re.sub(r"\s+", " ", content).strip()
        result.append(f"{t}|{c}")
    return result


def parse_date_key(key):
    try:
        return datetime.strptime(key, "%d.%m.%Y")
    except ValueError:
        return None


def build_new_snapshot():
    snapshot = {}
    today = datetime.now()
    for i in range(DAYS_AHEAD):
        target = today + timedelta(days=i)
        key = target.strftime("%d.%m.%Y")
        lessons = make_snapshot_for_day(target)
        if lessons:
            snapshot[key] = lessons
    return snapshot


def format_change_message(changes):
    lines = ["⚠️ Изменения в расписании!", ""]
    for change in changes:
        lines.append(change)
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n… (обрезано)"
    return msg


def diff_snapshots(old, new):
    changes = []
    all_keys = set(old.keys()) | set(new.keys())
    for key in sorted(all_keys, key=lambda k: parse_date_key(k) or datetime.min):
        old_l = old.get(key, [])
        new_l = new.get(key, [])
        if not old_l and new_l:
            changes.append(f"📆 {key} — появилось новое расписание")
            for les in new_l:
                changes.append(f"   ✅ {les}")
            continue
        if old_l and not new_l:
            changes.append(f"📆 {key} — расписание удалено")
            continue
        old_s = set(old_l)
        new_s = set(new_l)
        if old_s == new_s:
            continue
        added = new_s - old_s
        removed = old_s - new_s
        if added or removed:
            changes.append(f"📆 {key}")
            for r in sorted(removed):
                changes.append(f"   ❌ Было: {r}")
            for a in sorted(added):
                changes.append(f"   ✅ Стало: {a}")
    return changes


def check_schedule_changes():
    global schedule_snapshot
    logger.info("Проверка расписания на изменения...")
    new_snap = build_new_snapshot()
    if not schedule_snapshot:
        schedule_snapshot = new_snap
        save_cache()
        logger.info(f"Первый слепок сохранён: {len(new_snap)} дней.")
        return
    changes = diff_snapshots(schedule_snapshot, new_snap)
    schedule_snapshot = new_snap
    save_cache()
    if not changes:
        logger.info("Изменений нет.")
        return
    logger.info(f"Найдено изменений: {len(changes)} строк. Отправляем...")
    msg = format_change_message(changes)
    try:
        asyncio.run_coroutine_threadsafe(
            bot.send_message(ADMIN_CHAT_ID, msg, disable_web_page_preview=True),
            loop,
        )
        logger.info("Уведомление об изменениях отправлено.")
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")


def format_single_table(title, lessons):
    lines = ["━━━━━━━━━━━━━━━━━", f"📅 Расписание · {TARGET_GROUP}"]
    ds = ""
    if title:
        m = re.search(r"НА\s+(.+?года\s*\([^)]+\))", title, re.IGNORECASE)
        ds = m.group(1).strip() if m else title.strip()
    if ds:
        lines.append(f"📆 {ds}")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("")
    for ti, content in lessons:
        t = re.sub(r"\s+", " ", ti).strip().replace(".", ":")
        t = re.sub(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})", r"\1 – \2", t)
        lines.append(f"🔵 {t}")
        parts = [p.strip() for p in content.split(" / ") if p.strip()]
        for i, part in enumerate(parts):
            if i == 0:
                lines.append(f"   📚 {part}")
            elif i == len(parts) - 1 and len(parts) > 1:
                lines.append(f"   🚪 {part}")
            else:
                lines.append(f"   👤 {part}")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_multiple_tables(tables):
    msg = "\n\n".join(format_single_table(t["title"], t["lessons"]) for t in tables)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n… (обрезано)"
    return msg


def format_distance_message():
    return (
        "🎓 Дистанционное обучение\n\n"
        f"📚 Материалы для группы {TARGET_GROUP} ({TARGET_COURSE})\n\n"
        f"🔗 {CLOUD_URL}\n\n"
        f"Что делать:\n"
        f"1. Нажмите «{TARGET_COURSE}»\n"
        f"2. Затем «{TARGET_GROUP}»\n"
        f"3. Внутри — файлы и задания"
    )


def get_main_keyboard():
    k = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Календарь", callback_data="open_calendar")],
        [
            InlineKeyboardButton(text="📆 Сегодня", callback_data="quick:today"),
            InlineKeyboardButton(text="📆 Завтра", callback_data="quick:tomorrow"),
        ],
        [InlineKeyboardButton(text="🎓 Дистанционное обучение", callback_data="distance")],
    ])
    return k


def get_distance_keyboard():
    k = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")],
    ])
    return k


def get_months_keyboard(months, page=1):
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    start = (page - 1) * MONTHS_PER_PAGE
    pm = months[start:start + MONTHS_PER_PAGE]
    rows = []
    cur = MONTHS_NOM_RU[datetime.now().month]
    row = []
    for name, _ in pm:
        prefix = "🔵 " if name == cur else ""
        row.append(InlineKeyboardButton(text=f"{prefix}{name}", callback_data=f"month:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅ Месяцы", callback_data=f"months_page:{page-1}"))
    if page < total:
        nav.append(InlineKeyboardButton(text="➡ Месяцы", callback_data=f"months_page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_ranges_keyboard(month_name, ranges):
    rows = []
    for label, _ in ranges:
        rows.append([InlineKeyboardButton(text=f"📅 {label} {month_name}", callback_data=f"range:{month_name}|{label}")])
    rows.append([InlineKeyboardButton(text="⬅ К месяцам", callback_data="open_calendar")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_days_keyboard(month_name, range_label, dates, page=1):
    total = (len(dates) + DATES_PER_PAGE - 1) // DATES_PER_PAGE
    start = (page - 1) * DATES_PER_PAGE
    pd = dates[start:start + DATES_PER_PAGE]
    rows = []
    for d in pd:
        rows.append([InlineKeyboardButton(text=d, callback_data=f"date:{d}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅ Назад", callback_data=f"page:{month_name}|{range_label}|{page-1}"))
    if page < total:
        nav.append(InlineKeyboardButton(text="➡ Дальше", callback_data=f"page:{month_name}|{range_label}|{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=f"📅 Диапазоны {month_name}", callback_data=f"back_ranges:{month_name}")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        f"👋 Привет! Я слежу за расписанием группы {TARGET_GROUP}.",
        reply_markup=get_main_keyboard(),
    )


@dp.callback_query(lambda c: c.data == "main_menu")
async def main_menu_cb(callback: CallbackQuery):
    await callback.message.edit_text(
        f"👋 Главное меню. Группа {TARGET_GROUP}.",
        reply_markup=get_main_keyboard(),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "open_calendar")
async def open_calendar_cb(callback: CallbackQuery):
    months = await asyncio.to_thread(get_months_with_dates)
    if not months:
        await callback.message.edit_text("📭 Нет данных с сайта.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    await callback.message.edit_text(
        f"📅 Выберите месяц (страница 1 из {total}):",
        reply_markup=get_months_keyboard(months, page=1),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("months_page:"))
async def months_page_cb(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    months = await asyncio.to_thread(get_months_with_dates)
    total = (len(months) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE
    await callback.message.edit_text(
        f"📅 Выберите месяц (страница {page} из {total}):",
        reply_markup=get_months_keyboard(months, page=page),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("month:"))
async def month_cb(callback: CallbackQuery):
    mn = callback.data.split(":", 1)[1]
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    if not isinstance(dates, list) or not dates:
        await callback.message.edit_text(f"📭 В {mn} нет расписания.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    ranges = get_available_ranges(mn, dates)
    await callback.message.edit_text(
        f"📅 {mn}. Выберите диапазон:",
        reply_markup=get_ranges_keyboard(mn, ranges),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("range:"))
async def range_cb(callback: CallbackQuery):
    payload = callback.data.split(":", 1)[1]
    mn, rl = payload.split("|", 1)
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    filtered = filter_by_range_label(dates, rl)
    if not filtered:
        await callback.message.edit_text("📭 В этом диапазоне нет дат.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    total = (len(filtered) + DATES_PER_PAGE - 1) // DATES_PER_PAGE
    await callback.message.edit_text(
        f"📅 {mn}, {rl} — страница 1 из {total}.\nВыберите день:",
        reply_markup=get_days_keyboard(mn, rl, filtered, page=1),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("page:"))
async def page_cb(callback: CallbackQuery):
    payload = callback.data.split(":", 1)[1]
    mn, rl, page = payload.split("|")
    page = int(page)
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    filtered = filter_by_range_label(dates, rl)
    total = (len(filtered) + DATES_PER_PAGE - 1) // DATES_PER_PAGE
    await callback.message.edit_text(
        f"📅 {mn}, {rl} — страница {page} из {total}.\nВыберите день:",
        reply_markup=get_days_keyboard(mn, rl, filtered, page=page),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("back_ranges:"))
async def back_ranges_cb(callback: CallbackQuery):
    mn = callback.data.split(":", 1)[1]
    data = await asyncio.to_thread(get_api_data)
    dates = data.get(mn, [])
    ranges = get_available_ranges(mn, dates)
    await callback.message.edit_text(
        f"📅 {mn}. Выберите диапазон:",
        reply_markup=get_ranges_keyboard(mn, ranges),
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("date:"))
async def date_cb(callback: CallbackQuery):
    dt = callback.data.split(":", 1)[1]
    await callback.answer("Загружаю…")
    tables = await asyncio.to_thread(parse_schedule_for_date_text, dt)
    if not tables:
        await callback.message.answer(f"📭 На «{dt}» нет расписания.", reply_markup=get_main_keyboard())
        return
    await callback.message.answer(format_multiple_tables(tables), reply_markup=get_main_keyboard())


@dp.callback_query(lambda c: c.data == "quick:today")
async def today_cb(callback: CallbackQuery):
    target = datetime.now()
    tables = await asyncio.to_thread(parse_schedule_for_date, target)
    if not tables:
        await callback.message.answer("📭 На сегодня нет расписания.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    await callback.message.answer(format_multiple_tables(tables), reply_markup=get_main_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data == "quick:tomorrow")
async def tomorrow_cb(callback: CallbackQuery):
    target = datetime.now() + timedelta(days=1)
    tables = await asyncio.to_thread(parse_schedule_for_date, target)
    if not tables:
        await callback.message.answer("📭 На завтра нет расписания.", reply_markup=get_main_keyboard())
        await callback.answer()
        return
    await callback.message.answer(format_multiple_tables(tables), reply_markup=get_main_keyboard())
    await callback.answer()


@dp.callback_query(lambda c: c.data == "distance")
async def distance_cb(callback: CallbackQuery):
    await callback.message.edit_text(format_distance_message(), reply_markup=get_distance_keyboard())
    await callback.answer()


async def scheduler_loop():
    while True:
        try:
            await asyncio.to_thread(check_schedule_changes)
        except Exception as e:
            logger.error(f"Ошибка проверки расписания: {e}")
        try:
            await asyncio.to_thread(check_cloud_and_notify)
        except Exception as e:
            logger.error(f"Ошибка проверки облака: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


async def main():
    global loop
    loop = asyncio.get_running_loop()
    load_cache()
    asyncio.create_task(scheduler_loop())
    logger.info(f"TG-бот запущен. Группа {TARGET_GROUP}.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())    
    
