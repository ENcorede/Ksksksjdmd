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

DB_FILE = "star_otc.sqlite3"

# Сколько дней средства находятся в удержании
FREEZE_DAYS = 3

GIFT_RE = re.compile(
    r"^(?:https?://)?t\.me/nft/[^\s]+$",
    re.IGNORECASE
)

PRICE_RE = re.compile(
    r"^[1-9][0-9]*$"
)


# ============================================================
# ПРОВЕРКА ТОКЕНА
# ============================================================

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
    """
    Время для сообщений пользователям.
    Используется локальное время сервера.
    """
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def init_db():

    with db() as conn:

        # ----------------------------------------------------
        # USERS
        # ----------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                held_balance INTEGER NOT NULL DEFAULT 0,
                first_paid_at TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # DEALS
        # ----------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT,
                creator_id INTEGER NOT NULL,
                payer_id INTEGER,
                gift_url TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                paid_at TEXT,
                confirmed_at TEXT
            )
        """)

        # ----------------------------------------------------
        # OPERATIONS
        # ----------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                amount INTEGER NOT NULL,
                deal_id INTEGER,
                created_at TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # SUPPORT
        # ----------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # HOLDS
        #
        # Суммы, которые уже подтверждены продавцом,
        # но находятся на удержании 3 дня.
        # ----------------------------------------------------

        conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_holds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                deal_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                release_at TEXT NOT NULL,
                released INTEGER NOT NULL DEFAULT 0
            )
        """)

        # ====================================================
        # MIGRATIONS
        # ====================================================

        # ----------------------------------------------------
        # USERS
        # ----------------------------------------------------

        user_columns = conn.execute(
            "PRAGMA table_info(users)"
        ).fetchall()

        user_column_names = {
            column["name"]
            for column in user_columns
        }

        if "held_balance" not in user_column_names:

            conn.execute("""
                ALTER TABLE users
                ADD COLUMN held_balance INTEGER NOT NULL DEFAULT 0
            """)

        # ----------------------------------------------------
        # DEALS
        # ----------------------------------------------------

        deal_columns = conn.execute(
            "PRAGMA table_info(deals)"
        ).fetchall()

        deal_column_names = {
            column["name"]
            for column in deal_columns
        }

        if "code" not in deal_column_names:

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

        if "confirmed_at" not in deal_column_names:

            conn.execute("""
                ALTER TABLE deals
                ADD COLUMN confirmed_at TEXT
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

        code = "ST-" + "".join(
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
                    balance,
                    held_balance,
                    created_at
                )
                VALUES (?, ?, ?, 0, 0, ?)
            """, (
                user.id,
                user.username,
                user.first_name,
                now_iso()
            ))

        conn.commit()


