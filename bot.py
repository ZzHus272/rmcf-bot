import asyncio
import json
import logging
import os
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("rmc_bot")

DB_PATH = os.getenv("RMC_DB", "rmc.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

LANG_UNDER_CONSTRUCTION = {
    "ru": "Русский язык скоро добавим.",
    "es": "El idioma español se añadirá pronto.",
}

PASSPORT_RE = re.compile(r"^\d{7}$")

FLOW_NONE = None
FLOW_REGISTER = "register"
FLOW_ADD_BUSINESS = "add_business"
FLOW_INVITE_STAFF = "invite_staff"
FLOW_APPLY_JOB = "apply_job"
FLOW_TRANSFER_COMPANY = "transfer_company"
FLOW_INTERVIEW_ASK = "interview_ask"
FLOW_DELETE_BUSINESS_CONFIRM = "delete_business_confirm"
FLOW_DELETE_JOB_CONFIRM = "delete_job_confirm"
FLOW_LEAVE_JOB_CONFIRM = "leave_job_confirm"


# -----------------------------
# Database
# -----------------------------


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                passport_code TEXT PRIMARY KEY,
                tg_id INTEGER UNIQUE,
                first_name TEXT NOT NULL,
                middle_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                lang TEXT NOT NULL DEFAULT 'en',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS businesses (
                company_code TEXT PRIMARY KEY,
                owner_passport_code TEXT NOT NULL,
                name_native TEXT NOT NULL UNIQUE,
                name_latin TEXT NOT NULL UNIQUE,
                short_name TEXT NOT NULL UNIQUE,
                business_address TEXT NOT NULL,
                physical_addresses_json TEXT NOT NULL,
                budget_eur REAL NOT NULL,
                sector TEXT NOT NULL,
                staff_json TEXT NOT NULL,
                licenses_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                delete_requested_at TEXT,
                delete_finalize_at TEXT,
                transfer_pending_passport TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(owner_passport_code) REFERENCES users(passport_code)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_code TEXT NOT NULL,
                applicant_passport_code TEXT NOT NULL,
                initiator TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                interview_question TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS employment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_passport_code TEXT NOT NULL,
                company_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                UNIQUE(employee_passport_code, company_code)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transfer_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_code TEXT NOT NULL,
                from_owner_passport_code TEXT NOT NULL,
                to_passport_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


# -----------------------------
# Helpers
# -----------------------------


def get_user_by_tg(tg_id: int) -> Optional[sqlite3.Row]:
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
        return row


def get_user_by_passport(passport_code: str) -> Optional[sqlite3.Row]:
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE passport_code = ?", (passport_code,)).fetchone()
        return row


def bind_user_to_tg(passport_code: str, tg_id: int, first_name: str, middle_name: str, last_name: str, lang: str = "en") -> None:
    with connect_db() as conn:
        existing = conn.execute("SELECT passport_code FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
        if existing and existing[0] != passport_code:
            conn.execute("UPDATE users SET tg_id = NULL WHERE tg_id = ?", (tg_id,))

        conn.execute(
            """
            INSERT INTO users (passport_code, tg_id, first_name, middle_name, last_name, lang, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(passport_code) DO UPDATE SET
                tg_id=excluded.tg_id,
                first_name=excluded.first_name,
                middle_name=excluded.middle_name,
                last_name=excluded.last_name,
                lang=excluded.lang
            """,
            (passport_code, tg_id, first_name, middle_name, last_name, lang, now_iso()),
        )
        conn.commit()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def generate_company_code() -> str:
    with connect_db() as conn:
        while True:
            code = f"{random.randint(0, 9999999):07d}"
            exists = conn.execute("SELECT 1 FROM businesses WHERE company_code = ?", (code,)).fetchone()
            if not exists:
                return code


def count_active_owned_businesses(passport_code: str) -> int:
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM businesses
            WHERE owner_passport_code = ?
              AND status IN ('active', 'pending_delete')
            """,
            (passport_code,),
        ).fetchone()
        return int(row["c"])


def count_active_employment(passport_code: str) -> int:
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM employment
            WHERE employee_passport_code = ?
              AND status = 'active'
            """,
            (passport_code,),
        ).fetchone()
        return int(row["c"])


def count_total_active_roles(passport_code: str) -> int:
    return count_active_owned_businesses(passport_code) + count_active_employment(passport_code)


def get_business_by_code(company_code: str) -> Optional[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute("SELECT * FROM businesses WHERE company_code = ?", (company_code,)).fetchone()


def get_businesses_by_owner(passport_code: str) -> List[sqlite3.Row]:
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM businesses
            WHERE owner_passport_code = ?
            ORDER BY created_at DESC
            """,
            (passport_code,),
        ).fetchall()
        return list(rows)


def get_employments_for_user(passport_code: str) -> List[sqlite3.Row]:
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT e.*, b.name_latin, b.name_native, b.short_name, b.company_code
            FROM employment e
            JOIN businesses b ON b.company_code = e.company_code
            WHERE e.employee_passport_code = ? AND e.status = 'active'
            ORDER BY e.created_at DESC
            """,
            (passport_code,),
        ).fetchall()
        return list(rows)


def set_flow(context: ContextTypes.DEFAULT_TYPE, flow: Optional[str], payload: Optional[Dict[str, Any]] = None) -> None:
    context.user_data["flow"] = flow
    context.user_data["payload"] = payload or {}


def get_flow(context: ContextTypes.DEFAULT_TYPE) -> Tuple[Optional[str], Dict[str, Any]]:
    return context.user_data.get("flow"), context.user_data.get("payload", {})


def clear_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("flow", None)
    context.user_data.pop("payload", None)


def pretty_user(user: sqlite3.Row) -> str:
    return f"{user['last_name']} {user['first_name']} {user['middle_name']} ({user['passport_code']})"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Businesses", callback_data="menu_businesses")],
            [InlineKeyboardButton("Jobs", callback_data="menu_jobs")],
            [InlineKeyboardButton("Company", callback_data="menu_company")],
            [InlineKeyboardButton("Profile", callback_data="menu_profile")],
            [InlineKeyboardButton("Notifications / Help", callback_data="menu_help")],
        ]
    )


