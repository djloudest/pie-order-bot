# bot.py
# Telegram-бот для учета заказов пирогов. Использует aiogram для асинхронности, SQLite для БД, APScheduler для напоминаний.
# Разработан как опытный Python-разработчик: с FSM для состояний, обработкой ошибок, логгированием.
# Предполагаем, что бот используется только одним пользователем (мамой или тобой) — добавьте ваш Telegram user_id в ADMIN_ID.
# Для напоминаний: отправляются администратору за 24 часа до доставки.
# Прайс-лист фиксированный, но можно расширить.
# Установка: pip install aiogram apscheduler

import logging
import os
import sqlite3
import json
from datetime import datetime, timedelta
import asyncio

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# BOT_TOKEN = os.environ.get('BOT_TOKEN')          # это для Heroku
BOT_TOKEN = "8341079933:AAF_QwmChqgTq_6m6Wsq3kGCNcfCiUi_l5M"   # ← твой реальный токен здесь

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден! Проверь код.")

# ID администратора (замените на свой Telegram user_id, получите из /start)
ADMIN_ID = 1911702126  # <-- Замените на реальный ID!

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

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Scheduler для напоминаний
scheduler = AsyncIOScheduler()

# Подключение к БД
DB_FILE = 'pie_orders.db'
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()

# Создание таблиц, если не существуют
cursor.execute('''
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL
)
''')
cursor.execute('''
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    delivery_address TEXT,
    pies TEXT NOT NULL,  -- JSON: [{"name": "Пирог", "quantity": 1}]
    order_date DATETIME DEFAULT CURRENT_TIMESTAMP,
    delivery_date DATETIME NOT NULL,
    total_price INTEGER NOT NULL,
    status TEXT DEFAULT 'new',
    FOREIGN KEY (client_id) REFERENCES clients (id)
)
''')
conn.commit()

# Функции для работы с БД
def add_client(name: str, phone: str) -> int:
    cursor.execute('INSERT INTO clients (name, phone) VALUES (?, ?)', (name, phone))
    conn.commit()
    return cursor.lastrowid

def get_clients() -> list:
    cursor.execute('SELECT id, name, phone FROM clients')
    return cursor.fetchall()

def find_client_by_name_or_phone(query: str) -> list:
    cursor.execute('SELECT id, name, phone FROM clients WHERE name LIKE ? OR phone LIKE ?', (f'%{query}%', f'%{query}%'))
    return cursor.fetchall()

def add_order(client_id: int, address: str, pies: list, delivery_date: str) -> int:
    total_price = sum(PRICES[pie['name']] * pie['quantity'] for pie in pies)
    pies_json = json.dumps(pies)
    cursor.execute('''
    INSERT INTO orders (client_id, delivery_address, pies, delivery_date, total_price)
    VALUES (?, ?, ?, ?, ?)
    ''', (client_id, address, pies_json, delivery_date, total_price))
    conn.commit()
    return cursor.lastrowid

def get_orders_by_date(start_date: str, end_date: str) -> list:
    cursor.execute('''
    SELECT o.id, c.name, c.phone, o.delivery_address, o.pies, o.delivery_date, o.total_price, o.status
    FROM orders o JOIN clients c ON o.client_id = c.id
    WHERE o.delivery_date BETWEEN ? AND ?
    ''', (start_date, end_date))
    return cursor.fetchall()

def delete_order(order_id: int) -> bool:
    cursor.execute('DELETE FROM orders WHERE id = ?', (order_id,))
    conn.commit()
    return cursor.rowcount > 0

def get_upcoming_orders() -> list:
    now = datetime.now()
    in_24h_start = now + timedelta(hours=23)  # Окно от 23 до 25 часов для напоминания
    in_24h_end = now + timedelta(hours=25)
    cursor.execute('''
    SELECT o.id, c.name, o.delivery_date, o.pies
    FROM orders o JOIN clients c ON o.client_id = c.id
    WHERE o.status = 'new' AND o.delivery_date BETWEEN ? AND ?
    ''', (in_24h_start.isoformat(), in_24h_end.isoformat()))
    return cursor.fetchall()

# FSM состояния для добавления клиента
class AddClientForm(StatesGroup):
    name = State()
    phone = State()

