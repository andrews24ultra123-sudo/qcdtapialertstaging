# =========================================================
# BOOTSTRAP DEPENDENCIES (Railway-safe)
# =========================================================
import sys
import subprocess

REQUIRED_PACKAGES = [
    "httpx==0.25.2",
    "python-telegram-bot[job-queue]==20.7",
    "pytz",
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
# NORMAL IMPORTS (safe now)
# =========================================================
import os
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = -5299275232

API_URL = "https://uat.dmz.finance/stores/tdd/qcdt/new_price"

TZINFO = ZoneInfo("Asia/Singapore")
SGT_PYTZ = pytz.timezone("Asia/Singapore")

HOLIDAY_TIME = dtime(16, 45)
REMINDER_TIME = dtime(17, 30)

NAG_START = dtime(17, 30)   # exact kickoff
NAG_END = dtime(21, 0)
NAG_EVERY_MIN = 5

CHECK_EVERY_MIN = 2

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
# TIME HELPERS
# =========================
def now_sgt():
    return datetime.now(TZINFO)

def today_str():
    return now_sgt().strftime("%Y-%m-%d")

def is_weekday(dt=None):
    dt = dt or now_sgt()
    return dt.weekday() < 5

def pretty_today():
    return now_sgt().strftime("%d %b %Y").lstrip("0")

def pretty_date_yyyy_mm_dd(s):
    d = datetime.strptime(s, "%Y-%m-%d").date()
    return d.strftime("%d %b %Y").lstrip("0")

def should_send_error():
    last = state["last_error_at"]
    return last is None or (now_sgt() - last) >= ERROR_COOLDOWN

def within_time_window(now_t, start, end):
    return start <= now_t <= end

# =========================
# TELEGRAM HELPERS
# =========================
async def safe_send(bot, text, mode=None):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=mode)
    except Exception as e:
        logging.error("Send failed: %s", e)

async def safe_poll(bot, question, options):
    try:
        return await bot.send_poll(
            chat_id=CHAT_ID,
            question=question,
            options=options,
            is_anonymous=False,
        )
    except Exception as e:
        logging.error("Poll failed: %s", e)
        return None

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
# JOBS
# =========================
async def check_price(ctx):
    if state["stop_all"] or not is_weekday():
        return

    if not within_time_window(now_sgt().time(), dtime(15, 0), dtime(21, 0)):
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

            await safe_send(ctx.bot, f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>", ParseMode.HTML)
            await safe_send(ctx.bot, TAG_LINE)

            poll = await safe_poll(
                ctx.bot,
                "QCDT price update detected. Action?",
                ["✅ Acknowledge", "🕵️ Investigating / Dispute", "🎌 Public holiday"],
            )
            if poll:
                state["pending_update_poll_id"] = poll.poll.id

    except Exception as e:
        if should_send_error():
            state["last_error_at"] = now_sgt()
            await safe_send(ctx.bot, f"⚠️ Error:\n<pre>{e}</pre>", ParseMode.HTML)

async def nag_poll(ctx):
    if state["stop_all"] or state["stop_nags"] or state["update_detected"]:
        return

    if not is_weekday():
        return

    if not within_time_window(now_sgt().time(), NAG_START, NAG_END):
        return

    poll = await safe_poll(
        ctx.bot,
        "⚠️ QCDT price not updated yet. Action?",
        ["🕵️ Investigating / Dispute", "🎌 Public holiday"],
    )
    if poll:
        state["pending_nag_poll_id"] = poll.poll.id

async def nag_kickoff(ctx):
    await nag_poll(ctx)

async def daily_reset(ctx):
    for k in state:
        state[k] = False if isinstance(state[k], bool) else None
    await safe_send(ctx.bot, "🔄 QCDT bot daily reset (SGT).")

# =========================
# POLL HANDLER
# =========================
async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    if not pa or not pa.option_ids:
        return

    if pa.poll_id == state["pending_update_poll_id"]:
        state["stop_all"] = True
        payload = state["pending_update_payload"]
        await safe_send(ctx.bot, f"Updated today on {pretty_today()} for {pretty_date_yyyy_mm_dd(payload['data']['price_date'])}. {CC_LINE}")
        return

    if pa.poll_id == state["pending_nag_poll_id"] and pa.option_ids[0] == 1:
        state["stop_nags"] = True
        await safe_send(ctx.bot, "🎌 Public holiday noted. Nagging stopped.")

# =========================
# STARTUP
# =========================
async def post_init(app):
    await safe_send(app.bot, f"✅ QCDT bot online at {now_sgt():%a %d %b %Y %H:%M} (SGT)")

    # restart-resume nag
    if is_weekday() and within_time_window(now_sgt().time(), NAG_START, NAG_END):
        if not state["update_detected"] and not state["stop_nags"]:
            poll = await safe_poll(
                app.bot,
                "⚠️ QCDT price not updated yet. Action?",
                ["🕵️ Investigating / Dispute", "🎌 Public holiday"],
            )
            if poll:
                state["pending_nag_poll_id"] = poll.poll.id

def main():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN missing")
        return

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
    jq.run_daily(daily_reset, time=dtime(0, 1))
    jq.run_daily(nag_kickoff, time=NAG_START, days=(0,1,2,3,4))
    jq.run_repeating(nag_poll, interval=NAG_EVERY_MIN * 60, first=60)
    jq.run_repeating(check_price, interval=CHECK_EVERY_MIN * 60, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
