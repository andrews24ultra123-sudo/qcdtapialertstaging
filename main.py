import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo

import requests
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
BOT_TOKEN = "8183120153:AAF3k3FZViX33glskyf-CTi2F3LoxulGvV0"
CHAT_ID = -4680966417
API_URL = "https://www.dmz.finance/stores/tdd/qcdt/new_price"

TZ = ZoneInfo("Asia/Singapore")
SGT_PYTZ = pytz.timezone("Asia/Singapore")

# Weekday schedules (SGT)
HOLIDAY_TIME = dtime(16, 45)     # 4:45pm
REMINDER_TIME = dtime(17, 30)    # 5:30pm
NAG_START = dtime(18, 0)         # 6:00pm
NAG_END = dtime(21, 0)           # 9:00pm

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
# STATE (daily, in-memory)
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
    return datetime.now(TZ)

def today_str():
    return now_sgt().strftime("%Y-%m-%d")

def is_weekday():
    return now_sgt().weekday() < 5

def pretty_date(s):
    return datetime.strptime(s, "%Y-%m-%d").strftime("%d %b %Y").lstrip("0")

def pretty_today():
    return now_sgt().strftime("%d %b %Y").lstrip("0")

def within(now_t, start, end):
    return start <= now_t <= end

def should_send_error():
    return state["last_error_at"] is None or now_sgt() - state["last_error_at"] > ERROR_COOLDOWN

# =========================
# TELEGRAM SAFE SEND
# =========================
async def safe_send(ctx, text, mode=None):
    try:
        await ctx.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=mode)
    except Forbidden:
        logging.error("Forbidden: bot not in group or no permission.")
    except Exception as e:
        logging.error("Send failed: %s", e)

async def safe_send_poll(ctx, question, options):
    try:
        return await ctx.bot.send_poll(
            chat_id=CHAT_ID,
            question=question,
            options=options,
            is_anonymous=False,
        )
    except Forbidden:
        logging.error("Forbidden: cannot send poll.")
        return None
    except Exception as e:
        logging.error("Poll send failed: %s", e)
        return None

# =========================
# HOLIDAY SUMMARY (requests only)
# =========================
async def holiday_summary():
    today = now_sgt().date()
    year = today.year
    lines = ["📅 Public Holidays (SG / UAE) — This Week"]

    def fetch(country):
        try:
            r = requests.get(f"{HOLIDAY_API}/{year}/{country}", timeout=20)
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    for label, code in [("Singapore", "SG"), ("UAE", "AE")]:
        data = fetch(code)
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
# API FETCH
# =========================
async def fetch_payload():
    def _get():
        r = requests.get(API_URL, timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json()
    return await asyncio.to_thread(_get)

def parse_update_time(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)

# =========================
# PRICE CHECK
# =========================
async def check_price(ctx):
    if state["stop_all"] or not is_weekday():
        return

    now = now_sgt()
    if not within(now.time(), dtime(15, 0), dtime(21, 0)):
        return

    try:
        payload = await fetch_payload()
        ut = payload["data"]["update_time"]

        changed = ut != state["last_seen_update_time"]
        is_today = parse_update_time(ut).strftime("%Y-%m-%d") == today_str()

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

# =========================
# NAGGING
# =========================
async def nag_poll(ctx):
    if state["stop_all"] or state["stop_nags"] or state["update_detected"]:
        return
    if not is_weekday():
        return

    now = now_sgt().time()
    if not within(now, NAG_START, NAG_END):
        return

    poll = await safe_send_poll(
        ctx,
        "⚠️ QCDT price not updated yet. Action?",
        ["🕵️ Investigating / Dispute", "🎌 Public holiday"],
    )
    if poll:
        state["pending_nag_poll_id"] = poll.poll.id

# =========================
# SCHEDULED JOBS
# =========================
async def job_holiday(ctx):
    if is_weekday() and not state["stop_all"]:
        msg = await holiday_summary()
        await safe_send(ctx, msg)

async def job_reminder(ctx):
    if is_weekday() and not state["stop_all"]:
        await safe_send(ctx, DAILY_REMINDER)

async def job_nag_kickoff(ctx):
    if is_weekday() and not state["stop_all"] and not state["update_detected"]:
        await nag_poll(ctx)

async def daily_reset(ctx):
    for k in state:
        state[k] = False if isinstance(state[k], bool) else None
    state["last_seen_update_time"] = None
    await safe_send(ctx, "🔄 QCDT bot daily reset (SGT).")

# =========================
# POLL ANSWERS
# =========================
async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    if not pa or not pa.option_ids:
        return

    choice = pa.option_ids[0]

    if pa.poll_id == state["pending_update_poll_id"]:
        state["stop_all"] = True
        payload = state["pending_update_payload"]

        if choice == 0:
            d = payload["data"]
            await safe_send(
                ctx,
                f"Updated today on {pretty_today()} for {pretty_date(d['price_date'])} QCDT price. "
                f"Price of {d['price']} tallies with NAV report. {CC_LINE}",
            )
        elif choice == 1:
            await safe_send(ctx, "🕵️ Marked as Investigating / Dispute. Monitoring stopped for today.")
        else:
            await safe_send(ctx, "🎌 Marked as Public holiday. Monitoring stopped for today.")

    elif pa.poll_id == state["pending_nag_poll_id"]:
        if choice == 1:
            state["stop_nags"] = True
            await safe_send(ctx, "🎌 Public holiday noted. Nag reminders stopped.")

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
    weekdays = (0, 1, 2, 3, 4)

    jq.run_daily(daily_reset, time=dtime(0, 1), name="daily_reset")
    jq.run_daily(job_holiday, time=HOLIDAY_TIME, days=weekdays, name="holiday_1645")
    jq.run_daily(job_reminder, time=REMINDER_TIME, days=weekdays, name="reminder_1730")

    jq.run_daily(job_nag_kickoff, time=NAG_START, days=weekdays, name="nag_kickoff_1800")
    jq.run_repeating(nag_poll, interval=NAG_EVERY_MIN * 60, first=60, name="nag_repeat")

    jq.run_repeating(check_price, interval=CHECK_EVERY_MIN * 60, first=10, name="price_check")

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
