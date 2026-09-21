import os
import sys
import logging
import sqlite3
from datetime import datetime, timedelta
import pytz
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# تنظیمات لاگ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
tehran_tz = pytz.timezone('Asia/Tehran')

# --- بخش دیتابیس ---
DB_PATH = os.getenv('DATABASE_PATH', 'smoking_bot.db')

def get_db_connection():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS smoking_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        timestamp TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_config (
        user_id INTEGER PRIMARY KEY,
        interval_hours INTEGER
    )''')
    c.execute("INSERT OR IGNORE INTO settings VALUES ('start_text', 'سلام! همراه ترک سیگار شما آماده است.')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('start_gif', '')")
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

def get_setting(key):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else None

def set_setting(key, value):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_user_interval(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT interval_hours FROM user_config WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else 2

def set_user_interval(user_id, hours):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_config VALUES (?, ?)", (user_id, hours))
    conn.commit()
    conn.close()

def delete_last_smoke(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM smoking_logs WHERE id = (SELECT MAX(id) FROM smoking_logs WHERE user_id = ?)", (user_id,))
    changes = c.rowcount
    conn.commit()
    conn.close()
    return changes > 0

def add_log(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now(tehran_tz).isoformat()
    c.execute("INSERT INTO smoking_logs (user_id, timestamp) VALUES (?, ?)", (user_id, now))
    conn.commit()
    conn.close()

def get_today_stats(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    today = datetime.now(tehran_tz).date().isoformat()
    c.execute("SELECT timestamp FROM smoking_logs WHERE user_id = ? AND timestamp LIKE ?", (user_id, f"{today}%"))
    logs = c.fetchall()
    c.execute("SELECT timestamp FROM smoking_logs WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1", (user_id,))
    last_log = c.fetchone()
    conn.close()
    return len(logs), last_log[0] if last_log else None

def main_keyboard():
    return ReplyKeyboardMarkup([
        ['🚬 ثبت سیگار'],
        ['📊 وضعیت من', '❌ حذف آخرین ثبت'],
        ['⚙️ تنظیم زمان یادآوری']
    ], resize_keyboard=True)

def admin_keyboard():
    return ReplyKeyboardMarkup([
        ['📝 تغییر متن استارت'],
        ['🎬 تغییر گیف استارت'],
        ['🔙 برگشت']
    ], resize_keyboard=True)

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = get_setting('start_text')
    gif_url = get_setting('start_gif')
    try:
        if gif_url:
            await update.message.reply_animation(animation=gif_url, caption=caption, reply_markup=main_keyboard())
        else:
            await update.message.reply_text(caption, reply_markup=main_keyboard())
    except Exception as e:
        logger.error(f"Error sending start message: {e}")
        await update.message.reply_text(caption, reply_markup=main_keyboard())

async def set_interval_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [['1 ساعت', '2 ساعت', '3 ساعت'], ['4 ساعت', '5 ساعت', '6 ساعت'], ['🔙 برگشت']]
    await update.message.reply_text("زمان یادآوری را انتخاب کنید:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

async def handle_interval_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hours = int(update.message.text.split()[0])
        set_user_interval(update.effective_user.id, hours)
        await update.message.reply_text(f"✅ زمان یادآوری به {hours} ساعت تغییر یافت.", reply_markup=main_keyboard())
    except (ValueError, IndexError):
        await update.message.reply_text("لطفاً یک عدد صحیح انتخاب کنید.", reply_markup=main_keyboard())

async def undo_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if delete_last_smoke(update.effective_user.id):
        for job in context.job_queue.get_jobs_by_name(f"reminder_{update.effective_user.id}"):
            job.schedule_removal()
        await update.message.reply_text("❌ آخرین ثبت پاک شد.")
    else:
        await update.message.reply_text("چیزی برای حذف نیست.")

async def record_smoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_log(user_id)
    interval = get_user_interval(user_id)
    count, _ = get_today_stats(user_id)
    await update.message.reply_text(f"✅ ثبت شد. تعداد امروز: {count}\n{interval} ساعت دیگه خبرت می‌کنم.")
    
    job_name = f"reminder_{user_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    context.job_queue.run_once(send_reminder, interval * 3600, data=user_id, name=job_name)

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=context.job.data, text="🔔 وقتت آزاده، اگه هنوز نکشیدی دمت گرم!")

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count, last_time_str = get_today_stats(update.effective_user.id)
    if not last_time_str:
        await update.message.reply_text("هنوز ثبت نکردی.")
        return
    last_time = datetime.fromisoformat(last_time_str)
    if last_time.tzinfo is None:
        last_time = tehran_tz.localize(last_time)
    diff = datetime.now(tehran_tz) - last_time
    h, r = divmod(int(diff.total_seconds()), 3600)
    m, _ = divmod(r, 60)
    await update.message.reply_text(f"📊 امروز: {count} نخ\n⏱ فاصله: {h} ساعت و {m} دقیقه.")

# --- Admin Commands ---

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("منوی مدیریت:", reply_markup=admin_keyboard())

async def set_start_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['waiting_for'] = 'start_text'
    await update.message.reply_text("متن جدید پیام استارت رو بفرست:")

async def set_start_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['waiting_for'] = 'start_gif'
    await update.message.reply_text("گیف جدید رو بفرست (فایل گیف):")

async def handle_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle GIF files sent by admin"""
    waiting = context.user_data.get('waiting_for')
    
    if waiting == 'start_gif' and update.message.animation:
        file_id = update.message.animation.file_id
        set_setting('start_gif', file_id)
        context.user_data['waiting_for'] = None
        await update.message.reply_text("✅ گیف استارت با موفقیت تغییر کرد!", reply_markup=admin_keyboard())
        return True
    
    if waiting == 'start_text' and update.message.text:
        set_setting('start_text', update.message.text)
        context.user_data['waiting_for'] = None
        await update.message.reply_text(f"✅ متن استارت تغییر کرد:\n\n{update.message.text}", reply_markup=admin_keyboard())
        return True
    
    return False

# --- Fallback message handler ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    animation = update.message.animation
    
    # Admin input handling
    if await handle_gif(update, context):
        return
    
    if text == '📝 تغییر متن استارت':
        await set_start_text(update, context)
    elif text == '🎬 تغییر گیف استارت':
        await set_start_gif(update, context)
    elif text == '⚙️ مدیریت ربات':
        await admin_menu(update, context)
    else:
        await update.message.reply_text("از منوی زیر استفاده کن:", reply_markup=main_keyboard())

def main():
    TOKEN = os.getenv("BOT_TOKEN")
    if not TOKEN:
        logger.error("BOT_TOKEN environment variable is not set!")
        sys.exit(1)
    
    logger.info("Initializing database...")
    init_db()
    
    logger.info("Building application...")
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(MessageHandler(filters.Text(["🚬 ثبت سیگار"]), record_smoke))
    application.add_handler(MessageHandler(filters.Text(["📊 وضعیت من"]), show_status))
    application.add_handler(MessageHandler(filters.Text(["❌ حذف آخرین ثبت"]), undo_last))
    application.add_handler(MessageHandler(filters.Text(["⚙️ تنظیم زمان یادآوری"]), set_interval_menu))
    application.add_handler(MessageHandler(filters.Regex(r"^\d ساعت$"), handle_interval_choice))
    application.add_handler(MessageHandler(filters.Text(["🔙 برگشت"]), lambda u, c: u.message.reply_text("منوی اصلی:", reply_markup=main_keyboard())))
    application.add_handler(MessageHandler(filters.ALL, handle_message))
    
    logger.info("Bot is starting polling...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
