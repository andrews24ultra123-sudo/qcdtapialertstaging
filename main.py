# =========================================================
# BOOTSTRAP DEPENDENCIES (RUNS BEFORE ANY IMPORTS)
# =========================================================
import sys
import subprocess

REQUIRED_PACKAGES = [
    "python-telegram-bot[job-queue]==20.7",
    "httpx==0.27.0",
    "pytz==2025.1",
    "APScheduler==3.10.4",
]

def ensure_packages():
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg.split("[")[0].split("==")[0])
        except Exception:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

ensure_packages()

# =========================================================
# NORMAL IMPORTS (SAFE NOW)
# =========================================================
import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo

import httpx
import pytz
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    PollAnswerHandler,
    CommandHandler,
    Defaults,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8353492721:AAHTzmPHAAOF-7ihhFCiTT7B339MnhQnScU"
CHAT_ID = -5299275232
API_URL = "https://www.dmz.finance/stores/tdd/qcdt/new_price"

TZINFO = ZoneInfo("Asia/Singapore")
SGT_PYTZ = pytz.timezone("Asia/Singapore")

HOLIDAY_TIME = dtime(16, 45)
REMINDER_TIME = dtime(17, 30)
NAG_START = dtime(18, 0)
NAG_END = dtime(21, 0)

CHECK_EVERY_MIN = 2
NAG_EVERY_MIN = 15

TAG_LINE = "@mrpotato1234 please cross ref QCDT price to NAV pack email"
CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza @Ascentkaiwei"
DAILY_REMINDER = "📝 Ascent, please remember to update QCDT price on the portal."

HOLIDAY_API = "https://date.nager.at/api/v3/PublicHolidays"

HTTP_TIMEOUT_SECONDS = 15
ERROR_COOLDOWN = timedelta(minutes=60)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =========================
# STATE
# =========================
state = {
    "last_seen_update_time": None,
    "update_detected": False,
    "stop_all": False,
    "stop_nags": False,
    "pending_update_payload": None,
    "pending_update_poll_id": None,
    "pending_nag_poll_id": None,
    "last_error_at": None,
}

# =========================
# HELPERS
# =========================
def now_sgt():
    return datetime.now(TZINFO)

def today_str():
    return now_sgt().strftime("%Y-%m-%d")

def is_weekday():
    return now_sgt().weekday() < 5

def should_send_error():
    last = state["last_error_at"]
    return last is None or (now_sgt() - last) >= ERROR_COOLDOWN

# =========================
# TELEGRAM
# =========================
async def safe_send(ctx, text, mode=None):
    try:
        await ctx.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=mode)
    except Exception as e:
        logging.error("Send failed: %s", e)

# =========================
# API
# =========================
async def fetch_payload():
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(API_URL)
        r.raise_for_status()
        return r.json()

def parse_update_time_sgt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZINFO)

# =========================
# PRICE CHECK
# =========================
async def check_price(ctx):
    if state["stop_all"] or not is_weekday():
        return

    try:
        payload = await fetch_payload()
        ut = payload["data"]["update_time"]

        changed = ut != state["last_seen_update_time"]
        is_today = parse_update_time_sgt(ut).strftime("%Y-%m-%d") == today_str()
        state["last_seen_update_time"] = ut

        if changed and is_today and not state["update_detected"]:
            state["update_detected"] = True
            state["pending_update_payload"] = payload

            await safe_send(ctx, f"<pre>{json.dumps(payload)}</pre>", ParseMode.HTML)
            await safe_send(ctx, TAG_LINE)

            poll = await ctx.bot.send_poll(
                CHAT_ID,
                "QCDT price update detected. Action?",
                ["✅ Acknowledge", "🕵️ Investigating / Dispute", "🎌 Public holiday"],
                is_anonymous=False,
            )
            state["pending_update_poll_id"] = poll.poll.id

    except Exception as e:
        if should_send_error():
            state["last_error_at"] = now_sgt()
            await safe_send(ctx, f"⚠️ Error:\n<pre>{e}</pre>", ParseMode.HTML)

# =========================
# POLL HANDLER
# =========================
async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    if not pa or not pa.option_ids:
        return

    if pa.poll_id == state["pending_update_poll_id"]:
        state["stop_all"] = True
        await safe_send(ctx, "✅ Noted. Monitoring stopped for today.")

# =========================
# STARTUP
# =========================
async def post_init(app):
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=f"✅ QCDT bot online at {now_sgt():%a %d %b %H:%M} SGT",
    )

def main():
    defaults = Defaults(tzinfo=SGT_PYTZ)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .post_init(post_init)
        .build()
    )

    app.add_handler(PollAnswerHandler(on_poll_answer))

    jq = app.job_queue
    jq.run_repeating(check_price, interval=CHECK_EVERY_MIN * 60, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
