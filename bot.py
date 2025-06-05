import os
import asyncio
import random
import logging
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory_storage import MemoryStorage
from aiogram.utils import executor
from playwright.async_api import async_playwright
from faker import Faker
import asyncpg

logging.basicConfig(filename='mamba_bot.log', level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(bot, storage=MemoryStorage())
fake = Faker('ru_RU')
DB_CONFIG = {"dsn": os.getenv("DB_URL")}
VAK_SMS_API_KEY = os.getenv("VAK_SMS_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
IMAGES = ["https://your_drive_link/image1.jpg", "https://your_drive_link/image2.jpg"]  # Замени
PROXY = None  # Прокси не используем для простоты

async def send_log(message: str):
    try:
        await bot.send_message(ADMIN_CHAT_ID, f"Лог: {message}")
    except Exception as e:
        logger.error(f"Failed to send log: {e}")

async def check_vak_sms_balance():
    try:
        response = requests.get(f"https://vak-sms.com/api/balance?apiKey={VAK_SMS_API_KEY}", timeout=10)
        response.raise_for_status()
        balance = response.json().get("balance", 0)
        logger.info(f"Vak SMS balance: {balance}")
        await send_log(f"Vak SMS balance: {balance}")
        return balance > 0
    except Exception as e:
        logger.error(f"Vak SMS balance check failed: {e}")
        await send_log(f"Ошибка Vak SMS: {e}")
        return False

async def get_vak_sms_number():
    try:
        response = requests.get(
            f"https://vak-sms.com/api/getNumber?apiKey={VAK_SMS_API_KEY}&service=ms&country=ru",
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        if data.get("tel"):
            await send_log(f"Получен номер: {data['tel']}")
            return data["tel"], data["id"]
        raise Exception("Vak SMS error")
    except Exception as e:
        await send_log(f"Ошибка получения номера: {e}")
        raise

async def get_vak_sms_code(number_id):
    for _ in range(5):
        try:
            response = requests.get(
                f"https://vak-sms.com/api/getCode?apiKey={VAK_SMS_API_KEY}&id={number_id}",
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code"):
                await send_log(f"Получен код: {data['code']}")
                return data["code"]
            await asyncio.sleep(10)
        except Exception as e:
            await send_log(f"Ошибка получения кода: {e}")
    return None

async def init_db():
    try:
        conn = await asyncpg.connect(**DB_CONFIG)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id SERIAL PRIMARY KEY,
                login TEXT,
                password TEXT,
                name TEXT,
                age INTEGER,
                status TEXT,
                likes_count INTEGER,
                chats_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        await send_log("База данных готова")
        return conn
    except Exception as e:
        await send_log(f"Ошибка базы данных: {e}")
        raise

async def get_main_menu():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("Запустить регистрацию", "Лайкать анкеты")
    keyboard.add("Запустить спам", "Настройки")
    return keyboard

async def start_liking(page, profile_id: int, conn):
    try:
        await page.goto("https://www.mamba.ru/search")
        likes = 0
        for _ in range(10):  # Меньше лайков, чтобы не блокировали
            if await page.query_selector(".captcha-form"):
                await send_log("CAPTCHA обнаружена, пропускаем лайки")
                break
            await page.click(".like-button", timeout=5000)
            likes += 1
            await conn.execute("UPDATE profiles SET likes_count = likes_count + 1 WHERE id = $1", profile_id)
            await asyncio.sleep(random.randint(5, 10))
        await send_log(f"Анкета ID{profile_id}: {likes} лайков")
        return likes
    except Exception as e:
        await send_log(f"Ошибка лайков: {e}")
        return 0

async def count_chats(page, profile_id: int, conn):
    try:
        await page.goto("https://www.mamba.ru/chats")
        chats = len(await page.query_selector_all(".chat-item"))
        await conn.execute("UPDATE profiles SET chats_count = $1 WHERE id = $2", chats, profile_id)
        await send_log(f"Анкета ID{profile_id}: {chats} чатов")
        return chats
    except Exception as e:
        await send_log(f"Ошибка подсчёта чатов: {e}")
        return 0

@dp.message_handler(commands=['start'])
async def start_command(message: types.Message):
    if str(message.chat.id) != ADMIN_CHAT_ID:
        await message.answer("Доступ запрещён.")
        return
    await message.answer("Выберите действие:", reply_markup=await get_main_menu())
    await send_log("Бот запущен")

@dp.message_handler(lambda message: message.text == "Запустить регистрацию")
async def handle_registration(message: types.Message):
    if str(message.chat.id) != ADMIN_CHAT_ID:
        return
    await message.answer("Сколько анкет создать (1–10)?")
    dp.register_message_handler(process_registration_count, state="*")

async def process_registration_count(message: types.Message):
    try:
        count = int(message.text)
        if count < 1 or count > 10:
            await message.answer("Введите число от 1 до 10.", reply_markup=await get_main_menu())
            return
        if not await check_vak_sms_balance():
            await message.answer("Недостаточно средств на Vak SMS.", reply_markup=await get_main_menu())
            return

        conn = await init_db()
        settings = await conn.fetch("SELECT * FROM settings")
        name = next((s['value'] for s in settings if s['key'] == 'name'), 'Анна')

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for i in range(count):
                page = await browser.new_page()
                try:
                    await page.goto("https://www.mamba.ru")
                    if await page.query_selector(".captcha-form"):
                        await send_log("CAPTCHA при регистрации, пропускаем")
                        continue
                    login = fake.email()
                    password = fake.password()
                    await page.fill("#email", login)
                    await page.fill("#password", password)
                    await page.fill("#name", name)
                    await page.select_option("#gender", "female")
                    number, number_id = await get_vak_sms_number()
                    await page.fill("#phone", number)
                    await page.click("#submit")
                    code = await get_vak_sms_code(number_id)
                    if code:
                        await page.fill("#code", code)
                        await page.click("#verify")
                    await conn.execute(
                        "INSERT INTO profiles (id, login, password, name, age, status, likes_count, chats_count) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                        i+1, login, password, name, 25, "active", 0, 0
                    )
                    await send_log(f"Анкета {i+1}: {name}, ID: {i+1}")
                    likes = await start_liking(page, i+1, conn)
                    chats = await count_chats(page, i+1, conn)
                except Exception as e:
                    await send_log(f"Ошибка регистрации анкеты {i+1}: {e}")
                finally:
                    await page.close()
            await browser.close()
        await conn.close()
        await message.answer(f"Готово: {count} анкет.", reply_markup=await get_main_menu())
    except ValueError:
        await message.answer("Введите число.", reply_markup=await get_main_menu())

@dp.message_handler(lambda message: message.text == "Лайкать анкеты")
async def handle_liking(message: types.Message):
    if str(message.chat.id) != ADMIN_CHAT_ID:
        return
    conn = await init_db()
    profiles = await conn.fetch("SELECT id, login, password FROM profiles WHERE status = 'active'")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for profile in profiles:
            page = await browser.new_page()
            try:
                await page.goto("https://www.mamba.ru/login")
                await page.fill("#email", profile["login"])
                await page.fill("#password", profile["password"])
                await page.click("#login")
                likes = await start_liking(page, profile["id"], conn)
                chats = await count_chats(page, profile["id"], conn)
                await message.answer(f"Анкета ID{profile['id']}: {likes} лайков, {chats} чатов")
            except Exception as e:
                await send_log(f"Ошибка лайков ID{profile['id']}: {e}")
            finally:
                await page.close()
        await browser.close()
    await conn.close()
    await message.answer("Лайкинг завершён.", reply_markup=await get_main_menu())

@dp.message_handler(lambda message: message.text == "Запустить спам")
async def start_spam(message: types.Message):
    if str(message.chat.id) != ADMIN_CHAT_ID:
        return
    conn = await init_db()
    settings = await conn.fetch("SELECT * FROM settings")
    telegram_username = next((s['value'] for s in settings if s['key'] == 'telegram_username'), '@MyBot')
    profiles = await conn.fetch("SELECT id, login, password FROM profiles WHERE status = 'active'")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for profile in profiles:
            page = await browser.new_page()
            try:
                await page.goto("https://www.mamba.ru/login")
                await page.fill("#email", profile["login"])
                await page.fill("#password", profile["password"])
                await page.click("#login")
                await page.goto("https://www.mamba.ru/chats")
                chats = await page.query_selector_all(".chat-item")
                for chat in chats:
                    await chat.click()
                    await page.fill(".message-input", f"Привет! Давай в Telegram? {telegram_username}")
                    await page.click(".send-message-button")
                    await asyncio.sleep(5)
                await send_log(f"Спам отправлен для ID{profile['id']}")
            except Exception as e:
                await send_log(f"Ошибка спама ID{profile['id']}: {e}")
            finally:
                await page.close()
        await browser.close()
    await conn.close()
    await message.answer("Спам завершён.", reply_markup=await get_main_menu())

@dp.message_handler(lambda message: message.text == "Настройки")
async def settings_menu(message: types.Message):
    if str(message.chat.id) != ADMIN_CHAT_ID:
        return
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("Имя", "Telegram")
    await message.answer("Выберите настройку:", reply_markup=keyboard)

@dp.message_handler(lambda message: message.text in ["Имя", "Telegram"])
async def handle_settings(message: types.Message):
    if str(message.chat.id) != ADMIN_CHAT_ID:
        return
    key = {"Имя": "name", "Telegram": "telegram_username"}[message.text]
    await message.answer(f"Введите {message.text}:")
    dp.register_message_handler(lambda msg, state=key: save_setting(msg, state), state="*")

async def save_setting(message: types.Message, key: str):
    try:
        conn = await init_db()
        await conn.execute("INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2", key, message.text)
        await conn.close()
        await message.answer(f"{key} сохранено: {message.text}", reply_markup=await get_main_menu())
        await send_log(f"Настройка {key}: {message.text}")
    except Exception as e:
        await send_log(f"Ошибка настройки: {e}")

if __name__ == '__main__':
    for attempt in range(3):
        try:
            executor.start_polling(dp, skip_updates=True)
            break
        except Exception as e:
            asyncio.run(send_log(f"Ошибка запуска (попытка {attempt + 1}): {e}"))
            asyncio.sleep(10)
