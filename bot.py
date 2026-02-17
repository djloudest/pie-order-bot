# bot.py — полный рабочий бот для мамы (заказы пирогов)
# aiogram 2.25.1 + PostgreSQL (Heroku) + APScheduler
# ID администраторов: твой и мамы уже вставлены

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
    raise ValueError("BOT_TOKEN не установлен в Config Vars!")

# Ваши ID (твой и мамы)
ADMIN_IDS = [1037463389, 1911702126]

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

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

scheduler = AsyncIOScheduler()

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def add_client(name: str, phone: str) -> int:
    async with async_session() as session:
        async with session.begin():
            client = Client(name=name, phone=phone)
            session.add(client)
            await session.commit()
            return client.id

async def find_client_by_name_or_phone(query: str) -> list:
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id, name, phone FROM clients WHERE name ILIKE :q OR phone ILIKE :q"),
            {"q": f"%{query}%"}
        )
        return result.fetchall()

async def add_order(client_id: int, address: str, pies: list, delivery_date: datetime) -> int:
    total_price = sum(PRICES.get(p['name'], 0) * p['quantity'] for p in pies)
    pies_json = json.dumps(pies)
    async with async_session() as session:
        async with session.begin():
            order = Order(
                client_id=client_id,
                delivery_address=address,
                pies=pies_json,
                delivery_date=delivery_date,
                total_price=total_price
            )
            session.add(order)
            await session.commit()
            return order.id

async def get_orders_by_date(start_dt: datetime, end_dt: datetime) -> list:
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT o.id, c.name, c.phone, o.delivery_address, o.pies, o.delivery_date, o.total_price, o.status
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
                text("DELETE FROM orders WHERE id = :id"),
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

async def send_reminders():
    orders = await get_upcoming_orders()
    for o in orders:
        try:
            pies = json.loads(o[3])
            pies_str = ', '.join(f"{p['name']} x{p['quantity']}" for p in pies)
            delivery_date = o[2].strftime(DATE_FORMAT)
            text = f"Напоминание!\nЗаказ для {o[1]} на {delivery_date}\nПироги: {pies_str}\nПодготовьтесь!"
            for admin_id in ADMIN_IDS:
                await bot.send_message(admin_id, text)
        except Exception as e:
            logger.error(f"Ошибка напоминания: {e}")

# FSM состояния
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

# Старт и меню
@dp.message_handler(commands=['start', 'help'])
async def start(message: types.Message):
    if not await is_admin(message):
        return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add('/add_client (Добавить клиента)', '/new_order (Новый заказ)')
    kb.add('/report (Отчет)', '/delete_order (Удалить заказ)')
    await message.reply(
        "Привет! Это бот для учёта заказов пирогов.\n\n"
        "Что умею:\n"
        "/add_client — добавить клиента\n"
        "/new_order — оформить заказ\n"
        "/report — посмотреть заказы за день или период\n"
        "/delete_order — удалить заказ\n\n"
        "Пиши команды, всё просто!",
        reply_markup=kb
    )

# Добавление клиента
@dp.message_handler(commands=['add_client'])
async def cmd_add_client(message: types.Message):
    if not await is_admin(message):
        return
    await AddClientForm.name.set()
    await message.reply("Как зовут клиента?")

@dp.message_handler(state=AddClientForm.name)
async def add_client_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await AddClientForm.phone.set()
    await message.reply("Номер телефона клиента?")

@dp.message_handler(state=AddClientForm.phone)
async def add_client_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data['name']
    phone = message.text.strip()
    await add_client(name, phone)
    await state.finish()
    await message.reply(f"Клиент добавлен: {name}, тел. {phone}")

# Новый заказ — порядок: клиент → адрес → пироги → готово → дата доставки
@dp.message_handler(commands=['new_order'])
async def cmd_new_order(message: types.Message):
    if not await is_admin(message):
        return
    await NewOrderForm.client.set()
    await message.reply("Имя или номер клиента?\n(или напиши 'новый')")

@dp.message_handler(state=NewOrderForm.client)
async def new_order_client(message: types.Message, state: FSMContext):
    txt = message.text.strip().lower()
    if txt == 'новый':
        await NewOrderForm.new_client_name.set()
        await message.reply("Имя нового клиента?")
        return

    clients = await find_client_by_name_or_phone(txt)
    if not clients:
        await message.reply("Не нашёл. Напиши 'новый' для нового клиента.")
        return

    if len(clients) > 1:
        kb = InlineKeyboardMarkup(row_width=1)
        for cl in clients:
            kb.add(InlineKeyboardButton(f"{cl[1]} ({cl[2]})", callback_data=f"client_{cl[0]}"))
        await message.reply("Выбери клиента:", reply_markup=kb)
        return

    await state.update_data(client_id=clients[0][0])
    await NewOrderForm.address.set()
    await message.reply("Куда доставить заказ?")

@dp.callback_query_handler(lambda c: c.data.startswith('client_'), state=NewOrderForm.client)
async def select_client(callback: types.CallbackQuery, state: FSMContext):
    cid = int(callback.data.split('_')[1])
    await state.update_data(client_id=cid)
    await NewOrderForm.address.set()
    await callback.message.edit_text("Куда доставить заказ?")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.new_client_name)
async def new_client_name(message: types.Message, state: FSMContext):
    await state.update_data(new_client_name=message.text.strip())
    await NewOrderForm.new_client_phone.set()
    await message.reply("Телефон нового клиента?")

