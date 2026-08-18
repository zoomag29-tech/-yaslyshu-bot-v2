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
# КОНФИГУРАЦИЯ (секреты из переменных окружения)
# =======================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8025021798"))
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
CONTACT = "@MARGOKARDATOVA"

TERMINAL_KEY = os.environ.get("TERMINAL_KEY")
TERMINAL_PASSWORD = os.environ.get("TERMINAL_PASSWORD")

if not BOT_TOKEN or not DEEPSEEK_API_KEY or not TERMINAL_KEY or not TERMINAL_PASSWORD:
    raise RuntimeError("Не заданы обязательные переменные окружения: BOT_TOKEN, DEEPSEEK_API_KEY, TERMINAL_KEY, TERMINAL_PASSWORD")

# =======================================================
# DEEPSEEK (с принудительным отключением прокси)
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
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, 
                  subscription_plan TEXT, 
                  expires_at INTEGER,
                  reminder_time TEXT DEFAULT '09:30',
                  reminder_enabled INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS diary_entries
                 (user_id INTEGER, date TEXT, emotion TEXT, reason TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS mindfulness_log
                 (user_id INTEGER, date TEXT, practice_id INTEGER, feedback TEXT)''')
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
# FSM (СОСТОЯНИЯ)
# =======================================================
class EmotionStates(StatesGroup):
    waiting_for_situation = State()
    waiting_for_followup = State()

class DiaryStates(StatesGroup):
    waiting_for_emotion = State()
    waiting_for_reason = State()

class MindfulnessStates(StatesGroup):
    waiting_for_feedback = State()

class ReminderStates(StatesGroup):
    waiting_for_time = State()

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

def create_tbank_payment(amount, description, user_id):
    # Выбираем URL в зависимости от тестового ключа
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
    
    # Генерация токена по правилам Т-Банка:
    # 1. Сортируем ключи по алфавиту
    # 2. Склеиваем значения без разделителей
    # 3. Добавляем пароль в конец
    token_str = ''.join(str(payload[k]) for k in sorted(payload.keys()))
    token_str += TERMINAL_PASSWORD
    token = hashlib.sha256(token_str.encode('utf-8')).hexdigest()
    payload["Token"] = token
    
    try:
        response = requests.post(url, json=payload, timeout=10, verify=False)
        response.raise_for_status()
        data = response.json()
        if data.get("Success"):
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

def get_user_subscription(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT subscription_plan, expires_at FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row if row else (None, 0)

def update_subscription(user_id, plan, duration_days):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    expires_at = int(time.time()) + duration_days * 86400
    c.execute("INSERT OR REPLACE INTO users (user_id, subscription_plan, expires_at, reminder_time, reminder_enabled) VALUES (?, ?, ?, '09:30', 1)",
              (user_id, plan, expires_at))
    conn.commit()
    conn.close()

def check_subscription(user_id):
    plan, expires_at = get_user_subscription(user_id)
    if plan is None:
        return False, None, 0, 0
    now = int(time.time())
    days_left = max(0, (expires_at - now) // 86400)
    return now < expires_at, plan, expires_at, days_left

async def ensure_subscription(callback: types.CallbackQuery):
    """Проверяет подписку и при необходимости выдаёт пробный доступ."""
    user_id = callback.from_user.id
    active, plan, expires, days_left = check_subscription(user_id)
    if not active:
        update_subscription(user_id, "trial", 1)
        await callback.message.answer("✅ Тебе активирован пробный доступ на 24 часа. Теперь ты можешь пользоваться всеми функциями бота!")
    return active

# =======================================================
# 15 MINDFULNESS-ПРАКТИК (короткие темы)
# =======================================================
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

# =======================================================
# КЛАВИАТУРЫ
# =======================================================
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Поговорим", callback_data="start_training")
    builder.button(text="📓 Дневник эмоций", callback_data="diary")
    builder.button(text="🧘 Mindfulness", callback_data="mindfulness_menu")
    builder.button(text="📊 Мой прогресс", callback_data="my_progress")
    builder.button(text="💎 Подписка", callback_data="subscribe_menu")
    builder.button(text="📞 Поддержка", callback_data="support")
    builder.adjust(2)
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

# =======================================================
# ОБРАБОТЧИКИ
# =======================================================
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    active, plan, expires, days_left = check_subscription(user_id)
    if active:
        text = f"👋 Привет! Ты уже с нами.\nТвой тариф: {plan}\nОсталось дней: {days_left}"
    else:
        text = "👋 Привет! Я — «я слышу». Твой персональный наставник по эмоциональному интеллекту и осознанности.\n\n📓 Веди дневник эмоций.\n🧘 Каждый день выполняй mindfulness-практику.\n📊 Отслеживай свой прогресс.\n\nНачни бесплатный пробный период прямо сейчас — нажми любую кнопку."
    await message.answer(text, reply_markup=main_menu_keyboard())

@dp.callback_query(lambda c: c.data == "start_training")
async def start_training(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    active, plan, expires, days_left = check_subscription(user_id)
    if not active:
        update_subscription(user_id, "trial", 1)
        await callback.message.answer("✅ Тебе активирован пробный доступ на 24 часа. Описывай свою ситуацию!")
    await state.set_state(EmotionStates.waiting_for_situation)
    await callback.message.answer("💬 Поделись своей ситуацией или чувством одним-двумя предложениями. Я помогу тебе разобраться.")
    await callback.answer()

@dp.message(EmotionStates.waiting_for_situation)
async def process_situation(message: types.Message, state: FSMContext):
    situation = message.text
    await state.update_data(situation=situation)
    prompt = f"""Ты — тренер по эмоциональному интеллекту. Пользователь описывает ситуацию: {situation}.
Твоя задача — помочь ему разобраться в чувствах, задавая уточняющие вопросы и направляя к осознанию. Не давай готовых советов. Будь эмпатичным, без диагностики.
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
    prompt = f"""Ты — тренер по эмоциональному интеллекту. Ранее пользователь описал ситуацию: {situation}.
Затем он ответил на твой вопрос: {user_response}.
Продолжи диалог: задай следующий вопрос или подведи к итогу. Не давай диагнозов. Ответь на русском, без форматирования."""
    await message.answer("🌱 Продолжаем...")
    answer = call_deepseek(prompt)
    await message.answer(answer)

@dp.message(Command("cancel"))
async def cancel_command(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Возвращаюсь в главное меню.", reply_markup=main_menu_keyboard())

@dp.callback_query(lambda c: c.data == "subscribe_menu")
async def subscribe_menu(callback: types.CallbackQuery):
    await callback.message.answer("📅 Выбери тариф:", reply_markup=subscription_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "main_menu")
async def back_to_main(callback: types.CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("sub_"))
async def process_subscription(callback: types.CallbackQuery):
    plan = callback.data.split("_")[1]
    if plan == "week":
        price = 80
        desc = "Подписка я слышу (Неделя)"
    elif plan == "month":
        price = 180
        desc = "Подписка я слышу (Месяц)"
    elif plan == "year":
        price = 1800
        desc = "Подписка я слышу (Год)"
    else:
        await callback.message.answer("❌ Неизвестный тариф.")
        await callback.answer()
        return

    pay_url, payment_id, error_text = create_tbank_payment(price, desc, callback.from_user.id)
    if pay_url is None:
        await callback.message.answer("❌ Не удалось создать платёжную ссылку.")
        if error_text:
            await callback.message.answer(f"ℹ️ {error_text}")
    else:
        await callback.message.answer(
            f"💳 Для оплаты перейди по ссылке:\n\n{pay_url}\n\nПосле оплаты подписка активируется автоматически."
        )
    await callback.answer()

# =======================================================
# ДНЕВНИК ЭМОЦИЙ
# =======================================================
@dp.callback_query(lambda c: c.data == "diary")
async def diary_start(callback: types.CallbackQuery, state: FSMContext):
    await ensure_subscription(callback)
    await callback.message.answer("📓 Как ты себя чувствуешь сегодня? (напиши одним словом или эмодзи)")
    await state.set_state(DiaryStates.waiting_for_emotion)
    await callback.answer()

@dp.message(DiaryStates.waiting_for_emotion)
async def diary_get_emotion(message: types.Message, state: FSMContext):
    emotion = message.text
    await state.update_data(emotion=emotion)
    await message.answer("Что вызвало это чувство? (или напиши 'нет')")
    await state.set_state(DiaryStates.waiting_for_reason)

@dp.message(DiaryStates.waiting_for_reason)
async def diary_get_reason(message: types.Message, state: FSMContext):
    reason = message.text
    data = await state.get_data()
    emotion = data['emotion']
    user_id = message.from_user.id
    today = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO diary_entries (user_id, date, emotion, reason) VALUES (?, ?, ?, ?)",
              (user_id, today, emotion, reason))
    conn.commit()
    conn.close()

    await message.answer("✅ Запись сохранена! Ты молодец, что работаешь над собой. 🌱")
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())

# =======================================================
# ПРАКТИКА ОСОЗНАННОСТИ (ТЕКСТ)
# =======================================================
@dp.callback_query(lambda c: c.data == "mindfulness_menu")
async def mindfulness_menu(callback: types.CallbackQuery):
    await ensure_subscription(callback)
    await callback.message.answer(
        "Выбери действие:",
        reply_markup=mindfulness_menu_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "mindfulness_about")
async def mindfulness_about(callback: types.CallbackQuery):
    await ensure_subscription(callback)
    await callback.message.answer(
        "📖 Что такое Mindfulness (осознанность)?\n\n"
        "Осознанность — это умение быть здесь и сейчас, без осуждения и оценок. Это не сложная философия, а простая практика: обратить внимание на дыхание, на ощущения в теле, на мысли, которые приходят и уходят, как облака.\n\n"
        "Зачем это нужно?\n"
        "• Снижает стресс и тревогу.\n"
        "• Улучшает концентрацию и память.\n"
        "• Помогает лучше понимать свои эмоции и реакции.\n"
        "• Учит возвращаться в момент, когда ум убегает в прошлое или будущее.\n\n"
        "Как практиковать?\n"
        "1. Нажми '🌿 Выполнить практику'.\n"
        "2. Ты получишь короткое упражнение в текстовом виде.\n"
        "3. Выполни его в удобном темпе.\n"
        "4. Поделись своими ощущениями — это закрепит практику.\n\n"
        "Регулярная практика (всего 2–5 минут в день) меняет качество жизни. Попробуй!"
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "mindfulness_today")
async def mindfulness_today(callback: types.CallbackQuery, state: FSMContext):
    await ensure_subscription(callback)
    practice = random.choice(MINDFULNESS_PRACTICES)
    practice_id = MINDFULNESS_PRACTICES.index(practice)
    await state.update_data(practice_id=practice_id)

    prompt = f"""
Ты — тренер по осознанности. Предложи пользователю короткую практику mindfulness.

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
    practice_id = data['practice_id']
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

# =======================================================
# НАСТРОЙКА НАПОМИНАНИЙ
# =======================================================
@dp.callback_query(lambda c: c.data == "reminder_settings")
async def reminder_settings(callback: types.CallbackQuery, state: FSMContext):
    await ensure_subscription(callback)
    await callback.message.answer(
        "⏰ Настрой время, когда тебе удобно получать напоминание о mindfulness-практике.\n\n"
        "Введи время в формате ЧЧ:ММ (например, 09:30 или 21:00).\n\n"
        "Ты можешь изменить время в любой момент."
    )
    await state.set_state(ReminderStates.waiting_for_time)
    await callback.answer()

@dp.message(ReminderStates.waiting_for_time)
async def process_reminder_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат. Введи время в формате ЧЧ:ММ (например, 09:30).")
        return

    user_id = message.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET reminder_time = ? WHERE user_id = ?", (time_str, user_id))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Время напоминания установлено на {time_str}. Я буду напоминать тебе каждый день в это время. 🌱")
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())

@dp.callback_query(lambda c: c.data == "reminder_off")
async def reminder_off(callback: types.CallbackQuery):
    await ensure_subscription(callback)
    user_id = callback.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET reminder_enabled = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    await callback.message.answer("🔕 Напоминания отключены. Ты всегда можешь включить их заново в этом меню.")
    await callback.answer()

# =======================================================
# МОЙ ПРОГРЕСС (глубокая статистика)
# =======================================================
@dp.callback_query(lambda c: c.data == "my_progress")
async def my_progress(callback: types.CallbackQuery):
    await ensure_subscription(callback)
    user_id = callback.from_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT date, emotion, reason FROM diary_entries WHERE user_id = ? ORDER BY date DESC LIMIT 5", (user_id,))
    recent_entries = c.fetchall()

    c.execute("SELECT COUNT(*) FROM diary_entries WHERE user_id = ?", (user_id,))
    diary_count = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM mindfulness_log WHERE user_id = ?", (user_id,))
    mindfulness_count = c.fetchone()[0]

    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    c.execute("""
        SELECT emotion, COUNT(*) as cnt FROM diary_entries
        WHERE user_id = ? AND date >= ?
        GROUP BY emotion ORDER BY cnt DESC LIMIT 1
    """, (user_id, seven_days_ago))
    row = c.fetchone()
    top_emotion = row[0] if row else "ещё нет данных"

    c.execute("SELECT reminder_time, reminder_enabled FROM users WHERE user_id = ?", (user_id,))
    rem_row = c.fetchone()
    if rem_row and rem_row[1] == 1:
        reminder_status = f"⏰ Напоминание включено на {rem_row[0]}"
    else:
        reminder_status = "🔕 Напоминания отключены"
    
    conn.close()

    if diary_count == 0:
        text = (
            "🌱 Ты ещё ничего не записал(а) в дневник.\n"
            "Но это нормально — каждый путь начинается с первого шага. "
            "Нажми '📓 Дневник', чтобы зафиксировать свои чувства. "
            "Это поможет тебе лучше понять себя."
        )
    else:
        entries_list = ""
        for date, emotion, reason in recent_entries:
            reason_text = f" — {reason}" if reason and reason != "нет" else ""
            entries_list += f"▫️ {date} — {emotion}{reason_text}\n"

        text = (
            f"📖 Твой путь к осознанности\n\n"
            f"📓 Записей в дневнике: {diary_count}\n"
            f"🧘 Выполненных практик: {mindfulness_count}\n\n"
            f"🔥 Чаще всего за последнюю неделю ты чувствовал(а): {top_emotion}\n"
            f"💡 Это говорит о том, что твоё внимание сейчас направлено на эту область. Это ценный сигнал.\n\n"
            f"📝 Последние записи из дневника:\n{entries_list}\n"
            f"🔄 {reminder_status}\n\n"
            f"Ты проделываешь большую работу. Каждый шаг — это шаг к себе. 🌿"
        )

    await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "support")
async def support(callback: types.CallbackQuery):
    await callback.message.answer("📞 Если нужна помощь, пиши в личные сообщения: @MARGOKARDATOVA")
    await callback.answer()

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

# =======================================================
# ОБРАБОТЧИК ВЕБХУКА ОТ Т-БАНКА
# =======================================================
app = Flask(__name__)

@app.route('/payment_webhook', methods=['POST'])
def tbank_webhook():
    data = request.json
    print(f"🔔 Получен вебхук от Т-Банка: {data}")
    if data.get("Status") == "CONFIRMED" or data.get("Success") == True:
        order_id = data.get("OrderId", "")
        try:
            user_id = int(order_id.split("_")[1])
            update_subscription(user_id, "month", 30)
            print(f"✅ Подписка активирована для пользователя {user_id}")
        except Exception as e:
            print(f"❌ Ошибка при активации подписки: {e}")
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
# ЕЖЕДНЕВНОЕ НАПОМИНАНИЕ (маршрут для cron-job.org)
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
            print(f"⏳ В {current_time} нет пользователей для напоминания.")
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
                print(f"✅ Напоминание отправлено пользователю {user_id} в {current_time}")
            except Exception as e:
                print(f"❌ Ошибка отправки пользователю {user_id}: {e}")

        return f"Reminders sent for {current_time}", 200

    except Exception as e:
        print(f"❌ Критическая ошибка в маршруте напоминаний: {e}")
        return "Error", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)