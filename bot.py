# bot.py
# Telegram-бот для учёта заказов пирогов (для мамы)
# PostgreSQL на Heroku, aiogram 2.x, async SQLAlchemy + asyncpg
# Простые сообщения, даты ДД.ММ.ГГГГ ЧЧ:ММ, несколько админов

import logging
import os
import asyncio
import json
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Токен и админы
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в Config Vars!")

ADMIN_IDS = [1037463389, 1911702126]  # ← Замените на реальные Telegram ID (ваш и мамы)

# Прайс-лист
PRICES = {
    "Мясо с тыквой": 9000,
    "Мясо с картошкой": 9000,
    "Мясо с капустой": 9000,
    "Курица с картошкой": 7000,
    "Курица с капустой": 7000,
    "Курица с грибами": 10000,
    "Морской язык": 8000,
    "Брынза со шпинатом": 8000,
    "Яблочный": 7000,
    "Творожный": 7000,
    "Сёмга": 20000,
    "Восточный": 10000
}

DATE_FORMAT = '%d.%m.%Y %H:%M'

# PostgreSQL
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не найден в Config Vars!")

DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

class Client(Base):
    __tablename__ = 'clients'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    orders = relationship("Order", back_populates="client")

class Order(Base):
    __tablename__ = 'orders'
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey('clients.id'), nullable=False)
    delivery_address = Column(Text)
    pies = Column(Text, nullable=False)          # JSON
    order_date = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    delivery_date = Column(DateTime, nullable=False)
    total_price = Column(Integer, nullable=False)
    status = Column(String, default='new')
    client = relationship("Client", back_populates="orders")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

scheduler = AsyncIOScheduler()

# Создание таблиц
async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# БД-функции (async)
async def add_client(name: str, phone: str) -> int:
    async with async_session() as session:
        async with session.begin():
            client = Client(name=name, phone=phone)
            session.add(client)
            await session.commit()
            await session.refresh(client)
            return client.id

async def find_client_by_name_or_phone(query: str) -> list:
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id, name, phone FROM clients WHERE name ILIKE :q OR phone ILIKE :q"),
            {"q": f"%{query}%"}
        )
        return result.fetchall()

async def add_order(client_id: int, address: str, pies: list, delivery_dt: datetime) -> int:
    total = sum(PRICES.get(p['name'], 0) * p['quantity'] for p in pies)
    pies_json = json.dumps(pies)
    async with async_session() as session:
        async with session.begin():
            order = Order(
                client_id=client_id,
                delivery_address=address,
                pies=pies_json,
                delivery_date=delivery_dt,
                total_price=total
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            return order.id

async def get_orders_by_date(start_dt: datetime, end_dt: datetime) -> list:
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT o.id, c.name, c.phone, o.delivery_address, o.pies, o.delivery_date,
                       o.total_price, o.status
                FROM orders o JOIN clients c ON o.client_id = c.id
                WHERE o.delivery_date BETWEEN :start AND :end
            """),
            {"start": start_dt, "end": end_dt}
        )
        return result.fetchall()

async def delete_order(order_id: int) -> bool:
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                text("DELETE FROM orders WHERE id = :id RETURNING id"),
                {"id": order_id}
            )
            await session.commit()
            return result.rowcount > 0

async def get_upcoming_orders() -> list:
    now = datetime.now()
    start = now + timedelta(hours=23)
    end = now + timedelta(hours=25)
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT o.id, c.name, o.delivery_date, o.pies
                FROM orders o JOIN clients c ON o.client_id = c.id
                WHERE o.status = 'new' AND o.delivery_date BETWEEN :start AND :end
            """),
            {"start": start, "end": end}
        )
        return result.fetchall()

# FSM
class AddClientForm(StatesGroup):
    name = State()
    phone = State()

class NewOrderForm(StatesGroup):
    client = State()
    new_client_name = State()
    new_client_phone = State()
    address = State()
    pies = State()
    quantity = State()
    delivery_date = State()

current_pie = ""

async def is_admin(message: types.Message) -> bool:
    return message.from_user.id in ADMIN_IDS

# Старт / помощь
@dp.message_handler(commands=['start', 'help'])
async def cmd_start(message: types.Message):
    if not await is_admin(message):
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton('/add_client (Добавить клиента)'),
        KeyboardButton('/new_order (Новый заказ)')
    )
    kb.add(
        KeyboardButton('/report (Отчет)'),
        KeyboardButton('/delete_order (Удалить заказ)')
    )
    await message.answer(
        "Привет! Я помогу вести учёт заказов пирогов.\n\n"
        "Что я умею:\n"
        "• /add_client — добавить нового клиента\n"
        "• /new_order — оформить заказ\n"
        "• /report — посмотреть заказы за период\n"
        "• /delete_order — удалить заказ\n\n"
        "Просто пиши команды или спрашивай, если что-то непонятно!",
        reply_markup=kb
    )

