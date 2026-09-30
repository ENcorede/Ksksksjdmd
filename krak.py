import asyncio
import logging
import os
import re
import sqlite3
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
# STAR OTC — Telegram bot
# Python 3.10+
# Install:
#   pip install -U aiogram
#
# IMPORTANT:
# Put your NEW bot token into the BOT_TOKEN environment variable.
# Example Linux/macOS:
#   export BOT_TOKEN="YOUR_NEW_TOKEN"
#
# Windows PowerShell:
#   $env:BOT_TOKEN="YOUR_NEW_TOKEN"
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Администраторы
ADMINS = {5068550043, 6977407005}

DB_FILE = "star_otc.sqlite3"

# Срок заморозки средств после первой оплаченной сделки
FREEZE_DAYS = 3

# Формат:
# t.me/nft/любоеНазваниеПодарка-1234567
# Номер — от 1 до 7 цифр.
# Название подарка — латиница/цифры/подчёркивание.
GIFT_RE = re.compile(
    r"^(?:https?://)?t\.me/nft/([A-Za-z0-9_]+)-([0-9]{1,7})$"
)

# Целая положительная сумма в рублях
PRICE_RE = re.compile(r"^[1-9][0-9]*$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN. Создайте новый токен через @BotFather "
        "и задайте переменную окружения BOT_TOKEN."
    )

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
)
dp = Dispatcher()


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


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
                creator_id INTEGER NOT NULL,
                gift_url TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payer_id INTEGER,
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
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new'
            )
        """)

        conn.commit()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_user(tg_user):
    with db() as conn:
        conn.execute("""
            INSERT INTO users
                (user_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (
            tg_user.id,
            tg_user.username,
            tg_user.first_name or "",
            now_iso(),
        ))
        conn.commit()


