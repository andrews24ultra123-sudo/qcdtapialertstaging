import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo

import requests
import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    PollAnswerHandler,
    CommandHandler,
)
from telegram.error import Forbidden

# =========================
# CONFIG (YOUR VALUES)
# =========================
BOT_TOKEN = "8183120153:AAF3k3FZViX33glskyf-CTi2F3LoxulGvV0"
CHAT_ID = -4680966417
API_URL = "https://www.dmz.finance/stores/tdd/qcdt/new_price"

TZ = ZoneInfo("Asia/Singapore")

# Schedule (SGT, weekdays)
HOLIDAY_TIME = dtime(16, 30)      # 4:30pm
REMINDER_TIME = dtime(17, 30)    # 5:30pm
NAG_START = dtime(18, 0)         # 6:00pm
NAG_END = dtime(21, 0)           # 9:00pm

CHECK_EVERY_MIN = 2              # price polling
NAG_EVERY_MIN = 15               # nag cadence

TAG_LINE = "@mrpotato1234 please cross ref QCDT price to NAV pack email"
CC_LINE = "CC: @Nathan_DMZ @LEEKAIYANG @Duke_RWAlpha @AscentHamza @Ascentkaiwei"

DAILY_REMINDER = "📝 Ascent, please remember to update QCDT price on the portal."

HOLIDAY_API = "https://date.nager.at/api/v3/PublicHolidays"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =========================
# STATE (IN-MEMORY, DAILY)
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

ERROR_COOLDOWN = timedelta(minutes=60)

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

def should_send_error():
    return state["last_error_at"] is None or now_sgt() - state["last_error_at"] > ERROR_COOLDOWN

# =========================
# HOLIDAYS (SAFE)
# =========================
async def holiday_summary():
    today = now_sgt().date()
    year = today.year
    lines = ["📅 Public Holidays (SG / UAE) This Week"]

    async with httpx.AsyncClient() as client:
        for label, code in [("Singapore", "SG"), ("UAE", "AE")]:
            try:
                r = await client.get(f"{HOLIDAY_API}/{year}/{code}", timeout=20)
                data = r.json() if r.status_code == 200 else []
            except Exception:
                data = []

            found = []
            for h in data:
                try:
                    hd = date.fromisoformat(h["date"])
                except Exception:
                    continue
                if abs((hd - today).days) <= 7:
                    found.append(f"  - {hd:%a %d %b}: {h.get('name','Holiday')}")

            if found:
                lines.append(f"\n• {label}:")
                lines.extend(found)
            else:
                lines.append(f"\n• {label}: None")

    return "\n".join(lines)

# =========================
# API
# =========================
async def fetch_payload():
    return await asyncio.to_thread(
        lambda: requests.get(API_URL, timeout=15).json()
    )

def parse_update_time(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)

# =========================
# TELEGRAM HELPERS
# =========================
async def safe_send(ctx, text, mode=None):
    try:
        await ctx.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=mode)
    except Forbidden:
        logging.error("Forbidden: bot not in group or no permission.")
    except Exception as e:
        logging.error("Send failed: %s", e)

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
        is_today = parse_update_time(ut).strftime("%Y-%m-%d") == today_str()

        state["last_seen_update_time"] = ut

        if changed and is_today:
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
# NAG POLL
# =========================
async def nag_poll(ctx):
    if state["stop_all"] or state["stop_nags"] or state["update_detected"]:
        return
    if not is_weekday():
        return

    now = now_sgt().time()
    if not (NAG_START <= now <= NAG_END):
        return

    poll = await ctx.bot.send_poll(
        CHAT_ID,
        "⚠️ QCDT price not updated yet. Action?",
        ["🕵️ Investigating / Dispute", "🎌 Public holiday"],
        is_anonymous=False,
    )
    state["pending_nag_poll_id"] = poll.poll.id

# =========================
# POLL ANSWERS
# =========================
async def on_poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    if not pa:
        return

    choice = pa.option_ids[0]

    # Update-detected poll
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
            await safe_send(ctx, "🕵️ Marked as Investigating / Dispute.")
        else:
            await safe_send(ctx, "🎌 Marked as Public holiday.")

    # Nag poll
    elif pa.poll_id == state["pending_nag_poll_id"]:
        if choice == 1:
            state["stop_nags"] = True
            await safe_send(ctx, "🎌 Public holiday noted. Nag reminders stopped.")

# =========================
# DAILY RESET
# =========================
async def daily_reset(ctx):
    for k in state:
        state[k] = False if isinstance(state[k], bool) else None
    state["last_seen_update_time"] = None
    await safe_send(ctx, "🔄 QCDT bot daily reset (SGT).")

# =========================
# STARTUP
# =========================
async def post_init(app):
    try:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"✅ QCDT bot online at {now_sgt():%a %d %b %H:%M} SGT",
        )
    except Forbidden:
        logging.error("Startup message blocked (Forbidden).")
    except Exception as e:
        logging.error("Startup message failed: %s", e)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(CommandHandler("status", lambda u, c: safe_send(c, "Bot alive ✅")))

    jq = app.job_queue

    jq.run_repeating(check_price, interval=CHECK_EVERY_MIN * 60, first=10)
    jq.run_repeating(nag_poll, interval=NAG_EVERY_MIN * 60, first=60)

    jq.run_daily(lambda c: safe_send(c, asyncio.run(holiday_summary())), time=HOLIDAY_TIME)
    jq.run_daily(lambda c: safe_send(c, DAILY_REMINDER), time=REMINDER_TIME)
    jq.run_daily(daily_reset, time=dtime(0, 1))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