def get_balance(user_id):

    release_expired_holds()

    with db() as conn:

        row = conn.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
        """, (
            user_id,
        )).fetchone()

        if not row:
            return 0

        return int(row["balance"])


def get_held_balance(user_id):

    with db() as conn:

        row = conn.execute("""
            SELECT held_balance
            FROM users
            WHERE user_id = ?
        """, (
            user_id,
        )).fetchone()

        if not row:
            return 0

        return int(row["held_balance"])


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


def get_first_paid_at(user_id):

    with db() as conn:

        row = conn.execute("""
            SELECT first_paid_at
            FROM users
            WHERE user_id = ?
        """, (
            user_id,
        )).fetchone()

        if not row:
            return None

        return row["first_paid_at"]


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

            # Переводим из удержания
            # в обычный баланс

            cursor = conn.execute("""
                UPDATE users
                SET
                    held_balance = held_balance - ?,
                    balance = balance + ?
                WHERE user_id = ?
                AND held_balance >= ?
            """, (
                amount,
                amount,
                user_id,
                amount
            ))

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
                    deal_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                user_id,
                "hold_released",
                amount,
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
                "Ошибка автоматического освобождения средств"
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
# KEYBOARD BUTTON HELPER
# ============================================================

def button(
    text,
    callback_data,
    style=None
):

    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        style=style
    )


# ============================================================
# KEYBOARDS
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
                    "❇️ Кошелек ❇️",
                    "menu:wallet",
                    "success"
                )
            ],
            [
                button(
                    "💡 О сервисе 💡",
                    "menu:about",
                    "success"
                )
            ],
            [
                button(
                    "🛟 Поддержка 🛟",
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


def wallet_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "✳️ Вывод ✳️",
                    "wallet:withdraw",
                    "primary"
                )
            ],
            [
                button(
                    "📁 Пополнение 📁",
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


def support_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "🛟 Написать оператору",
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
                    "🔷 ПОДТВЕРДИТЬ 🔷",
                    f"deal:confirm:{deal_id}",
                    "primary"
                )
            ]
        ]
    )


def admin_reply_keyboard(user_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "💬 Ответить",
                    f"support:reply:{user_id}",
                    "primary"
                )
            ]
        ]
    )


# ============================================================
# SAFE EDIT
# ============================================================

async def safe_edit(
    callback: CallbackQuery,
    text: str,
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

    balance = get_balance(user.id)
    held_balance = get_held_balance(user.id)
    active_deals = get_active_deals_count(user.id)

    balance_text = f"{balance:,}".replace(",", " ")
    held_text = f"{held_balance:,}".replace(",", " ")

    text = (
        "⭐ *Добро пожаловать!*\n\n"
        "Здесь вы можете безопасно создавать сделки, "
        "обмениваться ссылками и управлять своим балансом.\n\n"
        f"💰 Баланс: *{balance_text}* ₽\n"
        f"❄️ На удержании: *{held_text}* ₽\n"
        f"📁 Активных сделок: *{active_deals}*\n\n"
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
# HOME CALLBACK
# ============================================================

@dp.callback_query(F.data == "menu:home")
async def callback_home(callback: CallbackQuery):

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
async def callback_create(callback: CallbackQuery):

    ensure_user(callback.from_user)

    set_state(
        callback.from_user.id,
        "waiting_gift"
    )

    await callback.answer()

    await safe_edit(
        callback,
        "📒 Пришлите ссылку на подарок\n\n"
        "Ссылка должна начинаться с:\n"
        "`t.me/nft/...`",
        back_keyboard()
    )


# ============================================================
# GIFT LINK
# ============================================================

async def process_gift_link(message: Message):

    value = message.text.strip()

    if not GIFT_RE.fullmatch(value):

        error_message = await message.answer(
            "❌ Ссылка указана неверно.\n\n"
            "Ссылка должна начинаться с:\n"
            "`t.me/nft/...`"
        )

        await asyncio.sleep(2)

        try:
            await error_message.delete()
        except Exception:
            pass

        return

    user_states[message.from_user.id] = {
        "state": "waiting_price",
        "gift_url": value
    }

    await message.answer(
        "🪙 Выберите цену сделки в рублях целым числом"
    )


# ============================================================
# PRICE
# ============================================================

async def process_price(message: Message):

    state = user_states.get(message.from_user.id)

    if not isinstance(state, dict):

        clear_state(message.from_user.id)

        return

    value = message.text.strip()

    if not PRICE_RE.fullmatch(value):

        error_message = await message.answer(
            "❌ Цена должна быть целым числом больше 0."
        )

        await asyncio.sleep(2)

        try:
            await error_message.delete()
        except Exception:
            pass

        return

    amount = int(value)

    gift_url = state["gift_url"]

    with db() as conn:

        deal_code = generate_deal_code(conn)

        cursor = conn.execute("""
            INSERT INTO deals (
                code,
                creator_id,
                gift_url,
                amount,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (
            deal_code,
            message.from_user.id,
            gift_url,
            amount,
            now_iso()
        ))

        conn.commit()

    clear_state(message.from_user.id)

    amount_text = f"{amount:,}".replace(",", " ")

    deal_text = (
        "〽️ *СДЕЛКА STAR OTC* 〽️\n\n"
        f"*{gift_url}*\n"
        f"*{amount_text}* ₽\n\n"
        f"🔑 Код сделки: `{deal_code}`\n\n"
        "🪙 Передайте этот код покупателю для оплаты сделки"
    )

    await message.answer(
        deal_text,
        reply_markup=deal_keyboard()
    )


