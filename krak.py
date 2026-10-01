import asyncio
import logging
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMINS = {
    5068550043,
    6977407005,
}

DB_FILE = "funpay.sqlite3"

FREEZE_DAYS = 3

BONUS_STARS = 200

# Акция: 1–25 октября
BONUS_START = (10, 1)
BONUS_END = (10, 25)

GIFT_RE = re.compile(
    r"^(?:https?://)?t\.me/nft/[^\s]+$",
    re.IGNORECASE
)

PRICE_RE = re.compile(
    r"^[1-9][0-9]*$"
)

CURRENCIES = {
    "RUB": "₽",
    "GRAM": "Gram",
    "STARS": "Stars",
    "USDT": "USDT",
}


if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN.\n"
        "Задайте переменную окружения BOT_TOKEN."
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.MARKDOWN
    )
)

dp = Dispatcher()


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def display_datetime():
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def init_db():

    with db() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,

                balance INTEGER NOT NULL DEFAULT 0,
                held_balance INTEGER NOT NULL DEFAULT 0,

                gram_balance INTEGER NOT NULL DEFAULT 0,
                gram_held_balance INTEGER NOT NULL DEFAULT 0,

                stars_balance INTEGER NOT NULL DEFAULT 0,
                stars_held_balance INTEGER NOT NULL DEFAULT 0,

                usdt_balance INTEGER NOT NULL DEFAULT 0,
                usdt_held_balance INTEGER NOT NULL DEFAULT 0,

                first_paid_at TEXT,
                bonus_claimed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                creator_id INTEGER NOT NULL,
                payer_id INTEGER,

                gift_url TEXT NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',

                status TEXT NOT NULL DEFAULT 'pending',

                created_at TEXT NOT NULL,
                paid_at TEXT,
                confirmed_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                deal_id INTEGER,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_holds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                deal_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                release_at TEXT NOT NULL,
                released INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS deposit_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)

        # ====================================================
        # MIGRATIONS
        # ====================================================

        columns = {
            x["name"]
            for x in conn.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        migrations = {
            "held_balance":
                "ALTER TABLE users ADD COLUMN held_balance INTEGER NOT NULL DEFAULT 0",

            "gram_balance":
                "ALTER TABLE users ADD COLUMN gram_balance INTEGER NOT NULL DEFAULT 0",

            "gram_held_balance":
                "ALTER TABLE users ADD COLUMN gram_held_balance INTEGER NOT NULL DEFAULT 0",

            "stars_balance":
                "ALTER TABLE users ADD COLUMN stars_balance INTEGER NOT NULL DEFAULT 0",

            "stars_held_balance":
                "ALTER TABLE users ADD COLUMN stars_held_balance INTEGER NOT NULL DEFAULT 0",

            "usdt_balance":
                "ALTER TABLE users ADD COLUMN usdt_balance INTEGER NOT NULL DEFAULT 0",

            "usdt_held_balance":
                "ALTER TABLE users ADD COLUMN usdt_held_balance INTEGER NOT NULL DEFAULT 0",

            "bonus_claimed":
                "ALTER TABLE users ADD COLUMN bonus_claimed INTEGER NOT NULL DEFAULT 0",
        }

        for column, sql in migrations.items():

            if column not in columns:
                conn.execute(sql)

        deal_columns = {
            x["name"]
            for x in conn.execute(
                "PRAGMA table_info(deals)"
            ).fetchall()
        }

        if "code" not in deal_columns:
            conn.execute(
                "ALTER TABLE deals ADD COLUMN code TEXT"
            )

            old_deals = conn.execute(
                "SELECT id FROM deals WHERE code IS NULL"
            ).fetchall()

            for deal in old_deals:

                code = generate_deal_code(conn)

                conn.execute("""
                    UPDATE deals
                    SET code = ?
                    WHERE id = ?
                """, (
                    code,
                    deal["id"]
                ))

        if "confirmed_at" not in deal_columns:
            conn.execute("""
                ALTER TABLE deals
                ADD COLUMN confirmed_at TEXT
            """)

        if "currency" not in deal_columns:
            conn.execute("""
                ALTER TABLE deals
                ADD COLUMN currency TEXT NOT NULL DEFAULT 'RUB'
            """)

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_deals_code
            ON deals(code)
        """)

        conn.commit()


# ============================================================
# DEAL CODE
# ============================================================

def generate_deal_code(conn):

    alphabet = string.ascii_uppercase + string.digits

    while True:

        code = "FP-" + "".join(
            secrets.choice(alphabet)
            for _ in range(6)
        )

        exists = conn.execute("""
            SELECT id
            FROM deals
            WHERE code = ?
            LIMIT 1
        """, (
            code,
        )).fetchone()

        if not exists:
            return code


# ============================================================
# USERS
# ============================================================

def ensure_user(user):

    with db() as conn:

        existing = conn.execute("""
            SELECT user_id
            FROM users
            WHERE user_id = ?
        """, (
            user.id,
        )).fetchone()

        if existing:

            conn.execute("""
                UPDATE users
                SET
                    username = ?,
                    first_name = ?
                WHERE user_id = ?
            """, (
                user.username,
                user.first_name,
                user.id
            ))

        else:

            conn.execute("""
                INSERT INTO users (
                    user_id,
                    username,
                    first_name,
                    created_at
                )
                VALUES (?, ?, ?, ?)
            """, (
                user.id,
                user.username,
                user.first_name,
                now_iso()
            ))

        conn.commit()


# ============================================================
# BALANCES
# ============================================================

def balance_column(currency):
    return {
        "RUB": "balance",
        "GRAM": "gram_balance",
        "STARS": "stars_balance",
        "USDT": "usdt_balance",
    }[currency]


def held_column(currency):
    return {
        "RUB": "held_balance",
        "GRAM": "gram_held_balance",
        "STARS": "stars_held_balance",
        "USDT": "usdt_held_balance",
    }[currency]


def get_balance(user_id, currency="RUB"):

    release_expired_holds()

    column = balance_column(currency)

    with db() as conn:

        row = conn.execute(
            f"""
            SELECT {column}
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

        if not row:
            return 0

        return int(row[column])


def get_held_balance(user_id, currency="RUB"):

    column = held_column(currency)

    with db() as conn:

        row = conn.execute(
            f"""
            SELECT {column}
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

        if not row:
            return 0

        return int(row[column])


def fmt(amount):
    return f"{int(amount):,}".replace(",", " ")


def currency_name(currency):
    return CURRENCIES.get(currency, currency)


# ============================================================
# ACTIVE DEALS
# ============================================================

def get_active_deals_count(user_id):

    with db() as conn:

        row = conn.execute("""
            SELECT COUNT(*) AS count
            FROM deals
            WHERE creator_id = ?
            AND status IN ('pending', 'paid')
        """, (
            user_id,
        )).fetchone()

        return int(row["count"])


# ============================================================
# BONUS
# ============================================================

def bonus_available():

    now = datetime.now()

    if now.month != BONUS_START[0]:
        return False

    return BONUS_START[1] <= now.day <= BONUS_END[1]


def user_completed_deal(user_id):

    with db() as conn:

        row = conn.execute("""
            SELECT id
            FROM deals
            WHERE status = 'confirmed'
            AND (
                creator_id = ?
                OR payer_id = ?
            )
            LIMIT 1
        """, (
            user_id,
            user_id
        )).fetchone()

        return bool(row)


def can_claim_bonus(user_id):

    if not bonus_available():
        return False

    with db() as conn:

        user = conn.execute("""
            SELECT bonus_claimed
            FROM users
            WHERE user_id = ?
        """, (
            user_id,
        )).fetchone()

        if not user:
            return False

        if int(user["bonus_claimed"]) == 1:
            return False

    return user_completed_deal(user_id)


# ============================================================
# RELEASE HOLDS
# ============================================================

def release_expired_holds():

    now = datetime.now(timezone.utc)

    with db() as conn:

        holds = conn.execute("""
            SELECT *
            FROM balance_holds
            WHERE released = 0
        """).fetchall()

        for hold in holds:

            try:
                release_at = datetime.fromisoformat(
                    hold["release_at"]
                )

                if release_at.tzinfo is None:
                    release_at = release_at.replace(
                        tzinfo=timezone.utc
                    )

            except Exception:
                logging.exception(
                    "Ошибка даты удержания %s",
                    hold["id"]
                )
                continue

            if now < release_at:
                continue

            amount = int(hold["amount"])
            user_id = int(hold["user_id"])
            currency = hold["currency"]

            balance_col = balance_column(currency)
            held_col = held_column(currency)

            cursor = conn.execute(
                f"""
                UPDATE users
                SET
                    {held_col} = {held_col} - ?,
                    {balance_col} = {balance_col} + ?
                WHERE user_id = ?
                AND {held_col} >= ?
                """,
                (
                    amount,
                    amount,
                    user_id,
                    amount
                )
            )

            if cursor.rowcount != 1:
                logging.error(
                    "Не удалось освободить удержание %s",
                    hold["id"]
                )
                continue

            conn.execute("""
                UPDATE balance_holds
                SET released = 1
                WHERE id = ?
            """, (
                hold["id"],
            ))

            conn.execute("""
                INSERT INTO operations (
                    user_id,
                    kind,
                    amount,
                    currency,
                    deal_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                "hold_released",
                amount,
                currency,
                hold["deal_id"],
                now_iso()
            ))

        conn.commit()


async def release_holds_loop():

    while True:

        try:
            release_expired_holds()

        except Exception:
            logging.exception(
                "Ошибка освобождения удержаний"
            )

        await asyncio.sleep(60)


# ============================================================
# STATES
# ============================================================

user_states = {}


def set_state(user_id, state):
    user_states[user_id] = state


def get_state(user_id):
    return user_states.get(user_id)


def clear_state(user_id):
    user_states.pop(user_id, None)


# ============================================================
# BUTTON
# ============================================================

def button(text, callback_data, style=None):

    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        style=style
    )