def get_user(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()


def get_balance(user_id):
    row = get_user(user_id)
    return int(row["balance"]) if row else 0


def get_active_deals(user_id):
    with db() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS cnt
            FROM deals
            WHERE creator_id = ? AND status = 'pending'
        """, (user_id,)).fetchone()
        return int(row["cnt"])


def has_paid_deal(user_id):
    with db() as conn:
        row = conn.execute("""
            SELECT 1
            FROM deals
            WHERE creator_id = ? AND status = 'paid'
            LIMIT 1
        """, (user_id,)).fetchone()
        return row is not None


def first_paid_date(user_id):
    row = get_user(user_id)
    if not row or not row["first_paid_at"]:
        return None
    try:
        return datetime.fromisoformat(row["first_paid_at"])
    except ValueError:
        return None


# ============================================================
# UI
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
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
        ],
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔴 Выйти",
                callback_data="menu:home"
            )
        ]
    ])


def wallet_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✳️ Вывод ✳️",
                callback_data="wallet:withdraw"
            ),
            InlineKeyboardButton(
                text="📁 Пополнение 📁",
                callback_data="wallet:deposit"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔴 Выйти",
                callback_data="menu:home"
            )
        ],
    ])


def support_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🛟 Связаться с поддержкой",
                callback_data="support:contact"
            )
        ],
        [
            InlineKeyboardButton(
                text="🔴 Выйти",
                callback_data="menu:home"
            )
        ],
    ])


def withdraw_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔴 Выйти",
                callback_data="menu:home"
            )
        ]
    ])


# ============================================================
# TEXTS
# ============================================================

def home_text(user_id):
    balance = get_balance(user_id)
    active = get_active_deals(user_id)

    return (
        "⭐ *Добро пожаловать!*\n\n"
        "Здесь вы можете безопасно создавать сделки, обмениваться "
        "ссылками и управлять своим балансом.\n\n"
        f"💰 Баланс: *{balance:,}* ₽\n"
        f"📁 Активных сделок: *{active}*\n\n"
        "Выберите нужный раздел ниже 👇"
    ).replace(",", " ")


WALLET_TEXT = (
    "💰 *Ваш кошелёк*\n\n"
    "Баланс: *{balance}* ₽\n\n"
    "Здесь вы можете управлять средствами: пополнять баланс, "
    "выводить деньги и просматривать историю операций.\n\n"
    "Выберите действие ниже 👇"
)

ABOUT_TEXT = """✨ *О нашем сервисе*

Всё началось с простой идеи — сделать сделки между людьми понятнее и удобнее.

Когда между двумя пользователями происходит обмен, всегда остаются вопросы: кто отправит первым, где хранить деньги, как передать ссылку и что делать, если что-то пошло не так.

Мы решили собрать всё необходимое в одном месте.

🤝 *Создание сделки*
Создайте сделку, укажите условия и передайте ссылку второй стороне.

🔗 *Сделки по ссылке*
Не нужно искать пользователя вручную — достаточно отправить готовую ссылку.

💰 *Кошелёк*
Баланс отображается в рублях. Пополнение и вывод находятся в одном разделе.

🛟 *Поддержка*
Если возник вопрос или проблема со сделкой, можно обратиться в поддержку.

Спасибо, что пользуетесь сервисом, ваш StarsOtc ❤️"""

SUPPORT_TEXT = """🛟 *Здесь вы можете задать вопросы или прочитать ответы на уже решённые вопросы*

🔐 *Насколько безопасны сделки?*

Мы стараемся сделать процесс сделки максимально понятным и защищённым: информация о сделке фиксируется в системе, а её статус можно отслеживать в боте.

🛡 *Что будет, если второй участник не выполнит условия?*

Не подтверждайте завершение сделки, пока не убедились, что условия действительно выполнены. Если возник спорная ситуация, обратитесь в поддержку и предоставьте номер сделки и необходимые материалы.

👤 *Можно ли доверять человеку, с которым я заключаю сделку?*

Пользователи которые оплачивают проходят проверку телеграмма на содержание в скам базах"""


# ============================================================
# MESSAGE EDITING
# ============================================================

async def safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup=None
):
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup
        )
    except TelegramBadRequest as e:
        # "message is not modified" — ничего делать не нужно
        if "message is not modified" not in str(e).lower():
            raise


async def show_home(callback: CallbackQuery):
    await safe_edit(
        callback,
        home_text(callback.from_user.id),
        main_keyboard()
    )


async def show_wallet(callback: CallbackQuery):
    balance = get_balance(callback.from_user.id)

    text = WALLET_TEXT.format(
        balance=f"{balance:,}".replace(",", " ")
    )

    await safe_edit(callback, text, wallet_keyboard())


# ============================================================
# COMMANDS
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    ensure_user(message.from_user)

    await message.answer(
        home_text(message.from_user.id),
        reply_markup=main_keyboard()
    )


@dp.message(Command("delbal"))
async def cmd_delbal(message: Message):
    ensure_user(message.from_user)

    if message.from_user.id not in ADMINS:
        await message.answer("⛔ Команда доступна только администраторам.")
        return

    with db() as conn:
        conn.execute("UPDATE users SET balance = 0")
        conn.commit()

    await message.answer("✅ Балансы всех пользователей очищены.")


@dp.message(Command("delsdel"))
async def cmd_delsdel(message: Message):
    ensure_user(message.from_user)

    if message.from_user.id not in ADMINS:
        await message.answer("⛔ Команда доступна только администраторам.")
        return

    with db() as conn:
        conn.execute("DELETE FROM deals")
        conn.commit()

    await message.answer("✅ Все сделки удалены.")


@dp.message(Command("m"))
async def cmd_m(message: Message):
    ensure_user(message.from_user)

    if message.from_user.id not in ADMINS:
        await message.answer("⛔ Команда доступна только администраторам.")
        return

    parts = message.text.split(maxsplit=2)

    if len(parts) != 3:
        await message.answer(
            "Формат:\n`/m 100000 @username`"
        )
        return

    try:
        amount = int(parts[1])
    except ValueError:
        await message.answer("❌ Сумма должна быть целым числом.")
        return

    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше нуля.")
        return

    username = parts[2].strip()

    if not username.startswith("@") or len(username) < 2:
        await message.answer("❌ Укажи пользователя в формате @username.")
        return

    username_clean = username[1:].lower()

    with db() as conn:
        target = conn.execute("""
            SELECT user_id, username
            FROM users
            WHERE LOWER(username) = ?
        """, (username_clean,)).fetchone()

        if not target:
            await message.answer(
                "❌ Пользователь ещё не запускал бота, поэтому его нельзя найти по @username."
            )
            return

        conn.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
        """, (amount, target["user_id"]))

        conn.execute("""
            INSERT INTO operations
                (user_id, kind, amount, created_at)
            VALUES (?, 'admin_deposit', ?, ?)
        """, (target["user_id"], amount, now_iso()))

        conn.commit()

    await message.answer(
        f"✅ Пользователю {username} зачислено *{amount:,}* ₽."
        .replace(",", " ")
    )


# ============================================================
# MAIN MENU CALLBACKS
# ============================================================