# ============================================================
# WALLET
# ============================================================

@dp.callback_query(F.data == "menu:wallet")
async def callback_wallet(callback: CallbackQuery):

    ensure_user(callback.from_user)
    clear_state(callback.from_user.id)

    release_expired_holds()

    balance = get_balance(callback.from_user.id)
    held_balance = get_held_balance(callback.from_user.id)

    balance_text = f"{balance:,}".replace(",", " ")
    held_text = f"{held_balance:,}".replace(",", " ")

    # --------------------------------------------------------
    # История операций специально не показывается.
    # --------------------------------------------------------

    text = (
        "💰 *Ваш кошелёк*\n\n"
        f"Доступно: *{balance_text}* ₽\n"
        f"❄️ На удержании: *{held_text}* ₽\n\n"
        "📜 *История операций*\n"
        "Истории операций пока нет.\n\n"
        "Выберите действие ниже 👇"
    )

    await callback.answer()

    await safe_edit(
        callback,
        text,
        wallet_keyboard()
    )


# ============================================================
# ABOUT
# ============================================================

@dp.callback_query(F.data == "menu:about")
async def callback_about(callback: CallbackQuery):

    clear_state(callback.from_user.id)

    text = (
        "✨ *О нашем сервисе*\n\n"
        "Всё началось с простой идеи — сделать сделки "
        "между людьми понятнее и удобнее.\n\n"
        "Когда между двумя пользователями происходит "
        "обмен, всегда остаются вопросы: кто отправит первым, "
        "где хранить деньги, как передать ссылку и что делать, "
        "если что-то пошло не так.\n\n"
        "Мы решили собрать всё необходимое в одном месте.\n\n"
        "🤝 *Создание сделки*\n"
        "Создайте сделку, укажите условия и передайте "
        "код второй стороне.\n\n"
        "🔗 *Сделки по коду*\n"
        "Не нужно искать пользователя вручную — достаточно "
        "передать код сделки.\n\n"
        "💰 *Кошелёк*\n"
        "Баланс отображается в рублях. Пополнение и вывод "
        "находятся в одном разделе.\n\n"
        "🛟 *Поддержка*\n"
        "Если возник вопрос или проблема со сделкой, "
        "можно обратиться в поддержку.\n\n"
        "Спасибо, что пользуетесь сервисом, ваш StarsOtc ❤️"
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
async def callback_support(callback: CallbackQuery):

    clear_state(callback.from_user.id)

    text = (
        "🛟 *Здесь вы можете задать вопросы или прочитать "
        "ответы на уже решённые вопросы*\n\n"
        "🔐 *Насколько безопасны сделки?*\n\n"
        "Мы стараемся сделать процесс сделки максимально "
        "понятным и защищённым: информация о сделке "
        "фиксируется в системе, а её статус можно "
        "отслеживать в боте.\n\n"
        "🛡 *Что будет, если второй участник не выполнит условия?*\n\n"
        "Не подтверждайте завершение сделки, пока не убедились, "
        "что условия действительно выполнены. Если возникла "
        "спорная ситуация, обратитесь в поддержку и предоставьте "
        "код сделки и необходимые материалы.\n\n"
        "👤 *Можно ли доверять человеку, с которым я заключаю сделку?*\n\n"
        "Пользователи которые оплачивают проходят проверку "
        "телеграмма на содержание в скам базах."
    )

    await callback.answer()

    await safe_edit(
        callback,
        text,
        support_keyboard()
    )


# ============================================================
# SUPPORT OPERATOR
# ============================================================

@dp.callback_query(F.data == "support:operator")
async def callback_operator(callback: CallbackQuery):

    user = callback.from_user

    ensure_user(user)

    with db() as conn:

        conn.execute("""
            INSERT INTO support_requests (
                user_id,
                created_at
            )
            VALUES (?, ?)
        """, (
            user.id,
            now_iso()
        ))

        conn.commit()

    await callback.answer(
        "Заявка отправлена оператору.",
        show_alert=True
    )

    for admin_id in ADMINS:

        try:

            username = (
                f"@{user.username}"
                if user.username
                else "без username"
            )

            await bot.send_message(
                admin_id,
                "🛟 *Новая заявка в поддержку*\n\n"
                f"👤 Пользователь: {username}\n"
                f"🆔 ID: `{user.id}`\n"
                f"🕐 Время: `{display_datetime()}`",
                reply_markup=admin_reply_keyboard(user.id)
            )

        except Exception:

            logging.exception(
                "Не удалось уведомить администратора"
            )


# ============================================================
# ADMIN REPLY BUTTON
# ============================================================

@dp.callback_query(
    F.data.startswith("support:reply:")
)
async def callback_support_reply(callback: CallbackQuery):

    admin_id = callback.from_user.id

    if admin_id not in ADMINS:

        await callback.answer(
            "❌ У вас нет доступа.",
            show_alert=True
        )

        return

    try:

        user_id = int(
            callback.data.split(":")[-1]
        )

    except ValueError:

        await callback.answer(
            "❌ Ошибка пользователя.",
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
        "💬 *Введите сообщение для пользователя.*\n\n"
        "Следующее текстовое сообщение будет отправлено "
        "ему от имени администрации.\n\n"
        "Для отмены напишите `/cancel`."
    )


# ============================================================
# CANCEL STATE
# ============================================================

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):

    user_id = message.from_user.id

    if get_state(user_id):

        clear_state(user_id)

        await message.answer(
            "✅ Действие отменено."
        )

    else:

        await message.answer(
            "Нет активного действия."
        )