# ============================================================
# MAIN KEYBOARD
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "🔷 Создать сделку 🔷",
                    "menu:create",
                    "primary"
                )
            ],
            [
                button(
                    "Кошелек",
                    "menu:wallet",
                    "success"
                )
            ],
            [
                button(
                    "Бонусы",
                    "menu:bonus",
                    "success"
                )
            ],
            [
                button(
                    "О сервисе",
                    "menu:about",
                    "success"
                )
            ],
            [
                button(
                    "Поддержка",
                    "menu:support",
                    "success"
                )
            ]
        ]
    )


def back_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "Выйти",
                    "menu:home",
                    "danger"
                )
            ]
        ]
    )


# ============================================================
# CURRENCY KEYBOARDS
# ============================================================

def deposit_currency_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("₽", "deposit:currency:RUB", "primary"),
                button("Gram", "deposit:currency:GRAM", "primary"),
                button("Stars", "deposit:currency:STARS", "primary"),
                button("Usdt", "deposit:currency:USDT", "primary"),
            ],
            [
                button("Выйти", "menu:home", "danger")
            ]
        ]
    )


def withdraw_currency_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("↗️ ₽", "withdraw:currency:RUB", "primary"),
                button("Gram", "withdraw:currency:GRAM", "primary"),
                button("Stars", "withdraw:currency:STARS", "primary"),
                button("Usdt", "withdraw:currency:USDT", "primary"),
            ],
            [
                button("Выйти", "menu:home", "danger")
            ]
        ]
    )


def wallet_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "↗️ Вывод",
                    "wallet:withdraw",
                    "primary"
                )
            ],
            [
                button(
                    "🔶 Пополнение",
                    "wallet:deposit",
                    "success"
                )
            ],
            [
                button(
                    "Выйти",
                    "menu:home",
                    "danger"
                )
            ]
        ]
    )


# ============================================================
# SUPPORT KEYBOARDS
# ============================================================

def support_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "Написать в поддержку",
                    "support:operator",
                    "primary"
                )
            ],
            [
                button(
                    "Выйти",
                    "menu:home",
                    "danger"
                )
            ]
        ]
    )


# ============================================================
# DEAL KEYBOARD
# ============================================================

def deal_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "Выйти",
                    "menu:home",
                    "danger"
                )
            ]
        ]
    )


def confirm_deal_keyboard(deal_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "ПОДТВЕРДИТЬ",
                    f"deal:confirm:{deal_id}",
                    "primary"
                )
            ]
        ]
    )


# ============================================================
# SAFE EDIT
# ============================================================

async def safe_edit(
    callback,
    text,
    keyboard=None
):

    try:

        await callback.message.edit_text(
            text,
            reply_markup=keyboard
        )

    except TelegramBadRequest as e:

        if "message is not modified" not in str(e).lower():
            raise


# ============================================================
# HOME
# ============================================================

async def show_home(target, user):

    ensure_user(user)
    release_expired_holds()

    rub = get_balance(user.id, "RUB")
    gram = get_balance(user.id, "GRAM")
    stars = get_balance(user.id, "STARS")
    usdt = get_balance(user.id, "USDT")

    held = get_held_balance(user.id, "RUB")
    active = get_active_deals_count(user.id)

    text = (
        "🔷 *Добро пожаловать на FunPay!*\n\n"
        "Здесь вы можете безопасно создавать сделки "
        "и управлять своим балансом.\n\n"
        f"Баланс: {fmt(rub)} ₽\n\n"
        f"~ {fmt(stars)} Telegram Stars\n"
        f"~ {fmt(gram)} Gram\n"
        f"~ {fmt(usdt)} Usdt\n\n"
        f"❄️ На удержании: {fmt(held)} ₽\n"
        f"🔁 Активных сделок: {active}\n\n"
        "Выберите нужный раздел ниже 👇"
    )

    if isinstance(target, CallbackQuery):

        await safe_edit(
            target,
            text,
            main_keyboard()
        )

    else:

        await target.answer(
            text,
            reply_markup=main_keyboard()
        )


# ============================================================
# START
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):

    ensure_user(message.from_user)
    clear_state(message.from_user.id)

    await show_home(
        message,
        message.from_user
    )


# ============================================================
# HOME
# ============================================================

@dp.callback_query(F.data == "menu:home")
async def callback_home(callback):

    ensure_user(callback.from_user)
    clear_state(callback.from_user.id)

    await callback.answer()

    await show_home(
        callback,
        callback.from_user
    )


# ============================================================
# CREATE DEAL
# ============================================================

@dp.callback_query(F.data == "menu:create")
async def callback_create(callback):

    ensure_user(callback.from_user)

    set_state(
        callback.from_user.id,
        "waiting_gift"
    )

    await callback.answer()

    await safe_edit(
        callback,
        "Пришлите ссылку на подарок.\n\n"
        "Ссылка должна начинаться с:\n"
        "`t.me/nft/...`",
        back_keyboard()
    )