@dp.message_handler(state=NewOrderForm.new_client_phone)
async def new_client_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data['new_client_name']
    phone = message.text.strip()
    cid = await add_client(name, phone)
    await state.update_data(client_id=cid)
    await NewOrderForm.address.set()
    await message.reply("Куда доставить заказ?")

@dp.message_handler(state=NewOrderForm.address)
async def new_order_address(message: types.Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    await NewOrderForm.pies.set()

    kb = InlineKeyboardMarkup(row_width=2)
    for pie in PRICES.keys():
        kb.insert(InlineKeyboardButton(pie, callback_data=f"pie_{pie}"))
    kb.add(InlineKeyboardButton("Готово", callback_data="pies_done"))

    await message.reply("Какие пироги заказали? Выбирай:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith('pie_'), state=NewOrderForm.pies)
async def select_pie(callback: types.CallbackQuery, state: FSMContext):
    global current_pie
    current_pie = callback.data.split('_')[1]
    await NewOrderForm.quantity.set()
    await callback.message.reply(f"Сколько '{current_pie}' нужно?")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.quantity)
async def new_order_quantity(message: types.Message, state: FSMContext):
    try:
        q = int(message.text.strip())
        if q <= 0:
            raise ValueError
    except:
        await message.reply("Введи нормальное число больше 0")
        return

    data = await state.get_data()
    pies = data.get('pies', [])
    pies.append({"name": current_pie, "quantity": q})
    await state.update_data(pies=pies)
    await NewOrderForm.pies.set()
    await message.reply("Добавлено. Ещё пирог или жмём Готово?")

@dp.callback_query_handler(lambda c: c.data == 'pies_done', state=NewOrderForm.pies)
async def pies_done(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('pies'):
        await callback.message.reply("Нужно выбрать хотя бы один пирог")
        await callback.answer()
        return

    await NewOrderForm.delivery_date.set()
    await callback.message.edit_text("Когда доставить?\nФормат: ДД.ММ.ГГГГ ЧЧ:ММ\nПример: 18.02.2026 14:00")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.delivery_date)
async def process_delivery_date(message: types.Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text.strip(), DATE_FORMAT)
    except ValueError:
        await message.reply("Неверный формат. Пример: 18.02.2026 14:00")
        return

    data = await state.get_data()
    order_id = await add_order(data['client_id'], data['address'], data['pies'], dt)
    pies_str = ", ".join(f"{p['name']} ×{p['quantity']}" for p in data['pies'])
    await state.finish()
    await message.reply(
        f"Заказ оформлен!\n"
        f"Пироги: {pies_str}\n"
        f"Доставка: {message.text}\n"
        f"Номер заказа: {order_id}\n\n"
        f"Удалить можно через /delete_order"
    )

# Отчет — поддержка одной или двух дат
@dp.message_handler(commands=['report'])
async def report_cmd(message: types.Message):
    if not await is_admin(message):
        return
    await message.reply(
        "Отчёт за какой период?\n"
        "• Одна дата: 18.02.2026 (только этот день)\n"
        "• Две даты: 01.02.2026 28.02.2026 (период)"
    )

@dp.message_handler(lambda m: 1 <= len(m.text.split()) <= 2)
async def generate_report(message: types.Message):
    if not await is_admin(message):
        return

    parts = message.text.strip().split()
    try:
        if len(parts) == 1:
            day = datetime.strptime(parts[0], '%d.%m.%Y')
            start = day
            end = day + timedelta(days=1) - timedelta(seconds=1)
            period = parts[0]
        else:
            start = datetime.strptime(parts[0], '%d.%m.%Y')
            end = datetime.strptime(parts[1], '%d.%m.%Y') + timedelta(days=1) - timedelta(seconds=1)
            period = f"{parts[0]} — {parts[1]}"

        orders = await get_orders_by_date(start, end)
        if not orders:
            await message.reply(f"За {period} заказов нет")
            return

        txt = f"Заказы за {period}:\n\n"
        total = 0
        for o in orders:
            pies = json.loads(o[4])
            pstr = ", ".join(f"{p['name']} ×{p['quantity']}" for p in pies)
            ddate = o[5].strftime(DATE_FORMAT)
            txt += f"#{o[0]} • {o[1]} ({o[2]}) • {o[3]}\n   {pstr}\n   {ddate} • {o[6]} тг\n\n"
            total += o[6]

        txt += f"Итого: {len(orders)} заказов, {total} тг"
        await message.reply(txt)

    except ValueError:
        await message.reply("Неверный формат. Примеры:\n18.02.2026\n01.02.2026 28.02.2026")

# Удаление заказа
@dp.message_handler(commands=['delete_order'])
async def cmd_delete_order(message: types.Message):
    if not await is_admin(message):
        return
    await message.reply("Номер заказа, который удалить:")

@dp.message_handler(lambda m: m.text.isdigit())
async def delete_order_handler(message: types.Message):
    if not await is_admin(message):
        return
    try:
        oid = int(message.text.strip())
        if await delete_order(oid):
            await message.reply(f"Заказ #{oid} удалён")
        else:
            await message.reply(f"Заказ #{oid} не найден")
    except:
        await message.reply("Введи только номер заказа (число)")

# Запуск бота
async def on_startup():
    await create_tables()
    logger.info("База готова")
    scheduler.add_job(send_reminders, IntervalTrigger(hours=1))
    scheduler.start()
    logger.info("Напоминания запущены")

async def main():
    await on_startup()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling()

if __name__ == '__main__':
    asyncio.run(main())
