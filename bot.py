import asyncio
import gc
import os
import random
import re
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaPoll, MessageMediaPhoto, MessageMediaDocument
)
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
    rpcerrorlist,
)

from db import (
    add_channel, remove_channel, get_channels,
    get_user, set_user_field, set_user_fields, close_db,
    save_job, get_job, clear_job,
)

# ======================== ENV ============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID    = os.getenv("TELEGRAM_API_ID")
API_HASH  = os.getenv("TELEGRAM_API_HASH")

if not all([BOT_TOKEN, API_ID, API_HASH]):
    raise RuntimeError("Missing env vars: BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH")

API_ID = int(API_ID)

# ======================== PATHS ==========================
IMAGE_DIR = "quiz_images"
os.makedirs(IMAGE_DIR, exist_ok=True)

# ======================== DELAYS =========================
SEND_DELAY    = 3.0
VOTE_DELAY    = 2.5
AUTO_VOTE     = True
OUTPUT_JSON   = "quiz_output.json"
OUTPUT_TXT    = "quiz_output.txt"

# Self-ping to keep Render free tier alive
RENDER_URL    = os.getenv("RENDER_EXTERNAL_URL", "")
PING_INTERVAL = 20
PING_PORT     = int(os.getenv("PORT", "10000"))

# Rolling buffer — never more than this many message objects in RAM at once
BUFFER_SIZE   = 100
FETCH_AHEAD   = 100   # how many IDs to fetch per MTProto call


# ================== TELETHON SESSION HELPER ==============

async def get_client(user_id: str) -> Optional[TelegramClient]:
    user_doc = await get_user(user_id)
    session_str = user_doc.get("session_string", "")
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    return client


async def save_session(user_id: str, client: TelegramClient):
    session_str = client.session.save()
    await set_user_field(user_id, "session_string", session_str)


# ==================== CONVERSATION STATES =================
LOGIN_PHONE, LOGIN_OTP, LOGIN_2FA = range(3)
SCRAPE_START_LINK, SCRAPE_END_LINK, SCRAPE_DEST = range(3, 6)

# ======================== HELPERS ========================