# ============================================================
# GIFT
# ============================================================

async def process_gift_link(message):

    value = message.text.strip()

    if not GIFT_RE.fullmatch(value):

        error = await message.answer(
            "Ссылка указана неверно.\n\n"
            "Используйте формат:\n"
            "`t.me/nft/...`"
        )

        await asyncio.sleep(2)

        try:
            await error.delete()
        except Exception:
            pass

        return

    set_state(
        message.from_user.id,
        {
            "state": "waiting_price",
            "gift_url": value
        }
    )

    await message.answer(
        "Выберите цену сделки в рублях целым числом."
    )


# ============================================================
# PRICE
# ============================================================

async def process_price(message):

    state = get_state(message.from_user.id)

    if not isinstance(state, dict):
        clear_state(message.from_user.id)
        return

    value = message.text.strip()

    if not PRICE_RE.fullmatch(value):

        error = await message.answer(
            "Цена должна быть целым числом больше 0."
        )

        await asyncio.sleep(2)

        try:
            await error.delete()
        except Exception:
            pass

        return

    amount = int(value)
    gift_url = state["gift_url"]

    with db() as conn:

        code = generate_deal_code(conn)

        conn.execute("""
            INSERT INTO deals (
                code,
                creator_id,
                gift_url,
                amount,
                currency,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, 'RUB', 'pending', ?)
        """, (
            code,
            message.from_user.id,
            gift_url,
            amount,
            now_iso()
        ))

        conn.commit()

    clear_state(message.from_user.id)

    await message.answer(
        "🔷 *СДЕЛКА FUNPAY* 🔷\n\n"
        f"{gift_url}\n"
        f"{fmt(amount)} ₽\n\n"
        f"Код сделки: `{code}`\n\n"
        "Передайте код покупателю для оплаты сделки.",
        reply_markup=deal_keyboard()
    )


# ============================================================
# WALLET
# ============================================================

@dp.callback_query(F.data == "menu:wallet")
async def callback_wallet(callback):

    ensure_user(callback.from_user)
    clear_state(callback.from_user.id)

    release_expired_holds()

    rub = get_balance(callback.from_user.id, "RUB")
    gram = get_balance(callback.from_user.id, "GRAM")
    stars = get_balance(callback.from_user.id, "STARS")
    usdt = get_balance(callback.from_user.id, "USDT")

    held = get_held_balance(
        callback.from_user.id,
        "RUB"
    )

    text = (
        "*Кошелек FunPay*\n\n"
        f"₽ {fmt(rub)}\n"
        f"Gram {fmt(gram)}\n"
        f"Stars {fmt(stars)}\n"
        f"Usdt {fmt(usdt)}\n\n"
        f"❄️ На удержании: {fmt(held)} ₽\n\n"
        "Выберите действие."
    )

    await callback.answer()

    await safe_edit(
        callback,
        text,
        wallet_keyboard()
    )


# ============================================================
# DEPOSIT
# ============================================================

@dp.callback_query(F.data == "wallet:deposit")
async def callback_deposit(callback):

    clear_state(callback.from_user.id)

    await callback.answer()

    await safe_edit(
        callback,
        "*Пополнение*\n\n"
        "Выберите валюту для пополнения:",
        deposit_currency_keyboard()
    )


@dp.callback_query(
    F.data.startswith("deposit:currency:")
)
async def callback_deposit_currency(callback):

    currency = callback.data.split(":")[-1]

    if currency not in CURRENCIES:

        await callback.answer(
            "Неизвестная валюта.",
            show_alert=True
        )
        return

    set_state(
        callback.from_user.id,
        {
            "state": "deposit_amount",
            "currency": currency
        }
    )

    await callback.answer()

    await safe_edit(
        callback,
        f"Пополнение: *{currency_name(currency)}*\n\n"
        "Введите желаемую сумму целым числом.",
        back_keyboard()
    )


# ============================================================
# WITHDRAW
# ============================================================

@dp.callback_query(F.data == "wallet:withdraw")
async def callback_withdraw(callback):

    clear_state(callback.from_user.id)

    await callback.answer()

    await safe_edit(
        callback,
        "*Вывод средств*\n\n"
        "Выберите валюту:",
        withdraw_currency_keyboard()
    )


@dp.callback_query(
    F.data.startswith("withdraw:currency:")
)
async def callback_withdraw_currency(callback):

    currency = callback.data.split(":")[-1]

    if currency not in CURRENCIES:

        await callback.answer(
            "Неизвестная валюта.",
            show_alert=True
        )
        return

    balance = get_balance(
        callback.from_user.id,
        currency
    )

    set_state(
        callback.from_user.id,
        {
            "state": "withdraw_amount",
            "currency": currency
        }
    )

    await callback.answer()

    await safe_edit(
        callback,
        f"Вывод: *{currency_name(currency)}*\n\n"
        f"Доступно: *{fmt(balance)}* {currency_name(currency)}\n\n"
        "Введите сумму вывода целым числом.",
        back_keyboard()
    )


# ============================================================
# BONUS
# ============================================================

def bonus_keyboard(user_id):

    claimed = False

    with db() as conn:

        row = conn.execute("""
            SELECT bonus_claimed
            FROM users
            WHERE user_id = ?
        """, (
            user_id,
        )).fetchone()

        if row:
            claimed = bool(row["bonus_claimed"])

    if claimed:

        text = "Забрано"

    elif user_completed_deal(user_id):

        text = "Забрать приз (1/1)"

    else:

        text = "Забрать приз (0/1)"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    text,
                    "bonus:claim",
                    "primary"
                )
            ],
            [
                button(
                    "Выйти",
                    "menu:home",
                    "danger"
                )
            ]
        ]
    )


@dp.callback_query(F.data == "menu:bonus")
async def callback_bonus(callback):

    clear_state(callback.from_user.id)

    text = (
        "💥 *ОКТЯБРЬ НА FUNPAY* "
        "(С 1 по 25 октября)\n\n"
        "Проведи свою первую сделку на FunPay "
        "и получи +200 ⭐ Telegram Stars!\n\n"
        "Чтобы получить приз:\n\n"
        "• Создай или соверши первую сделку\n"
        "• Успешно заверши её\n"
        "• Получи 200 ⭐ Stars в подарок в этом меню\n\n"
        "🔥 Отличный повод попробовать FunPay прямо сейчас!"
    )

    await callback.answer()

    await safe_edit(
        callback,
        text,
        bonus_keyboard(callback.from_user.id)
    )