# ============================================================
# ADMIN SUPPORT MESSAGE
# ============================================================

async def process_admin_support_reply(message: Message):

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
            "🛟 *Ответ администрации*\n\n"
            f"{message.text}"
        )

        await message.answer(
            "✅ Сообщение отправлено пользователю."
        )

    except Exception:

        logging.exception(
            "Не удалось отправить ответ поддержки"
        )

        await message.answer(
            "❌ Не удалось отправить сообщение пользователю."
        )

    return True


# ============================================================
# DEPOSIT
# ============================================================

@dp.callback_query(F.data == "wallet:deposit")
async def callback_deposit(callback: CallbackQuery):

    clear_state(callback.from_user.id)

    text = (
        "📁 *Пополнение баланса*\n\n"
        "Для пополнения баланса обратитесь в поддержку.\n\n"
        "Оператор поможет оформить пополнение."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "🛟 Поддержка",
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

    await callback.answer()

    await safe_edit(
        callback,
        text,
        keyboard
    )


# ============================================================
# WITHDRAW
# ============================================================

@dp.callback_query(F.data == "wallet:withdraw")
async def callback_withdraw(callback: CallbackQuery):

    user_id = callback.from_user.id

    ensure_user(callback.from_user)

    release_expired_holds()

    balance = get_balance(user_id)
    held_balance = get_held_balance(user_id)

    if balance <= 0:

        if held_balance > 0:

            held_text = f"{held_balance:,}".replace(",", " ")

            await callback.answer(
                f"❄️ Все доступные средства находятся "
                f"на удержании: {held_text} ₽",
                show_alert=True
            )

        else:

            await callback.answer(
                "❌ На балансе нет средств для вывода.",
                show_alert=True
            )

        return

    balance_text = f"{balance:,}".replace(",", " ")
    held_text = f"{held_balance:,}".replace(",", " ")

    text = (
        "✳️ *Вывод средств*\n\n"
        f"Доступно: *{balance_text}* ₽\n"
        f"❄️ На удержании: *{held_text}* ₽\n\n"
        "Для оформления вывода обратитесь в поддержку."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "🛟 Оформить вывод",
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

    await callback.answer()

    await safe_edit(
        callback,
        text,
        keyboard
    )


# ============================================================
# CONFIRM DEAL
# ============================================================

@dp.callback_query(
    F.data.startswith("deal:confirm:")
)
async def callback_confirm_deal(callback: CallbackQuery):

    buyer_id = callback.from_user.id

    try:

        deal_id = int(
            callback.data.split(":")[-1]
        )

    except ValueError:

        await callback.answer(
            "❌ Некорректная сделка.",
            show_alert=True
        )

        return

    ensure_user(callback.from_user)

    with db() as conn:

        # ----------------------------------------------------
        # Блокируем логику через проверку status='paid'
        # ----------------------------------------------------

        deal = conn.execute("""
            SELECT *
            FROM deals
            WHERE id = ?
            LIMIT 1
        """, (
            deal_id,
        )).fetchone()

        if not deal:

            await callback.answer(
                "❌ Сделка не найдена.",
                show_alert=True
            )

            return

        if deal["payer_id"] != buyer_id:

            await callback.answer(
                "❌ Вы не являетесь покупателем этой сделки.",
                show_alert=True
            )

            return

        if deal["status"] != "paid":

            if deal["status"] == "confirmed":

                await callback.answer(
                    "❌ Сделка уже подтверждена.",
                    show_alert=True
                )

            else:

                await callback.answer(
                    "❌ Сделка пока недоступна для подтверждения.",
                    show_alert=True
                )

            return

        amount = int(deal["amount"])
        confirmed_at = now_iso()

        # ----------------------------------------------------
        # Меняем статус
        # ----------------------------------------------------

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
                "❌ Сделка уже была подтверждена.",
                show_alert=True
            )

            return

        # ----------------------------------------------------
        # Зачисляем продавцу в УДЕРЖАНИЕ
        # ----------------------------------------------------

        conn.execute("""
            UPDATE users
            SET
                held_balance = held_balance + ?,
                first_paid_at =
                    CASE
                        WHEN first_paid_at IS NULL
                        THEN ?
                        ELSE first_paid_at
                    END
            WHERE user_id = ?
        """, (
            amount,
            confirmed_at,
            deal["creator_id"]
        ))

        # ----------------------------------------------------
        # Создаём удержание
        # ----------------------------------------------------

        release_at = (
            datetime.now(timezone.utc)
            + timedelta(days=FREEZE_DAYS)
        ).isoformat()

        conn.execute("""
            INSERT INTO balance_holds (
                user_id,
                amount,
                deal_id,
                created_at,
                release_at,
                released
            )
            VALUES (?, ?, ?, ?, ?, 0)
        """, (
            deal["creator_id"],
            amount,
            deal_id,
            confirmed_at,
            release_at
        ))

        # ----------------------------------------------------
        # Операция продавца
        # ----------------------------------------------------

        conn.execute("""
            INSERT INTO operations (
                user_id,
                kind,
                amount,
                deal_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            deal["creator_id"],
            "deal_confirmed_hold",
            amount,
            deal_id,
            confirmed_at
        ))

        conn.commit()

    amount_text = f"{amount:,}".replace(",", " ")

    # ========================================================
    # Покупателю
    # ========================================================

    await callback.answer(
        "Сделка подтверждена!",
        show_alert=True
    )

    try:

        await callback.message.edit_text(
            "✅ *Сделка подтверждена.*\n\n"
            "Средства успешно зачислены продавцу "
            "и помещены на удержание."
        )

    except TelegramBadRequest:
        pass

    # ========================================================
    # Продавцу
    # ========================================================

    try:

        await bot.send_message(
            deal["creator_id"],
            "❄️ *Сделка подтверждена*\n\n"
            f"💰 Сумма: *{amount_text}* ₽\n"
            f"🔑 Код сделки: `{deal['code']}`\n\n"
            "Средства зачислены на ваш счёт и находятся "
            f"на удержании в течение {FREEZE_DAYS} дней.\n\n"
            "После окончания удержания средства автоматически "
            "станут доступны для вывода."
        )

    except Exception:

        logging.exception(
            "Не удалось уведомить продавца "
            "о подтверждении сделки"
        )


# ============================================================
# /P CODE
#
# Обычный пользователь:
# - баланс должен быть достаточным
# - деньги списываются
# - продавцу сразу НЕ начисляются
#
# Администратор:
# - может оплатить даже без баланса
# - его баланс НЕ изменяется
#
# После оплаты:
# - статус = paid
# - покупатель получает кнопку подтверждения
# - продавец получает уведомление
# ============================================================

@dp.message(Command("p"))
async def cmd_pay(message: Message):

    ensure_user(message.from_user)

    parts = message.text.split(maxsplit=1)

    if len(parts) != 2:

        await message.answer(
            "❌ Укажите код сделки.\n\n"
            "Пример:\n"
            "`/p ST-7K4P2Q`"
        )

        return

    code = parts[1].strip().upper()

    payer_id = message.from_user.id

    is_admin = payer_id in ADMINS

    with db() as conn:

        # ----------------------------------------------------
        # Находим сделку
        # ----------------------------------------------------

        deal = conn.execute("""
            SELECT *
            FROM deals
            WHERE UPPER(code) = ?
            LIMIT 1
        """, (
            code,
        )).fetchone()

        if not deal:

            await message.answer(
                "❌ Сделка с таким кодом не найдена."
            )

            return

        # ----------------------------------------------------
        # Проверяем статус
        # ----------------------------------------------------

        if deal["status"] != "pending":

            if deal["status"] == "paid":

                await message.answer(
                    "❌ Эта сделка уже оплачена и ожидает "
                    "подтверждения покупателя."
                )

            elif deal["status"] == "confirmed":

                await message.answer(
                    "❌ Эта сделка уже подтверждена."
                )

            else:

                await message.answer(
                    "❌ Эта сделка уже обработана."
                )

            return

        # ----------------------------------------------------
        # Нельзя оплатить свою сделку
        # ----------------------------------------------------

        if deal["creator_id"] == payer_id:

            await message.answer(
                "❌ Свою сделку оплатить нельзя."
            )

            return

        amount = int(deal["amount"])

        # ----------------------------------------------------
        # Обычный пользователь
        # ----------------------------------------------------

        if not is_admin:

            payer = conn.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (
                payer_id,
            )).fetchone()

            if not payer:

                await message.answer(
                    "❌ Пользователь не найден."
                )

                return

            payer_balance = int(
                payer["balance"]
            )

            if payer_balance < amount:

                missing = amount - payer_balance

                await message.answer(
                    "❌ Недостаточно средств.\n\n"
                    f"Стоимость сделки: *{amount:,}* ₽\n"
                    f"Ваш баланс: *{payer_balance:,}* ₽\n"
                    f"Не хватает: *{missing:,}* ₽"
                    .replace(",", " ")
                )

                return

            # ------------------------------------------------
            # Списываем деньги у покупателя
            # ------------------------------------------------

            cursor = conn.execute("""
                UPDATE users
                SET balance = balance - ?
                WHERE user_id = ?
                AND balance >= ?
            """, (
                amount,
                payer_id,
                amount
            ))

            if cursor.rowcount != 1:

                await message.answer(
                    "❌ Не удалось провести оплату. "
                    "Попробуйте ещё раз."
                )

                return

        # ----------------------------------------------------
        # Меняем статус сделки
        # ----------------------------------------------------

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

            # Если обычный пользователь уже был списан,
            # возвращаем ему деньги.

            if not is_admin:

                conn.execute("""
                    UPDATE users
                    SET balance = balance + ?
                    WHERE user_id = ?
                """, (
                    amount,
                    payer_id
                ))

            await message.answer(
                "❌ Эта сделка уже была оплачена."
            )

            return

        # ----------------------------------------------------
        # Операция покупателя
        # ----------------------------------------------------

        if not is_admin:

            conn.execute("""
                INSERT INTO operations (
                    user_id,
                    kind,
                    amount,
                    deal_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                payer_id,
                "deal_purchase",
                -amount,
                deal["id"],
                paid_at
            ))

        conn.commit()

    amount_text = f"{amount:,}".replace(",", " ")

    # ========================================================
    # Покупателю
    # ========================================================

    buyer_text = (
        "🎁 *Если вы получили подарок — подтвердите сделку*"
    )

    await message.answer(
        buyer_text,
        reply_markup=confirm_deal_keyboard(
            deal["id"]
        )
    )

    # ========================================================
    # Дополнительное сообщение об оплате
    # ========================================================

    if is_admin:

        try:

            await message.answer(
                "✅ *Сделка оплачена администратором.*\n\n"
                f"🔑 Код: `{code}`\n"
                f"💰 Сумма: *{amount_text}* ₽\n\n"
                "Баланс администратора не изменён.\n"
                "Средства сделки ожидают подтверждения покупателя."
            )

        except Exception:

            logging.exception(
                "Ошибка сообщения администратору"
            )

    # ========================================================
    # Продавцу
    # ========================================================

    try:

        await bot.send_message(
            deal["creator_id"],
            "💸 *Покупатель оплатил сделку, средства "
            "на удержании, за сделкой следит модератор бота.*\n\n"
            "Вы можете передавать подарок, после этого "
            "сделка будет подтверждена."
        )

    except Exception:

        logging.exception(
            "Не удалось отправить уведомление продавцу"
        )


# ============================================================
# /TXT MESSAGE
#
# Только администраторы.
#
# Использование:
# /txt Ваш текст
# ============================================================

@dp.message(Command("txt"))
async def cmd_txt(message: Message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "❌ У вас нет доступа к этой команде."
        )

        return

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2:

        await message.answer(
            "❌ Укажите текст сообщения.\n\n"
            "Пример:\n"
            "`/txt Сегодня технические работы.`"
        )

        return

    custom_text = parts[1].strip()

    if not custom_text:

        await message.answer(
            "❌ Сообщение не может быть пустым."
        )

        return

    broadcast_text = (
        "📣 Сообщение от администрации\n\n"
        f"{custom_text}"
    )

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
                broadcast_text,
                parse_mode=None
            )

            sent += 1

            # Небольшая пауза между сообщениями
            await asyncio.sleep(0.04)

        except Exception:

            failed += 1

            logging.exception(
                "Не удалось отправить /txt пользователю %s",
                user["user_id"]
            )

    await message.answer(
        "✅ Рассылка завершена.\n\n"
        f"📨 Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}"
    )