# FSM для нового заказа
class NewOrderForm(StatesGroup):
    client = State()
    new_client_name = State()
    new_client_phone = State()
    address = State()
    pies = State()
    quantity = State()
    delivery_date = State()

# Текущее выбранное пирог (для количества)
current_pie = ""

# Проверка на админа
async def is_admin(message: types.Message) -> bool:
    return message.from_user.id == ADMIN_ID

# Старт и помощь
@dp.message_handler(commands=['start', 'help'])
async def start(message: types.Message):
    if not await is_admin(message):
        return
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton('/add_client'), KeyboardButton('/new_order'))
    keyboard.add(KeyboardButton('/report'), KeyboardButton('/delete_order'))
    await message.reply("Привет! Я бот для учета заказов пирогов.\nКоманды:\n/add_client - Добавить клиента\n/new_order - Новый заказ\n/report - Отчет по датам\n/delete_order - Удалить заказ", reply_markup=keyboard)

# Добавление клиента
@dp.message_handler(commands=['add_client'])
async def add_client_start(message: types.Message):
    if not await is_admin(message):
        return
    await AddClientForm.name.set()
    await message.reply("Введите имя клиента:")

@dp.message_handler(state=AddClientForm.name)
async def add_client_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await AddClientForm.phone.set()
    await message.reply("Введите номер телефона:")

@dp.message_handler(state=AddClientForm.phone)
async def add_client_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data['name']
    phone = message.text
    client_id = add_client(name, phone)
    await state.finish()
    await message.reply(f"Клиент добавлен: {name} ({phone}), ID: {client_id}")

# Новый заказ
@dp.message_handler(commands=['new_order'])
async def new_order_start(message: types.Message):
    if not await is_admin(message):
        return
    await NewOrderForm.client.set()
    await message.reply("Введите имя или номер клиента (или 'новый' для добавления):")

@dp.message_handler(state=NewOrderForm.client)
async def new_order_client(message: types.Message, state: FSMContext):
    query = message.text.strip().lower()
    if query == 'новый':
        await NewOrderForm.new_client_name.set()
        await message.reply("Введите имя нового клиента:")
        return
    clients = find_client_by_name_or_phone(query)
    if not clients:
        await message.reply("Клиент не найден. Введите 'новый' для добавления.")
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
    await message.reply("Введите адрес доставки:")

@dp.callback_query_handler(Text(startswith='client_'), state=NewOrderForm.client)
async def select_client(callback: types.CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split('_')[1])
    await state.update_data(client_id=client_id)
    await NewOrderForm.address.set()
    await callback.message.reply("Введите адрес доставки:")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.new_client_name)
async def new_client_name(message: types.Message, state: FSMContext):
    await state.update_data(new_client_name=message.text)
    await NewOrderForm.new_client_phone.set()
    await message.reply("Введите номер телефона нового клиента:")

@dp.message_handler(state=NewOrderForm.new_client_phone)
async def new_client_phone(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data['new_client_name']
    phone = message.text
    client_id = add_client(name, phone)
    await state.update_data(client_id=client_id)
    await NewOrderForm.address.set()
    await message.reply(f"Новый клиент добавлен. Введите адрес доставки:")

@dp.message_handler(state=NewOrderForm.address)
async def new_order_address(message: types.Message, state: FSMContext):
    await state.update_data(address=message.text, pies=[])
    await NewOrderForm.pies.set()
    keyboard = InlineKeyboardMarkup(row_width=2)
    for pie in PRICES.keys():
        keyboard.add(InlineKeyboardButton(pie, callback_data=f"pie_{pie}"))
    keyboard.add(InlineKeyboardButton("Завершить выбор", callback_data="pies_done"))
    await message.reply("Выберите пироги:", reply_markup=keyboard)

@dp.callback_query_handler(Text(startswith='pie_'), state=NewOrderForm.pies)
async def select_pie(callback: types.CallbackQuery, state: FSMContext):
    global current_pie
    current_pie = callback.data.split('_')[1]
    await NewOrderForm.quantity.set()
    await callback.message.reply(f"Введите количество для '{current_pie}':")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.quantity)