@dp.callback_query(F.data == "bonus:claim")
async def callback_bonus_claim(callback):

    user_id = callback.from_user.id

    with db() as conn:

        user = conn.execute("""
            SELECT bonus_claimed
            FROM users
            WHERE user_id = ?
        """, (
            user_id,
        )).fetchone()

        if not user:

            await callback.answer(
                "Пользователь не найден.",
                show_alert=True
            )
            return

        if user["bonus_claimed"]:

            await callback.answer(
                "Вы уже получили этот бонус.",
                show_alert=True
            )
            return

        if not bonus_available():

            await callback.answer(
                "Акция сейчас недоступна.",
                show_alert=True
            )
            return

        completed = conn.execute("""
            SELECT id
            FROM deals
            WHERE status = 'confirmed'
            AND (
                creator_id = ?
                OR payer_id = ?
            )
            LIMIT 1
        """, (
            user_id,
            user_id
        )).fetchone()

        if not completed:

            await callback.answer(
                "Сначала успешно завершите первую сделку.",
                show_alert=True
            )
            return

        conn.execute("""
            UPDATE users
            SET
                stars_balance = stars_balance + ?,
                bonus_claimed = 1
            WHERE user_id = ?
            AND bonus_claimed = 0
        """, (
            BONUS_STARS,
            user_id
        ))

        conn.execute("""
            INSERT INTO operations (
                user_id,
                kind,
                amount,
                currency,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            user_id,
            "october_bonus",
            BONUS_STARS,
            "STARS",
            now_iso()
        ))

        conn.commit()

    await callback.answer(
        "Вы получили 200 Stars!",
        show_alert=True
    )

    await safe_edit(
        callback,
        "💥 *ОКТЯБРЬ НА FUNPAY*\n\n"
        "Бонус успешно получен.\n\n"
        "На ваш баланс начислено:\n"
        "*+200 ⭐ Telegram Stars*\n\n"
        "Повторно получить бонус нельзя.",
        bonus_keyboard(user_id)
    )


# ============================================================
# ABOUT
# ============================================================

@dp.callback_query(F.data == "menu:about")
async def callback_about(callback):

    clear_state(callback.from_user.id)

    text = (
        "*FunPay*\n\n"
        "Сервис для безопасной торговли NFT "
        "и цифровыми товарами в Telegram.\n\n"
        "Создавайте сделки, покупайте и продавайте NFT, "
        "проводите оплату через удобную систему "
        "и взаимодействуйте с другими пользователями.\n\n"
        "FunPay объединяет покупателей и продавцов "
        "в одном месте, помогая сделать торговлю "
        "простой, быстрой и удобной.\n\n"
        "Все основные операции доступны прямо в Telegram."
    )

    await callback.answer()

    await safe_edit(
        callback,
        text,
        back_keyboard()
    )


# ============================================================
# SUPPORT
# ============================================================

@dp.callback_query(F.data == "menu:support")
async def callback_support(callback):

    clear_state(callback.from_user.id)

    text = (
        "*Поддержка FunPay*\n\n"
        "Здесь вы можете задать вопрос оператору "
        "или обратиться по поводу сделки.\n\n"
        "Если проблема связана со сделкой, "
        "укажите её код и подробно опишите ситуацию."
    )

    await callback.answer()

    await safe_edit(
        callback,
        text,
        support_keyboard()
    )


@dp.callback_query(F.data == "support:operator")
async def callback_operator(callback):

    user = callback.from_user

    ensure_user(user)

    set_state(
        user.id,
        "support_message"
    )

    await callback.answer()

    await safe_edit(
        callback,
        "*Сообщение в поддержку*\n\n"
        "Напишите ваше сообщение следующим сообщением.\n\n"
        "Оно будет передано операторам FunPay.",
        back_keyboard()
    )


# ============================================================
# ADMIN SUPPORT
# ============================================================

def admin_reply_keyboard(user_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "Ответить",
                    f"support:reply:{user_id}",
                    "primary"
                )
            ]
        ]
    )


@dp.callback_query(
    F.data.startswith("support:reply:")
)
async def callback_support_reply(callback):

    admin_id = callback.from_user.id

    if admin_id not in ADMINS:

        await callback.answer(
            "У вас нет доступа.",
            show_alert=True
        )
        return

    try:
        user_id = int(
            callback.data.split(":")[-1]
        )
    except ValueError:

        await callback.answer(
            "Ошибка пользователя.",
            show_alert=True
        )
        return

    set_state(
        admin_id,
        {
            "state": "support_reply",
            "user_id": user_id
        }
    )

    await callback.answer()

    await bot.send_message(
        admin_id,
        "*Введите ответ пользователю.*\n\n"
        "Следующее сообщение будет отправлено ему.\n\n"
        "Для отмены: `/cancel`"
    )


# ============================================================
# CANCEL
# ============================================================

@dp.message(Command("cancel"))
async def cmd_cancel(message):

    if get_state(message.from_user.id):

        clear_state(message.from_user.id)

        await message.answer(
            "Действие отменено."
        )

    else:

        await message.answer(
            "Нет активного действия."
        )


# ============================================================
# ADMIN MESSAGE
# ============================================================

async def process_admin_support_reply(message):

    admin_id = message.from_user.id

    state = get_state(admin_id)

    if not isinstance(state, dict):
        return False

    if state.get("state") != "support_reply":
        return False

    target_user_id = int(
        state["user_id"]
    )

    clear_state(admin_id)

    try:

        await bot.send_message(
            target_user_id,
            "*Ответ поддержки FunPay*\n\n"
            f"{message.text}"
        )

        await message.answer(
            "Сообщение отправлено."
        )

    except Exception:

        logging.exception(
            "Ошибка отправки ответа поддержки"
        )

        await message.answer(
            "Не удалось отправить сообщение."
        )

    return True


# ============================================================
# SUPPORT MESSAGE
# ============================================================

async def process_support_message(message):

    user = message.from_user

    if not message.text:
        return

    with db() as conn:

        request = conn.execute("""
            INSERT INTO support_requests (
                user_id,
                created_at
            )
            VALUES (?, ?)
            RETURNING id
        """, (
            user.id,
            now_iso()
        )).fetchone()

        request_id = request["id"]

        conn.commit()

    clear_state(user.id)

    username = (
        f"@{user.username}"
        if user.username
        else "без username"
    )

    admin_text = (
        "*Новое сообщение в поддержку*\n\n"
        f"Заявка: #{request_id}\n"
        f"Пользователь: {username}\n"
        f"ID: `{user.id}`\n\n"
        f"{message.text}"
    )

    for admin_id in ADMINS:

        try:

            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=admin_reply_keyboard(
                    user.id
                )
            )

        except Exception:

            logging.exception(
                "Ошибка уведомления поддержки"
            )

    await message.answer(
        "Сообщение отправлено в поддержку.\n\n"
        "Оператор ответит вам в этом чате."
    )


# ============================================================
# DEPOSIT REQUEST
# ============================================================

async def process_deposit_amount(message):

    state = get_state(message.from_user.id)

    if not isinstance(state, dict):
        return False

    if state.get("state") != "deposit_amount":
        return False

    if not message.text.isdigit():

        await message.answer(
            "Введите целое число больше 0."
        )
        return True

    amount = int(message.text)

    if amount <= 0:

        await message.answer(
            "Сумма должна быть больше 0."
        )
        return True

    currency = state["currency"]

    with db() as conn:

        cursor = conn.execute("""
            INSERT INTO deposit_requests (
                user_id,
                currency,
                amount,
                status,
                created_at
            )
            VALUES (?, ?, ?, 'pending', ?)
        """, (
            message.from_user.id,
            currency,
            amount,
            now_iso()
        ))

        request_id = cursor.lastrowid

        conn.commit()

    clear_state(message.from_user.id)

    await message.answer(
        "*Заявка на пополнение создана.*\n\n"
        f"Сумма: *{fmt(amount)}* {currency_name(currency)}\n"
        f"Заявка: `#{request_id}`\n\n"
        "Ожидайте ответа оператора."
    )

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "без username"
    )

    for admin_id in ADMINS:

        try:

            await bot.send_message(
                admin_id,
                "*Новая заявка на пополнение*\n\n"
                f"Заявка: #{request_id}\n"
                f"Пользователь: {username}\n"
                f"ID: `{message.from_user.id}`\n"
                f"Сумма: *{fmt(amount)}* "
                f"{currency_name(currency)}"
            )

        except Exception:
            logging.exception(
                "Ошибка уведомления о пополнении"
            )

    return True


# ============================================================
# WITHDRAW REQUEST
# ============================================================

async def process_withdraw_amount(message):

    state = get_state(message.from_user.id)

    if not isinstance(state, dict):
        return False

    if state.get("state") != "withdraw_amount":
        return False

    if not message.text.isdigit():

        await message.answer(
            "Введите целое число больше 0."
        )
        return True

    amount = int(message.text)
    currency = state["currency"]

    if amount <= 0:

        await message.answer(
            "Сумма должна быть больше 0."
        )
        return True

    balance = get_balance(
        message.from_user.id,
        currency
    )

    if amount > balance:

        await message.answer(
            "Недостаточно средств.\n\n"
            f"Доступно: *{fmt(balance)}* "
            f"{currency_name(currency)}"
        )

        return True

    with db() as conn:

        balance_col = balance_column(currency)

        cursor = conn.execute(
            f"""
            UPDATE users
            SET {balance_col} = {balance_col} - ?
            WHERE user_id = ?
            AND {balance_col} >= ?
            """,
            (
                amount,
                message.from_user.id,
                amount
            )
        )

        if cursor.rowcount != 1:

            await message.answer(
                "Не удалось создать заявку. "
                "Попробуйте ещё раз."
            )

            return True

        cursor = conn.execute("""
            INSERT INTO withdrawal_requests (
                user_id,
                currency,
                amount,
                status,
                created_at
            )
            VALUES (?, ?, ?, 'pending', ?)
        """, (
            message.from_user.id,
            currency,
            amount,
            now_iso()
        ))

        request_id = cursor.lastrowid

        conn.execute("""
            INSERT INTO operations (
                user_id,
                kind,
                amount,
                currency,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            message.from_user.id,
            "withdraw_request",
            -amount,
            currency,
            now_iso()
        ))

        conn.commit()

    clear_state(message.from_user.id)

    await message.answer(
        "*Заявка на вывод создана.*\n\n"
        f"Сумма: *{fmt(amount)}* {currency_name(currency)}\n"
        f"Заявка: `#{request_id}`\n\n"
        "Ожидайте обработки оператором."
    )

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "без username"
    )

    for admin_id in ADMINS:

        try:

            await bot.send_message(
                admin_id,
                "*Новая заявка на вывод*\n\n"
                f"Заявка: #{request_id}\n"
                f"Пользователь: {username}\n"
                f"ID: `{message.from_user.id}`\n"
                f"Сумма: *{fmt(amount)}* "
                f"{currency_name(currency)}"
            )

        except Exception:
            logging.exception(
                "Ошибка уведомления о выводе"
            )

    return True