# ============================================================
# /PROFIT AMOUNT
#
# Только администраторы.
#
# Пример:
# /profit 50000
# ============================================================

@dp.message(Command("profit"))
async def cmd_profit(message: Message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "❌ У вас нет доступа к этой команде."
        )

        return

    parts = message.text.split(
        maxsplit=1
    )

    if len(parts) != 2:

        await message.answer(
            "❌ Укажите сумму.\n\n"
            "Пример:\n"
            "`/profit 50000`"
        )

        return

    value = parts[1].strip()

    if not value.isdigit():

        await message.answer(
            "❌ Сумма должна быть целым числом."
        )

        return

    amount = int(value)

    if amount <= 0:

        await message.answer(
            "❌ Сумма должна быть больше 0."
        )

        return

    amount_text = f"{amount:,}".replace(",", " ")

    broadcast_text = (
        "📣 Сообщение всем пользователям\n\n"
        "В боте только что произошла большая сделка "
        f"({amount_text} рублей)\n\n"
        f"{display_datetime()}"
    )

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
                broadcast_text,
                parse_mode=None
            )

            sent += 1

            await asyncio.sleep(0.04)

        except Exception:

            failed += 1

            logging.exception(
                "Не удалось отправить /profit пользователю %s",
                user["user_id"]
            )

    await message.answer(
        "✅ Уведомление отправлено.\n\n"
        f"💰 Сумма: {amount_text} ₽\n"
        f"📨 Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}"
    )


