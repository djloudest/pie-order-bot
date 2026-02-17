# bot.py
# Telegram-бот для учета заказов пирогов с PostgreSQL на Heroku (async SQLAlchemy + asyncpg)
# Простые сообщения для мамы, даты DD.MM.YYYY HH:MM, несколько админов

import logging
import os
import asyncio
import json
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import SQLAlchemyError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Логи
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('8341079933:AAF_QwmChqgTq_6m6Wsq3kGCNcfCiUi_l5M')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")

ADMIN_IDS = [1037463389, 1911702126]  # ← Замени на реальные ID (твой и мамы)

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

# PostgreSQL настройка
DATABASE_URL = os.environ.get('postgres://ud0gm7lgk67rb7:pc4aa9be7fc209fc60fecd82e313c667f9289f0f810d0f265f89348180489aae4@c4pml560q9pviv.cluster-czz5s0kz4scl.eu-west-1.rds.amazonaws.com:5432/dbb5s8n2m36qkt')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не найден! Проверь Heroku Config Vars.")

# Heroku использует postgres://, SQLAlchemy хочет postgresql://
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

# Модели
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
    pies = Column(Text, nullable=False)  # JSON string
    order_date = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    delivery_date = Column(DateTime, nullable=False)
    total_price = Column(Integer, nullable=False)
    status = Column(String, default='new')
    client = relationship("Client", back_populates="orders")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

scheduler = AsyncIOScheduler()

# Создание таблиц при запуске (один раз)
async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# Асинхронные функции БД
async def add_client(name: str, phone: str) -> int:
    async with async_session() as session:
        async with session.begin():
            client = Client(name=name, phone=phone)
            session.add(client)
            await session.commit()
            await session.refresh(client)
            return client.id

async def get_clients() -> list:
    async with async_session() as session:
        result = await session.execute(text("SELECT id, name, phone FROM clients"))
        return result.fetchall()

async def find_client_by_name_or_phone(query: str) -> list:
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id, name, phone FROM clients WHERE name ILIKE :q OR phone ILIKE :q"),
            {"q": f"%{query}%"}
        )
        return result.fetchall()

async def add_order(client_id: int, address: str, pies: list, delivery_date: str) -> int:
    total_price = sum(PRICES[pie['name']] * pie['quantity'] for pie in pies)
    pies_json = json.dumps(pies)
    async with async_session() as session:
        async with session.begin():
            order = Order(
                client_id=client_id,
                delivery_address=address,
                pies=pies_json,
                delivery_date=datetime.fromisoformat(delivery_date),
                total_price=total_price
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            return order.id

async def get_orders_by_date(start_iso: str, end_iso: str) -> list:
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT o.id, c.name, c.phone, o.delivery_address, o.pies, o.delivery_date, o.total_price, o.status
                FROM orders o JOIN clients c ON o.client_id = c.id
                WHERE o.delivery_date BETWEEN :start AND :end
            """),
            {"start": start_iso, "end": end_iso}
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
    start = (now + timedelta(hours=23)).isoformat()
    end = (now + timedelta(hours=25)).isoformat()
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

# FSM формы (остались почти без изменений)
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

# ... (все хендлеры остаются такими же, как в предыдущей версии, только замени функции БД на async версии выше)
# Например:
# client_id = await add_client(name, phone)
# clients = await find_client_by_name_or_phone(query)
# и т.д.

# Для напоминаний
async def send_reminders():
    orders = await get_upcoming_orders()
    for o in orders:
        pies = json.loads(o[3])
        pies_str = ', '.join(f"{p['name']} x {p['quantity']}" for p in pies)
        delivery_date = datetime.fromisoformat(o[2]).strftime(DATE_FORMAT)
        reminder = f"Напоминание: Заказ для {o[1]} на {delivery_date}.\nПироги: {pies_str}\nНе забудьте подготовить!"
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, reminder)
            except Exception as e:
                logger.error(f"Не удалось отправить напоминание админу {admin_id}: {e}")

# Запуск
async def on_startup():
    await create_tables()
    logger.info("Таблицы созданы или уже существуют")
    scheduler.add_job(send_reminders, IntervalTrigger(hours=1))
    scheduler.start()
    logger.info("Scheduler запущен")

async def main():
    await on_startup()
    try:
        await dp.start_polling()
    finally:
        await engine.dispose()

if __name__ == '__main__':
    asyncio.run(main())