@dp.callback_query(F.data == "menu:home")
async def cb_home(callback: CallbackQuery):
    ensure_user(callback.from_user)
    await callback.answer()
    await show_home(callback)


@dp.callback_query(F.data == "menu:create")
async def cb_create(callback: CallbackQuery):
    ensure_user(callback.from_user)
    await callback.answer()

    await safe_edit(
        callback,
        "📒 *Пришлите ссылку на подарок*\n\n"
        "Формат:\n"
        "`t.me/nft/НазваниеПодарка-1234567`\n\n"
        "Номер подарка — не больше 7 цифр.",
        back_keyboard()
    )


@dp.callback_query(F.data == "menu:wallet")
async def cb_wallet(callback: CallbackQuery):
    ensure_user(callback.from_user)
    await callback.answer()
    await show_wallet(callback)


@dp.callback_query(F.data == "menu:about")
async def cb_about(callback: CallbackQuery):
    ensure_user(callback.from_user)
    await callback.answer()

    await safe_edit(
        callback,
        ABOUT_TEXT,
        back_keyboard()
    )


@dp.callback_query(F.data == "menu:support")
async def cb_support(callback: CallbackQuery):
    ensure_user(callback.from_user)
    await callback.answer()

    await safe_edit(
        callback,
        SUPPORT_TEXT,
        support_keyboard()
    )


# ============================================================
# CREATE DEAL — STATE
# Простое состояние в памяти. Для одного процесса бота достаточно.
# ============================================================

user_states = {}


def set_state(user_id, state, **data):
    user_states[user_id] = {
        "state": state,
        **data,
    }


def get_state(user_id):
    return user_states.get(user_id)


def clear_state(user_id):
    user_states.pop(user_id, None)


@dp.message(F.text)
async def text_handler(message: Message):
    ensure_user(message.from_user)

    # Команды обработаны выше
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id
    state = get_state(user_id)

    if not state:
        return

    if state["state"] == "gift":
        await process_gift_link(message)
        return

    if state["state"] == "price":
        await process_price(message)
        return

    if state["state"] == "withdraw_amount":
        await process_withdraw_amount(message)
        return

    if state["state"] == "deposit_amount":
        await process_deposit_amount(message)
        return


async def process_gift_link(message: Message):
    user_id = message.from_user.id
    value = message.text.strip()

    match = GIFT_RE.fullmatch(value)

    if not match:
        await message.answer(
            "❌ Ссылка указана неверно.\n\n"
            "Используйте формат:\n"
            "`t.me/nft/НазваниеПодарка-1234567`\n\n"
            "Последняя часть должна содержать от 1 до 7 цифр."
        )
        return

    # Нормализуем ссылку без https://
    gift_url = f"t.me/nft/{match.group(1)}-{match.group(2)}"

    set_state(
        user_id,
        "price",
        gift_url=gift_url
    )

    await message.answer(
        "🪙 *Выберите цену сделки в рублях целым числом*"
    )


async def process_price(message: Message):
    user_id = message.from_user.id
    value = message.text.strip().replace(" ", "")

    if not PRICE_RE.fullmatch(value):
        await message.answer(
            "❌ Цена должна быть положительным целым числом.\n"
            "Например: `15000`"
        )
        return

    amount = int(value)

    if amount > 2_000_000_000:
        await message.answer("❌ Слишком большая сумма.")
        return

    state = get_state(user_id)

    if not state or "gift_url" not in state:
        clear_state(user_id)
        await message.answer(
            "Сессия создания сделки сброшена. Нажмите «Создать сделку» ещё раз."
        )
        return

    gift_url = state["gift_url"]

    with db() as conn:
        cursor = conn.execute("""
            INSERT INTO deals
                (creator_id, gift_url, amount, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
        """, (
            user_id,
            gift_url,
            amount,
            now_iso()
        ))
        deal_id = cursor.lastrowid
        conn.commit()

    clear_state(user_id)

    deal_text = (
        "〽️ *СДЕЛКА STAR OTC* 〽️\n\n"
        f"*{gift_url}*\n"
        f"*{amount:,}* ₽\n\n"
        "🪙 Чтобы сделку подтвердили перешлите это сообщение покупателю"
    ).replace(",", " ")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💳 ОПЛАТИТЬ 💳",
                callback_data=f"pay:{deal_id}"
            )
        ]
    ])

    await message.answer(deal_text, reply_markup=keyboard)


# ============================================================
# PAY DEAL
# ============================================================