# ============================================================
# CONFIRM DEAL
# ============================================================

@dp.callback_query(
    F.data.startswith("deal:confirm:")
)
async def callback_confirm_deal(callback):

    buyer_id = callback.from_user.id

    try:
        deal_id = int(
            callback.data.split(":")[-1]
        )
    except ValueError:

        await callback.answer(
            "Некорректная сделка.",
            show_alert=True
        )
        return

    ensure_user(callback.from_user)

    with db() as conn:

        deal = conn.execute("""
            SELECT *
            FROM deals
            WHERE id = ?
        """, (
            deal_id,
        )).fetchone()

        if not deal:

            await callback.answer(
                "Сделка не найдена.",
                show_alert=True
            )
            return

        if deal["payer_id"] != buyer_id:

            await callback.answer(
                "Вы не являетесь покупателем.",
                show_alert=True
            )
            return

        if deal["status"] != "paid":

            await callback.answer(
                "Сделка уже обработана или недоступна.",
                show_alert=True
            )
            return

        amount = int(deal["amount"])
        currency = deal["currency"]
        confirmed_at = now_iso()

        cursor = conn.execute("""
            UPDATE deals
            SET
                status = 'confirmed',
                confirmed_at = ?
            WHERE id = ?
            AND status = 'paid'
        """, (
            confirmed_at,
            deal_id
        ))

        if cursor.rowcount != 1:

            await callback.answer(
                "Сделка уже была подтверждена.",
                show_alert=True
            )
            return

        held_col = held_column(currency)

        conn.execute(
            f"""
            UPDATE users
            SET
                {held_col} = {held_col} + ?,
                first_paid_at =
                    CASE
                        WHEN first_paid_at IS NULL
                        THEN ?
                        ELSE first_paid_at
                    END
            WHERE user_id = ?
            """,
            (
                amount,
                confirmed_at,
                deal["creator_id"]
            )
        )

        release_at = (
            datetime.now(timezone.utc)
            + timedelta(days=FREEZE_DAYS)
        ).isoformat()

        conn.execute("""
            INSERT INTO balance_holds (
                user_id,
                amount,
                currency,
                deal_id,
                created_at,
                release_at,
                released
            )
            VALUES (?, ?, ?, ?, ?, ?, 0)
        """, (
            deal["creator_id"],
            amount,
            currency,
            deal_id,
            confirmed_at,
            release_at
        ))

        conn.execute("""
            INSERT INTO operations (
                user_id,
                kind,
                amount,
                currency,
                deal_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            deal["creator_id"],
            "deal_confirmed_hold",
            amount,
            currency,
            deal_id,
            confirmed_at
        ))

        conn.commit()

    await callback.answer(
        "Сделка подтверждена!",
        show_alert=True
    )

    try:

        await callback.message.edit_text(
            "*Сделка подтверждена.*\n\n"
            "Средства зачислены продавцу "
            "и помещены на удержание."
        )

    except TelegramBadRequest:
        pass

    try:

        await bot.send_message(
            deal["creator_id"],
            "*Сделка подтверждена*\n\n"
            f"Сумма: *{fmt(amount)}* "
            f"{currency_name(currency)}\n"
            f"Код сделки: `{deal['code']}`\n\n"
            f"Средства находятся на удержании "
            f"{FREEZE_DAYS} дня."
        )

    except Exception:
        logging.exception(
            "Ошибка уведомления продавца"
        )


# ============================================================
# ADMIN PAYMENT
# ============================================================

async def admin_pay_deal(deal_id, admin_id):

    if admin_id not in ADMINS:
        return False, "Нет доступа."

    with db() as conn:

        deal = conn.execute("""
            SELECT *
            FROM deals
            WHERE id = ?
        """, (
            deal_id,
        )).fetchone()

        if not deal:
            return False, "Сделка не найдена."

        if deal["status"] != "pending":
            return False, "Сделка уже обработана."

        if deal["creator_id"] == admin_id:
            return False, "Нельзя оплатить свою сделку."

        paid_at = now_iso()

        cursor = conn.execute("""
            UPDATE deals
            SET
                status = 'paid',
                payer_id = ?,
                paid_at = ?
            WHERE id = ?
            AND status = 'pending'
        """, (
            admin_id,
            paid_at,
            deal_id
        ))

        if cursor.rowcount != 1:
            return False, "Сделка уже оплачена."

        conn.commit()

    return True, deal


# ============================================================
# /P CODE
# ============================================================

@dp.message(Command("p"))
async def cmd_pay(message):

    ensure_user(message.from_user)

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:

        await message.answer(
            "Укажите код сделки.\n\n"
            "Пример:\n"
            "`/p FP-7K4P2Q`"
        )
        return

    code = parts[1].strip().upper()

    payer_id = message.from_user.id
    is_admin = payer_id in ADMINS

    with db() as conn:

        deal = conn.execute("""
            SELECT *
            FROM deals
            WHERE UPPER(code) = ?
        """, (
            code,
        )).fetchone()

        if not deal:

            await message.answer(
                "Сделка с таким кодом не найдена."
            )
            return

        if deal["status"] != "pending":

            await message.answer(
                "Эта сделка уже обработана."
            )
            return

        if deal["creator_id"] == payer_id:

            await message.answer(
                "Свою сделку оплатить нельзя."
            )
            return

        amount = int(deal["amount"])
        currency = deal["currency"]

        if not is_admin:

            balance_col = balance_column(currency)

            payer = conn.execute(
                f"""
                SELECT {balance_col}
                FROM users
                WHERE user_id = ?
                """,
                (payer_id,)
            ).fetchone()

            if not payer:

                await message.answer(
                    "Пользователь не найден."
                )
                return

            payer_balance = int(
                payer[balance_col]
            )

            if payer_balance < amount:

                missing = amount - payer_balance

                await message.answer(
                    "Недостаточно средств.\n\n"
                    f"Стоимость: *{fmt(amount)}* "
                    f"{currency_name(currency)}\n"
                    f"Баланс: *{fmt(payer_balance)}* "
                    f"{currency_name(currency)}\n"
                    f"Не хватает: *{fmt(missing)}* "
                    f"{currency_name(currency)}"
                )
                return

            cursor = conn.execute(
                f"""
                UPDATE users
                SET {balance_col} = {balance_col} - ?
                WHERE user_id = ?
                AND {balance_col} >= ?
                """,
                (
                    amount,
                    payer_id,
                    amount
                )
            )

            if cursor.rowcount != 1:

                await message.answer(
                    "Не удалось провести оплату."
                )
                return

        paid_at = now_iso()

        cursor = conn.execute("""
            UPDATE deals
            SET
                status = 'paid',
                payer_id = ?,
                paid_at = ?
            WHERE id = ?
            AND status = 'pending'
        """, (
            payer_id,
            paid_at,
            deal["id"]
        ))

        if cursor.rowcount != 1:

            if not is_admin:

                balance_col = balance_column(currency)

                conn.execute(
                    f"""
                    UPDATE users
                    SET {balance_col} = {balance_col} + ?
                    WHERE user_id = ?
                    """,
                    (
                        amount,
                        payer_id
                    )
                )

            await message.answer(
                "Сделка уже была оплачена."
            )
            return

        if not is_admin:

            conn.execute("""
                INSERT INTO operations (
                    user_id,
                    kind,
                    amount,
                    currency,
                    deal_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                payer_id,
                "deal_purchase",
                -amount,
                currency,
                deal["id"],
                paid_at
            ))

        conn.commit()

    await message.answer(
        "Если вы получили подарок — подтвердите сделку.",
        reply_markup=confirm_deal_keyboard(
            deal["id"]
        )
    )

    try:

        await bot.send_message(
            deal["creator_id"],
            "Покупатель оплатил сделку.\n\n"
            f"Сумма: *{fmt(amount)}* "
            f"{currency_name(currency)}\n\n"
            "После передачи подарка покупатель сможет "
            "подтвердить сделку."
        )

    except Exception:
        logging.exception(
            "Ошибка уведомления продавца"
        )