def clean_text(text: str) -> str:
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group(0).replace('`', ''), text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_',       r'\1', text, flags=re.DOTALL)
    text = re.sub(r'~~(.+?)~~',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\|\|(.+?)\|\|', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'`(.+?)`',       r'\1', text, flags=re.DOTALL)
    return text


def parse_private_link(link: str) -> Optional[Tuple[int, int]]:
    link = link.strip()
    m = re.search(r"t\.me/c/(\d+)/\d+/(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"channel=(\d+)&post=(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    return None


def escape_md(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(special)}])", r"\\\1", str(text))


OPTION_LETTERS = ["A", "B", "C", "D", "E", "F"]


def build_bot_caption(quiz: dict, number: int) -> str:
    lines = [f"📋 *Quiz \\#{number}*", ""]
    lines.append(f"*Q: {escape_md(quiz['question'])}*")
    lines.append("")
    correct = quiz["correct_answer_index"]
    for ans in quiz["answers"]:
        letter = OPTION_LETTERS[ans["index"]] if ans["index"] < len(OPTION_LETTERS) else str(ans["index"] + 1)
        text = escape_md(ans["text"])
        if correct is not None and ans["index"] == correct:
            lines.append(f"✅ *{letter}\\. {text}*")
        else:
            lines.append(f"❌ {letter}\\. {text}")
    if quiz.get("explanation"):
        lines += ["", f"💡 _{escape_md(quiz['explanation'])}_"]
    if quiz.get("auto_voted"):
        lines += ["", "_\\(answer revealed via auto\\-vote\\)_"]
    return "\n".join(lines)


# =================== BOT COMMANDS ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first_name = user.first_name if user and user.first_name else "there"
    await update.message.reply_text(
        f"👋 *Welcome, {first_name}!*\n\n"
        "I'm your *Quiz Scraper Bot* — I scrape quizzes and polls from private Telegram channels "
        "and deliver them to any chat of your choice.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔐 *Account*\n"
        "• /login — connect your Telegram account\n"
        "• /logout — revoke your session\n"
        "• /status — check login status\n\n"
        "🚀 *Scraping*\n"
        "• /scrape — scrape quizzes from a message range\n"
        "• /set\\_destination — set where results are sent\n\n"
        "⚙️ *Other*\n"
        "• /cancel — cancel any ongoing operation\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👉 New here? Start with /login to connect your account.",
        parse_mode=ParseMode.MARKDOWN
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    context.user_data.clear()
    await _cleanup_login_state(user_id)

    scrape_tasks_by_user = context.application.bot_data.get("scrape_tasks_by_user", {})
    task = scrape_tasks_by_user.pop(user_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await update.message.reply_text("⛔ Scrape cancelled.")
    else:
        await update.message.reply_text("❌ Cancelled.")


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    if not user_doc.get("session_string"):
        await update.message.reply_text("❌ You are not logged in.")
        return

    try:
        client = await get_client(user_id)
        if await client.is_user_authorized():
            await client.log_out()
        else:
            await client.disconnect()
    except Exception:
        pass

    await set_user_field(user_id, "session_string", "")
    await _cleanup_login_state(user_id)
    await update.message.reply_text(
        "✅ Logged out successfully.\n"
        "Your session has been revoked from Telegram.\n\n"
        "Use /login to log in again."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    if user_doc.get("session_string"):
        try:
            client = await get_client(user_id)
            if await client.is_user_authorized():
                me = await client.get_me()
                await client.disconnect()
                await update.message.reply_text(
                    f"✅ Logged in as {me.first_name} (@{me.username})"
                )
                return
            await client.disconnect()
        except Exception:
            pass
    await update.message.reply_text("❌ Not logged in. Use /login to authenticate.")


# ---------------- LOGIN STATE (in-memory) ----------------
LOGIN_STATE = {}


async def _cleanup_login_state(user_id: str):
    state = LOGIN_STATE.pop(user_id, None)
    if state:
        client = state.get("data", {}).get("client")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


# ---------------- LOGIN CONVERSATION --------------------

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    user_doc = await get_user(user_id)
    if user_doc.get("session_string"):
        try:
            client = await get_client(user_id)
            if await client.is_user_authorized():
                await client.disconnect()
                await update.message.reply_text(
                    "✅ You are already logged in.\n\nUse /status to check or /cancel to abort."
                )
                return ConversationHandler.END
            await client.disconnect()
        except Exception:
            pass

    await _cleanup_login_state(user_id)
    LOGIN_STATE[user_id] = {"step": "WAITING_PHONE", "data": {}}

    await update.message.reply_text(
        "📱 *Login — Step 1 of 3*\n\n"
        "Send your phone number with country code.\n\n"
        "📎 Example: `+919876543210`\n\n"
        "Or /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN
    )
    return LOGIN_PHONE


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    phone = update.message.text.strip().replace(" ", "")

    if not re.match(r"^\+\d{7,15}$", phone):
        await update.message.reply_text("❌ Invalid format. Use +1234567890.")
        return LOGIN_PHONE

    await update.message.reply_text("📩 Sending OTP...")

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        LOGIN_STATE[user_id] = {
            "step": "WAITING_CODE",
            "data": {
                "client": client,
                "phone":  phone,
                "hash":   sent.phone_code_hash,
            }
        }
        await set_user_field(user_id, "phone_number", phone)
        await update.message.reply_text(
            "📲 *Login — Step 2 of 3*\n\n"
            "A verification code has been sent to your Telegram app.\n\n"
            "Send it here — spaces are fine:\n`1 2 3 4 5` or `12345`\n\n"
            "Or /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return LOGIN_OTP

    except PhoneNumberInvalidError:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text("❌ Phone number is invalid. Try again.")
        return LOGIN_PHONE
    except Exception as e:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text(f"❌ Failed to send OTP: {e}\n\nTry /login again.")
        return ConversationHandler.END


async def login_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    otp = update.message.text.strip().replace(" ", "")

    if not otp.isdigit():
        await update.message.reply_text("❌ Send only the numeric code.")
        return LOGIN_OTP

    state = LOGIN_STATE.get(user_id)
    if not state or state["step"] != "WAITING_CODE":
        await update.message.reply_text("❌ Session lost. Please /login again.")
        return ConversationHandler.END

    client       = state["data"]["client"]
    phone        = state["data"]["phone"]
    phone_hash   = state["data"]["hash"]

    try:
        await client.sign_in(phone, otp, phone_code_hash=phone_hash)
        await _finalize_login(update, client, user_id)
        return ConversationHandler.END

    except PhoneCodeInvalidError:
        await update.message.reply_text("❌ OTP is incorrect. Try again.")
        return LOGIN_OTP

    except PhoneCodeExpiredError:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text("❌ OTP expired. Please /login again.")
        return ConversationHandler.END

    except SessionPasswordNeededError:
        LOGIN_STATE[user_id]["step"] = "WAITING_PASSWORD"
        await update.message.reply_text(
            "🔐 *Login — Step 3 of 3*\n\n"
            "Two-step verification is enabled on your account.\n\n"
            "Please send your 2FA password.\n\n"
            "Or /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return LOGIN_2FA

    except Exception as e:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text(f"❌ Error: {e}\n\nPlease /login again.")
        return ConversationHandler.END


async def login_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = str(update.effective_user.id)
    password = update.message.text.strip()

    state = LOGIN_STATE.get(user_id)
    if not state or state["step"] != "WAITING_PASSWORD":
        await update.message.reply_text("❌ Session lost. Please /login again.")
        return ConversationHandler.END

    client = state["data"]["client"]

    try:
        await client.sign_in(password=password)
        await _finalize_login(update, client, user_id)
        return ConversationHandler.END

    except PasswordHashInvalidError:
        await update.message.reply_text("❌ Wrong password. Try again.")
        return LOGIN_2FA

    except Exception as e:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text(f"❌ 2FA error: {e}\n\nPlease /login again.")
        return ConversationHandler.END


async def _finalize_login(update: Update, client: TelegramClient, user_id: str):
    try:
        me = await client.get_me()
        await save_session(user_id, client)
        await client.disconnect()
        LOGIN_STATE.pop(user_id, None)
        await update.message.reply_text(
            f"✅ *Logged in successfully!*\n\n"
            f"👤 Name: {me.first_name}\n"
            f"🔖 Username: @{me.username}\n\n"
            "Your session has been saved. You're ready to /scrape!\n\n"
            "_If you ever get an auth error, use /logout then /login again._",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        LOGIN_STATE.pop(user_id, None)
        await update.message.reply_text(f"❌ Error finalizing login: {e}")


# ================================================================
# DESTINATION MANAGEMENT
# ================================================================

ADD_DEST_PICK  = 10
ADD_DEST_TYPED = 11

_DEST_PREFIX  = "dest:"
_DVIEW_PREFIX = "dview:"
_RDEST_PREFIX = "rdest:"
_DADD_PREFIX  = "dadd:"
_DADD_FETCH   = "dadd_fetch"


async def _get_destinations(user_id: str) -> list[dict]:
    channels = await get_channels(user_id)
    return [{"label": ch["title"], "chat_id": str(ch["channel_id"])} for ch in channels]


async def _add_destination(user_id: str, label: str, chat_id) -> list[dict]:
    await add_channel(user_id, int(chat_id), label)
    return await _get_destinations(user_id)


async def _remove_destination(user_id: str, chat_id) -> list[dict]:
    await remove_channel(user_id, int(chat_id))
    return await _get_destinations(user_id)


def _manage_keyboard(dests: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for d in dests:
        buttons.append([
            InlineKeyboardButton(
                f"📢 {d['label']}",
                callback_data=f"{_DVIEW_PREFIX}{d['chat_id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("➕ Add by Chat ID", callback_data=_DADD_FETCH)])
    return InlineKeyboardMarkup(buttons)


def _detail_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Remove", callback_data=f"{_RDEST_PREFIX}{chat_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="dback")],
    ])


def _pick_keyboard(dests: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for d in dests:
        buttons.append([
            InlineKeyboardButton(
                f"📢 {d['label']}",
                callback_data=f"{_DEST_PREFIX}{d['chat_id']}",
            )
        ])
    return InlineKeyboardMarkup(buttons)


async def set_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    dests   = await _get_destinations(user_id)
    text    = (
        "📬 *Destinations*\n\nTap a destination to view or remove it."
        if dests else
        "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
    )
    await update.message.reply_text(
        text,
        reply_markup=_manage_keyboard(dests),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADD_DEST_PICK


async def manage_dest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    data    = query.data

    if data.startswith(_DVIEW_PREFIX):
        chat_id = data[len(_DVIEW_PREFIX):]
        dests   = await _get_destinations(user_id)
        entry   = next((d for d in dests if str(d["chat_id"]) == chat_id), None)
        label   = entry["label"] if entry else chat_id
        await query.edit_message_text(
            f"📢 *{escape_md(label)}*\n`{chat_id}`",
            reply_markup=_detail_keyboard(chat_id),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ADD_DEST_PICK

    if data.startswith(_RDEST_PREFIX):
        chat_id = data[len(_RDEST_PREFIX):]
        dests   = await _remove_destination(user_id, chat_id)
        text    = (
            "📬 *Destinations*\n\nTap a destination to view or remove it."
            if dests else
            "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
        )
        await query.edit_message_text(
            text,
            reply_markup=_manage_keyboard(dests),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_DEST_PICK

    if data == "dback":
        dests = await _get_destinations(user_id)
        text  = (
            "📬 *Destinations*\n\nTap a destination to view or remove it."
            if dests else
            "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
        )
        await query.edit_message_text(
            text,
            reply_markup=_manage_keyboard(dests),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_DEST_PICK

    if data == _DADD_FETCH:
        await query.edit_message_text(
            "➕ *Add destination*\n\n"
            "You can add a destination in *two ways*:\n\n"
            "1️⃣ *Forward any message* from the channel/group to this chat — the bot will detect it automatically.\n\n"
            "2️⃣ *Type the ID or @username* manually:\n"
            "• `-1001234567890`\n"
            "• `@mychannelname`\n\n"
            "Or /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_DEST_TYPED

    if data.startswith(_DADD_PREFIX):
        payload   = data[len(_DADD_PREFIX):]
        colon     = payload.index(":")
        chat_id   = payload[:colon]
        label     = payload[colon + 1:].replace("｜", ":")
        dests     = await _add_destination(user_id, label, chat_id)
        text      = (
            "📬 *Destinations*\n\nTap a destination to view or remove it."
            if dests else
            "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
        )
        await query.edit_message_text(
            f"✅ *{escape_md(label)}* added\\!\n\n" + escape_md(text.split("\n\n", 1)[1]),
            reply_markup=_manage_keyboard(dests),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ADD_DEST_PICK


async def add_dest_typed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    msg     = update.message

    fwd = msg.forward_origin if hasattr(msg, "forward_origin") else None
    fwd_chat = None
    if fwd is not None:
        fwd_chat = getattr(fwd, "chat", None)
    if fwd_chat is None:
        fwd_chat = getattr(msg, "forward_from_chat", None)

    if fwd_chat is not None:
        raw_id    = fwd_chat.id
        chat_type = fwd_chat.type
        if chat_type in ("channel", "supergroup"):
            abs_str = str(abs(raw_id))
            chat_id_str = "-" + (abs_str if abs_str.startswith("100") else "100" + abs_str)
        else:
            chat_id_str = str(raw_id)
        title = (getattr(fwd_chat, "title", None) or getattr(fwd_chat, "username", None) or chat_id_str)[:40]

        dests = await _add_destination(user_id, title, chat_id_str)
        text = (
            "📬 *Destinations*\n\nTap a destination to view or remove it."
            if dests else
            "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
        )
        await msg.reply_text(
            f"✅ *{escape_md(title)}* added\\!\n`{chat_id_str}`\n\n" + escape_md(text.split("\n\n", 1)[1]),
            reply_markup=_manage_keyboard(dests),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ConversationHandler.END

    raw_text = (msg.text or "").strip()
    if not raw_text:
        await msg.reply_text(
            "❌ Please forward a message from the channel, or type its ID / @username.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_DEST_TYPED

    await msg.reply_text("🔍 Looking up the chat…")

    client = await get_client(user_id)
    if not await client.is_user_authorized():
        await client.disconnect()
        await update.message.reply_text(
            "❌ You're not logged in. Use /login first, then try /set\\_destination again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    entity = None
    chat_id_str = None
    title = None

    try:
        entity = await client.get_entity(raw_text)
    except Exception:
        pass

    if entity is None:
        numeric = raw_text.lstrip("-")
        if numeric.startswith("100") and len(numeric) > 10:
            bare_id = int(numeric[3:])
        else:
            bare_id = int(numeric) if numeric.isdigit() else None

        if bare_id is not None:
            from telethon.tl.types import InputPeerChannel
            try:
                peer = InputPeerChannel(channel_id=bare_id, access_hash=0)
                entity = await client.get_entity(peer)
            except Exception:
                pass

            if entity is None:
                try:
                    target_id = int(raw_text)
                    async for dialog in client.iter_dialogs():
                        if dialog.id == target_id:
                            entity = dialog.entity
                            break
                except Exception:
                    pass

    await client.disconnect()

    if entity is None:
        stripped = raw_text.lstrip("-")
        if stripped.isdigit():
            chat_id_str = raw_text
            title = raw_text[:40]
            await update.message.reply_text(
                f"⚠️ Could not auto-resolve the chat name (your account may not have joined it yet), "
                f"but the ID `{raw_text}` looks valid and has been saved.\n\n"
                "If sending fails later, make sure your account is a member of that chat.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"❌ Could not find that chat: `{raw_text}`\n\n"
                "Make sure the ID is correct and your account has access to that chat.\n\n"
                "Send another ID or /cancel to abort.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ADD_DEST_TYPED
    else:
        raw_id     = entity.id
        is_channel = getattr(entity, "broadcast", False)
        is_mega    = getattr(entity, "megagroup",  False)
        is_giga    = getattr(entity, "gigagroup",  False)
        if is_channel or is_mega or is_giga:
            chat_id_str = str(int("-100" + str(raw_id)))
        else:
            chat_id_str = str(raw_id)
        title = (getattr(entity, "title", None) or getattr(entity, "username", None) or raw_text)[:40]

    dests = await _add_destination(user_id, title, chat_id_str)
    text = (
        "📬 *Destinations*\n\nTap a destination to view or remove it."
        if dests else
        "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
    )
    await update.message.reply_text(
        f"✅ *{escape_md(title)}* added\\!\n\n" + escape_md(text.split("\n\n", 1)[1]),
        reply_markup=_manage_keyboard(dests),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END


# ---------- Scrape destination picker ----------

async def _show_scrape_dest_picker(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    user_id: str) -> bool:
    dests  = await _get_destinations(user_id)
    target = update.message

    bot_chat  = {"label": "🤖 This chat (bot)", "chat_id": str(update.effective_user.id)}
    full_list = [bot_chat] + list(dests)
    context.user_data["dest_list"] = full_list

    lines = ["📬 *Where should the results be sent?*", ""]
    for i, d in enumerate(full_list, 1):
        lines.append(f"*{i}.* {d['label']}  (`{d['chat_id']}`)")
    lines += ["", "Reply with the *number* of your choice, or /cancel to abort."]

    await target.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    return True


async def scrape_dest_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw   = update.message.text.strip()
    dests = context.user_data.get("dest_list", [])

    start_id   = context.user_data.get("start_id")
    end_id     = context.user_data.get("end_id")
    channel_id = context.user_data.get("channel_id")

    if start_id is None or end_id is None or channel_id is None:
        await update.message.reply_text(
            "❌ *Session lost* — the bot may have restarted.\n\nPlease run /scrape again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    if not raw.isdigit() or not (1 <= int(raw) <= len(dests)):
        await update.message.reply_text(
            f"❌ Please reply with a number between *1* and *{len(dests)}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return SCRAPE_DEST

    chosen = dests[int(raw) - 1]
    label  = chosen["label"]
    dest   = int(chosen["chat_id"])
    total  = end_id - start_id + 1

    await update.message.reply_text(
        f"✅ *Sending to:* {label}\n\n"
        "⏳ *Scrape started!*\n\n"
        f"📡 Channel: `{channel_id}`\n"
        f"📨 Range: `{start_id}` → `{end_id}` ({total} messages)\n\n"
        "I\'ll notify you here when it\'s done.",
        parse_mode=ParseMode.MARKDOWN,
    )
    user_id = context.user_data.get("scrape_user_id", str(update.effective_user.id))
    task = asyncio.create_task(run_scrape(
        context.bot, user_id, channel_id, start_id, end_id, dest
    ))
    scrape_tasks_by_user = context.application.bot_data.setdefault("scrape_tasks_by_user", {})
    scrape_tasks_by_user[user_id] = task
    task.add_done_callback(lambda t: scrape_tasks_by_user.pop(user_id, None))
    scrape_tasks = context.application.bot_data.setdefault("scrape_tasks", set())
    scrape_tasks.add(task)
    task.add_done_callback(lambda t: scrape_tasks.discard(t))
    return ConversationHandler.END


# ---------------- SCRAPE CONVERSATION --------------------

async def scrape_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    if not user_doc.get("session_string"):
        await update.message.reply_text(
            "❌ *Not logged in.*\n\nUse /login to connect your Telegram account first.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    try:
        client = await get_client(user_id)
        if not await client.is_user_authorized():
            await client.disconnect()
            await update.message.reply_text(
                "❌ *Session expired.*\n\nPlease /login again.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END
        await client.disconnect()
    except Exception:
        await update.message.reply_text(
            "❌ *Session error.*\n\nPlease /login again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    context.user_data["scrape_user_id"] = user_id
    await update.message.reply_text(
        "🚀 *Scrape*\n\n"
        "Paste the *start message link* (first quiz in the range).\n\n"
        "📎 Example:\n`https://t.me/c/1234567890/42`\n\n"
        "Or /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN
    )
    return SCRAPE_START_LINK


async def scrape_start_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    parsed = parse_private_link(link)
    if not parsed:
        await update.message.reply_text(
            "❌ *Couldn't read that link.*\n\n"
            "Make sure it looks like:\n`https://t.me/c/1234567890/42`\n\n"
            "Try again or /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCRAPE_START_LINK
    context.user_data["channel_id"] = parsed[0]
    context.user_data["start_id"]   = parsed[1]
    await update.message.reply_text(
        f"✅ Start set \\(ID: `{parsed[1]}`\\)\n\n"
        "Now paste the *end message link* \\(last quiz in the range\\)\\.\n\n"
        "📎 Example:\n`https://t.me/c/1234567890/99`\n\n"
        "Or /cancel to abort\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return SCRAPE_END_LINK


async def scrape_end_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "start_id" not in context.user_data:
        return await scrape_start_link(update, context)

    link = update.message.text.strip()
    parsed = parse_private_link(link)
    if not parsed:
        await update.message.reply_text(
            "❌ *Couldn't read that link.*\n\n"
            "Make sure it looks like:\n`https://t.me/c/1234567890/99`\n\nTry again or /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCRAPE_END_LINK

    channel_id = context.user_data["channel_id"]
    if parsed[0] != channel_id:
        await update.message.reply_text(
            "❌ *Wrong channel.*\n\nThe end link must be from the same channel. Try again or /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCRAPE_END_LINK

    end_id   = parsed[1]
    start_id = context.user_data["start_id"]
    if end_id < start_id:
        await update.message.reply_text(
            "❌ *End must come after start.*\n\nTry again or /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCRAPE_END_LINK

    context.user_data["end_id"] = end_id
    user_id = context.user_data["scrape_user_id"]
    await _show_scrape_dest_picker(update, context, user_id)
    return SCRAPE_DEST


# ==================== FORMAT HELPERS ===================

def format_quiz_text(quiz: dict, number: int) -> str:
    lines = [
        "="*60,
        f"Quiz #{number}  |  ID: {quiz['message_id']}  |  {quiz['date']}"
        + ("  [auto-voted]" if quiz.get("auto_voted") else ""),
        "="*60,
        f"Q: {quiz['question']}\n",
    ]
    for ans in quiz["answers"]:
        marker = ""
        if quiz["correct_answer_index"] is not None:
            marker = " ✅" if ans["index"] == quiz["correct_answer_index"] else " ❌"
        voters = f"  [{ans.get('voters','?')} votes]" if ans.get("voters") is not None else ""
        lines.append(f"  {ans['index']+1}. {ans['text']}{marker}{voters}")
    if quiz.get("explanation"):
        lines.append(f"\n💡 {quiz['explanation']}")
    if quiz.get("image_path"):
        lines.append(f"\n🖼️  Image: {quiz['image_path']}")
    if quiz.get("caption"):
        lines.append(f"\n📝 Caption: {quiz['caption']}")
    if quiz.get("image_caption"):
        lines.append(f"\n🖼️ Image caption: {quiz['image_caption']}")
    lines.append(
        f"\nType: {'Quiz' if quiz['is_quiz'] else 'Poll'} | "
        f"Total voters: {quiz['total_voters'] or 'N/A'}"
    )
    return "\n".join(lines)


# ==================== BACKGROUND SCRAPING =================

def _is_image_message(message) -> bool:
    if isinstance(message.media, MessageMediaPhoto):
        return True
    if isinstance(message.media, MessageMediaDocument):
        mime = getattr(getattr(message.media, "document", None), "mime_type", "") or ""
        return mime.startswith("image/")
    return False


async def _cleanup_image(image_path: Optional[str]):
    if image_path and os.path.exists(image_path):
        try:
            os.remove(image_path)
        except Exception as e:
            print(f"      ⚠️  Could not delete temp image {image_path}: {e}")


# -----------------------------------------------------------------------
# Rolling-buffer fetcher
# -----------------------------------------------------------------------
# The producer coroutine fetches FETCH_AHEAD IDs at a time and puts each
# resolved message onto an asyncio.Queue capped at BUFFER_SIZE.
# The consumer (run_scrape) pulls one message at a time — memory never
# exceeds BUFFER_SIZE objects regardless of the total range.
# -----------------------------------------------------------------------

async def _fetch_producer(
    client,
    entity,
    msg_ids: list,
    queue: asyncio.Queue,
    stop_event: asyncio.Event,
):
    """
    Fetch messages in FETCH_AHEAD-sized slices and push them onto `queue`
    in ascending ID order.  Puts None as a sentinel when done (or on cancel).
    """
    total = len(msg_ids)
    try:
        for slice_start in range(0, total, FETCH_AHEAD):
            if stop_event.is_set():
                break

            slice_ids = msg_ids[slice_start : slice_start + FETCH_AHEAD]
            print(f"  📦  Fetching IDs {slice_ids[0]}–{slice_ids[-1]} "
                  f"({len(slice_ids)} msgs, "
                  f"queue depth: {queue.qsize()}/{BUFFER_SIZE})…")

            try:
                fetched = await client.get_messages(entity, ids=slice_ids)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"  ⚠️  Fetch error for slice {slice_ids[0]}-{slice_ids[-1]}: {e}")
                fetched = []

            # Sort within the slice before queuing so consumer sees them in order
            resolved = sorted(
                (m for m in fetched if m is not None),
                key=lambda m: m.id,
            )
            del fetched

            for msg in resolved:
                if stop_event.is_set():
                    break
                # put() blocks if queue is full — this is the back-pressure
                # that keeps memory bounded: producer waits for consumer to
                # drain before fetching more.
                await queue.put(msg)

            del resolved
            gc.collect()

    except asyncio.CancelledError:
        pass
    finally:
        # Always send sentinel so consumer knows we're done
        await queue.put(None)


async def _flush_standalone_image(
    bot, dest_chat_id, prev_msg, sent_as_image_for_poll: set, client
):
    """
    If prev_msg is an image that was never claimed by a poll, send it now.
    Called whenever a non-poll message follows an image message.
    """
    if (
        prev_msg is not None
        and _is_image_message(prev_msg)
        and prev_msg.id not in sent_as_image_for_poll
    ):
        print(f"      🖼️  Flushing standalone image {prev_msg.id}")
        img_path = await download_image(client, prev_msg, prev_msg.id)
        if img_path:
            caption = (prev_msg.text or "").strip()
            try:
                with open(img_path, "rb") as f:
                    await bot.send_photo(
                        chat_id = dest_chat_id,
                        photo   = f,
                        caption = caption[:1024] if caption else None,
                    )
            except Exception as e:
                print(f"  ❌  Standalone image flush failed {prev_msg.id}: {e}")
            finally:
                await _cleanup_image(img_path)
            await asyncio.sleep(SEND_DELAY)


async def run_scrape(
    bot,
    user_id: str,
    channel_id: int,
    start_id: int,
    end_id: int,
    dest_chat_id,
):
    """
    Memory-safe rolling scrape.

    Architecture
    ────────────
    • Producer task fetches FETCH_AHEAD IDs at a time and pushes resolved
      message objects onto a bounded asyncio.Queue (max BUFFER_SIZE deep).
    • Consumer (this coroutine) pulls one message at a time and processes it
      immediately.
    • Back-pressure: queue.put() in the producer blocks whenever the consumer
      falls behind, so at most BUFFER_SIZE + FETCH_AHEAD message objects
      ever exist simultaneously — typically ~200 objects ≈ 3 MB peak.
    • prev_msg holds exactly one message for image→poll lookahead; it is
      replaced on every iteration, so only 1 extra object exists at a time.
    """
    await bot.send_message(
        chat_id=dest_chat_id,
        text="⏳ Scraping started… quizzes will appear here as they are processed."
    )

    client     = None
    stop_event = asyncio.Event()

    total_fetched  = 0
    quiz_counter   = 0
    text_counter   = 0
    auto_voted_n   = 0
    already_done_n = 0

    try:
        client = await get_client(user_id)
        if not await client.is_user_authorized():
            await bot.send_message(chat_id=dest_chat_id, text="❌ Session expired. /login again.")
            await client.disconnect()
            return

        try:
            entity = await client.get_entity(channel_id)
        except Exception as e:
            await bot.send_message(
                chat_id=dest_chat_id,
                text=f"❌ Could not access channel: {e}\nMake sure you are a member."
            )
            await client.disconnect()
            return

        title   = getattr(entity, "title", str(channel_id))
        msg_ids = list(range(start_id, end_id + 1))
        total   = len(msg_ids)

        print(f"\n{'─'*58}")
        print(f"  Channel    : {title}")
        print(f"  Msg range  : {start_id} → {end_id} ({total} messages)")
        print(f"  Buffer     : {BUFFER_SIZE} slots  |  Fetch ahead: {FETCH_AHEAD}")
        print(f"  Auto-vote  : {'ON ⚡' if AUTO_VOTE else 'OFF'}")
        print(f"  Dest chat  : {dest_chat_id}")
        print(f"{'─'*58}\n")

        try:
            await bot.send_message(
                chat_id    = dest_chat_id,
                text       = (
                    f"📚 *Quiz Export — {escape_md(title)}*\n"
                    f"Range: `{start_id}` → `{end_id}` \\({total} messages\\)"
                ),
                parse_mode = ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            print(f"  ⚠️  Header send failed: {e}")

        # Bounded queue — producer blocks when full; consumer drains it
        queue: asyncio.Queue = asyncio.Queue(maxsize=BUFFER_SIZE)

        # Start producer in the background
        producer_task = asyncio.create_task(
            _fetch_producer(client, entity, msg_ids, queue, stop_event)
        )

        sent_as_image_for_poll: set = set()
        prev_msg = None   # single-object lookahead for image→poll detection

        # ── Consumer loop ──────────────────────────────────────────────
        while True:
            message = await queue.get()

            # Sentinel from producer — we're done
            if message is None:
                break

            total_fetched += 1

            # ── Plain text ─────────────────────────────────────────────
            if not message.media and message.text and message.text.strip():
                # Flush any dangling standalone image before this text msg
                await _flush_standalone_image(
                    bot, dest_chat_id, prev_msg, sent_as_image_for_poll, client
                )
                text_counter += 1
                text = clean_text(message.text.strip())
                print(f"  📝  Text #{message.id}: \"{text[:60]}\"")
                for chunk in [text[j:j+4000] for j in range(0, len(text), 4000)]:
                    try:
                        await bot.send_message(chat_id=dest_chat_id, text=chunk)
                    except Exception as e:
                        print(f"  ❌  Text #{message.id} failed: {e}")
                    await asyncio.sleep(SEND_DELAY)
                prev_msg = message
                continue

            # ── Poll / quiz ────────────────────────────────────────────
            if isinstance(message.media, MessageMediaPoll):
                poll_caption = message.text or ""
                poll_data    = parse_poll(message, caption=poll_caption)
                if poll_data is None:
                    prev_msg = message
                    continue

                if is_closed(message.media):
                    kind = "quiz" if poll_data["is_quiz"] else "poll"
                    print(f"  🔒  Closed {kind}: \"{poll_data['question'][:50]}\"")
                    poll_data = read_closed_results(message, poll_data)
                    already_done_n += 1
                elif is_unattempted(message.media):
                    if AUTO_VOTE:
                        poll_data = await auto_vote_and_reveal(
                            client, entity, message, poll_data
                        )
                        if poll_data["auto_voted"]:
                            auto_voted_n += 1
                    else:
                        print(f"  ➖  Skipped unattempted (auto-vote OFF)")
                else:
                    already_done_n += 1
                    print(f"  ✔  Already answered: \"{poll_data['question'][:52]}\"")

                # Image→poll lookahead: prev_msg is the immediately preceding
                # message — if it's an image, send it first and link the poll.
                reply_to_id   = None
                image_caption = ""

                if prev_msg is not None and _is_image_message(prev_msg):
                    print(f"      🖼️  prev msg {prev_msg.id} is image — sending before poll")
                    image_path    = await download_image(client, prev_msg, prev_msg.id)
                    image_caption = (prev_msg.text or "").strip()
                    if image_path:
                        try:
                            with open(image_path, "rb") as f:
                                sent_photo = await bot.send_photo(
                                    chat_id = dest_chat_id,
                                    photo   = f,
                                    caption = image_caption[:1024] if image_caption else None,
                                )
                            reply_to_id = sent_photo.message_id
                            sent_as_image_for_poll.add(prev_msg.id)
                            await asyncio.sleep(SEND_DELAY)
                        except Exception as e:
                            print(f"  ❌  Image send failed for {prev_msg.id}: {e}")
                        finally:
                            await _cleanup_image(image_path)

                poll_data["image_path"]    = None
                poll_data["image_caption"] = image_caption

                quiz_counter += 1
                try:
                    await recreate_quiz_poll(
                        bot, poll_data, dest_chat_id, quiz_counter,
                        reply_to_id=reply_to_id
                    )
                except Exception as e:
                    print(f"  ⚠️  Failed to send quiz #{quiz_counter}: {e}")
                    try:
                        plain = (
                            f"Quiz #{quiz_counter}\nQ: {poll_data['question']}\n"
                            + "\n".join(
                                f"  {ans['index']+1}. {ans['text']}"
                                + (" ✅" if ans["index"] == poll_data.get("correct_answer_index") else " ❌")
                                for ans in poll_data["answers"]
                            )
                        )
                        if poll_data.get("explanation"):
                            plain += f"\n\n💡 {poll_data['explanation']}"
                        await bot.send_message(chat_id=dest_chat_id, text=plain)
                    except Exception as e2:
                        print(f"  ❌  Fallback also failed #{quiz_counter}: {e2}")

                await asyncio.sleep(SEND_DELAY)
                prev_msg = message
                continue

            # ── Image / document ───────────────────────────────────────
            if isinstance(message.media, (MessageMediaPhoto, MessageMediaDocument)):
                # Already sent as part of an earlier poll's image — skip
                if message.id in sent_as_image_for_poll:
                    print(f"      ↩️  Msg {message.id} already sent as poll image — skipping")
                    prev_msg = message
                    continue

                if not _is_image_message(message):
                    # Non-image document: flush any pending image first
                    await _flush_standalone_image(
                        bot, dest_chat_id, prev_msg, sent_as_image_for_poll, client
                    )
                    doc  = getattr(message.media, "document", None)
                    mime = getattr(doc, "mime_type", "") if doc else ""
                    caption = (message.text or "").strip()
                    label   = "📄 PDF" if "pdf" in mime else "📎 Document"
                    note    = f"{label} (msg #{message.id})"
                    if caption:
                        note += f"\n{caption}"
                    try:
                        await bot.send_message(chat_id=dest_chat_id, text=note)
                    except Exception as e:
                        print(f"  ❌  Document notice failed: {e}")
                    await asyncio.sleep(SEND_DELAY)
                    prev_msg = message
                    continue

                # It's an image — store in prev_msg and wait.
                # If the very next message is a poll, the poll handler above
                # will pick it up via prev_msg.
                # If the next message is NOT a poll, _flush_standalone_image
                # will send it at the top of that handler.
                # Either way we never lose it.
                print(f"      🖼️  Img #{message.id} — holding for next msg")
                prev_msg = message
                continue

            # ── Anything else ──────────────────────────────────────────
            prev_msg = message

        # ── Consumer loop ended — wait for producer to finish cleanly ──
        await producer_task

        # Final flush: if the very last message in the range was a
        # standalone image that was never followed by a poll
        await _flush_standalone_image(
            bot, dest_chat_id, prev_msg, sent_as_image_for_poll, client
        )

        await client.disconnect()
        client = None

        print(f"\n{'═'*58}")
        print(f"  📨  Fetched  : {total_fetched}")
        print(f"  🧩  Quizzes  : {quiz_counter}")
        print(f"  📝  Texts    : {text_counter}")
        print(f"  🗳️  AutoVote : {auto_voted_n}")
        print(f"  ✅  Already  : {already_done_n}")
        print(f"{'═'*58}\n")

        if quiz_counter == 0 and text_counter == 0:
            await bot.send_message(
                chat_id=dest_chat_id,
                text="⚠️ Nothing found in this message range."
            )

        done_text = (
            "✅ *Scrape complete\\!*\n\n"
            f"📡 Channel: `{escape_md(title)}`\n"
            f"📨 Messages fetched: `{total_fetched}`\n"
            f"🧩 Quizzes sent: `{quiz_counter}`\n"
            f"📝 Text messages: `{text_counter}`\n"
            f"🗳️ Auto\\-voted: `{auto_voted_n}`\n\n"
            f"📬 Sent to: `{escape_md(str(dest_chat_id))}`"
        )
        await bot.send_message(
            chat_id    = user_id,
            text       = done_text,
            parse_mode = ParseMode.MARKDOWN_V2,
        )
        if str(dest_chat_id) != str(user_id):
            await bot.send_message(
                chat_id    = dest_chat_id,
                text       = (
                    f"✅ *Done\\! {quiz_counter} quiz\\(es\\) and "
                    f"{text_counter} text message\\(s\\) delivered\\.*"
                ),
                parse_mode = ParseMode.MARKDOWN_V2,
            )

    except asyncio.CancelledError:
        print("  ⚠️  run_scrape cancelled")
        stop_event.set()
        try:
            await bot.send_message(
                chat_id    = user_id,
                text       = (
                    f"⛔ *Scrape stopped\\.*\n\n"
                    f"🧩 Quizzes sent so far: `{quiz_counter}`\n"
                    f"📨 Messages fetched: `{total_fetched}`"
                ),
                parse_mode = ParseMode.MARKDOWN_V2,
            )
        except Exception:
            pass
        raise

    except Exception as e:
        print(f"  ❌  run_scrape error: {e}")
        stop_event.set()
        try:
            await bot.send_message(chat_id=dest_chat_id, text=f"❌ Scrape error: {e}")
        except Exception:
            pass
        try:
            await bot.send_message(chat_id=user_id, text=f"❌ Scrape failed: {e}")
        except Exception:
            pass

    finally:
        stop_event.set()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


# ================== POLL HELPERS =================

def is_unattempted(media: MessageMediaPoll) -> bool:
    results = media.results
    if results is None:
        return True
    if results.results:
        return not any(getattr(r, "chosen", False) for r in results.results)
    return True


def is_closed(media: MessageMediaPoll) -> bool:
    return getattr(media.poll, "closed", False)


def read_closed_results(message, poll_data: dict) -> dict:
    media   = message.media
    poll    = media.poll
    results = media.results

    answers = []
    for i, answer in enumerate(poll.answers):
        text  = answer.text.text if hasattr(answer.text, "text") else str(answer.text)
        entry = {"index": i, "text": text, "option": answer.option}
        if results and results.results:
            for res in results.results:
                if res.option == answer.option:
                    entry["voters"] = res.voters
                    entry["chosen"] = getattr(res, "chosen", False)
                    break
        answers.append(entry)

    is_quiz = poll_data.get("is_quiz", False)
    if is_quiz:
        correct = get_correct_index(poll, results)
        label   = f"option {correct + 1}" if correct is not None else "unknown"
        print(f"      ✅  Closed quiz — correct: {label}")
    else:
        correct = get_max_votes_index(poll, results)
        if correct is not None:
            winning_votes = answers[correct].get("voters", "?")
            label = f"option {correct + 1} ({winning_votes} votes)"
        else:
            label = "no votes"
        print(f"      📊  Closed poll — top answer: {label}")

    poll_data["answers"]              = answers
    poll_data["correct_answer_index"] = correct
    poll_data["total_voters"]         = results.total_voters if results else None
    poll_data["explanation"]          = (
        results.solution if results and getattr(results, "solution", None) else None
    )
    poll_data["auto_voted"] = False
    poll_data["was_closed"] = True
    return poll_data


def get_correct_index(poll, results) -> Optional[int]:
    if results and results.results:
        for res in results.results:
            if getattr(res, "correct", False):
                for i, ans in enumerate(poll.answers):
                    if ans.option == res.option:
                        return i
    return None


def get_max_votes_index(poll, results) -> Optional[int]:
    if not results or not results.results:
        return None
    best_i      = None
    best_voters = -1
    for res in results.results:
        v = res.voters or 0
        if v > best_voters:
            best_voters = v
            for i, ans in enumerate(poll.answers):
                if ans.option == res.option:
                    best_i = i
                    break
    return best_i


def parse_poll(message, caption: str = "") -> Optional[dict]:
    media = message.media
    if not isinstance(media, MessageMediaPoll):
        return None

    poll    = media.poll
    results = media.results

    question_text = (
        poll.question.text if hasattr(poll.question, "text") else str(poll.question)
    )

    answers = []
    for i, answer in enumerate(poll.answers):
        answer_text = (
            answer.text.text if hasattr(answer.text, "text") else str(answer.text)
        )
        entry = {"index": i, "text": answer_text, "option": answer.option}
        if results and results.results:
            for res in results.results:
                if res.option == answer.option:
                    entry["voters"] = res.voters
                    entry["chosen"] = getattr(res, "chosen", False)
                    break
        answers.append(entry)

    return {
        "message_id":           message.id,
        "date":                 message.date.isoformat(),
        "question":             question_text,
        "is_quiz":              poll.quiz,
        "anonymous":            not getattr(poll, "public_voters", False),
        "multiple_choice":      getattr(poll, "multiple_choice", False),
        "total_voters":         results.total_voters if results else None,
        "answers":              answers,
        "correct_answer_index": get_correct_index(poll, results),
        "explanation": (
            results.solution
            if results and getattr(results, "solution", None) else None
        ),
        "image_path":    None,
        "auto_voted":    False,
        "caption":       caption,
        "image_caption": "",
    }


async def auto_vote_and_reveal(client, entity, message, poll_data: dict) -> dict:
    dummy     = [random.choice(message.media.poll.answers).option]
    q_preview = poll_data['question'][:50]
    is_quiz   = poll_data.get("is_quiz", False)
    kind_lbl  = "quiz" if is_quiz else "poll"
    print(f"      🗳️  Voting ({kind_lbl}): \"{q_preview}\"")

    try:
        await client(functions.messages.SendVoteRequest(
            peer=entity, msg_id=message.id, options=dummy
        ))
    except rpcerrorlist.MessagePollClosedError:
        print("      ⚠️  Poll closed — cannot vote.")
        return poll_data
    except Exception as e:
        print(f"      ⚠️  Vote error: {e}")
        return poll_data

    await asyncio.sleep(random.uniform(2.0, 5.0))

    try:
        refreshed = await client.get_messages(entity, ids=message.id)
    except Exception as e:
        print(f"      ⚠️  Re-fetch failed: {e}")
        return poll_data

    if not refreshed or not isinstance(refreshed.media, MessageMediaPoll):
        return poll_data

    up   = refreshed.media.poll
    ures = refreshed.media.results

    updated_answers = []
    for i, answer in enumerate(up.answers):
        text  = answer.text.text if hasattr(answer.text, "text") else str(answer.text)
        entry = {"index": i, "text": text, "option": answer.option}
        if ures and ures.results:
            for res in ures.results:
                if res.option == answer.option:
                    entry["voters"] = res.voters
                    entry["chosen"] = getattr(res, "chosen", False)
                    break
        updated_answers.append(entry)

    if is_quiz:
        correct = get_correct_index(up, ures)
        label   = f"option {correct + 1}" if correct is not None else "still hidden"
        print(f"      ✅  Correct answer: {label}")
    else:
        correct = get_max_votes_index(up, ures)
        if correct is not None:
            winning_votes = updated_answers[correct].get("voters", "?")
            label = f"option {correct + 1} ({winning_votes} votes)"
        else:
            label = "no votes yet"
        print(f"      📊  Top answer: {label}")

    poll_data["answers"]              = updated_answers
    poll_data["correct_answer_index"] = correct
    poll_data["total_voters"]         = ures.total_voters if ures else None
    poll_data["auto_voted"]           = True
    poll_data["explanation"]          = (
        ures.solution if ures and getattr(ures, "solution", None) else None
    )
    return poll_data


async def download_image(client, message, msg_id: int) -> Optional[str]:
    Path(IMAGE_DIR).mkdir(exist_ok=True)
    media = message.media

    if isinstance(media, MessageMediaPhoto):
        path = os.path.join(IMAGE_DIR, f"quiz_{msg_id}.jpg")
    elif isinstance(media, MessageMediaDocument):
        doc  = media.document
        ext  = ".jpg"
        mime = getattr(doc, "mime_type", "")
        for attr in doc.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                ext = Path(attr.file_name).suffix or ext
                break
        if "png"  in mime: ext = ".png"
        if "gif"  in mime: ext = ".gif"
        if "webp" in mime: ext = ".webp"
        path = os.path.join(IMAGE_DIR, f"quiz_{msg_id}{ext}")
    else:
        return None

    try:
        await client.download_media(message, file=path)
        return path
    except Exception as e:
        print(f"      ⚠️  Image download failed: {e}")
        return None


async def recreate_quiz_poll(bot, quiz: dict, chat_id, number: int,
                              reply_to_id: Optional[int] = None):
    correct       = quiz.get("correct_answer_index")
    answers       = quiz.get("answers", [])
    image_caption = quiz.get("image_caption", "")

    if correct is None or not answers:
        print(f"  ⚠️  Quiz #{number} — correct answer unknown, sending as text")
        caption = build_bot_caption(quiz, number)
        img = quiz.get("image_path")
        try:
            if img and os.path.exists(img):
                with open(img, "rb") as f:
                    if image_caption:
                        await bot.send_photo(chat_id=chat_id, photo=f,
                                             caption=image_caption[:1024])
                    else:
                        await bot.send_photo(chat_id=chat_id, photo=f,
                                             caption=caption,
                                             parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await bot.send_message(chat_id=chat_id, text=caption,
                                       parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            print(f"  ❌  Text fallback failed for #{number}: {e}")
        return

    question     = quiz["question"][:300]
    option_texts = [ans["text"][:100] for ans in answers]
    explanation  = (quiz.get("explanation") or "")[:200]
    is_quiz_type = quiz.get("is_quiz", True)

    try:
        if is_quiz_type:
            await bot.send_poll(
                chat_id             = chat_id,
                question            = question,
                options             = option_texts,
                type                = "quiz",
                correct_option_ids  = [correct],
                explanation         = explanation or None,
                is_anonymous        = True,
                open_period         = None,
                reply_to_message_id = reply_to_id,
            )
            print(f"  🗳️  Recreated quiz #{number}: \"{question[:50]}\"")
        else:
            await bot.send_poll(
                chat_id             = chat_id,
                question            = question,
                options             = option_texts,
                type                = "regular",
                is_anonymous        = True,
                open_period         = None,
                reply_to_message_id = reply_to_id,
            )
            print(f"  📊  Recreated poll #{number}: \"{question[:50]}\"")
            if explanation:
                await asyncio.sleep(SEND_DELAY)
                winning  = option_texts[correct] if correct is not None else "N/A"
                exp_text = f"🎯 Top answer: {winning}\n\n💡 {explanation}"
                await bot.send_message(chat_id=chat_id, text=exp_text)
                print(f"      💡  Sent poll explanation for #{number}")
    except Exception as e:
        print(f"  ⚠️  Poll API error for #{number}: {e} — falling back to text")
        try:
            caption = build_bot_caption(quiz, number)
            await bot.send_message(chat_id=chat_id, text=caption,
                                   parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e2:
            print(f"  ❌  Text fallback also failed: {e2}")


# ===================== SELF-PING =========================

async def health_handler(request):
    return web.Response(text="OK")


async def start_ping_server():
    app_web = web.Application()
    app_web.router.add_get("/", health_handler)
    app_web.router.add_get("/health", health_handler)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PING_PORT)
    await site.start()
    print(f"🌐 Health server running on port {PING_PORT}")


async def self_ping_loop():
    if not RENDER_URL:
        print("⚠️  RENDER_EXTERNAL_URL not set — self-ping disabled.")
        return
    url = RENDER_URL.rstrip("/") + "/health"
    print(f"🔁 Self-ping enabled → {url} every {PING_INTERVAL}s")
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    print(f"  🏓 Ping → {resp.status}")
            except Exception as e:
                print(f"  ⚠️  Ping failed: {e}")


# ======================== MAIN ============================

def main():
    import httpx

    try:
        httpx.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
            params={"drop_pending_updates": True},
            timeout=10,
        )
        print("✅ Webhook cleared.")
    except Exception as e:
        print(f"⚠️  Could not clear webhook: {e}")

    async def post_init(application):
        await start_ping_server()
        application.bot_data["ping_task"] = asyncio.create_task(self_ping_loop())

    async def post_shutdown(application):
        ping_task = application.bot_data.get("ping_task")
        if ping_task and not ping_task.done():
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

        scrape_tasks = application.bot_data.get("scrape_tasks", set())
        if scrape_tasks:
            print(f"⏳ Cancelling {len(scrape_tasks)} in-flight scrape task(s)…")
            for t in list(scrape_tasks):
                t.cancel()
            await asyncio.gather(*scrape_tasks, return_exceptions=True)
            print("✅ Scrape tasks cancelled.")

        try:
            await close_db()
            print("✅ DB closed cleanly.")
        except Exception as e:
            print(f"⚠️  DB close error: {e}")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Login
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            LOGIN_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            LOGIN_OTP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp)],
            LOGIN_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, login_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Manage saved destinations
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("set_destination", set_destination)],
        states={
            ADD_DEST_PICK: [
                CallbackQueryHandler(manage_dest_callback),
            ],
            ADD_DEST_TYPED: [
                MessageHandler(
                    (filters.TEXT & ~filters.COMMAND) | filters.FORWARDED,
                    add_dest_typed,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Scrape
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("scrape", scrape_start)],
        states={
            SCRAPE_START_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_start_link)],
            SCRAPE_END_LINK:   [MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_end_link)],
            SCRAPE_DEST:       [MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_dest_number)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))

    print("Bot is running...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
