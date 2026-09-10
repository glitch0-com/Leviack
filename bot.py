# -*- coding: utf-8 -*-
"""
ربات مبصر گروه (Group Monitor / Admin Bot)
---------------------------------------------
امکانات:
  - اخطار دادن به کاربر (/warn) و رسیدن به سقف اخطار => میوت خودکار
  - حذف اخطار (/unwarn) و نمایش تعداد اخطارها (/warnings)
  - سکوت موقت / نامحدود کاربر (/mute [دقیقه]) و رفع سکوت (/unmute)
  - بن کردن (/ban) و رفع بن با آیدی عددی (/unban <user_id>)
  - اخراج بدون بن دائم؛ امکان برگشت به گروه (/kick)
  - نمایش اطلاعات ثبت‌شده از عضو (/info) شامل تاریخ اولین دیده‌شدن، تعداد پیام، تعداد اخطار
  - ثبت خودکار مشخصات و تعداد پیام هر عضو در پایگاه‌داده SQLite

نکته: تمام دستورات مدیریتی فقط برای ادمین‌های گروه فعال هستند و ربات هم باید ادمین گروه باشد.
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta

from telegram import Update, ChatPermissions
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAX_WARNINGS = int(os.environ.get("MAX_WARNINGS", "3"))
DEFAULT_MUTE_MINUTES = int(os.environ.get("DEFAULT_MUTE_MINUTES", "60"))
DB_PATH = os.environ.get("DB_PATH", "mobasser.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mobasser_bot")

# ---------------------------------------------------------------------------
# پایگاه‌داده
# ---------------------------------------------------------------------------


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            first_seen TEXT,
            message_count INTEGER DEFAULT 0,
            warnings INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )
    conn.commit()
    conn.close()


def upsert_member(chat_id: int, user_id: int, username: str, full_name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            """
            INSERT INTO members (chat_id, user_id, username, full_name, first_seen, message_count, warnings)
            VALUES (?, ?, ?, ?, ?, 0, 0)
            """,
            (chat_id, user_id, username, full_name, datetime.utcnow().isoformat()),
        )
    else:
        cur.execute(
            "UPDATE members SET username=?, full_name=? WHERE chat_id=? AND user_id=?",
            (username, full_name, chat_id, user_id),
        )
    conn.commit()
    conn.close()


def increment_message_count(chat_id: int, user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE members SET message_count = message_count + 1 WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    conn.commit()
    conn.close()


def get_member_row(chat_id: int, user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    )
    row = cur.fetchone()
    conn.close()
    return row


def add_warning(chat_id: int, user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE members SET warnings = warnings + 1 WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    conn.commit()
    cur.execute(
        "SELECT warnings FROM members WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    warnings = cur.fetchone()["warnings"]
    conn.close()
    return warnings


def reset_warnings(chat_id: int, user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE members SET warnings = 0 WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    conn.commit()
    conn.close()


def remove_one_warning(chat_id: int, user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE members SET warnings = MAX(warnings - 1, 0) WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    conn.commit()
    cur.execute(
        "SELECT warnings FROM members WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    warnings = cur.fetchone()["warnings"]
    conn.close()
    return warnings


# ---------------------------------------------------------------------------
# توابع کمکی
# ---------------------------------------------------------------------------


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """بررسی می‌کند آیا فرستنده پیام، ادمین گروه است یا نه."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return False
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def get_target_user(update: Update):
    """کاربر هدف را از روی ریپلای پیام برمی‌گرداند."""
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    return None


def parse_minutes(args, default=DEFAULT_MUTE_MINUTES) -> int:
    if args:
        try:
            return max(1, int(args[0]))
        except ValueError:
            pass
    return default


ADMIN_ONLY_MSG = "⛔ این دستور فقط برای ادمین‌های گروه مجاز است."
REPLY_NEEDED_MSG = "❗️ لطفاً روی پیام کاربر مورد نظر ریپلای کن و دوباره دستور رو بفرست."

