import asyncio
import logging
import random
import string
import secrets
import hashlib
from datetime import datetime
from typing import Optional, List, Dict

import aiosqlite
import validators
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.deep_linking import create_start_link
from config import BOT_TOKEN

# --- CONFIG ---

TOKEN=BOT_TOKEN
DB_PATH = "bot_database.db"
APP_ID = "link_shortener_v1"

# --- DATABASE LOGIC ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица метаданных
        await db.execute("""
            CREATE TABLE IF NOT EXISTS meta_data (
                id TEXT PRIMARY KEY,
                user_tg_reg_date TEXT,
                user_bot_reg_date TEXT,
                device_meta TEXT,
                browser TEXT
            )
        """)
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                nickname TEXT,
                meta_data_id TEXT,
                FOREIGN KEY (meta_data_id) REFERENCES meta_data (id)
            )
        """)
        # Таблица ссылок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS short_links (
                short_id TEXT PRIMARY KEY,
                original_url TEXT,
                creator_id INTEGER,
                folder_id TEXT,
                created_at TEXT
            )
        """)
        # Таблица папок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                folder_id TEXT PRIMARY KEY,
                name TEXT,
                creator_id INTEGER
            )
        """)
        # Таблица хэшей удаленных ссылок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deleted_links_hash (
                hash_id TEXT PRIMARY KEY,
                original_url_hash TEXT,
                deleted_at TEXT,
                creator_id INTEGER
            )
        """)
        await db.commit()

def generate_id(length=12):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

# --- BOT INITIALIZATION ---
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- HANDLERS ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    # Проверка на deep-link (переход по сокращенной ссылке)
    args = message.text.split()
    if len(args) > 1:
        short_id = args[1]
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT original_url FROM short_links WHERE short_id = ?", (short_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return await message.answer(
                        f"🔗 Ваша ссылка готова:\n{row[0]}",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="Перейти", url=row[0])]
                        ])
                    )
                else:
                    return await message.answer("❌ Ссылка не найдена или была удалена.")

    # Регистрация пользователя
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT telegram_id FROM user WHERE telegram_id = ?", (message.from_user.id,)) as cursor:
            if not await cursor.fetchone():
                m_id = generate_id()
                now = datetime.now().isoformat()
                
                # Имитация получения метаданных (в реальном боте через API ограничено)
                await db.execute("""
                    INSERT INTO meta_data (id, user_tg_reg_date, user_bot_reg_date, device_meta, browser)
                    VALUES (?, ?, ?, ?, ?)
                """, (m_id, "Unknown", now, "Mobile/Desktop", "In-App Telegram"))
                
                await db.execute("""
                    INSERT INTO user (telegram_id, username, nickname, meta_data_id)
                    VALUES (?, ?, ?, ?)
                """, (message.from_user.id, message.from_user.username, message.from_user.full_name, m_id))
                await db.commit()

    await message.answer(
        "👋 Привет! Я бот для сокращения ссылок.\n\n"
        "🔹 Отправь мне любую ссылку, и я её сокращу.\n"
        "🔹 Используй /my_links для управления своими ссылками.\n"
        "🔹 Использу/folders для управления папками."
    )

@dp.message(F.text.regexp(r'^https?://'))
async def create_link(message: types.Message):
    url = message.text.strip()
    if not validators.url(url):
        return await message.answer("❌ Некорректный формат ссылки.")

    short_id = generate_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO short_links (short_id, original_url, creator_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (short_id, url, message.from_user.id, datetime.now().isoformat()))
        await db.commit()

    bot_info = await bot.get_me()
    short_url = f"https://t.me/{bot_info.username}?start={short_id}"
    
    await message.answer(
        f"✅ Ссылка сокращена!\n\n"
        f"Оригинал: {url}\n"
        f"Сокращенная: `{short_url}`",
        parse_mode="Markdown"
    )

@dp.message(Command("my_links"))
async def list_links(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT short_id, original_url FROM short_links WHERE creator_id = ?", (message.from_user.id,)) as cursor:
            links = await cursor.fetchall()
            
    if not links:
        return await message.answer("У вас еще нет сокращенных ссылок.")

    text = "📂 Ваши ссылки:\n\n"
    keyboard = []
    for s_id, url in links:
        text += f"• {url[:30]}... (ID: `{s_id}`)\n"
        keyboard.append([InlineKeyboardButton(text=f"Удалить {s_id}", callback_data=f"del_{s_id}")])
    
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("del_"))
async def delete_link_callback(callback: types.CallbackQuery):
    short_id = callback.data.split("_")[1]
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем данные перед удалением для хэширования
        async with db.execute("SELECT original_url, creator_id FROM short_links WHERE short_id = ?", (short_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                url, creator_id = row
                url_hash = hashlib.sha256(url.encode()).hexdigest()
                
                # Сохраняем в таблицу удаленных
                await db.execute("""
                    INSERT INTO deleted_links_hash (hash_id, original_url_hash, deleted_at, creator_id)
                    VALUES (?, ?, ?, ?)
                """, (short_id, url_hash, datetime.now().isoformat(), creator_id))
                
                # Удаляем оригинал
                await db.execute("DELETE FROM short_links WHERE short_id = ?", (short_id,))
                await db.commit()
                await callback.answer("✅ Ссылка удалена и хэширована.")
                await callback.message.edit_text("Ссылка была успешно удалена.")
            else:
                await callback.answer("❌ Ссылка не найдена.")

@dp.message(Command("folders"))
async def cmd_folders(message: types.Message):
    # Упрощенная логика папок: просмотр существующих
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT folder_id, name FROM folders WHERE creator_id = ?", (message.from_user.id,)) as cursor:
            folders = await cursor.fetchall()
            
    if not folders:
        # Кнопка создания для примера
        kb = [[InlineKeyboardButton(text="Создать папку 'Работа'", callback_data="create_folder_work")]]
        return await message.answer("У вас пока нет папок.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

    text = "🗂 Ваши папки:\n"
    for f_id, name in folders:
        text += f"• {name} (ID: `{f_id}`)\n"
    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "create_folder_work")
async def create_folder_example(callback: types.CallbackQuery):
    f_id = generate_id()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO folders (folder_id, name, creator_id) VALUES (?, ?, ?)", 
                         (f_id, "Работа", callback.from_user.id))
        await db.commit()
    await callback.message.edit_text(f"✅ Создана папка 'Работа' с ID: `{f_id}`", parse_mode="Markdown")

# --- MAIN ---
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Exit')