def back_keyboard(to: str = "menu_home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=to)]])


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("English", callback_data="lang_en")],
            [InlineKeyboardButton("Русский", callback_data="lang_ru")],
            [InlineKeyboardButton("Español", callback_data="lang_es")],
        ]
    )


def yes_no_keyboard(yes_cb: str, no_cb: str, extra_cb: Optional[Tuple[str, str]] = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Yes", callback_data=yes_cb), InlineKeyboardButton("No", callback_data=no_cb)]]
    if extra_cb:
        rows.append([InlineKeyboardButton(extra_cb[0], callback_data=extra_cb[1])])
    return InlineKeyboardMarkup(rows)


def business_actions_keyboard(company_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Add staff", callback_data=f"biz_add_staff:{company_code}"),
             InlineKeyboardButton("Invite staff", callback_data=f"biz_invite_staff:{company_code}")],
            [InlineKeyboardButton("Delete business", callback_data=f"biz_delete:{company_code}"),
             InlineKeyboardButton("Transfer company", callback_data=f"biz_transfer:{company_code}")],
            [InlineKeyboardButton("View staff", callback_data=f"biz_view_staff:{company_code}")],
            [InlineKeyboardButton("Back", callback_data="menu_businesses")],
        ]
    )


def job_actions_keyboard(company_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Apply here", callback_data=f"job_apply:{company_code}")],
            [InlineKeyboardButton("Leave job", callback_data=f"job_leave:{company_code}")],
            [InlineKeyboardButton("Back", callback_data="menu_jobs")],
        ]
    )


async def safe_send(bot, chat_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("Failed to send to %s: %s", chat_id, exc)


# -----------------------------
# Renderers
# -----------------------------


async def show_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    user = get_user_by_tg(update.effective_user.id)
    if user:
        await update.message.reply_text(
            f"Welcome back, <b>{user['first_name']}</b>.\n\nChoose a section:",
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "Welcome to <b>RMC — Registro Mercantil Central en Franzengueric</b>.\n\nChoose a language:",
            reply_markup=language_keyboard(),
            parse_mode=ParseMode.HTML,
        )


async def show_home(chat, context: ContextTypes.DEFAULT_TYPE, text: str = "Main menu") -> None:
    await chat.reply_text(text, reply_markup=main_menu_keyboard())


async def show_businesses(chat, user: sqlite3.Row) -> None:
    businesses = get_businesses_by_owner(user["passport_code"])
    if not businesses:
        text = "You do not have any businesses yet."
    else:
        lines = ["<b>Your businesses</b>:\n"]
        for b in businesses:
            lines.append(
                f"• <b>{b['name_latin']}</b> / {b['name_native']}\n"
                f"  Code: <code>{b['company_code']}</code> | Status: {b['status']}"
            )
        text = "\n".join(lines)
    buttons = [[InlineKeyboardButton("Add business", callback_data="biz_add")]]
    for b in businesses[:6]:
        buttons.append([InlineKeyboardButton(f"Open {b['short_name']}", callback_data=f"biz_open:{b['company_code']}")])
    buttons.append([InlineKeyboardButton("Back", callback_data="menu_home")])
    await chat.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def show_jobs(chat, user: sqlite3.Row) -> None:
    employments = get_employments_for_user(user["passport_code"])
    if not employments:
        text = "You are not employed anywhere yet."
    else:
        lines = ["<b>Your jobs</b>:\n"]
        for e in employments:
            lines.append(
                f"• <b>{e['name_latin']}</b> / {e['name_native']}\n"
                f"  Company code: <code>{e['company_code']}</code>"
            )
        text = "\n".join(lines)
    buttons = [[InlineKeyboardButton("Apply to company", callback_data="job_apply_menu")]]
    for e in employments[:6]:
        buttons.append([InlineKeyboardButton(f"Leave {e['short_name']}", callback_data=f"job_leave:{e['company_code']}")])
    buttons.append([InlineKeyboardButton("Back", callback_data="menu_home")])
    await chat.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def show_company_overview(chat, user: sqlite3.Row) -> None:
    businesses = get_businesses_by_owner(user["passport_code"])
    if not businesses:
        await chat.reply_text("No companies under your name yet.", reply_markup=back_keyboard())
        return
    lines = ["<b>Company management</b>:\n"]
    for b in businesses:
        lines.append(
            f"• <b>{b['name_latin']}</b> | code <code>{b['company_code']}</code> | {b['status']}"
        )
    buttons = [[InlineKeyboardButton(f"Open {b['short_name']}", callback_data=f"biz_open:{b['company_code']}")] for b in businesses[:8]]
    buttons.append([InlineKeyboardButton("Back", callback_data="menu_home")])
    await chat.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)