# ---------------------------------------------------------------------------
# دستورات عمومی
# ---------------------------------------------------------------------------


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "سلام! من ربات مبصر گروه هستم 🎓\n"
        "برای دیدن دستورات از /help استفاده کن."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 دستورات ربات مبصر:\n\n"
        "🔹 /warn — (ریپلای روی پیام کاربر) دادن اخطار\n"
        "🔹 /unwarn — (ریپلای) کم کردن یک اخطار\n"
        "🔹 /warnings — (ریپلای) دیدن تعداد اخطارهای کاربر\n"
        "🔹 /mute [دقیقه] — (ریپلای) سکوت موقت کاربر (پیش‌فرض 60 دقیقه)\n"
        "🔹 /unmute — (ریپلای) رفع سکوت\n"
        "🔹 /ban — (ریپلای) بن کردن دائم\n"
        "🔹 /unban <user_id> — رفع بن با آیدی عددی\n"
        "🔹 /kick — (ریپلای) اخراج از گروه (امکان بازگشت وجود دارد)\n"
        "🔹 /info — (ریپلای) دیدن اطلاعات ثبت‌شده کاربر\n\n"
        f"⚠️ بعد از {MAX_WARNINGS} اخطار، کاربر به‌صورت خودکار میوت می‌شود."
    )
    await update.effective_message.reply_text(text)