@dp.callback_query(F.data.startswith("pay:"))
async def cb_pay_deal(callback: CallbackQuery):
    ensure_user(callback.from_user)

    try:
        deal_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректная сделка.", show_alert=True)
        return

    payer_id = callback.from_user.id

    with db() as conn:
        deal = conn.execute("""
            SELECT *
            FROM deals
            WHERE id = ?
        """, (deal_id,)).fetchone()

        if not deal:
            await callback.answer(
                "❌ Сделка не найдена.",
                show_alert=True
            )
            return

        if deal["status"] != "pending":
            await callback.answer(
                "❌ Эта сделка уже оплачена или закрыта.",
                show_alert=True
            )
            return

        if deal["creator_id"] == payer_id:
            await callback.answer(
                "❌ Свою сделку оплатить нельзя.",
                show_alert=True
            )
            return

        # Для данной логики баланс плательщика не списывается:
        # оплата сделки начисляется создателю сделки.
        # Это соответствует описанному пользователем сценарию.
        paid_at = now_iso()

        conn.execute("""
            UPDATE deals
            SET status = 'paid',
                payer_id = ?,
                paid_at = ?
            WHERE id = ? AND status = 'pending'
        """, (payer_id, paid_at, deal_id))

        conn.execute("""
            UPDATE users
            SET balance = balance + ?,
                first_paid_at =
                    CASE
                        WHEN first_paid_at IS NULL THEN ?
                        ELSE first_paid_at
                    END
            WHERE user_id = ?
        """, (
            deal["amount"],
            paid_at,
            deal["creator_id"]
        ))

        conn.execute("""
            INSERT INTO operations
                (user_id, kind, amount, deal_id, created_at)
            VALUES (?, 'deal_paid', ?, ?, ?)
        """, (
            deal["creator_id"],
            deal["amount"],
            deal_id,
            paid_at
        ))

        conn.commit()

    await callback.answer("✅ Сделка оплачена.")

    # Уведомляем создателя сделки
    try:
        await bot.send_message(
            deal["creator_id"],
            "〽️ *Сделка оплачена. Деньги поступили на счёт, "
            "передайте подарок пользователю, если вы его ещё не передали*"
        )
    except Exception:
        logging.exception(
            "Не удалось отправить уведомление создателю сделки %s",
            deal_id
        )

    # Обновляем кнопку сделки, чтобы повторно оплатить нельзя было
    try:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ ОПЛАЧЕНО",
                        callback_data="paid:no"
                    )
                ]
            ])
        )
    except Exception:
        pass


@dp.callback_query(F.data == "paid:no")
async def cb_paid_no(callback: CallbackQuery):
    await callback.answer(
        "Эта сделка уже оплачена.",
        show_alert=True
    )


# ============================================================
# WALLET
# ============================================================

@dp.callback_query(F.data == "wallet:withdraw")
async def cb_withdraw(callback: CallbackQuery):
    ensure_user(callback.from_user)
    await callback.answer()

    user_id = callback.from_user.id

    # Если хоть одна сделка пользователя была оплачена,
    # средства заморожены на 3 дня от первой оплаченной сделки.
    if has_paid_deal(user_id):
        first_paid = first_paid_date(user_id)

        if first_paid:
            available_at = first_paid + timedelta(days=FREEZE_DAYS)
            current = datetime.now(timezone.utc)

            if current < available_at:
                await safe_edit(
                    callback,
                    "❄️ *Средства в заморозке. "
                    "Подождите 3 дня для вывода средств.*",
                    withdraw_back_keyboard()
                )
                return

            # После 3 дней вывод доступен
            balance = get_balance(user_id)

            if balance <= 0:
                await safe_edit(
                    callback,
                    "❌ На балансе недостаточно средств для вывода.",
                    withdraw_back_keyboard()
                )
                return

            set_state(user_id, "withdraw_amount")

            await safe_edit(
                callback,
                "💸 *Средства доступны для вывода.*\n\n"
                f"Баланс: *{balance:,}* ₽\n\n"
                "Введите сумму вывода целым числом.",
                withdraw_back_keyboard()
            )
            return

    # Если оплаченных сделок ещё не было
    await safe_edit(
        callback,
        "❄️ *Средства в заморозке. "
        "Подождите 3 дня для вывода средств.*",
        withdraw_back_keyboard()
    )