# ============================================================
# /M AMOUNT @USERNAME
# ============================================================

@dp.message(Command("m"))
async def cmd_money(message: Message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "❌ У вас нет доступа к этой команде."
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
            "❌ Сумма должна быть числом."
        )

        return

    if amount <= 0:

        await message.answer(
            "❌ Сумма должна быть больше 0."
        )

        return

    username = parts[2].strip()

    if not username.startswith("@"):

        await message.answer(
            "❌ Укажите username через @."
        )

        return

    username = username[1:]

    with db() as conn:

        user = conn.execute("""
            SELECT *
            FROM users
            WHERE LOWER(username) = LOWER(?)
            LIMIT 1
        """, (
            username,
        )).fetchone()

        if not user:

            await message.answer(
                "❌ Пользователь с таким username "
                "не найден в базе бота."
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
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            user["user_id"],
            "admin_credit",
            amount,
            now_iso()
        ))

        conn.commit()

    amount_text = f"{amount:,}".replace(",", " ")

    await message.answer(
        "✅ Баланс пополнен.\n\n"
        f"👤 @{username}\n"
        f"💰 +{amount_text} ₽"
    )

    try:

        await bot.send_message(
            user["user_id"],
            "💰 *Баланс пополнен*\n\n"
            f"Вам начислено: *{amount_text}* ₽"
        )

    except Exception:

        logging.exception(
            "Не удалось уведомить пользователя "
            "о пополнении"
        )


