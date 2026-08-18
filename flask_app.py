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
from pydub import AudioSegment
from io import BytesIO

# =======================================================
# КОНФИГУРАЦИЯ (секреты из переменных окружения)
# =======================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8025021798"))
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
CONTACT = "@MARGOKARDATOVA"

TERMINAL_KEY = os.environ.get("TERMINAL_KEY")
TERMINAL_PASSWORD = os.environ.get("TERMINAL_PASSWORD")

YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")

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

def synthesize_speech(text: str, voice: str = "lera", speed: float = 0.9) -> BytesIO:
    """Синтез речи через Яндекс SpeechKit. Возвращает BytesIO с аудио в mp3."""
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise RuntimeError("Не заданы YANDEX_API_KEY или YANDEX_FOLDER_ID")
    url = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "text": text,
        "voice": voice,
        "emotion": "good",
        "speed": str(speed),
        "format": "mp3",
        "folderId": YANDEX_FOLDER_ID
    }
    response = requests.post(url, headers=headers, data=data, timeout=30)
    if response.status_code != 200:
        raise Exception(f"SpeechKit error: {response.status_code} {response.text}")
    return BytesIO(response.content)

def mix_audio(speech: BytesIO, music_path: str = "background_music.mp3", music_volume: int = -20) -> BytesIO:
    """Наложение фоновой музыки на речь. music_volume - громкость музыки в dB (отрицательное значение уменьшает громкость)."""
    speech_audio = AudioSegment.from_file(speech, format="mp3")
    if os.path.exists(music_path):
        music_audio = AudioSegment.from_file(music_path)
        # Обрезаем или зацикливаем музыку до длины речи
        if len(music_audio) < len(speech_audio):
            loops = len(speech_audio) // len(music_audio) + 1
            music_audio = (music_audio * loops)[:len(speech_audio)]
        else:
            music_audio = music_audio[:len(speech_audio)]
        # Уменьшаем громкость музыки
        music_audio = music_audio + music_volume
        # Смешиваем
        mixed = speech_audio.overlay(music_audio)
    else:
        mixed = speech_audio
    output = BytesIO()
    mixed.export(output, format="mp3")
    output.seek(0)
    return output

async def send_voice_practice(chat_id: int, speech_bytes: BytesIO):
    """Отправка голосового сообщения в Telegram."""
    speech_bytes.seek(0)
    await bot.send_voice(chat_id, voice=speech_bytes)

def create_tbank_payment(amount, description, user_id):
    # Выбираем URL в зависимости от тестового ключа
    if "DEMO" in TERMINAL_KEY:
        url = "https://rest-api-test.tinkoff.ru/v2/Init"
    else:
        url = "https://securepay.tinkoff.ru/v2/Init"
    
    amount_kop = amount * 100
    order_id = f"order_{user_id}_{int(time.time())}"
    
    # Генерация токена (SHA256)
    token_str = f"{TERMINAL_KEY}{order_id}{amount_kop}{description}{TERMINAL_PASSWORD}"
    token = hashlib.sha256(token_str.encode('utf-8')).hexdigest()
    
    payload = {
        "TerminalKey": TERMINAL_KEY,
        "Amount": amount_kop,
        "OrderId": order_id,
        "Description": description,
        "Token": token,
        "NotificationURL": "https://yaslyshu-bot-v2.onrender.com/payment_webhook",
        "SuccessURL": "https://t.me/yaslyshu_bot",
        "FailURL": "https://t.me/yaslyshu_bot"
    }
    try:
        response = requests.post(url, json=payload, timeout=10, verify=False)
        response.raise_for_status()
        data = response.json()
        if data.get("Success"):
            return data["PaymentURL"], data["PaymentId"], None
        else:
            error_text = f"Т-Банк: {data.get('ErrorCode', '')} {data.get('Message', '')} {data.get('Details', '')}"
            error_text += f"\n\nОтправленный JSON: {payload}"
            error_text += f"\n\nURL: {url}"
            print(f"❌ Т-Банк ответил: {data}")
            return None, None, error_text
    except Exception as e:
        error_text = f"Ошибка запроса: {e}"
        error_text += f"\n\nОтправленный JSON: {payload}"
        error_text += f"\n\nURL: {url}"
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
    "Сделай 3 медленных, осознанных шага по комнате. Почувству