async def show_profile(chat, user: sqlite3.Row) -> None:
    owned = count_active_owned_businesses(user["passport_code"])
    jobs = count_active_employment(user["passport_code"])
    total = owned + jobs
    await chat.reply_text(
        (
            "<b>Your profile</b>\n\n"
            f"Name: <b>{user['last_name']} {user['first_name']} {user['middle_name']}</b>\n"
            f"Passport code: <code>{user['passport_code']}</code>\n"
            f"Owned businesses: <b>{owned}/3</b>\n"
            f"Other jobs: <b>{jobs}</b>\n"
            f"Total active roles: <b>{total}/5</b>"
        ),
        reply_markup=back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def show_help(chat, user: Optional[sqlite3.Row]) -> None:
    await chat.reply_text(
        (
            "<b>RMC help</b>\n\n"
            "This bot manages fictional businesses and companies for your Minecraft country.\n"
            "Useful sections: Businesses, Jobs, Company, Profile.\n"
            "Languages now: English only. Russian and Spanish are marked as coming soon."
        ),
        reply_markup=back_keyboard(),
        parse_mode=ParseMode.HTML,
    )


# -----------------------------
# Core commands and callbacks
# -----------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_start(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_flow(context)
    await update.message.reply_text("Cancelled.", reply_markup=main_menu_keyboard())


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    tg_id = query.from_user.id
    user = get_user_by_tg(tg_id)

    if data.startswith("lang_"):
        lang = data.split("_", 1)[1]
        if lang == "en":
            if user:
                with connect_db() as conn:
                    conn.execute("UPDATE users SET lang = 'en' WHERE tg_id = ?", (tg_id,))
                    conn.commit()
                await query.message.reply_text("English selected.", reply_markup=main_menu_keyboard())
            else:
                await query.message.reply_text(
                    "English selected.\n\nNow register or log in by sending your fictional passport code.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Register / Log in", callback_data="auth_begin")]]),
                )
            return
        await query.message.reply_text(LANG_UNDER_CONSTRUCTION.get(lang, "Coming soon."))
        return

    if data == "auth_begin":
        set_flow(context, FLOW_REGISTER, {"step": 1})
        await query.message.reply_text(
            "Enter your game first name.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="menu_home")]]),
        )
        return

    if data == "menu_home":
        if user:
            await query.message.reply_text("Main menu:", reply_markup=main_menu_keyboard())
        else:
            await query.message.reply_text(
                "Please select a language first, then register.",
                reply_markup=language_keyboard(),
            )
        return

    if data == "menu_businesses":
        if not user:
            await query.message.reply_text("Register first.")
            return
        await show_businesses(query.message, user)
        return

    if data == "menu_jobs":
        if not user:
            await query.message.reply_text("Register first.")
            return
        await show_jobs(query.message, user)
        return

    if data == "menu_company":
        if not user:
            await query.message.reply_text("Register first.")
            return
        await show_company_overview(query.message, user)
        return

    if data == "menu_profile":
        if not user:
            await query.message.reply_text("Register first.")
            return
        await show_profile(query.message, user)
        return

    if data == "menu_help":
        await show_help(query.message, user)
        return

    if data == "biz_add":
        if not user:
            await query.message.reply_text("Register first.")
            return
        if count_active_owned_businesses(user["passport_code"]) >= 3:
            await query.message.reply_text("You already own the maximum of 3 businesses.")
            return
        set_flow(context, FLOW_ADD_BUSINESS, {"step": 1})
        await query.message.reply_text("Enter the original business name.")
        return

    if data.startswith("biz_open:"):
        if not user:
            await query.message.reply_text("Register first.")
            return
        code = data.split(":", 1)[1]
  
