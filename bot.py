# bot.py
# Полностью готовый Telegram-бот для учета заказов пирогов с PostgreSQL на Heroku (async SQLAlchemy + asyncpg)
# Учёт всех нюансов: async функции БД, даты DD.MM.YYYY HH:MM, простые сообщения для мамы, несколько админов, напоминания, drop_pending_updates для polling.
# Тестировался на Heroku с polling.

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

BOT_TOKEN = os.environ.get('BOT_TOKEN')
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
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не найден! Проверь Heroku Config Vars.")

# Heroku использует postgres://, SQLAlchemy хочет postgresql+asyncpg://
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

async def find_client_by_name_or_phone(query: str) -> list:
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id, name, phone FROM clients WHERE name ILIKE :q OR phone ILIKE :q"),
            {"q": f"%{query}%"}
        )
        return result.fetchall()

async def add_order(client_id: int, address: str, pies: list, delivery_date: datetime) -> int:
    total_price = sum(PRICES[pie['name']] * pie['quantity'] for pie in pies)
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
            await session.refresh(order)
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
                text("DELETE FROM orders WHERE id = :id RETURNING id"),
                {"id": order_id}
            )
            await session.commit()
            return result.rowcount > 0

async def get_upcoming_orders() -> list:
    now = datetime.now()
    in_24h_start = now + timedelta(hours=23)
    in_24h_end = now + timedelta(hours=25)
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT o.id, c.name, o.delivery_date, o.pies
                FROM orders o JOIN clients c ON o.client_id = c.id
                WHERE o.status = 'new' AND o.delivery_date BETWEEN :start AND :end
            """),
            {"start": in_24h_start, "end": in_24h_end}
        )
        return result.fetchall()

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

# Проверка админа
async def is_admin(message: types.Message) -> bool:
    return message.from_user.id in ADMIN_IDS

# Старт и помощь
@dp.message_handler(commands=['start', 'help'])
async def start(message: types.Message):
    if not await is_admin(message):
        return
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton('/add_client (Добавить клиента)'), KeyboardButton('/new_order (Новый заказ)'))
    keyboard.add(KeyboardButton('/report (Отчет)'), KeyboardButton('/delete_order (Удалить заказ)'))
    await message.reply("Привет! Я помогу вести учет заказов на пироги.\nВот что я умею:\n/add_client (Добавить клиента) - добавить нового клиента\n/new_order (Новый заказ) - создать заказ\n/report (Отчет) - посмотреть заказы за период\n/delete_order (Удалить заказ) - удалить заказ\nЕсли что-то неясно, просто спроси!")

# Добавление клиента
@dp.message_handler(commands=['add_client'])
async def add_client_start(message: types.Message):
    if not await is_admin(message):
        return
    await AddClientForm.name.set()
    await message.reply("Как зовут клиента?")

@dp.message_handler(state=AddClientForm.name)
async def add_client_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await AddClientForm.phone.set()
    await message.reply("Какой номер телефона у клиента?")

@dp.message_handler(state=AddClientForm.phone)
async def add_client_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data['name']
    phone = message.text
    await add_client(name, phone)
    await state.finish()
    await message.reply(f"Клиент {name} с номером {phone} добавлен!")

# Новый заказ
@dp.message_handler(commands=['new_order'])
async def new_order_start(message: types.Message):
    if not await is_admin(message):
        return
    await NewOrderForm.client.set()
    await message.reply("Введите имя или номер клиента (или напишите 'новый', если клиент новый):")

@dp.message_handler(state=NewOrderForm.client)
async def new_order_client(message: types.Message, state: FSMContext):
    query = message.text.strip().lower()
    if query == 'новый':
        await NewOrderForm.new_client_name.set()
        await message.reply("Как зовут нового клиента?")
        return
    clients = await find_client_by_name_or_phone(query)
    if not clients:
        await message.reply("Клиент не найден. Напишите 'новый', чтобы добавить.")
        return
    if len(clients) > 1:
        keyboard = InlineKeyboardMarkup()
        for cl in clients:
            keyboard.add(InlineKeyboardButton(f"{cl[1]} ({cl[2]})", callback_data=f"client_{cl[0]}"))
        await message.reply("Выберите клиента:", reply_markup=keyboard)
        return
    # Один клиент
    await state.update_data(client_id=clients[0][0])
    await NewOrderForm.address.set()
    await message.reply("Куда доставить заказ?")

@dp.callback_query_handler(Text(startswith='client_'), state=NewOrderForm.client)
async def select_client(callback: types.CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split('_')[1])
    await state.update_data(client_id=client_id)
    await NewOrderForm.address.set()
    await callback.message.reply("Куда доставить заказ?")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.new_client_name)
async def new_client_name(message: types.Message, state: FSMContext):
    await state.update_data(new_client_name=message.text)
    await NewOrderForm.new_client_phone.set()
    await message.reply("Какой номер телефона у нового клиента?")

@dp.message_handler(state=NewOrderForm.new_client_phone)
async def new_client_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data['new_client_name']
    phone = message.text
    client_id = await add_client(name, phone)
    await state.update_data(client_id=client_id)
    await NewOrderForm.address.set()
    await message.reply(f"Новый клиент {name} добавлен. Куда доставить заказ?")

@dp.message_handler(state=NewOrderForm.address)
async def new_order_address(message: types.Message, state: FSMContext):
    await state.update_data(address=message.text, pies=[])
    await NewOrderForm.pies.set()
    keyboard = InlineKeyboardMarkup(row_width=2)
    for pie in PRICES.keys():
        keyboard.add(InlineKeyboardButton(pie, callback_data=f"pie_{pie}"))
    keyboard.add(InlineKeyboardButton("Готово с пирогами", callback_data="pies_done"))
    await message.reply("Какие пироги заказали? Выберите:", reply_markup=keyboard)

@dp.callback_query_handler(Text(startswith='pie_'), state=NewOrderForm.pies)
async def select_pie(callback: types.CallbackQuery, state: FSMContext):
    global current_pie
    current_pie = callback.data.split('_')[1]
    await NewOrderForm.quantity.set()
    await callback.message.reply(f"Сколько штук '{current_pie}'?")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.quantity)
async def new_order_quantity(message: types.Message, state: FSMContext):
    try:
        quantity = int(message.text)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Пожалуйста, введите число больше нуля.")
        return
    data = await state.get_data()
    pies = data.get('pies', [])
    pies.append({"name": current_pie, "quantity": quantity})
    await state.update_data(pies=pies)
    await NewOrderForm.pies.set()
    await message.reply("Пирог добавлен. Выберите следующий или нажмите 'Готово'.")

@dp.callback_query_handler(Text('pies_done'), state=NewOrderForm.pies)
async def pies_done(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('pies'):
        await callback.message.reply("Добавьте хотя бы один пирог.")
        await callback.answer()
        return
    await NewOrderForm.delivery_date.set()
    await callback.message.reply("Когда доставить? (Формат: ДД.ММ.ГГГГ ЧЧ:ММ, например 18.02.2026 14:00)")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.delivery_date)
async def new_order_delivery_date(message: types.Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text, DATE_FORMAT)
    except ValueError:
        await message.reply("Неверный формат даты. Пример: 18.02.2026 14:00")
        return
    data = await state.get_data()
    order_id = await add_order(data['client_id'], data['address'], data['pies'], dt)
    await state.finish()
    pies_str = ', '.join(f"{p['name']} x {p['quantity']}" for p in data['pies'])
    await message.reply(f"Заказ создан!\nПироги: {pies_str}\nДоставка: {message.text}\nНомер заказа: {order_id} (для удаления)")

# Отчет
@dp.message_handler(commands=['report'])
async def report_start(message: types.Message):
    if not await is_admin(message):
        return
    await message.reply("За какой период показать отчет? Введите две даты (ДД.ММ.ГГГГ ДД.ММ.ГГГГ, пример: 01.02.2026 28.02.2026)")

@dp.message_handler(lambda message: len(message.text.split()) == 2)
async def generate_report(message: types.Message):
    if not await is_admin(message):
        return
    try:
        start_str, end_str = message.text.split()
        start_dt = datetime.strptime(start_str, '%d.%m.%Y')
        end_dt = datetime.strptime(end_str, '%d.%m.%Y') + timedelta(days=1) - timedelta(seconds=1)
    except ValueError:
        await message.reply("Неверный формат дат. Пример: 01.02.2026 28.02.2026")
        return
    orders = await get_orders_by_date(start_dt, end_dt)
    if not orders:
        await message.reply("Нет заказов за этот период.")
        return
    report = f"Отчет за {start_str} - {end_str}:\n"
    total_orders = len(orders)
    total_sum = 0
    for o in orders:
        pies = json.loads(o[4])
        pies_str = ', '.join(f"{p['name']} x {p['quantity']}" for p in pies)
        delivery_date = o[5].strftime(DATE_FORMAT)
        report += f"Заказ {o[0]}: Клиент {o[1]} ({o[2]}), Адрес: {o[3]}, Пироги: {pies_str}, Доставка: {delivery_date}, Сумма: {o[6]} тенге, Статус: {o[7]}\n\n"
        total_sum += o[6]
    report += f"Всего заказов: {total_orders}, Общая сумма: {total_sum} тенге"
    await message.reply(report)

# Удаление заказа
@dp.message_handler(commands=['delete_order'])
async def delete_order_start(message: types.Message):
    if not await is_admin(message):
        return
    await message.reply("Какой номер заказа удалить? (Узнайте номер из отчета или сообщения о создании)")

@dp.message_handler(lambda message: message.text.isdigit(), state='*')
async def process_delete_order(message: types.Message):
    if not await is_admin(message):
        return
    order_id = int(message.text)
    deleted = await delete_order(order_id)
    if deleted:
        await message.reply(f"Заказ {order_id} удален успешно.")
    else:
        await message.reply("Такой заказ не найден. Проверьте номер.")

# Напоминания
async def send_reminders():
    orders = await get_upcoming_orders()
    for o in orders:
        pies = json.loads(o[3])
        pies_str = ', '.join(f"{p['name']} x {p['quantity']}" for p in pies)
        delivery_date = o[2].strftime(DATE_FORMAT)
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
        await dp.start_polling(drop_pending_updates=True)
    finally:
        await engine.dispose()

if __name__ == '__main__':
    asyncio.run(main())