async def new_order_quantity(message: types.Message, state: FSMContext):
    try:
        quantity = int(message.text)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        await message.reply("Введите положительное число.")
        return
    data = await state.get_data()
    pies = data['pies']
    pies.append({"name": current_pie, "quantity": quantity})
    await state.update_data(pies=pies)
    await NewOrderForm.pies.set()
    await message.reply("Пирог добавлен. Выберите следующий или завершите.")

@dp.callback_query_handler(Text('pies_done'), state=NewOrderForm.pies)
async def pies_done(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('pies'):
        await callback.message.reply("Добавьте хотя бы один пирог.")
        await callback.answer()
        return
    await NewOrderForm.delivery_date.set()
    await callback.message.reply("Введите дату и время доставки (YYYY-MM-DD HH:MM):")
    await callback.answer()

@dp.message_handler(state=NewOrderForm.delivery_date)
async def new_order_delivery_date(message: types.Message, state: FSMContext):
    try:
        delivery_date = datetime.strptime(message.text, '%Y-%m-%d %H:%M').isoformat()
    except ValueError:
        await message.reply("Неверный формат. Пример: 2026-02-18 14:00")
        return
    data = await state.get_data()
    order_id = add_order(data['client_id'], data['address'], data['pies'], delivery_date)
    await state.finish()
    pies_str = ', '.join(f"{p['name']} x{p['quantity']}" for p in data['pies'])
    await message.reply(f"Заказ добавлен! ID: {order_id}\nКлиент: {data['client_id']}\nПироги: {pies_str}\nДоставка: {message.text}")

# Отчет
@dp.message_handler(commands=['report'])
async def report_start(message: types.Message):
    if not await is_admin(message):
        return
    await message.reply("Введите период (start_date end_date, пример: 2026-02-01 2026-02-28):")

@dp.message_handler(lambda message: len(message.text.split()) == 2)
async def generate_report(message: types.Message):
    if not await is_admin(message):
        return
    try:
        start, end = message.text.split()
        datetime.strptime(start, '%Y-%m-%d')
        datetime.strptime(end, '%Y-%m-%d')
    except ValueError:
        await message.reply("Неверный формат дат.")
        return
    orders = get_orders_by_date(start, end)
    if not orders:
        await message.reply("Нет заказов за этот период.")
        return
    report = f"Отчет за {start} - {end}:\n"
    total_orders = len(orders)
    total_sum = 0
    for o in orders:
        pies = json.loads(o[4])
        pies_str = ', '.join(f"{p['name']} x{p['quantity']}" for p in pies)
        report += f"ID: {o[0]}, Клиент: {o[1]} ({o[2]}), Адрес: {o[3]}, Пироги: {pies_str}, Дата: {o[5]}, Сумма: {o[6]} тг, Статус: {o[7]}\n"
        total_sum += o[6]
    report += f"\nИтого заказов: {total_orders}, Общая сумма: {total_sum} тг"
    await message.reply(report)

# Удаление заказа
@dp.message_handler(commands=['delete_order'])
async def delete_order_start(message: types.Message):
    if not await is_admin(message):
        return
    await message.reply("Введите ID заказа для удаления:")

@dp.message_handler(lambda message: message.text.isdigit(), state='*')
async def process_delete_order(message: types.Message):
    if not await is_admin(message):
        return
    order_id = int(message.text)
    if delete_order(order_id):
        await message.reply(f"Заказ {order_id} удален.")
    else:
        await message.reply("Заказ не найден.")

# Функция напоминаний
async def send_reminders():
    orders = get_upcoming_orders()
    for o in orders:
        pies = json.loads(o[3])
        pies_str = ', '.join(f"{p['name']} x{p['quantity']}" for p in pies)
        reminder = f"Напоминание: Заказ ID {o[0]} для {o[1]} на {o[2]}.\nПироги: {pies_str}"
        await bot.send_message(ADMIN_ID, reminder)
        # Опционально: обновить статус на 'reminded'

# Запуск scheduler
scheduler.add_job(send_reminders, IntervalTrigger(hours=1))  # Проверять каждый час
scheduler.start()

# Запуск бота
async def main():
    try:
        await dp.start_polling()
    finally:
        conn.close()

if __name__ == '__main__':
    asyncio.run(main())