# Добавление клиента
@dp.message_handler(commands=['add_client'])
async def cmd_add_client(message: types.Message):
    if not await is_admin(message):
        return
    await AddClientForm.name.set()
    await message.answer("Как зовут клиента?")

@dp.message_handler(state=AddClientForm.name)
async def add_client_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await AddClientForm.phone.set()
    await message.answer("Номер телефона клиента?")

@dp.message_handler(state=AddClientForm.phone)
async def add_client_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data['name']
    phone = message.text.strip()
    await add_client(name, phone)
    await state.finish()
    await message.answer(f"Клиент {name} (тел. {phone}) добавлен ✓")

# Новый заказ (остальная логика аналогична предыдущим версиям, но с lambda-фильтрами)

@dp.message_handler(commands=['new_order'])
async def cmd_new_order(message: types.Message):
    if not await is_admin(message):
        return
    await NewOrderForm.client.set()
    await message.answer("Имя или номер телефона клиента?\n(или напиши 'новый')")

@dp.message_handler(state=NewOrderForm.client)
async def new_order_client(message: types.Message, state: FSMContext):
    txt = message.text.strip().lower()
    if txt == 'новый':
        await NewOrderForm.new_client_name.set()
        await message.answer("Имя нового клиента?")
        return

    clients = await find_client_by_name_or_phone(txt)
    if not clients:
        await message.answer("Никого не нашёл. Напиши 'новый' для добавления.")
        return

    if len(clients) > 1:
        kb = InlineKeyboardMarkup(row_width=1)
        for cid, name, phone in clients:
            kb.add(InlineKeyboardButton(f"{name} ({phone})", callback_data=f"client_{cid}"))
        await message.answer("Выбери клиента:", reply_markup=kb)
        return

    await state.update_data(client_id=clients[0][0])
    await NewOrderForm.address.set()
    await message.answer("Адрес доставки?")

@dp.callback_query_handler(lambda c: c.data.startswith('client_'), state=NewOrderForm.client)
async def select_client(callback: types.CallbackQuery, state: FSMContext):
    cid = int(callback.data.split('_')[1])
    await state.update_data(client_id=cid)
    await NewOrderForm.address.set()
    await callback.message.edit_text("Адрес доставки?")
    await callback.answer()

# ... (остальные хендлеры new_order: new_client_name, new_client_phone, address, pies, quantity, pies_done, delivery_date)
# Они почти идентичны предыдущим версиям, просто используй await для функций БД

# Пример для pies_done и delivery_date (остальные аналогично)

@dp.callback_query_handler(lambda c: c.data == 'pies_done', state=NewOrderForm.pies)
async def pies_done(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('pies'):
        await callback.message.answer("Добавь хотя бы один пирог.")
        await callback.answer()
        return
    await NewOrderForm.delivery_date.set()
    await callback.message.edit_text("Когда доставить?\nФормат: 18.02.2026 14:00")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.delivery_date)
async def process_delivery_date(message: types.Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text.strip(), DATE_FORMAT)
    except ValueError:
        await message.answer("Неправильный формат. Пример: 18.02.2026 14:00")
        return

    data = await state.get_data()
    order_id = await add_order(
        data['client_id'],
        data['address'],
        data['pies'],
        dt
    )
    pies_str = ", ".join(f"{p['name']} ×{p['quantity']}" for p in data['pies'])
    await state.finish()
    await message.answer(
        f"Заказ оформлен!\n"
        f"Пироги: {pies_str}\n"
        f"Доставка: {message.text}\n"
        f"Номер заказа: {order_id}\n\n"
        f"Если нужно удалить — /delete_order и номер"
    )

# Отчет, удаление, напоминания — аналогично предыдущим версиям

# Запуск
async def on_startup():
    await create_tables()
    logger.info("Таблицы созданы / проверены")
    scheduler.add_job(send_reminders, IntervalTrigger(hours=1))
    scheduler.start()
    logger.info("Напоминания запущены")

async def main():
    await on_startup()
    await bot.delete_webhook(drop_pending_updates=True)  # Очистка старых обновлений
    try:
        await dp.start_polling(drop_pending_updates=True)
    finally:
        await engine.dispose()

if __name__ == '__main__':
    asyncio.run(main())
