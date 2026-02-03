# =========================================================
# BOOTSTRAP (Railway-safe): ensure deps exist before imports
# =========================================================
import sys
import subprocess

REQUIRED = [
    "python-telegram-bot[job-queue]==20.7",
    "httpx==0.25.2",
    "pytz",
    "APScheduler==3.10.4",
]

def _ensure():
    # Best-effort: if already installed, pip will be fast/no-op
    subprocess.check_call([sys.executable, "-m", "pip", "install", *REQUIRED])

try:
    _ensure()
except Exception:
    # If pip install fails, let the normal import error show up
    pass

# =========================================================
# NORMAL IMPORTS
# =========================================================
import os
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
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = -5299275232

# ✅ UAT endpoint
API_URL = "https://uat.dmz.finance/stores/tdd/qcdt/new_price"

# Timezone
TZINFO = ZoneInfo("Asia/Singapore")
SGT_PYTZ = pytz.timezone("Asia/Singapore")

# Weekday schedules (SGT)
HOLIDAY_TIME = dtime(16, 45)     # 4:45pm
REMINDER_TIME = dtime(17, 30)    # 5:30pm

# ✅ NAG window: 5:30pm → 9:00pm
NAG_START = dtime(17, 30)        # kickoff exactly at 5:30pm
NAG_END = dtime(21, 0)           # 9:00pm
NAG_EVERY_MIN = 5                # every 5 min

CHECK_EVERY_MIN = 2              # price polling

TAG_LINE = "@mrpotato1234 please cross ref QCDT price to NAV pack email"
CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza @Ascentkaiwei"
DAILY_REMINDER = "📝 Ascent, please remember to update QCDT price on the portal."
HOLIDAY_API = "https://date.nager.at/api/v3/PublicHolidays"

HTTP_TIMEOUT_SECONDS = 15
ERROR_COOLDOWN = timedelta(minutes=60)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =========================
# STATE (in-memory, resets daily)
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
def now_sgt() -> datetime:
    return datetime.now(TZINFO)

def today_str() -> str:
    return now_sgt().strftime("%Y-%m-%d")

def is_weekday(dt: datetime | None = None) -> bool:
    dt = dt or now_sgt()
    return dt.weekday() < 5

def pretty_date_yyyy_mm_dd(s: str) -> str:
    d = datetime.strptime(s, "%Y-%m-%d").date()
    return d.strftime("%d %b %Y").lstrip("0")

def pretty_today() -> str:
    return now_sgt().strftime("%d %b %Y").lstrip("0")

def should_send_error() -> bool:
    last = state["last_error_at"]
    return last is None or (now_sgt() - last) >= ERROR_COOLDOWN

def within_time_window(now_t: dtime, start: dtime, end: dtime) -> bool:
    return start <= now_t <= end

# =========================
# TELEGRAM SAFE SEND
# =========================
async def safe_send(ctx: ContextTypes.DEFAULT_TYPE, text: str, mode=None):
    try:
        await ctx.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=mode)
    except Forbidden:
        logging.error("Forbidden: bot not in group or no permission to post.")
    except Exception as e:
        logging.error("Send failed: %s", e)

async def safe_send_poll(ctx: ContextTypes.DEFAULT_TYPE, question: str, options: list[str]):
    try:
        return await ctx.bot.send_poll(
            chat_id=CHAT_ID,
            question=question,
            options=options,
            is_anonymous=False,
        )
    except Forbidden:
        logging.error("Forbidden: cannot send poll (bot not in group or no permission).")
        return None
    except Exception as e:
        logging.error("Poll send failed: %s", e)
        return None

# =========================
# HOLIDAY SUMMARY (SG + UAE)
# =========================
async def holiday_summary_this_week() -> str:
    today = now_sgt().date()
    year = today.year
    lines = [f"📅 Public Holidays (SG / UAE) — Week of {today:%d %b %Y}"]

    async with httpx.AsyncClient(timeout=20) as client:
        for label, code in [("Singapore", "SG"), ("UAE", "AE")]:
            try:
                r = await client.get(f"{HOLIDAY_API}/{year}/{code}")
                data = r.json() if r.status_code == 200 else []
            except Exception:
                data = []

            found = []
            for h in data:
                try:
                    hd = date.fromisoformat(h.get("date", ""))
                except Exception:
                    continue
                if abs((hd - today).days) <= 7:
                    found.append(f"  - {hd:%a %d %b}: {h.get('name') or h.get('localName') or 'Holiday'}")

            if found:
                lines.append(f"\n• {label}:")
                lines.extend(found)
            else:
                lines.append(f"\n• {label}: None")

    return "\n".join(lines)

# =========================
# API FETCH + PARSE
# =========================
async def fetch_payload() -> dict:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(API_URL)
        r.raise_for_status()
        return r.json()

def parse_update_time_sgt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZINFO)