async def process_withdraw_amount(message: Message):
    user_id = message.from_user.id
    value = message.text.strip().replace(" ", "")

    if not value.isdigit() or int(value) <= 0:
        await message.answer(
            "❌ Введите положительную сумму целым числом."
        )
        return

    amount = int(value)
    balance = get_balance(user_id)

    if amount > balance:
        await message.answer(
            f"❌ Недостаточно средств.\n"
            f"Ваш баланс: *{balance:,}* ₽".replace(",", " ")
        )
        return

    # Автоматический реальный вывод здесь намеренно не выполняется:
    # для него нужен конкретный платёжный провайдер/реквизиты.
    with db() as conn:
        conn.execute("""
            INSERT INTO operations
                (user_id, kind, amount, created_at)
            VALUES (?, 'withdraw_request', ?, ?)
        """, (user_id, amount, now_iso()))
        conn.commit()

    clear_state(user_id)

    # Сообщаем админам о заявке
    user = get_user(user_id)
    username = f"@{user['username']}" if user and user["username"] else "без username"

    admin_text = (
        "💸 *Новая заявка на вывод*\n\n"
        f"👤 Пользователь: {username}\n"
        f"🆔 ID: `{user_id}`\n"
        f"💰 Сумма: *{amount:,}* ₽"
    ).replace(",", " ")

    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, admin_text)
        except Exception:
            logging.exception("Не удалось уведомить администратора %s", admin_id)

    await message.answer(
        "✅ Заявка на вывод принята.\n\n"
        "Оператор обработает её вручную."
    )


@dp.callback_query(F.data == "wallet:deposit")
async def cb_deposit(callback: CallbackQuery):
    ensure_user(callback.from_user)
    await callback.answer()

    set_state(callback.from_user.id, "deposit_amount")

    await safe_edit(
        callback,
        "📁 *Пополнение*\n\n"
        "Введите сумму пополнения целым числом.\n\n"
        "После этого заявка будет передана оператору.",
        withdraw_back_keyboard()
    )


async def process_deposit_amount(message: Message):
    user_id = message.from_user.id
    value = message.text.strip().replace(" ", "")

    if not value.isdigit() or int(value) <= 0:
        await message.answer(
            "❌ Введите положительную сумму целым числом."
        )
        return

    amount = int(value)

    with db() as conn:
        conn.execute("""
            INSERT INTO operations
                (user_id, kind, amount, created_at)
            VALUES (?, 'deposit_request', ?, ?)
        """, (user_id, amount, now_iso()))
        conn.commit()

    clear_state(user_id)

    user = get_user(user_id)
    username = f"@{user['username']}" if user and user["username"] else "без username"

    admin_text = (
        "📁 *Новая заявка на пополнение*\n\n"
        f"👤 Пользователь: {username}\n"
        f"🆔 ID: `{user_id}`\n"
        f"💰 Сумма: *{amount:,}* ₽"
    ).replace(",", " ")

    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, admin_text)
        except Exception:
            logging.exception("Не удалось уведомить администратора %s", admin_id)

    await message.answer(
        "✅ Заявка на пополнение создана.\n\n"
        "Оператор обработает её вручную."
    )


# ============================================================
# SUPPORT
# ============================================================

@dp.callback_query(F.data == "support:contact")
async def cb_support_contact(callback: CallbackQuery):
    ensure_user(callback.from_user)

    with db() as conn:
        conn.execute("""
            INSERT INTO support_requests
                (user_id, created_at, status)
            VALUES (?, ?, 'new')
        """, (callback.from_user.id, now_iso()))
        conn.commit()

    await callback.answer(
        "Все операторы заняты. Мы записали ваше желание написать "
        "и ответим вскоре.",
        show_alert=True
    )

    for admin_id in ADMINS:
        try:
            await bot.send_message(
                admin_id,
                "🛟 *Новая заявка в поддержку*\n\n"
                f"Пользователь: `{callback.from_user.id}`\n"
                f"Username: @{callback.from_user.username}"
                if callback.from_user.username
                else
                "🛟 *Новая заявка в поддержку*\n\n"
                f"Пользователь: `{callback.from_user.id}`"
            )
        except Exception:
            logging.exception("Не удалось уведомить администратора")


# ============================================================
# UNKNOWN CALLBACK
# ============================================================

@dp.callback_query()
async def unknown_callback(callback: CallbackQuery):
    await callback.answer()


# ============================================================
# START
# ============================================================

async def main():
    init_db()

    me = await bot.get_me()
    logging.info(
        "STAR OTC запущен: @%s (%s)",
        me.username,
        me.id
    )

    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
