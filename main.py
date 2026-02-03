import asyncio
import json
import logging
from datetime import datetime, time as dtime, timedelta, date
from zoneinfo import ZoneInfo

import requests
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

# Timezone
TZINFO = ZoneInfo("Asia/Singapore")
SGT_PYTZ = pytz.timezone("Asia/Singapore")  # for PTB JobQueue Defaults

# Weekday schedules (SGT)
HOLIDAY_TIME = dtime(16, 45)     # 4:45pm SGT  ✅ (changed)
REMINDER_TIME = dtime(17, 30)    # 5:30pm SGT
NAG_START = dtime(18, 0)         # 6:00pm SGT
NAG_END = dtime(21, 0)           # 9:00pm SGT

CHECK_EVERY_MIN = 2              # check API every 2 min
NAG_EVERY_MIN = 15               # nag every 15 min

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
    "update_detected": False,            # True once we detect today’s update_time change
    "stop_all": False,                   # True after update-action poll selection
    "stop_nags": False,                  # True if user selects Public holiday on nag poll
    "pending_update_payload": None,
    "pending_update_poll_id": None,
    "pending_nag_poll_id": None,
    "last_error_at": None,               # datetime in SGT
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
    # "2026-02-02" -> "2 Feb 2026"
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
                    hd = date.fromisoformat(h.get("date", ""))
                except Exception:
                    continue
                # show holidays +/- 7 days around today (practical "this week-ish")
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
    def _get():
        r = requests.get(API_URL, timeout=HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json()
    return await asyncio.to_thread(_get)

def parse_update_time_sgt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZINFO)

# =========================
# PRICE UPDATE FLOW
# =========================
async def check_price(ctx: ContextTypes.DEFAULT_TYPE):
    # hard stops
    if state["stop_all"]:
        return

    dt = now_sgt()
    if not is_weekday(dt):
        return

    # only check meaningfully during afternoon/evening
    # (still fine if it runs; just reduces noise)
    # If you want always-on weekdays, remove this gate.
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

            # 1) JSON only
            await safe_send(ctx, f"<pre>{json.dumps(payload, ensure_ascii=False)}</pre>", ParseMode.HTML)
            # 2) Tag line
            await safe_send(ctx, TAG_LINE)

            # 3) Action poll
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
# NAGGING (after 6pm if not updated)
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

# =========================
# SCHEDULED JOBS
# =========================
async def job_holiday_summary(ctx: ContextTypes.DEFAULT_TYPE):
    if state["stop_all"]:
        return
    if not is_weekday():
        return
    msg = await holiday_summary_this_week()
    await safe_send(ctx, msg)

async def job_portal_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    if state["stop_all"]:
        return
    if not is_weekday():
        return
    await safe_send(ctx, DAILY_REMINDER)

async def job_6pm_nag_kickoff(ctx: ContextTypes.DEFAULT_TYPE):
    """
    At 6:00pm: if still not updated, send the first nag immediately.
    After that, repeating nag job covers 6:15, 6:30, ... until 9:00.
    """
    if state["stop_all"] or state["stop_nags"] or state["update_detected"]:
        return
    if not is_weekday():
        return
    await nag_poll(ctx)

async def daily_reset(ctx: ContextTypes.DEFAULT_TYPE):
    # reset daily state at 00:01 SGT
    state["update_detected"] = False
    state["stop_all"] = False
    state["stop_nags"] = False
    state["pending_update_payload"] = None
    state["pending_update_poll_id"] = None
    state["pending_nag_poll_id"] = None
    state["last_error_at"] = None
    # keep last_seen_update_time or reset it? safer to reset:
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

    # Update-detected poll: ANY choice stops everything for the day
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

    # Nag poll: Public holiday stops nags (but not necessarily full monitoring)
    if poll_id == state["pending_nag_poll_id"]:
        # options: 0=Investigating, 1=Public holiday
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
        logging.error("Startup message blocked (Forbidden). Bot not in group / no permission.")
    except Exception as e:
        logging.error("Startup message failed: %s", e)

def main():
    # IMPORTANT: defaults tzinfo makes JobQueue run_daily interpret times in SGT
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
        raise RuntimeError('JobQueue missing. Use: python-telegram-bot[job-queue]==21.6')

    weekdays = (0, 1, 2, 3, 4)  # Mon-Fri

    # Daily reset
    jq.run_daily(daily_reset, time=dtime(0, 1), name="daily_reset")

    # Scheduled notices (SGT)
    jq.run_daily(job_holiday_summary, time=HOLIDAY_TIME, days=weekdays, name="holiday_1645")
    jq.run_daily(job_portal_reminder, time=REMINDER_TIME, days=weekdays, name="reminder_1730")

    # Nag kickoff at 6pm and repeating nags every 15 min
    jq.run_daily(job_6pm_nag_kickoff, time=NAG_START, days=weekdays, name="nag_kickoff_1800")
    jq.run_repeating(nag_poll, interval=NAG_EVERY_MIN * 60, first=60, name="nag_repeat_15m")

    # Price check repeating every 2 minutes
    jq.run_repeating(check_price, interval=CHECK_EVERY_MIN * 60, first=10, name="price_check_2m")

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
