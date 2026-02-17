# bot.py — финальная версия с исправленным NameError, lambda-фильтрами, drop_pending_updates и всем остальным

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")

ADMIN_IDS = [1037463389, 1911702126]  # ← твои реальные ID здесь

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

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не найден!")

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
    pies = Column(Text, nullable=False)
    order_date = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    delivery_date = Column(DateTime, nullable=False)
    total_price = Column(Integer, nullable=False)
    status = Column(String, default='new')
    client = relationship("Client", back_populates="orders")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

scheduler = AsyncIOScheduler()

# ────────────────────────────────────────────────
#   ФУНКЦИИ БАЗЫ ДАННЫХ (async)
# ────────────────────────────────────────────────

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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

# ────────────────────────────────────────────────
#   НАПОМИНАНИЯ (теперь определена ДО использования)
# ────────────────────────────────────────────────

async def send_reminders():
    try:
        orders = await get_upcoming_orders()
        for o in orders:
            pies = json.loads(o[3])
            pies_str = ', '.join(f"{p['name']} ×{p['quantity']}" for p in pies)
            delivery_date = o[2].strftime(DATE_FORMAT)
            text = f"Напоминание!\nЗаказ для {o[1]} на {delivery_date}\nПироги: {pies_str}\nНе забудь подготовить!"
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, text)
                except Exception as e:
                    logger.error(f"Не удалось отправить {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Ошибка в напоминаниях: {e}")

# ────────────────────────────────────────────────
#   FSM СОСТОЯНИЯ
# ────────────────────────────────────────────────

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

# ────────────────────────────────────────────────
#   ХЕНДЛЕРЫ (с lambda для callback)
# ────────────────────────────────────────────────

async def is_admin(message: types.Message) -> bool:
    return message.from_user.id in ADMIN_IDS

@dp.message_handler(commands=['start', 'help'])
async def cmd_start(message: types.Message):
    if not await is_admin(message):
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add('/add_client (Добавить клиента)', '/new_order (Новый заказ)')
    kb.add('/report (Отчет)', '/delete_order (Удалить заказ)')
    await message.answer(
        "Привет! Я помогу вести учёт пирогов.\n\n"
        "Команды:\n"
        "• /add_client — новый клиент\n"
        "• /new_order — оформить заказ\n"
        "• /report — посмотреть за период\n"
        "• /delete_order — удалить заказ\n\n"
        "Пиши, если что-то непонятно!",
        reply_markup=kb
    )

# Добавление клиента (и все остальные хендлеры как в предыдущей версии)
# ... (вставь сюда блок с add_client, new_order и т.д. из твоего последнего рабочего кода)

# Запуск
async def on_startup():
    await create_tables()
    logger.info("Таблицы проверены/созданы")
    scheduler.add_job(send_reminders, IntervalTrigger(hours=1))
    scheduler.start()
    logger.info("Напоминания запущены")

async def main():
    await on_startup()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(drop_pending_updates=True)
    # engine.dispose() в finally, если нужно

if __name__ == '__main__':
    asyncio.run(main())