async def track_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هر پیام عادی گروه را برای ثبت آمار پردازش می‌کند."""
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None or msg is None:
        return
    if user.is_bot:
        return
    full_name = user.full_name or ""
    username = f"@{user.username}" if user.username else "-"
    upsert_member(chat.id, user.id, username, full_name)
    increment_message_count(chat.id, user.id)


# ---------------------------------------------------------------------------
# دستورات مدیریتی
# ---------------------------------------------------------------------------


async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.effective_message.reply_text(ADMIN_ONLY_MSG)
        return
    target = get_target_user(update)
    if target is None:
        await update.effective_message.reply_text(REPLY_NEEDED_MSG)
        return
    chat_id = update.effective_chat.id
    upsert_member(chat_id, target.id, f"@{target.username}" if target.username else "-", target.full_name)
    reason = " ".join(context.args) if context.args else "بدون دلیل ذکرشده"
    warnings = add_warning(chat_id, target.id)

    await update.effective_message.reply_text(
        f"⚠️ اخطار به {target.mention_html()} ثبت شد.\n"
        f"دلیل: {reason}\n"
        f"تعداد اخطارها: {warnings}/{MAX_WARNINGS}",
        parse_mode="HTML",
    )

    if warnings >= MAX_WARNINGS:
        until = datetime.utcnow() + timedelta(minutes=DEFAULT_MUTE_MINUTES)
        try:
            await context.bot.restrict_chat_member(
                chat_id,
                target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            reset_warnings(chat_id, target.id)
            await update.effective_message.reply_text(
                f"🔇 {target.mention_html()} به دلیل رسیدن به سقف اخطار، "
                f"به مدت {DEFAULT_MUTE_MINUTES} دقیقه سکوت شد.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("خطا در میوت خودکار")
            await update.effective_message.reply_text(f"خطا در اعمال سکوت خودکار: {e}")


async def unwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.effective_message.reply_text(ADMIN_ONLY_MSG)
        return
    target = get_target_user(update)
    if target is None:
        await update.effective_message.reply_text(REPLY_NEEDED_MSG)
        return
    chat_id = update.effective_chat.id
    warnings = remove_one_warning(chat_id, target.id)
    await update.effective_message.reply_text(
        f"✅ یک اخطار از {target.mention_html()} کم شد. اخطارهای فعلی: {warnings}",
        parse_mode="HTML",
    )


async def warnings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = get_target_user(update)
    if target is None:
        await update.effective_message.reply_text(REPLY_NEEDED_MSG)
        return
    chat_id = update.effective_chat.id
    row = get_member_row(chat_id, target.id)
    warnings = row["warnings"] if row else 0
    await update.effective_message.reply_text(
        f"⚠️ تعداد اخطارهای {target.mention_html()}: {warnings}/{MAX_WARNINGS}",
        parse_mode="HTML",
    )


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.effective_message.reply_text(ADMIN_ONLY_MSG)
        return
    target = get_target_user(update)
    if target is None:
        await update.effective_message.reply_text(REPLY_NEEDED_MSG)
        return
    minutes = parse_minutes(context.args)
    chat_id = update.effective_chat.id
    until = datetime.utcnow() + timedelta(minutes=minutes)
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await update.effective_message.reply_text(
            f"🔇 {target.mention_html()} به مدت {minutes} دقیقه سکوت شد.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("خطا در mute")
        await update.effective_message.reply_text(f"خطا: {e}")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.effective_message.reply_text(ADMIN_ONLY_MSG)
        return
    target = get_target_user(update)
    if target is None:
        await update.effective_message.reply_text(REPLY_NEEDED_MSG)
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await update.effective_message.reply_text(
            f"🔊 سکوت {target.mention_html()} برداشته شد.", parse_mode="HTML"
        )
    except Exception as e:
        logger.exception("خطا در unmute")
        await update.effective_message.reply_text(f"خطا: {e}")


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.effective_message.reply_text(ADMIN_ONLY_MSG)
        return
    target = get_target_user(update)
    if target is None:
        await update.effective_message.reply_text(REPLY_NEEDED_MSG)
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        await update.effective_message.reply_text(
            f"⛔️ {target.mention_html()} بن شد.\n"
            f"برای رفع بن: <code>/unban {target.id}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("خطا در ban")
        await update.effective_message.reply_text(f"خطا: {e}")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.effective_message.reply_text(ADMIN_ONLY_MSG)
        return
    if not context.args:
        await update.effective_message.reply_text(
            "لطفاً آیدی عددی کاربر را وارد کن. مثال:\n/unban 123456789"
        )
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("آیدی عددی نامعتبر است.")
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        await update.effective_message.reply_text(f"✅ کاربر {user_id} رفع بن شد.")
    except Exception as e:
        logger.exception("خطا در unban")
        await update.effective_message.reply_text(f"خطا: {e}")


async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.effective_message.reply_text(ADMIN_ONLY_MSG)
        return
    target = get_target_user(update)
    if target is None:
        await update.effective_message.reply_text(REPLY_NEEDED_MSG)
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        await context.bot.unban_chat_member(chat_id, target.id)
        await update.effective_message.reply_text(
            f"👢 {target.mention_html()} از گروه اخراج شد (امکان بازگشت با لینک دعوت وجود دارد).",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("خطا در kick")
        await update.effective_message.reply_text(f"خطا: {e}")


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = get_target_user(update)
    if target is None:
        target = update.effective_user
    chat_id = update.effective_chat.id
    row = get_member_row(chat_id, target.id)
    if row is None:
        await update.effective_message.reply_text(
            "هنوز اطلاعاتی از این کاربر ثبت نشده (باید حداقل یک پیام در گروه فرستاده باشد)."
        )
        return
    first_seen = row["first_seen"]
    try:
        first_seen_fmt = datetime.fromisoformat(first_seen).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        first_seen_fmt = first_seen
    text = (
        f"👤 <b>اطلاعات کاربر</b>\n"
        f"نام: {target.full_name}\n"
        f"یوزرنیم: {row['username']}\n"
        f"آیدی عددی: <code>{row['user_id']}</code>\n"
        f"اولین حضور: {first_seen_fmt}\n"
        f"تعداد پیام‌ها: {row['message_count']}\n"
        f"تعداد اخطار: {row['warnings']}/{MAX_WARNINGS}"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# اجرای برنامه
# ---------------------------------------------------------------------------


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "توکن ربات پیدا نشد! لطفاً متغیر محیطی BOT_TOKEN را تنظیم کن."
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("unwarn", unwarn_cmd))
    app.add_handler(CommandHandler("warnings", warnings_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("info", info_cmd))

    # ثبت آمار برای همه پیام‌های متنی گروه (باید بعد از دستورات اضافه شود)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_messages))

    # -----------------------------------------------------------------
    # تشخیص خودکار حالت اجرا:
    #   - اگر روی Render (یا هر سرویس مشابه) اجرا بشه و RENDER_EXTERNAL_URL
    #     تنظیم شده باشه => حالت webhook (سرور HTTP داخلی روی PORT گوش می‌ده)
    #   - در غیر این صورت (مثلاً روی کامپیوتر خودت) => حالت polling معمولی
    # -----------------------------------------------------------------
    external_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    port = int(os.environ.get("PORT", "10000"))

    if external_url:
        webhook_path = BOT_TOKEN  # مسیر مخفی، خودِ توکن رو به‌عنوان path استفاده می‌کنیم
        full_webhook_url = f"{external_url.rstrip('/')}/{webhook_path}"
        logger.info("اجرای ربات در حالت WEBHOOK روی پورت %s ...", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=full_webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("اجرای ربات در حالت POLLING (اجرای محلی)...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