# ============================================================
# /ADMIN
# ============================================================

@dp.message(Command("admin"))
async def cmd_admin(message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "У вас нет доступа к этой команде."
        )
        return

    text = (
        "*Административные команды*\n\n"
        "`/admin` — список команд\n"
        "`/work` — управление сделками\n"
        "`/p CODE` — оплатить сделку\n"
        "`/txt ТЕКСТ` — рассылка всем пользователям\n"
        "`/profit СУММА` — уведомление о крупной сделке\n"
        "`/m СУММА @username` — начислить баланс в ₽\n"
        "`/delbal` — очистить балансы\n"
        "`/delsdel` — удалить все сделки\n"
        "`/cancel` — отменить текущее действие\n\n"
        "Все перечисленные команды доступны только администраторам."
    )

    await message.answer(text)


# ============================================================
# /WORK
# ============================================================

WORK_PAGE_SIZE = 3
work_pages = {}


def work_keyboard(page, total):

    buttons = []

    if page > 0:

        buttons.append(
            button(
                "⬅️",
                f"work:page:{page - 1}",
                "primary"
            )
        )

    if (page + 1) * WORK_PAGE_SIZE < total:

        buttons.append(
            button(
                "➡️",
                f"work:page:{page + 1}",
                "primary"
            )
        )

    rows = []

    if buttons:
        rows.append(buttons)

    rows.append([
        button(
            "Обновить",
            f"work:page:{page}",
            "success"
        )
    ])

    rows.append([
        button(
            "Закрыть",
            "menu:home",
            "danger"
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def deal_admin_keyboard(deal_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "Оплатить",
                    f"work:pay:{deal_id}",
                    "primary"
                ),
                button(
                    "Отменить",
                    f"work:cancel:{deal_id}",
                    "danger"
                )
            ],
            [
                button(
                    "Удалить",
                    f"work:delete:{deal_id}",
                    "danger"
                )
            ]
        ]
    )


async def render_work(
    target,
    page=0
):

    with db() as conn:

        total_row = conn.execute("""
            SELECT COUNT(*) AS count
            FROM deals
        """).fetchone()

        total = int(total_row["count"])

        if total == 0:

            text = (
                "*Управление сделками*\n\n"
                "Сделок пока нет."
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        button(
                            "Закрыть",
                            "menu:home",
                            "danger"
                        )
                    ]
                ]
            )

        else:

            deals = conn.execute("""
                SELECT
                    d.*,

                    seller.username AS seller_username,
                    seller.first_name AS seller_name,

                    buyer.username AS buyer_username,
                    buyer.first_name AS buyer_name

                FROM deals d

                LEFT JOIN users seller
                    ON seller.user_id = d.creator_id

                LEFT JOIN users buyer
                    ON buyer.user_id = d.payer_id

                ORDER BY d.id DESC

                LIMIT ? OFFSET ?
            """, (
                WORK_PAGE_SIZE,
                page * WORK_PAGE_SIZE
            )).fetchall()

            text = (
                f"*Управление сделками*\n"
                f"Страница {page + 1}\n\n"
            )

            for deal in deals:

                seller = (
                    f"@{deal['seller_username']}"
                    if deal["seller_username"]
                    else str(deal["creator_id"])
                )

                buyer = (
                    f"@{deal['buyer_username']}"
                    if deal["buyer_username"]
                    else (
                        str(deal["payer_id"])
                        if deal["payer_id"]
                        else "—"
                    )
                )

                text += (
                    f"*#{deal['id']} — {deal['code']}*\n"
                    f"Статус: `{deal['status']}`\n"
                    f"Продавец: {seller}\n"
                    f"Покупатель: {buyer}\n"
                    f"Сумма: *{fmt(deal['amount'])}* "
                    f"{currency_name(deal['currency'])}\n"
                    f"NFT: {deal['gift_url']}\n"
                    f"Создана: {deal['created_at']}\n\n"
                )

            # Отдельная кнопка управления для каждой сделки
            rows = []

            for deal in deals:

                rows.append([
                    button(
                        f"#{deal['id']} {deal['code']}",
                        f"work:open:{deal['id']}",
                        "primary"
                    )
                ])

            nav = []

            if page > 0:
                nav.append(
                    button(
                        "⬅️",
                        f"work:page:{page - 1}",
                        "primary"
                    )
                )

            if (page + 1) * WORK_PAGE_SIZE < total:
                nav.append(
                    button(
                        "➡️",
                        f"work:page:{page + 1}",
                        "primary"
                    )
                )

            if nav:
                rows.append(nav)

            rows.append([
                button(
                    "Обновить",
                    f"work:page:{page}",
                    "success"
                )
            ])

            rows.append([
                button(
                    "Закрыть",
                    "menu:home",
                    "danger"
                )
            ])

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=rows
            )

    if isinstance(target, CallbackQuery):

        await safe_edit(
            target,
            text,
            keyboard
        )

    else:

        await target.answer(
            text,
            reply_markup=keyboard
        )


