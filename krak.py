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


def init_db():

    with db() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                first_paid_at TEXT,
                created_at TEXT NOT NULL
            )
        """)

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
                paid_at TEXT
            )
        """)

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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # ----------------------------------------------------
        # Миграция старой БД:
        # если раньше поля code не было — добавляем
        # ----------------------------------------------------

        columns = conn.execute(
            "PRAGMA table_info(deals)"
        ).fetchall()

        column_names = {
            column["name"]
            for column in columns
        }

        if "code" not in column_names:

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
        """, (code,)).fetchone()

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
                    created_at
                )
                VALUES (?, ?, ?, 0, ?)
            """, (
                user.id,
                user.username,
                user.first_name,
                now_iso()
            ))

        conn.commit()


def get_balance(user_id):

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


def get_active_deals_count(user_id):

    with db() as conn:

        row = conn.execute("""
            SELECT COUNT(*) AS count
            FROM deals
            WHERE creator_id = ?
            AND status = 'pending'
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
# KEYBOARDS
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔷 Создать сделку 🔷",
                    callback_data="menu:create"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❇️ Кошелек ❇️",
                    callback_data="menu:wallet"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💡 О сервисе 💡",
                    callback_data="menu:about"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛟 Поддержка 🛟",
                    callback_data="menu:support"
                )
            ]
        ]
    )


def back_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔴 Выйти",
                    callback_data="menu:home"
                )
            ]
        ]
    )


def wallet_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✳️ Вывод ✳️",
                    callback_data="wallet:withdraw"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📁 Пополнение 📁",
                    callback_data="wallet:deposit"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔴 Выйти",
                    callback_data="menu:home"
                )
            ]
        ]
    )


def support_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛟 Написать оператору",
                    callback_data="support:operator"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔴 Выйти",
                    callback_data="menu:home"
                )
            ]
        ]
    )


def deal_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔴 Выйти",
                    callback_data="menu:home"
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

    balance = get_balance(user.id)
    active_deals = get_active_deals_count(user.id)

    text = (
        "⭐ *Добро пожаловать!*\n\n"
        "Здесь вы можете безопасно создавать сделки, "
        "обмениваться ссылками и управлять своим балансом.\n\n"
        f"💰 Баланс: *{balance:,}* ₽\n"
        f"📁 Активных сделок: *{active_deals}*\n\n"
        "Выберите нужный раздел ниже 👇"
    ).replace(",", " ")

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

    # --------------------------------------------------------
    # Сохраняем ссылку во временное состояние
    # --------------------------------------------------------

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

        deal_id = cursor.lastrowid

        conn.commit()

    clear_state(message.from_user.id)

    amount_text = f"{amount:,}".replace(",", " ")

    deal_text = (
        "〽️ *СДЕЛКА STAR OTC* 〽️\n\n"
        f"*{gift_url}*\n"
        f"*{amount_text}* ₽\n\n"
        f"🔑 Код сделки: `{deal_code}`\n\n"
        "🪙 Передайте этот код покупателю для подтверждения сделки"
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

    balance = get_balance(callback.from_user.id)

    balance_text = f"{balance:,}".replace(",", " ")

    text = (
        "💰 *Ваш кошелёк*\n\n"
        f"Баланс: *{balance_text}* ₽\n\n"
        "Здесь вы можете управлять средствами: "
        "пополнять баланс, выводить деньги и "
        "просматривать историю операций.\n\n"
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
        "ссылку второй стороне.\n\n"
        "🔗 *Сделки по ссылке*\n"
        "Не нужно искать пользователя вручную — достаточно "
        "отправить готовую ссылку.\n\n"
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
        "номер сделки и необходимые материалы.\n\n"
        "👤 *Можно ли доверять человеку, с которым я заключаю сделку?*\n\n"
        "Пользователи которые оплачивают проходят проверку "
        "телеграмма на содержание в скам базах"
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
        "Все операторы заняты. Мы записали ваше желание "
        "написать и ответим вскоре.",
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
                f"🕐 Время: `{now_iso()}`"
            )

        except Exception:

            logging.exception(
                "Не удалось уведомить администратора"
            )


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
                InlineKeyboardButton(
                    text="🛟 Поддержка",
                    callback_data="support:operator"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔴 Выйти",
                    callback_data="menu:home"
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

    first_paid_at = get_first_paid_at(user_id)

    if not first_paid_at:

        await callback.answer(
            "❄️ Средства в заморозке. "
            "Подождите 3 дня для вывода средств.",
            show_alert=True
        )

        return

    try:

        paid_time = datetime.fromisoformat(
            first_paid_at
        )

        if paid_time.tzinfo is None:
            paid_time = paid_time.replace(
                tzinfo=timezone.utc
            )

    except Exception:

        await callback.answer(
            "❄️ Средства в заморозке. "
            "Подождите 3 дня для вывода средств.",
            show_alert=True
        )

        return

    unlock_time = paid_time + timedelta(
        days=FREEZE_DAYS
    )

    now = datetime.now(timezone.utc)

    if now < unlock_time:

        await callback.answer(
            "❄️ Средства в заморозке. "
            "Подождите 3 дня для вывода средств.",
            show_alert=True
        )

        return

    balance = get_balance(user_id)

    if balance <= 0:

        await callback.answer(
            "❌ На балансе нет средств для вывода.",
            show_alert=True
        )

        return

    balance_text = f"{balance:,}".replace(",", " ")

    text = (
        "✳️ *Вывод средств*\n\n"
        f"Доступно: *{balance_text}* ₽\n\n"
        "Для оформления вывода обратитесь в поддержку."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛟 Оформить вывод",
                    callback_data="support:operator"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔴 Выйти",
                    callback_data="menu:home"
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
# /P CODE
#
# АДМИН:
# - может оплатить любую сделку
# - баланс администратора НЕ списывается
#
# ОБЫЧНЫЙ ПОЛЬЗОВАТЕЛЬ:
# - должен иметь достаточно средств
# - деньги списываются с его баланса
# - деньги начисляются продавцу
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

            await message.answer(
                "❌ Эта сделка уже оплачена."
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
        # Обычный пользователь должен иметь деньги
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
            # Списываем деньги у обычного покупателя
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

            # Защита от двойной оплаты одновременно
            if cursor.rowcount != 1:

                await message.answer(
                    "❌ Не удалось провести оплату. "
                    "Попробуйте ещё раз."
                )

                return

        # ----------------------------------------------------
        # Админ:
        # ничего со своего баланса не списывает
        # ----------------------------------------------------

        # ----------------------------------------------------
        # Начисляем продавцу
        # ----------------------------------------------------

        paid_at = now_iso()

        conn.execute("""
            UPDATE users
            SET
                balance = balance + ?,
                first_paid_at =
                    CASE
                        WHEN first_paid_at IS NULL
                        THEN ?
                        ELSE first_paid_at
                    END
            WHERE user_id = ?
        """, (
            amount,
            paid_at,
            deal["creator_id"]
        ))

        # ----------------------------------------------------
        # Меняем статус сделки только если она ещё pending
        # ----------------------------------------------------

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

            # Теоретически сюда можно попасть при
            # одновременной оплате двумя людьми.
            # Для обычного пользователя возвращаем деньги.

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
            "deal_paid",
            amount,
            deal["id"],
            paid_at
        ))

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

    # ========================================================
    # Сообщение тому, кто оплатил
    # ========================================================

    amount_text = f"{amount:,}".replace(",", " ")

    if is_admin:

        await message.answer(
            "✅ *Сделка оплачена администратором!*\n\n"
            f"🔑 Код: `{code}`\n"
            f"💰 Сумма: *{amount_text}* ₽\n\n"
            "Баланс администратора не изменён."
        )

    else:

        await message.answer(
            "✅ *Сделка оплачена!*\n\n"
            f"🔑 Код: `{code}`\n"
            f"💰 Сумма: *{amount_text}* ₽\n\n"
            "Средства списаны с вашего баланса."
        )

    # ========================================================
    # Уведомление продавца
    # ========================================================

    try:

        await bot.send_message(
            deal["creator_id"],
            "〽️ Сделка оплачена. Деньги поступили "
            "на счёт, передайте подарок пользователю, "
            "если вы его ещё не передали"
        )

    except Exception:

        logging.exception(
            "Не удалось отправить уведомление продавцу"
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
            SET balance = 0
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

        conn.commit()

    await message.answer(
        "✅ Все сделки удалены."
    )


# ============================================================
# TEXT HANDLER
# ============================================================

@dp.message(F.text)
async def text_handler(message: Message):

    if message.text.startswith("/"):

        return

    ensure_user(message.from_user)

    state = get_state(message.from_user.id)

    # --------------------------------------------------------
    # Состояние ожидания ссылки
    # --------------------------------------------------------

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

    logging.info("STAR OTC запускается...")

    await dp.start_polling(bot)


if __name__ == "__main__":

    asyncio.run(main())