# ============================================================
# /DELBAL
# ============================================================

@dp.message(Command("delbal"))
async def cmd_delbal(message: Message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "❌ У вас нет доступа к этой команде."
        )

        return

    with db() as conn:

        conn.execute("""
            UPDATE users
            SET
                balance = 0,
                held_balance = 0
        """)

        conn.execute("""
            UPDATE balance_holds
            SET released = 1
            WHERE released = 0
        """)

        conn.commit()

    await message.answer(
        "✅ Балансы всех пользователей очищены."
    )


# ============================================================
# /DELSDEL
# ============================================================

@dp.message(Command("delsdel"))
async def cmd_delsdel(message: Message):

    if message.from_user.id not in ADMINS:

        await message.answer(
            "❌ У вас нет доступа к этой команде."
        )

        return

    with db() as conn:

        conn.execute("""
            DELETE FROM deals
        """)

        conn.execute("""
            DELETE FROM operations
        """)

        conn.execute("""
            DELETE FROM balance_holds
        """)

        conn.commit()

    await message.answer(
        "✅ Все сделки удалены."
    )


# ============================================================
# TEXT HANDLER
# ============================================================

@dp.message(F.text)
async def text_handler(message: Message):

    # --------------------------------------------------------
    # Команды здесь не обрабатываем
    # --------------------------------------------------------

    if message.text.startswith("/"):

        return

    ensure_user(message.from_user)

    user_id = message.from_user.id

    # --------------------------------------------------------
    # Ответ администратора пользователю
    # --------------------------------------------------------

    if user_id in ADMINS:

        handled = await process_admin_support_reply(
            message
        )

        if handled:
            return

    # --------------------------------------------------------
    # Состояние ожидания ссылки
    # --------------------------------------------------------

    state = get_state(user_id)

    if state == "waiting_gift":

        await process_gift_link(message)

        return

    # --------------------------------------------------------
    # Состояние ожидания цены
    # --------------------------------------------------------

    if isinstance(state, dict):

        if state.get("state") == "waiting_price":

            await process_price(message)

            return


# ============================================================
# CALLBACK ERRORS
# ============================================================

@dp.callback_query()
async def unknown_callback(callback: CallbackQuery):

    await callback.answer()


# ============================================================
# START BOT
# ============================================================

async def main():

    init_db()

    logging.info(
        "STAR OTC запускается..."
    )

    # Автоматическое освобождение средств
    # после окончания 3-дневного удержания.
    asyncio.create_task(
        release_holds_loop()
    )

    await dp.start_polling(bot)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    asyncio.run(main())