@dp.message(Command("work"))
async def cmd_work(message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "У вас нет доступа к этой команде."
        )
        return

    await render_work(
        message,
        0
    )


@dp.callback_query(
    F.data.startswith("work:page:")
)
async def callback_work_page(callback):

    if callback.from_user.id not in ADMINS:

        await callback.answer(
            "У вас нет доступа.",
            show_alert=True
        )
        return

    try:
        page = int(
            callback.data.split(":")[-1]
        )
    except ValueError:
        page = 0

    await callback.answer()

    await render_work(
        callback,
        max(0, page)
    )


@dp.callback_query(
    F.data.startswith("work:open:")
)
async def callback_work_open(callback):

    if callback.from_user.id not in ADMINS:

        await callback.answer(
            "У вас нет доступа.",
            show_alert=True
        )
        return

    try:
        deal_id = int(
            callback.data.split(":")[-1]
        )
    except ValueError:

        await callback.answer(
            "Ошибка сделки.",
            show_alert=True
        )
        return

    with db() as conn:

        deal = conn.execute("""
            SELECT
                d.*,

                seller.username AS seller_username,
                buyer.username AS buyer_username

            FROM deals d

            LEFT JOIN users seller
                ON seller.user_id = d.creator_id

            LEFT JOIN users buyer
                ON buyer.user_id = d.payer_id

            WHERE d.id = ?
        """, (
            deal_id,
        )).fetchone()

    if not deal:

        await callback.answer(
            "Сделка не найдена.",
            show_alert=True
        )
        return

    seller = (
        f"@{deal['seller_username']}"
        if deal["seller_username"]
        else str(deal["creator_id"])
    )

    buyer = (
        f"@{deal['buyer_username']}"
        if deal["buyer_username"]
        else (
            str(deal["payer_id"])
            if deal["payer_id"]
            else "—"
        )
    )

    text = (
        f"*Сделка #{deal['id']}*\n\n"
        f"Код: `{deal['code']}`\n"
        f"Статус: `{deal['status']}`\n\n"
        f"Продавец: {seller}\n"
        f"ID продавца: `{deal['creator_id']}`\n\n"
        f"Покупатель: {buyer}\n"
        f"ID покупателя: "
        f"`{deal['payer_id'] or '—'}`\n\n"
        f"NFT: {deal['gift_url']}\n"
        f"Сумма: *{fmt(deal['amount'])}* "
        f"{currency_name(deal['currency'])}\n\n"
        f"Создана: {deal['created_at']}"
    )

    await callback.answer()

    await safe_edit(
        callback,
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "Оплатить",
                        f"work:pay:{deal_id}",
                        "primary"
                    ),
                    button(
                        "Отменить",
                        f"work:cancel:{deal_id}",
                        "danger"
                    )
                ],
                [
                    button(
                        "Удалить",
                        f"work:delete:{deal_id}",
                        "danger"
                    )
                ],
                [
                    button(
                        "Назад",
                        "work:page:0",
                        "success"
                    )
                ]
            ]
        )
    )


@dp.callback_query(
    F.data.startswith("work:pay:")
)
async def callback_work_pay(callback):

    if callback.from_user.id not in ADMINS:

        await callback.answer(
            "У вас нет доступа.",
            show_alert=True
        )
        return

    try:
        deal_id = int(
            callback.data.split(":")[-1]
        )
    except ValueError:

        await callback.answer(
            "Ошибка.",
            show_alert=True
        )
        return

    success, result = await admin_pay_deal(
        deal_id,
        callback.from_user.id
    )

    if not success:

        await callback.answer(
            result,
            show_alert=True
        )
        return

    deal = result

    await callback.answer(
        "Сделка оплачена.",
        show_alert=True
    )

    try:

        await bot.send_message(
            deal["creator_id"],
            "Покупатель оплатил сделку.\n\n"
            f"Код: `{deal['code']}`\n"
            f"Сумма: *{fmt(deal['amount'])}* "
            f"{currency_name(deal['currency'])}"
        )

    except Exception:
        logging.exception(
            "Ошибка уведомления продавца"
        )

    await render_work(
        callback,
        0
    )