# =========================
# PRICE UPDATE FLOW
# =========================
async def check_price(ctx: ContextTypes.DEFAULT_TYPE):
    if state["stop_all"]:
        return

    dt = now_sgt()
    if not is_weekday(dt):
        return

    # same "meaningful hours" gate
    if not within_time_window(dt.time(), dtime(15, 0), dtime(21, 0)):
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

            await safe_send(ctx, f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>", ParseMode.HTML)
            await safe_send(ctx, TAG_LINE)

            poll = await safe_send_poll(
                ctx,
                "QCDT price update detected. Action?",
                ["✅ Acknowledge", "🕵️ Investigating / Dispute", "🎌 Public holiday"],
            )
            if poll:
                state["pending_update_poll_id"] = poll.poll.id

    except Exception as e:
        if should_send_error():
            state["last_error_at"] = now_sgt()
            await safe_send(ctx, f"⚠️ QCDT monitor error:\n<pre>{e}</pre>", ParseMode.HTML)

def build_ack_message(payload: dict) -> str:
    d = payload.get("data", {})
    price_date = d.get("price_date", "")
    price = d.get("price", "")
    return (
        f"Updated today on {pretty_today()} for {pretty_date_yyyy_mm_dd(price_date)} QCDT price. "
        f"Price of {price} tallies with NAV report. {CC_LINE}"
    )

# =========================
# NAGGING (5:30pm–9:00pm if not updated)
# =========================
async def nag_poll(ctx: ContextTypes.DEFAULT_TYPE):
    if state["stop_all"] or state["stop_nags"] or state["update_detected"]:
        return

    dt = now_sgt()
    if not is_weekday(dt):
        return

    if not within_time_window(dt.time(), NAG_START, NAG_END):
        return

    poll = await safe_send_poll(
        ctx,
        "⚠️ QCDT price not updated yet. Action?",
        ["🕵️ Investigating / Dispute", "🎌 Public holiday"],
    )
    if poll:
        state["pending_nag_poll_id"] = poll.poll.id

# ✅ Kickoff nag exactly at 5:30pm
async def nag_kickoff(ctx: ContextTypes.DEFAULT_TYPE):
    await nag_poll(ctx)

# =========================
# SCHEDULED JOBS
# =========================
async def job_holiday_summary(ctx: ContextTypes.DEFAULT_TYPE):
    if state["stop_all"] or not is_weekday():
        return
    await safe_send(ctx, await holiday_summary_this_week())

async def job_portal_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    if state["stop_all"] or not is_weekday():
        return
    await safe_send(ctx, DAILY_REMINDER)

async def daily_reset(ctx: ContextTypes.DEFAULT_TYPE):
    state["update_detected"] = False
    state["stop_all"] = False
    state["stop_nags"] = False
    state["pending_update_payload"] = None
    state["pending_update_poll_id"] = None
    state["pending_nag_poll_id"] = None
    state["last_error_at"] = None
    state["last_seen_update_time"] = None
    await safe_send(ctx, "🔄 QCDT bot daily reset (SGT).")

# =========================
# POLL ANSWERS
# =========================
async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    if not pa or not pa.option_ids:
        return

    poll_id = pa.poll_id
    choice = pa.option_ids[0]

    # Update-detected poll: any choice stops all
    if poll_id == state["pending_update_poll_id"]:
        state["stop_all"] = True
        payload = state["pending_update_payload"]

        if choice == 0 and payload:
            await safe_send(ctx, build_ack_message(payload))
        elif choice == 1:
            await safe_send(ctx, "🕵️ Marked as Investigating / Dispute. Monitoring stopped for today.")
        elif choice == 2:
            await safe_send(ctx, "🎌 Marked as Public holiday. Monitoring stopped for today.")
        else:
            await safe_send(ctx, "Noted. Monitoring stopped for today.")
        return

    # Nag poll: public holiday stops nags
    if poll_id == state["pending_nag_poll_id"]:
        if choice == 1:
            state["stop_nags"] = True
            await safe_send(ctx, "🎌 Public holiday noted. Nag reminders stopped for today.")
        else:
            await safe_send(ctx, "🕵️ Noted: Investigating / Dispute.")

# =========================
# COMMANDS
# =========================
async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        payload = await fetch_payload()
        await update.message.reply_text(
            f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ /status failed: {e}")

# =========================
# STARTUP
# =========================
async def post_init(app):
    try:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"✅ QCDT bot online at {now_sgt():%a %d %b %Y %H:%M} (SGT)",
        )
    except Forbidden:
        logging.error("Startup message blocked (Forbidden).")
    except Exception as e:
        logging.error("Startup message failed: %s", e)

def main():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN missing. Set Railway Variables: BOT_TOKEN=xxxxx")
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
    app.add_handler(CommandHandler("status", status_cmd))

    jq = app.job_queue
    if jq is None:
        logging.error("JobQueue missing. Install python-telegram-bot[job-queue]==20.7")
        return

    weekdays = (0, 1, 2, 3, 4)

    jq.run_daily(daily_reset, time=dtime(0, 1), name="daily_reset")
    jq.run_daily(job_holiday_summary, time=HOLIDAY_TIME, days=weekdays, name="holiday_1645")
    jq.run_daily(job_portal_reminder, time=REMINDER_TIME, days=weekdays, name="reminder_1730")

    # ✅ exact 5:30pm kickoff nag
    jq.run_daily(nag_kickoff, time=NAG_START, days=weekdays, name="nag_kickoff_1730")

    # ✅ repeating nags every 5 min (only sends within 5:30pm–9:00pm)
    jq.run_repeating(nag_poll, interval=NAG_EVERY_MIN * 60, first=60, name="nag_repeat_5m")

    # price check repeating every 2 min
    jq.run_repeating(check_price, interval=CHECK_EVERY_MIN * 60, first=10, name="price_check_2m")

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
