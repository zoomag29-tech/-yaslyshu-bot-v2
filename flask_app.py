import random, time, asyncio, requests, re, traceback, sqlite3, os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from flask import Flask, request
from aiogram.types import Update
from openai import OpenAI
import httpx
import hashlib

# =======================================================
# КОНФИГУРАЦИЯ
# =======================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "726250140"))
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
CONTACT = "@MARGOKARDATOVA"

TERMINAL_KEY = os.environ.get("TERMINAL_KEY")
TERMINAL_PASSWORD = os.environ.get("TERMINAL_PASSWORD")
RECEIPT_EMAIL = os.environ.get("RECEIPT_EMAIL", "no-reply@example.com")

if not BOT_TOKEN or not DEEPSEEK_API_KEY or not TERMINAL_KEY or not TERMINAL_PASSWORD:
    raise RuntimeError("Не заданы обязательные переменные окружения")

# =======================================================
# DEEPSEEK
# =======================================================
http_client = httpx.Client(trust_env=False)
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com", http_client=http_client)

# =======================================================
# БАЗА ДАННЫХ (SQLite)
# =======================================================
DB_PATH = 'yaslyshu.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Пользователи
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, 
                  subscription_plan TEXT, 
                  expires_at INTEGER,
                  reminder_time TEXT DEFAULT '09:30',
                  reminder_enabled INTEGER DEFAULT 1,
                  trial_used INTEGER DEFAULT 0,
                  gender TEXT,
                  created_at INTEGER)''')
    for col in ["trial_used", "gender", "created_at"]:
        try:
            if col == "trial_used":
                c.execute("ALTER TABLE users ADD COLUMN trial_used INTEGER DEFAULT 0")
            elif col == "gender":
                c.execute("ALTER TABLE users ADD COLUMN gender TEXT")
            else:
                c.execute("ALTER TABLE users ADD COLUMN created_at INTEGER")
        except sqlite3.OperationalError:
            pass
    # Платежи
    c.execute('''CREATE TABLE IF NOT EXISTS payments
                 (order_id TEXT PRIMARY KEY,
                  user_id INTEGER,
                  plan TEXT,
                  duration_days INTEGER,
                  amount INTEGER,
                  created_at INTEGER,
                  status TEXT DEFAULT 'pending')''')
    # Дневник (расширенный)
    c.execute('''CREATE TABLE IF NOT EXISTS diary_entries
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  date TEXT,
                  emotion TEXT,
                  emotion_group TEXT,
                  intensity INTEGER,
                  reason TEXT)''')
    # Практики
    c.execute('''CREATE TABLE IF NOT EXISTS mindfulness_log
                 (user_id INTEGER, date TEXT, practice_id INTEGER, feedback TEXT)''')
    # Прогресс школы
    c.execute('''CREATE TABLE IF NOT EXISTS school_progress
                 (user_id INTEGER,
                  module_id INTEGER,
                  lesson_id INTEGER,
                  completed INTEGER DEFAULT 0,
                  PRIMARY KEY (user_id, module_id, lesson_id))''')
    conn.commit()
    conn.close()

init_db()

# =======================================================
# БОТ И ДИСПЕТЧЕР
# =======================================================
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# =======================================================
# FSM
# =======================================================
class EmotionStates(StatesGroup):
    waiting_for_situation = State()
    waiting_for_followup = State()

class DiaryStates(StatesGroup):
    waiting_for_group = State()
    waiting_for_emotion = State()
    waiting_for_intensity = State()
    waiting_for_reason = State()

class MindfulnessStates(StatesGroup):
    waiting_for_feedback = State()

class ReminderStates(StatesGroup):
    waiting_for_time = State()

class GenderState(StatesGroup):
    waiting_for_gender = State()

class SchoolStates(StatesGroup):
    viewing_modules = State()
    viewing_lessons = State()
    doing_task = State()

# =======================================================
# УТИЛИТЫ
# =======================================================
def call_deepseek(prompt: str) -> str:
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=1500
        )
        text = response.choices[0].message.content.strip()
        text = re.sub(r'[*_~`]', '', text)
        return text
    except Exception as e:
        print(f"❌ DeepSeek error: {e}")
        return "Извините, сейчас сервис временно недоступен. Попробуйте позже."

def ensure_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if c.fetchone() is None:
        c.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (user_id, int(time.time())))
        conn.commit()
    conn.close()

def get_user_gender(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else None

def set_user_gender(user_id, gender):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET gender = ? WHERE user_id = ?", (gender, user_id))
    conn.commit()
    conn.close()

def create_tbank_payment(amount, description, user_id, plan, duration_days):
    if "DEMO" in TERMINAL_KEY:
        url = "https://rest-api-test.tinkoff.ru/v2/Init"
    else:
        url = "https://securepay.tinkoff.ru/v2/Init"
    amount_kop = amount * 100
    order_id = f"order_{user_id}_{int(time.time())}"
    payload = {
        "TerminalKey": TERMINAL_KEY,
        "Amount": amount_kop,
        "OrderId": order_id,
        "Description": description,
        "NotificationURL": "https://yaslyshu-bot-v2.onrender.com/payment_webhook",
        "SuccessURL": "https://t.me/yaslyshu_bot",
        "FailURL": "https://t.me/yaslyshu_bot"
    }
    token_payload = payload.copy()
    token_payload["Password"] = TERMINAL_PASSWORD
    sorted_keys = sorted(token_payload.keys())
    token_str = ''.join(str(token_payload[k]) for k in sorted_keys)
    token = hashlib.sha256(token_str.encode('utf-8')).hexdigest()
    payload["Token"] = token
    payload["Receipt"] = {
        "Taxation": "usn_income",
        "Email": RECEIPT_EMAIL,
        "Items": [{"Name": description, "Price": amount_kop, "Quantity": 1, "Amount": amount_kop, "Tax": "none"}]
    }
    try:
        response = requests.post(url, json=payload, timeout=10, verify=False)
        response.raise_for_status()
        data = response.json()
        if data.get("Success"):
            save_payment(order_id, user_id, plan, duration_days, amount_kop)
            return data["PaymentURL"], data["PaymentId"], None
        else:
            error_text = f"Т-Банк: {data.get('ErrorCode', '')} {data.get('Message', '')} {data.get('Details', '')}"
            error_text += f"\n\nОтправленный JSON: {payload}"
            print(f"❌ Т-Банк ответил: {data}")
            return None, None, error_text
    except Exception as e:
        error_text = f"Ошибка запроса: {e}"
        print(f"❌ Ошибка Т-Банка: {e}")
        return None, None, error_text

def save_payment(order_id, user_id, plan, duration_days, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO payments (order_id, user_id, plan, duration_days, amount, created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
              (order_id, user_id, plan, duration_days, amount, int(time.time())))
    conn.commit()
    conn.close()

def get_payment(order_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, plan, duration_days, status FROM payments WHERE order_id = ?", (order_id,))
    row = c.fetchone()
    conn.close()
    return row

def update_payment_status(order_id, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE payments SET status = ? WHERE order_id = ?", (status, order_id))
    conn.commit()
    conn.close()

def get_user_subscription(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT subscription_plan, expires_at FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row if row else (None, 0)

def update_subscription(user_id, plan, duration_days):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    expires_at = int(time.time()) + duration_days * 86400
    c.execute("UPDATE users SET subscription_plan = ?, expires_at = ? WHERE user_id = ?",
              (plan, expires_at, user_id))
    conn.commit()
    conn.close()

def give_trial(user_id):
    ensure_user(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT trial_used FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row and row[0] == 1:
        conn.close()
        return False
    expires_at = int(time.time()) + 1 * 86400
    c.execute("UPDATE users SET subscription_plan='trial', expires_at=?, trial_used=1 WHERE user_id = ?",
              (expires_at, user_id))
    conn.commit()
    conn.close()
    return True

def check_subscription(user_id):
    plan, expires_at = get_user_subscription(user_id)
    if plan is None:
        return False, None, 0, 0
    now = int(time.time())
    days_left = max(0, (expires_at - now) // 86400)
    return now < expires_at, plan, expires_at, days_left

async def ensure_subscription(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id == ADMIN_ID:
        return True
    active, plan, expires, days_left = check_subscription(user_id)
    if active:
        return True
    if give_trial(user_id):
        await callback.message.answer("✅ Тебе активирован пробный доступ на 24 часа. Теперь ты можешь пользоваться всеми функциями бота!")
        return True
    else:
        await callback.message.answer("⚠️ Пробный период уже был использован. Пожалуйста, оформи подписку в разделе 💎 Подписка.")
        return False

# =======================================================
# ЭМОЦИИ
# =======================================================
EMOTION_GROUPS = {
    "😊 Радость и позитивные": ["Радость", "Счастье", "Благодарность", "Гордость", "Надежда", "Восторг", "Умиротворение", "Любовь", "Вдохновение", "Интерес", "Удовлетворение", "Нежность", "Восхищение", "Ликование", "Спокойствие", "Облегчение", "Уверенность", "Энтузиазм", "Тепло", "Симпатия", "Признательность", "Доверие", "Радость общения", "Оптимизм"],
    "😢 Грусть и подавленность": ["Грусть", "Печаль", "Одиночество", "Разочарование", "Тоска", "Сожаление", "Уныние", "Безысходность", "Подавленность", "Скука", "Опустошённость", "Меланхолия", "Скорбь", "Жалость", "Разбитость", "Ностальгия", "Утрата", "Беспомощность", "Отчаяние", "Усталость"],
    "😠 Злость и раздражение": ["Раздражение", "Злость", "Гнев", "Обида", "Зависть", "Возмущение", "Ярость", "Недовольство", "Враждебность", "Досада", "Ревность", "Бешенство", "Нетерпение", "Презрение", "Отвращение", "Агрессия", "Фрустрация"],
    "😨 Страх и тревога": ["Тревога", "Страх", "Паника", "Неуверенность", "Беспокойство", "Ужас", "Испуг", "Нервозность", "Опасение", "Растерянность", "Застенчивость", "Напряжение", "Волнение", "Сомнение", "Скованность", "Боязнь", "Фобия", "Осторожность"],
    "😞 Стыд, вина, самооценка": ["Стыд", "Вина", "Смущение", "Унижение", "Раскаяние", "Самоедство", "Неловкость", "Сожаление о себе", "Самокритика", "Чувство неполноценности"],
    "🤢 Отвращение и неприятие": ["Отвращение", "Неприязнь", "Омерзение", "Отторжение", "Брезгливость"],
    "😲 Удивление и любопытство": ["Удивление", "Изумление", "Любопытство", "Растерянность", "Потрясение", "Внезапность", "Озадаченность", "Заинтригованность"],
    "💞 Сложные и смешанные": ["Амбивалентность", "Любовь-ненависть", "Трепет", "Восторг со страхом", "Облегчение с грустью", "Ревность с любовью", "Смущение с радостью", "Вина с облегчением", "Ностальгия с теплом", "Скука с тревогой"]
}

# =======================================================
# ШКОЛА ЭМОЦИЙ
# =======================================================
SCHOOL_MODULES = [
    {"id":1, "title":"Что такое эмоциональный интеллект?", "description":"Введение в EQ, базовые эмоции, знакомство с инструментами бота.", "lessons":["Эмоциональный интеллект — это способность понимать свои и чужие эмоции и управлять ими.\n\nИз чего состоит EQ:\n- Самосознание\n- Самоконтроль\n- Эмпатия\n- Навыки общения", "Базовые эмоции: радость, грусть, злость, страх, отвращение, удивление, доверие.\n\nЭти эмоции есть у всех людей, они помогают нам реагировать на мир.", "Инструменты бота:\n\n📓 Дневник эмоций — чтобы фиксировать и анализировать свои состояния.\n🧘 Mindfulness — чтобы успокаивать ум и снижать интенсивность эмоций.\n💬 Поговорим — чтобы не копить эмоции, а разбирать их в диалоге.", "Задание: выбери одну базовую эмоцию и запиши её в дневник с интенсивностью от 1 до 10."], "task_type":"diary", "task_practice_id":None},
    {"id":2, "title":"Самосознание", "description":"Научись замечать и называть свои эмоции.", "lessons":["Самосознание — это умение замечать свои эмоции в момент их появления.\n\nНаблюдай за телом: как физически проявляется злость или страх?", "Называй эмоции словами: «я сейчас тревожусь» или «я устал», а не просто «мне плохо».", "Задание: в течение 3 дней записывай по одной эмоции в дневник с интенсивностью и причиной."], "task_type":"diary", "task_practice_id":None},
    {"id":3, "title":"Радость и позитивные состояния", "description":"Расширь палитру положительных эмоций, научись их замечать.", "lessons":["Позитивные эмоции — не просто приятные состояния. Они укрепляют здоровье, отношения и устойчивость.", "Учись закреплять радость: замечай её, называй, благодари.", "Задание: запиши 3 положительные эмоции за неделю в дневник."], "task_type":"diary", "task_practice_id":None},
    {"id":4, "title":"Грусть и подавленные состояния", "description":"Научись принимать грусть, не бояться её.", "lessons":["Грусть — это нормально. Она помогает переработать потери и сигнализирует о потребности в заботе.", "Не подавляй грусть, а дай ей место. Плач может быть исцеляющим.", "Задание: запиши ситуацию, вызвавшую грусть, и разбери её в «Поговорим» или дневнике."], "task_type":"diary", "task_practice_id":None},
    {"id":5, "title":"Злость и раздражение", "description":"Научись экологично выражать злость.", "lessons":["Злость — это энергия, которая защищает границы. Важно не подавлять её, а выражать безопасно.", "Дыхательные практики помогают снизить накал злости и вернуть контроль.", "Задание: выполни дыхательную практику «Дыхание 4-4-4» через Mindfulness, затем запиши свою злость в дневник."], "task_type":"mindfulness", "task_practice_id":1},
    {"id":6, "title":"Страх и тревога", "description":"Научись распознавать тревогу и снижать её.", "lessons":["Страх — реакция на реальную угрозу, тревога — на воображаемую. Их можно успокоить через тело.", "Практики заземления и дыхания — первая помощь при тревоге.", "Задание: запиши тревожную ситуацию + выполни mindfulness."], "task_type":"mindfulness", "task_practice_id":2},
    {"id":7, "title":"Стыд, вина и самооценка", "description":"Проработай сложные чувства, связанные с самооценкой.", "lessons":["Стыд — «я плохой», вина — «я сделал плохо». Важно разделять их.", "Практика самосострадания: относись к себе как к другу.", "Задание: запиши ситуацию стыда/вины и разбери её в «Поговорим»."], "task_type":"diary", "task_practice_id":None},
    {"id":8, "title":"Отвращение, удивление и сложные состояния", "description":"Охвати оставшиеся базовые и смешанные эмоции.", "lessons":["Эмоции могут смешиваться: любовь-ненависть, грусть-радость. Это нормально.", "Расширяй словарь эмоций, чтобы точнее понимать себя.", "Задание: найди и запиши смешанную эмоцию из своей жизни."], "task_type":"diary", "task_practice_id":None},
    {"id":9, "title":"Интеграция и навыки общения", "description":"Примени всё в отношениях.", "lessons":["Эмпатия — это умение поставить себя на место другого и понять его чувства.", "Выражай чувства конструктивно: «Я-сообщения» вместо обвинений.", "Задание: проведи диалог в «Поговорим» на тему сложного общения и подведи итоги."], "task_type":"talk", "task_practice_id":None}
]

# =======================================================
# КЛАВИАТУРЫ
# =======================================================
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎓 EQ · Школа эмоций", callback_data="school_modules")
    builder.button(text="💬 Поговорим", callback_data="start_training")
    builder.button(text="📓 Дневник эмоций", callback_data="diary")
    builder.button(text="🧘 Mindfulness", callback_data="mindfulness_menu")
    builder.button(text="📊 Мой прогресс", callback_data="my_progress")
    builder.button(text="💎 Подписка", callback_data="subscribe_menu")
    builder.button(text="📞 Поддержка", callback_data="support")
    builder.adjust(1, 2, 2, 2)
    return builder.as_markup()

def mindfulness_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📖 Что это такое?", callback_data="mindfulness_about")
    builder.button(text="🌿 Выполнить практику", callback_data="mindfulness_today")
    builder.button(text="⏰ Настроить время напоминания", callback_data="reminder_settings")
    builder.button(text="🔕 Отключить напоминания", callback_data="reminder_off")
    builder.button(text="⬅ Назад", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def subscription_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Неделя — 80 ₽", callback_data="sub_week")
    builder.button(text="📅 Месяц — 180 ₽", callback_data="sub_month")
    builder.button(text="📅 Год — 1800 ₽", callback_data="sub_year")
    builder.button(text="⬅ Назад", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def gender_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Я девушка", callback_data="gender_female")
    builder.button(text="Я парень", callback_data="gender_male")
    builder.adjust(2)
    return builder.as_markup()

def emotion_group_keyboard():
    builder = InlineKeyboardBuilder()
    for group in EMOTION_GROUPS.keys():
        builder.button(text=group, callback_data=f"group:{group}")
    builder.button(text="⬅ Назад", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def emotion_list_keyboard(group):
    builder = InlineKeyboardBuilder()
    for emotion in EMOTION_GROUPS[group]:
        builder.button(text=emotion, callback_data=f"emotion:{emotion}")
    builder.button(text="✏️ Другая эмоция", callback_data="emotion_custom")
    builder.button(text="⬅ Назад", callback_data="diary_groups")
    builder.adjust(2)
    return builder.as_markup()

def school_modules_keyboard(completed_modules):
    builder = InlineKeyboardBuilder()
    for module in SCHOOL_MODULES:
        status = "✅" if module["id"] in completed_modules else "🔒" if not is_module_unlocked(module["id"], completed_modules) else "📖"
        builder.button(text=f"{status} {module['title']}", callback_data=f"module:{module['id']}")
    builder.button(text="⬅ Назад", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def is_module_unlocked(module_id, completed_modules):
    if module_id == 1:
        return True
    return (module_id - 1) in completed_modules

# =======================================================
# ОБРАБОТЧИКИ
# =======================================================
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    ensure_user(user_id)
    if user_id == ADMIN_ID:
        await message.answer("👋 Привет, Маргарита! Ты администратор, тебе доступны все функции без ограничений.")
        await message.answer("Главное меню:", reply_markup=main_menu_keyboard())
        return
    gender = get_user_gender(user_id)
    if gender is None:
        await message.answer("🌸 Привет! Добро пожаловать в «я слышу».\n\nЧтобы я могла обращаться к тебе правильно, скажи: ты девушка или парень?", reply_markup=gender_keyboard())
        await state.set_state(GenderState.waiting_for_gender)
    else:
        await show_main_menu(message, user_id)

async def show_main_menu(message, user_id):
    active, plan, expires, days_left = check_subscription(user_id)
    if active:
        text = f"👋 Привет! Ты уже с нами.\nТвой тариф: {plan}\nОсталось дней: {days_left}"
    else:
        text = "👋 Привет! Я — «я слышу». Твой персональный наставник по эмоциональному интеллекту и осознанности.\n\n📓 Веди дневник эмоций.\n🧘 Каждый день выполняй mindfulness-практику.\n📊 Отслеживай свой прогресс.\n\nНачни бесплатный пробный период прямо сейчас — нажми любую кнопку."
    await message.answer(text, reply_markup=main_menu_keyboard())

@dp.callback_query(GenderState.waiting_for_gender, F.data.startswith("gender_"))
async def process_gender(callback: types.CallbackQuery, state: FSMContext):
    gender = callback.data.split("_")[1]
    user_id = callback.from_user.id
    set_user_gender(user_id, gender)
    await state.clear()
    await callback.message.answer("Спасибо! Теперь я буду обращаться к тебе правильно 😊")
    await show_main_menu(callback.message, user_id)
    await callback.answer()

@dp.message(Command("stats"))
async def stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("У тебя нет прав для этой команды.")
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE expires_at > ? AND subscription_plan != 'trial'", (int(time.time()),))
    active_paid = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE expires_at > ? AND subscription_plan = 'trial'", (int(time.time()),))
    active_trials = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM diary_entries")
    diary_entries = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM mindfulness_log")
    mindfulness_logs = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE reminder_enabled = 1")
    reminders_on = c.fetchone()[0]
    seven_days_ago = int(time.time()) - 7 * 86400
    c.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (seven_days_ago,))
    new_users_7d = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM payments WHERE status = 'paid'")
    paid_count = c.fetchone()[0]
    conn.close()
    text = f"📊 Статистика бота «я слышу»\n\n" \
           f"👥 Всего пользователей: {total_users}\n" \
           f"🆕 Новых за 7 дней: {new_users_7d}\n" \
           f"✅ Активные подписки: {active_paid}\n" \
           f"🕐 Активные триалы: {active_trials}\n" \
           f"💰 Успешных платежей: {paid_count}\n" \
           f"📓 Записей в дневнике: {diary_entries}\n" \
           f"🧘 Выполнено практик: {mindfulness_logs}\n" \
           f"⏰ Включены напоминания: {reminders_on}"
    await message.answer(text)

# Ручная выдача подписки
@dp.message(Command("grant_week"))
async def grant_week(message: types.Message):
    await grant_subscription(message, 7, "week")
@dp.message(Command("grant_month"))
async def grant_month(message: types.Message):
    await grant_subscription(message, 30, "month")
@dp.message(Command("grant_year"))
async def grant_year(message: types.Message):
    await grant_subscription(message, 365, "year")

async def grant_subscription(message: types.Message, duration_days: int, plan: str):
    if message.from_user.id != ADMIN_ID:
        await message.answer("У тебя нет прав для этой команды.")
        return
    args = message.text.split()
    target_user_id = int(args[1]) if len(args) > 1 else message.from_user.id
    update_subscription(target_user_id, plan, duration_days)
    end_date = datetime.fromtimestamp(int(time.time()) + duration_days * 86400).strftime("%d.%m.%Y")
    await message.answer(f"✅ Подписка «{plan}» активирована для пользователя {target_user_id} до {end_date}.")

# Школа эмоций
@dp.callback_query(F.data == "school_modules")
async def show_school_modules(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT module_id FROM school_progress WHERE user_id = ? AND completed = 1 AND lesson_id = 999", (user_id,))
    rows = c.fetchall()
    conn.close()
    completed = set(row[0] for row in rows)
    await callback.message.edit_text("🎓 EQ · Школа эмоций\n\nВыбери модуль:", reply_markup=school_modules_keyboard(completed))
    await callback.answer()

@dp.callback_query(F.data.startswith("module:"))
async def show_module_lessons(callback: types.CallbackQuery, state: FSMContext):
    module_id = int(callback.data.split(":")[1])
    module = next(m for m in SCHOOL_MODULES if m["id"] == module_id)
    user_id = callback.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT module_id FROM school_progress WHERE user_id = ? AND completed = 1 AND lesson_id = 999", (user_id,))
    rows = c.fetchall()
    conn.close()
    completed = set(row[0] for row in rows)
    if not is_module_unlocked(module_id, completed):
        await callback.answer("Этот модуль ещё не открыт. Пройди предыдущие.", show_alert=True)
        return
    await state.update_data(current_module_id=module_id, current_lesson=0)
    lesson = module["lessons"][0]
    await callback.message.edit_text(f"📖 Модуль {module_id}: {module['title']}\n\n{lesson}\n\nНажми «Далее» для продолжения.",
                                     reply_markup=InlineKeyboardBuilder().button(text="Далее ➡️", callback_data="next_lesson").as_markup())
    await callback.answer()

@dp.callback_query(F.data == "next_lesson")
async def next_lesson(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    module_id = data.get("current_module_id")
    current_lesson = data.get("current_lesson", 0)
    module = next(m for m in SCHOOL_MODULES if m["id"] == module_id)
    if current_lesson + 1 < len(module["lessons"]):
        await state.update_data(current_lesson=current_lesson + 1)
        lesson = module["lessons"][current_lesson + 1]
        await callback.message.edit_text(f"📖 Модуль {module_id}: {module['title']}\n\n{lesson}\n\nНажми «Далее» или «Завершить» после последнего урока.",
                                         reply_markup=InlineKeyboardBuilder().button(text="Далее ➡️", callback_data="next_lesson").as_markup())
    else:
        await show_task(callback, state, module)
    await callback.answer()

async def show_task(callback, state, module):
    task_type = module["task_type"]
    if task_type == "diary":
        await callback.message.edit_text(f"🎯 Задание: {module['lessons'][-1]}\n\nПерейди в дневник и запиши эмоцию, затем возвращайся и нажми «Готово».",
                                         reply_markup=InlineKeyboardBuilder().button(text="📓 Перейти к дневнику", callback_data="diary_task").button(text="✅ Готово, продолжить", callback_data="task_done").as_markup())
    elif task_type == "mindfulness":
        await callback.message.edit_text(f"🎯 Задание: {module['lessons'][-1]}\n\nПерейди в Mindfulness и выполни указанную практику, затем возвращайся и нажми «Готово».",
                                         reply_markup=InlineKeyboardBuilder().button(text="🧘 Перейти к практике", callback_data=f"mindfulness_task:{module['task_practice_id']}").button(text="✅ Готово, продолжить", callback_data="task_done").as_markup())
    elif task_type == "talk":
        await callback.message.edit_text(f"🎯 Задание: {module['lessons'][-1]}\n\nПерейди в «Поговорим» и обсуди тему, затем возвращайся и нажми «Готово».",
                                         reply_markup=InlineKeyboardBuilder().button(text="💬 Перейти к разговору", callback_data="talk_task").button(text="✅ Готово, продолжить", callback_data="task_done").as_markup())
    await callback.answer()

@dp.callback_query(F.data == "diary_task")
async def go_to_diary_from_school(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Открой дневник и запиши эмоцию. После возвращайся и нажми «Готово».")
    await diary_start(callback, state)

@dp.callback_query(F.data.startswith("mindfulness_task:"))
async def go_to_mindfulness_from_school(callback: types.CallbackQuery, state: FSMContext):
    practice_id = int(callback.data.split(":")[1])
    await mindfulness_today(callback, state, specific_practice_id=practice_id)

@dp.callback_query(F.data == "talk_task")
async def go_to_talk_from_school(callback: types.CallbackQuery, state: FSMContext):
    await start_training(callback, state)

@dp.callback_query(F.data == "task_done")
async def complete_module(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    module_id = data.get("current_module_id")
    user_id = callback.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    module = next(m for m in SCHOOL_MODULES if m["id"] == module_id)
    for lesson_id in range(len(module["lessons"])):
        c.execute("INSERT OR REPLACE INTO school_progress (user_id, module_id, lesson_id, completed) VALUES (?, ?, ?, 1)",
                  (user_id, module_id, lesson_id))
    c.execute("INSERT OR REPLACE INTO school_progress (user_id, module_id, lesson_id, completed) VALUES (?, ?, 999, 1)",
              (user_id, module_id))
    conn.commit()
    conn.close()
    await callback.message.answer(f"🎉 Поздравляю! Модуль {module_id} завершён!")
    await show_school_modules(callback)
    await callback.answer()

# Дневник
@dp.callback_query(F.data == "diary")
async def diary_start(callback: types.CallbackQuery, state: FSMContext):
    if not await ensure_subscription(callback):
        await callback.answer()
        return
    await callback.message.answer("📓 Выбери группу эмоций:", reply_markup=emotion_group_keyboard())
    await state.set_state(DiaryStates.waiting_for_group)
    await callback.answer()

@dp.callback_query(F.data == "diary_groups")
async def back_to_groups(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("📓 Выбери группу эмоций:", reply_markup=emotion_group_keyboard())
    await state.set_state(DiaryStates.waiting_for_group)
    await callback.answer()

@dp.callback_query(DiaryStates.waiting_for_group, F.data.startswith("group:"))
async def process_group(callback: types.CallbackQuery, state: FSMContext):
    group = callback.data.split(":", 1)[1]
    await state.update_data(emotion_group=group)
    await callback.message.edit_text(f"Выбери эмоцию из группы «{group}»:", reply_markup=emotion_list_keyboard(group))
    await state.set_state(DiaryStates.waiting_for_emotion)
    await callback.answer()

@dp.callback_query(DiaryStates.waiting_for_emotion, F.data.startswith("emotion:"))
async def process_emotion_selection(callback: types.CallbackQuery, state: FSMContext):
    emotion = callback.data.split(":", 1)[1]
    await state.update_data(emotion=emotion)
    await callback.message.edit_text("Насколько сильно ты это чувствуешь? Напиши число от 1 до 10.")
    await state.set_state(DiaryStates.waiting_for_intensity)
    await callback.answer()

@dp.callback_query(DiaryStates.waiting_for_emotion, F.data == "emotion_custom")
async def custom_emotion(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Напиши свою эмоцию словами:")
    await state.set_state(DiaryStates.waiting_for_emotion)
    await callback.answer()

@dp.message(DiaryStates.waiting_for_emotion)
async def process_custom_emotion(message: types.Message, state: FSMContext):
    emotion = message.text.strip()
    await state.update_data(emotion=emotion)
    await message.answer("Насколько сильно ты это чувствуешь? Напиши число от 1 до 10.")
    await state.set_state(DiaryStates.waiting_for_intensity)

@dp.message(DiaryStates.waiting_for_intensity)
async def process_intensity(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or int(text) < 1 or int(text) > 10:
        await message.answer("Пожалуйста, введи число от 1 до 10.")
        return
    intensity = int(text)
    await state.update_data(intensity=intensity)
    await message.answer("Что вызвало эту эмоцию? Опиши коротко или напиши «не знаю».")
    await state.set_state(DiaryStates.waiting_for_reason)

@dp.message(DiaryStates.waiting_for_reason)
async def process_reason(message: types.Message, state: FSMContext):
    reason = message.text.strip()
    data = await state.get_data()
    user_id = message.from_user.id
    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    emotion_group = data.get("emotion_group", "")
    emotion = data.get("emotion", "")
    intensity = data.get("intensity", 0)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO diary_entries (user_id, date, emotion, emotion_group, intensity, reason) VALUES (?, ?, ?, ?, ?, ?)",
              (user_id, today, emotion, emotion_group, intensity, reason))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Запись сохранена!\n\nЭмоция: {emotion}\nИнтенсивность: {intensity}/10\nПричина: {reason}\n\nЧто хочешь сделать дальше?",
                         reply_markup=InlineKeyboardBuilder().button(text="💬 Разобрать в Поговорим", callback_data="start_training").button(text="🧘 Выполнить практику", callback_data="mindfulness_today").button(text="⬅ В меню", callback_data="main_menu").as_markup())
    await state.clear()

# Mindfulness
MINDFULNESS_PRACTICES = [
    "Сделай 3 глубоких вдоха и выдоха. Почувствуй, как воздух проходит через тело.",
    "Вдохни на 4 счета, задержи дыхание на 4, выдохни на 4. Повтори 3 раза.",
    "Вдохни на 4, задержи на 7, выдохни на 8. Повтори 3 раза.",
    "Закрой глаза и мысленно пройдись от макушки до пяток. Где есть напряжение? Просто заметь его.",
    "Сфокусируйся на ступнях. Почувствуй их соприкосновение с полом. Затем поднимись выше.",
    "Назови 5 вещей, которые ты видишь, 4, которые слышишь, 3, которые чувствуешь телом, 2, которые можешь понюхать, и 1, которую можешь попробовать на вкус.",
    "Почувствуй стопы на полу. Ощути давление. Представь, что корни растут из твоих ступней в землю.",
    "Сделай 3 медленных, осознанных шага по комнате. Почувствуй каждый шаг.",
    "Вспомни 3 вещи, за которые ты благодарен сегодня. Напиши их.",
    "Вспомни 3 вещи, за которые ты благодарен себе.",
    "Посиди 1 минуту в тишине и прислушайся к звукам вокруг. Что ты слышишь?",
    "Представь свои мысли как облака, проплывающие по небу. Не цепляйся за них, просто наблюдай.",
    "Скажи себе вслух: 'Я здесь. Я в безопасности.' Повтори три раза.",
    "Оглянись вокруг и назови 3 предмета, которые тебе нравятся.",
    "Закрой глаза и представь, что ты находишься в спокойном месте. Опиши его одним словом."
]

@dp.callback_query(F.data == "mindfulness_menu")
async def mindfulness_menu(callback: types.CallbackQuery):
    if not await ensure_subscription(callback):
        await callback.answer()
        return
    await callback.message.answer("Выбери действие:", reply_markup=mindfulness_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "mindfulness_about")
async def mindfulness_about(callback: types.CallbackQuery):
    if not await ensure_subscription(callback):
        await callback.answer()
        return
    await callback.message.answer(
        "📖 Что такое Mindfulness (осознанность)?\n\n"
        "Осознанность — это умение быть здесь и сейчас, без осуждения и оценок.\n\n"
        "Зачем это нужно?\n"
        "• Снижает стресс и тревогу.\n"
        "• Улучшает концентрацию и память.\n"
        "• Помогает лучше понимать свои эмоции и реакции.\n\n"
        "Как практиковать?\n"
        "1. Нажми '🌿 Выполнить практику'.\n"
        "2. Ты получишь короткое упражнение.\n"
        "3. Выполни его в удобном темпе.\n"
        "4. Поделись своими ощущениями."
    )
    await callback.answer()

@dp.callback_query(F.data == "mindfulness_today")
async def mindfulness_today(callback: types.CallbackQuery, state: FSMContext, specific_practice_id: int = None):
    if not await ensure_subscription(callback):
        await callback.answer()
        return
    if specific_practice_id is not None:
        practice = MINDFULNESS_PRACTICES[specific_practice_id]
    else:
        practice = random.choice(MINDFULNESS_PRACTICES)
    practice_id = MINDFULNESS_PRACTICES.index(practice)
    await state.update_data(practice_id=practice_id)
    user_id = callback.from_user.id
    gender = get_user_gender(user_id)
    gender_text = "мужчина" if gender == "male" else "женщина" if gender == "female" else "человек"
    prompt = f"""
Ты — женщина-тренер по осознанности. Предложи пользователю ({gender_text}) короткую практику mindfulness.
Обязательно обращайся от женского лица, но используй правильный род для пользователя.
Вот описание практики: {practice}.

Дополни его:
1. Чёткой инструкцией, что именно делать (по шагам).
2. Мягким, поддерживающим завершением.

ВАЖНО: Не используй звёздочки (*), подчёркивания, жирный шрифт, курсив или любые markdown-символы. Пиши только обычным текстом с эмодзи. Ответ не должен содержать выделений.
"""
    await callback.message.answer("🧘 Готовлю для тебя практику... Дай мне секунду.")
    answer = call_deepseek(prompt)
    await callback.message.answer(answer)
    await callback.message.answer("Как ты себя чувствуешь после этого? (напиши коротко)")
    await state.set_state(MindfulnessStates.waiting_for_feedback)
    await callback.answer()

@dp.message(MindfulnessStates.waiting_for_feedback)
async def mindfulness_feedback(message: types.Message, state: FSMContext):
    feedback = message.text
    data = await state.get_data()
    practice_id = data.get('practice_id', 0)
    user_id = message.from_user.id
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO mindfulness_log (user_id, date, practice_id, feedback) VALUES (?, ?, ?, ?)",
              (user_id, today, practice_id, feedback))
    conn.commit()
    conn.close()
    await message.answer("✅ Спасибо, что уделил время себе. Каждая практика приближает тебя к спокойствию. 🌿")
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())

# Поговорим
@dp.callback_query(F.data == "start_training")
async def start_training(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if user_id != ADMIN_ID:
        active, plan, expires, days_left = check_subscription(user_id)
        if not active:
            if give_trial(user_id):
                await callback.message.answer("✅ Тебе активирован пробный доступ на 24 часа. Описывай свою ситуацию!")
            else:
                await callback.message.answer("⚠️ Пробный период уже использован. Пожалуйста, оформи подписку.")
                await callback.answer()
                return
    await state.set_state(EmotionStates.waiting_for_situation)
    await callback.message.answer("💬 Поделись своей ситуацией или чувством одним-двумя предложениями. Я помогу тебе разобраться.")
    await callback.answer()

@dp.message(EmotionStates.waiting_for_situation)
async def process_situation(message: types.Message, state: FSMContext):
    situation = message.text
    await state.update_data(situation=situation)
    user_id = message.from_user.id
    gender = get_user_gender(user_id)
    gender_text = "мужчина" if gender == "male" else "женщина" if gender == "female" else "человек"
    prompt = f"""Ты — женщина-тренер по эмоциональному интеллекту. Пользователь ({gender_text}) описывает ситуацию: {situation}.
Твоя задача — помочь ему разобраться в чувствах, задавая уточняющие вопросы и направляя к осознанию. Не давай готовых советов. Будь эмпатичной, без диагностики. Обращайся от женского лица, но учитывай пол пользователя.
Ответь на русском, используй эмодзи.
ВАЖНО: Не используй звёздочки (*), подчёркивания (_) или другие символы markdown. Пиши обычным текстом."""
    await message.answer("🌱 Я слушаю... Дай мне секунду.")
    answer = call_deepseek(prompt)
    await message.answer(answer)
    await state.set_state(EmotionStates.waiting_for_followup)

@dp.message(EmotionStates.waiting_for_followup)
async def process_followup(message: types.Message, state: FSMContext):
    user_response = message.text
    data = await state.get_data()
    situation = data.get('situation', '')
    user_id = message.from_user.id
    gender = get_user_gender(user_id)
    gender_text = "мужчина" if gender == "male" else "женщина" if gender == "female" else "человек"
    prompt = f"""Ты — женщина-тренер по эмоциональному интеллекту. Ранее пользователь ({gender_text}) описал ситуацию: {situation}.
Затем он ответил на твой вопрос: {user_response}.
Продолжи диалог: задай следующий вопрос или подведи к итогу. Не давай диагнозов. Обращайся от женского лица, но учитывай пол пользователя. Ответь на русском, без форматирования."""
    await message.answer("🌱 Продолжаем...")
    answer = call_deepseek(prompt)
    await message.answer(answer)

@dp.message(Command("cancel"))
async def cancel_command(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Возвращаюсь в главное меню.", reply_markup=main_menu_keyboard())

# Подписка
@dp.callback_query(F.data == "subscribe_menu")
async def subscribe_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id == ADMIN_ID:
        await callback.message.answer("Ты администратор, подписка не требуется. Вот тарифы для ознакомления:", reply_markup=subscription_keyboard())
        await callback.answer()
        return
    active, plan, expires, days_left = check_subscription(user_id)
    if active:
        if plan == 'trial':
            text = f"🕐 Пробный период активен.\nОсталось дней: {days_left}\n\nОформи подписку, чтобы продолжить пользоваться всеми функциями."
        else:
            end_date = datetime.fromtimestamp(expires).strftime("%d.%m.%Y")
            text = f"💎 Твоя подписка:\nТариф: {plan}\nДействует до: {end_date}\nОсталось дней: {days_left}\n\nХочешь продлить или изменить тариф?"
    else:
        text = "💎 У тебя нет активной подписки.\nВыбери тариф:"
    await callback.message.answer(text, reply_markup=subscription_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "main_menu")
async def back_to_main(callback: types.CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("sub_"))
async def process_subscription(callback: types.CallbackQuery):
    plan = callback.data.split("_")[1]
    if plan == "week":
        price = 80
        duration_days = 7
        desc = "Подписка я слышу (Неделя)"
    elif plan == "month":
        price = 180
        duration_days = 30
        desc = "Подписка я слышу (Месяц)"
    elif plan == "year":
        price = 1800
        duration_days = 365
        desc = "Подписка я слышу (Год)"
    else:
        await callback.message.answer("❌ Неизвестный тариф.")
        await callback.answer()
        return
    pay_url, payment_id, error_text = create_tbank_payment(price, desc, callback.from_user.id, plan, duration_days)
    if pay_url is None:
        await callback.message.answer("❌ Не удалось создать платёжную ссылку.")
        if error_text:
            await callback.message.answer(f"ℹ️ {error_text}")
    else:
        await callback.message.answer(f"💳 Для оплаты перейди по ссылке:\n\n{pay_url}\n\nПосле оплаты подписка активируется автоматически.")
    await callback.answer()

# Прогресс
@dp.callback_query(F.data == "my_progress")
async def my_progress(callback: types.CallbackQuery):
    if not await ensure_subscription(callback):
        await callback.answer()
        return
    user_id = callback.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM diary_entries WHERE user_id = ?", (user_id,))
    diary_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM mindfulness_log WHERE user_id = ?", (user_id,))
    mindfulness_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM school_progress WHERE user_id = ? AND lesson_id = 999", (user_id,))
    modules_completed = c.fetchone()[0]
    total_modules = len(SCHOOL_MODULES)
    active, plan, expires, days_left = check_subscription(user_id)
    sub_info = f"💎 Подписка: {plan} (осталось {days_left} дн.)" if active else "💎 Подписка: неактивна"
    conn.close()
    text = f"📊 Твой прогресс\n\n📓 Записей в дневнике: {diary_count}\n🧘 Выполнено практик: {mindfulness_count}\n🎓 Школа эмоций: пройдено модулей {modules_completed} из {total_modules}\n{sub_info}"
    await callback.message.answer(text)
    await callback.answer()

# Поддержка
@dp.callback_query(F.data == "support")
async def support(callback: types.CallbackQuery):
    await callback.message.answer("📞 Если нужна помощь, пиши в личные сообщения: @MARGOKARDATOVA")
    await callback.answer()

# =======================================================
# ВЕБХУКИ
# =======================================================
app = Flask(__name__)

@app.route('/payment_webhook', methods=['POST'])
def tbank_webhook():
    data = request.json
    print(f"🔔 Получен вебхук от Т-Банка: {data}")
    if data.get("Status") == "CONFIRMED" or data.get("Success") == True:
        order_id = data.get("OrderId", "")
        payment = get_payment(order_id)
        if payment and payment[3] != 'paid':
            user_id, plan, duration_days, status = payment
            update_subscription(user_id, plan, duration_days)
            update_payment_status(order_id, 'paid')
            end_date = datetime.fromtimestamp(int(time.time()) + duration_days * 86400).strftime("%d.%m.%Y")
            message_text = f"✅ Оплата прошла! Твой тариф «{plan}» активен до {end_date}."
            try:
                get_event_loop().run_until_complete(bot.send_message(user_id, message_text))
            except Exception as e:
                print(f"❌ Ошибка отправки подтверждения: {e}")
        else:
            print(f"ℹ️ Заказ {order_id} не найден или уже обработан.")
    return "OK", 200

@app.post("/webhook")
def webhook():
    try:
        data = request.get_json()
        update = Update.model_validate(data)
        get_event_loop().run_until_complete(dp.feed_update(bot, update))
        return "OK", 200
    except Exception as e:
        print(f"❌ Ошибка в вебхуке: {e}")
        return "OK", 200

# =======================================================
# ЕЖЕДНЕВНОЕ НАПОМИНАНИЕ
# =======================================================
@app.route('/send_reminders', methods=['GET'])
def send_daily_reminders():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        now_timestamp = int(time.time())
        current_time = datetime.now().strftime("%H:%M")
        c.execute("""
            SELECT user_id FROM users
            WHERE expires_at > ?
            AND reminder_time = ?
            AND reminder_enabled = 1
            AND user_id NOT IN (
                SELECT user_id FROM mindfulness_log WHERE date = ?
            )
        """, (now_timestamp, current_time, today))
        users = c.fetchall()
        conn.close()
        if not users:
            return "No users at this time", 200
        for user_row in users:
            user_id = user_row[0]
            try:
                get_event_loop().run_until_complete(
                    bot.send_message(
                        user_id,
                        "🧘 Напоминание о практике осознанности!\n\n"
                        "Пришло время твоей ежедневной mindfulness-практики. Нажми кнопку '🧘 Mindfulness' в меню, чтобы начать.\n"
                        "Всего несколько минут — и ты почувствуешь себя спокойнее.",
                        reply_markup=main_menu_keyboard()
                    )
                )
            except Exception as e:
                print(f"❌ Ошибка отправки пользователю {user_id}: {e}")
        return f"Reminders sent for {current_time}", 200
    except Exception as e:
        print(f"❌ Критическая ошибка в маршруте напоминаний: {e}")
        return "Error", 500

# =======================================================
# ГЛОБАЛЬНЫЙ ЦИКЛ
# =======================================================
loop = None
def get_event_loop():
    global loop
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)