@dp.callback_query(
    F.data.startswith("work:cancel:")
)
async def callback_work_cancel(callback):

    if callback.from_user.id not in ADMINS:

        await callback.answer(
            "У вас нет доступа.",
            show_alert=True
        )
        return

    try:
        deal_id = int(
            callback.data.split(":")[-1]
        )
    except ValueError:

        await callback.answer(
            "Ошибка.",
            show_alert=True
        )
        return

    with db() as conn:

        deal = conn.execute("""
            SELECT *
            FROM deals
            WHERE id = ?
        """, (
            deal_id,
        )).fetchone()

        if not deal:

            await callback.answer(
                "Сделка не найдена.",
                show_alert=True
            )
            return

        if deal["status"] == "cancelled":

            await callback.answer(
                "Сделка уже отменена.",
                show_alert=True
            )
            return

        if deal["status"] == "confirmed":

            await callback.answer(
                "Подтвержденную сделку отменить нельзя.",
                show_alert=True
            )
            return

        # Если обычный покупатель уже оплатил,
        # возвращаем деньги.
        if deal["status"] == "paid" and deal["payer_id"]:

            payer_id = deal["payer_id"]
            currency = deal["currency"]
            amount = int(deal["amount"])

            # Админская оплата не списала баланс,
            # поэтому возвращать её не нужно.
            if payer_id not in ADMINS:

                balance_col = balance_column(currency)

                conn.execute(
                    f"""
                    UPDATE users
                    SET {balance_col} = {balance_col} + ?
                    WHERE user_id = ?
                    """,
                    (
                        amount,
                        payer_id
                    )
                )

                conn.execute("""
                    INSERT INTO operations (
                        user_id,
                        kind,
                        amount,
                        currency,
                        deal_id,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    payer_id,
                    "deal_refund",
                    amount,
                    currency,
                    deal_id,
                    now_iso()
                ))

        conn.execute("""
            UPDATE deals
            SET status = 'cancelled'
            WHERE id = ?
        """, (
            deal_id,
        ))

        conn.commit()

    await callback.answer(
        "Сделка отменена.",
        show_alert=True
    )

    try:

        await bot.send_message(
            deal["creator_id"],
            f"Сделка `{deal['code']}` была отменена администрацией."
        )

    except Exception:
        pass

    if deal["payer_id"]:

        try:

            await bot.send_message(
                deal["payer_id"],
                f"Сделка `{deal['code']}` была отменена администрацией."
            )

        except Exception:
            pass

    await render_work(
        callback,
        0
    )


@dp.callback_query(
    F.data.startswith("work:delete:")
)
async def callback_work_delete(callback):

    if callback.from_user.id not in ADMINS:

        await callback.answer(
            "У вас нет доступа.",
            show_alert=True
        )
        return

    try:
        deal_id = int(
            callback.data.split(":")[-1]
        )
    except ValueError:

        await callback.answer(
            "Ошибка.",
            show_alert=True
        )
        return

    with db() as conn:

        deal = conn.execute("""
            SELECT *
            FROM deals
            WHERE id = ?
        """, (
            deal_id,
        )).fetchone()

        if not deal:

            await callback.answer(
                "Сделка не найдена.",
                show_alert=True
            )
            return

        # Защита от удаления подтвержденной сделки,
        # по которой уже создавалось удержание.
        if deal["status"] == "confirmed":

            await callback.answer(
                "Подтвержденную сделку удалить нельзя.",
                show_alert=True
            )
            return

        conn.execute("""
            DELETE FROM deals
            WHERE id = ?
        """, (
            deal_id,
        ))

        conn.execute("""
            DELETE FROM operations
            WHERE deal_id = ?
        """, (
            deal_id,
        ))

        conn.execute("""
            DELETE FROM balance_holds
            WHERE deal_id = ?
        """, (
            deal_id,
        ))

        conn.commit()

    await callback.answer(
        "Сделка удалена.",
        show_alert=True
    )

    await render_work(
        callback,
        0
    )


# ============================================================
# /TXT
# ============================================================

@dp.message(Command("txt"))
async def cmd_txt(message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "У вас нет доступа."
        )
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:

        await message.answer(
            "Пример:\n"
            "`/txt Сегодня технические работы.`"
        )
        return

    custom_text = parts[1].strip()

    with db() as conn:

        users = conn.execute("""
            SELECT user_id
            FROM users
        """).fetchall()

    sent = 0
    failed = 0

    for user in users:

        try:

            await bot.send_message(
                user["user_id"],
                "Сообщение от администрации\n\n"
                + custom_text,
                parse_mode=None
            )

            sent += 1

            await asyncio.sleep(0.04)

        except Exception:

            failed += 1

    await message.answer(
        "Рассылка завершена.\n\n"
        f"Отправлено: {sent}\n"
        f"Не доставлено: {failed}"
    )


# ============================================================
# /PROFIT
# ============================================================

@dp.message(Command("profit"))
async def cmd_profit(message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "У вас нет доступа."
        )
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():

        await message.answer(
            "Пример:\n"
            "`/profit 50000`"
        )
        return

    amount = int(parts[1])

    with db() as conn:

        users = conn.execute("""
            SELECT user_id
            FROM users
        """).fetchall()

    sent = 0
    failed = 0

    text = (
        "Сообщение всем пользователям\n\n"
        "В боте только что произошла большая сделка "
        f"({fmt(amount)} рублей)\n\n"
        f"{display_datetime()}"
    )

    for user in users:

        try:

            await bot.send_message(
                user["user_id"],
                text,
                parse_mode=None
            )

            sent += 1

            await asyncio.sleep(0.04)

        except Exception:
            failed += 1

    await message.answer(
        f"Отправлено: {sent}\n"
        f"Не доставлено: {failed}"
    )


# ============================================================
# /M
# ============================================================

@dp.message(Command("m"))
async def cmd_money(message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "У вас нет доступа."
        )
        return

    parts = message.text.split()

    if len(parts) != 3:

        await message.answer(
            "Использование:\n"
            "`/m 100000 @username`"
        )
        return

    try:
        amount = int(parts[1])
    except ValueError:

        await message.answer(
            "Сумма должна быть числом."
        )
        return

    if amount <= 0:

        await message.answer(
            "Сумма должна быть больше 0."
        )
        return

    username = parts[2].lstrip("@")

    with db() as conn:

        user = conn.execute("""
            SELECT *
            FROM users
            WHERE LOWER(username) = LOWER(?)
        """, (
            username,
        )).fetchone()

        if not user:

            await message.answer(
                "Пользователь не найден."
            )
            return

        conn.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
        """, (
            amount,
            user["user_id"]
        ))

        conn.execute("""
            INSERT INTO operations (
                user_id,
                kind,
                amount,
                currency,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            user["user_id"],
            "admin_credit",
            amount,
            "RUB",
            now_iso()
        ))

        conn.commit()

    await message.answer(
        f"Баланс @{username} пополнен на "
        f"*{fmt(amount)} ₽*."
    )

    try:

        await bot.send_message(
            user["user_id"],
            "Баланс пополнен.\n\n"
            f"Вам начислено: *{fmt(amount)} ₽*"
        )

    except Exception:
        pass


# ============================================================
# /DELBAL
# ============================================================

@dp.message(Command("delbal"))
async def cmd_delbal(message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "У вас нет доступа."
        )
        return

    with db() as conn:

        conn.execute("""
            UPDATE users
            SET
                balance = 0,
                held_balance = 0,
                gram_balance = 0,
                gram_held_balance = 0,
                stars_balance = 0,
                stars_held_balance = 0,
                usdt_balance = 0,
                usdt_held_balance = 0
        """)

        conn.execute("""
            UPDATE balance_holds
            SET released = 1
            WHERE released = 0
        """)

        conn.commit()

    await message.answer(
        "Балансы всех пользователей очищены."
    )


# ============================================================
# /DELSDEL
# ============================================================

@dp.message(Command("delsdel"))
async def cmd_delsdel(message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "У вас нет доступа."
        )
        return

    with db() as conn:

        conn.execute("DELETE FROM deals")
        conn.execute("DELETE FROM operations")
        conn.execute("DELETE FROM balance_holds")

        conn.commit()

    await message.answer(
        "Все сделки удалены."
    )


# ============================================================
# TEXT HANDLER
# ============================================================

@dp.message(F.text)
async def text_handler(message):

    if message.text.startswith("/"):
        return

    ensure_user(message.from_user)

    user_id = message.from_user.id

    # --------------------------------------------------------
    # Администратор отвечает пользователю
    # --------------------------------------------------------

    if user_id in ADMINS:

        handled = await process_admin_support_reply(
            message
        )

        if handled:
            return

    # --------------------------------------------------------
    # Сообщение пользователя в поддержку
    # --------------------------------------------------------

    state = get_state(user_id)

    if state == "support_message":

        await process_support_message(message)
        return

    # --------------------------------------------------------
    # Пополнение
    # --------------------------------------------------------

    if isinstance(state, dict):

        if state.get("state") == "deposit_amount":

            await process_deposit_amount(message)
            return

        if state.get("state") == "withdraw_amount":

            await process_withdraw_amount(message)
            return

    # --------------------------------------------------------
    # Создание сделки
    # --------------------------------------------------------

    if state == "waiting_gift":

        await process_gift_link(message)
        return

    if isinstance(state, dict):

        if state.get("state") == "waiting_price":

            await process_price(message)
            return


# ============================================================
# UNKNOWN CALLBACK
# ============================================================

@dp.callback_query()
async def unknown_callback(callback):

    await callback.answer()


# ============================================================
# MAIN
# ============================================================

async def main():

    init_db()

    logging.info(
        "FUNPAY запускается..."
    )

    asyncio.create_task(
        release_holds_loop()
    )

    await dp.start_polling(bot)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
