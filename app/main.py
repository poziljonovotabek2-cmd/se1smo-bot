import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from fastapi import FastAPI
from sqlalchemy import delete, select

from app.config import settings
from app.db import PendingCheck, Session, User, SeenEvent, init_db
from app.earthquakes import assess, describe, fetch_near, feed_status_text, local_time
from app.i18n import tr
from app.places import city_to_place, coords_to_place

bot = Bot(settings.bot_token)
dp = Dispatcher()
USGS_URL = "https://earthquake.usgs.gov/earthquakes/map/"
EMSC_URL = "https://www.emsc-csem.org/"


def menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=tr(lang, "menu_check")), KeyboardButton(text=tr(lang, "menu_sub"))],
        [KeyboardButton(text=tr(lang, "menu_loc")), KeyboardButton(text=tr(lang, "menu_lang"))],
        [KeyboardButton(text=tr(lang, "menu_help"))],
    ], resize_keyboard=True, is_persistent=True, input_field_placeholder=tr(lang, "menu_check"))


def source_links(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="USGS", url=USGS_URL),
        InlineKeyboardButton(text="EMSC", url=EMSC_URL),
    ]])


def location_prompt(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=tr(lang, "share_location"), request_location=True)],
        [KeyboardButton(text=tr(lang, "menu_check")), KeyboardButton(text=tr(lang, "menu_lang"))],
        [KeyboardButton(text=tr(lang, "menu_help"))],
    ], resize_keyboard=True, is_persistent=True)


def subscription_keyboard(user: User, lang: str) -> InlineKeyboardMarkup:
    state_key = "off" if user.subscribed else "on"
    state_label = tr(lang, state_key)
    mag_values = (3.5, 4.0, 4.5)
    dist_values = (100, 250, 500)
    rows = [[InlineKeyboardButton(text=f"🔔 {state_label}", callback_data="prefs:sub")]]
    rows.append([InlineKeyboardButton(text=("✓ " if user.min_magnitude == value else "") + f"M ≥ {value:.1f}", callback_data=f"prefs:mag:{value}") for value in mag_values])
    rows.append([InlineKeyboardButton(text=("✓ " if user.max_distance_km == value else "") + f"{value} km", callback_data=f"prefs:dist:{value}") for value in dist_values])
    quiet_label = tr(lang, "quiet_on" if user.quiet_hours else "quiet_off")
    rows.append([InlineKeyboardButton(text=f"{tr(lang, 'quiet_button')}: {quiet_label}", callback_data="prefs:quiet")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_text(user: User, lang: str) -> str:
    state = tr(lang, "sub_state_on" if user.subscribed else "sub_state_off")
    quiet = tr(lang, "quiet_on" if user.quiet_hours else "quiet_off")
    return tr(lang, "settings_title", state=state, magnitude=f"{user.min_magnitude:.1f}",
              distance=user.max_distance_km, quiet=quiet)


async def show_subscription(message: Message, user: User) -> None:
    await message.answer(subscription_text(user, user.language), reply_markup=subscription_keyboard(user, user.language))


def is_quiet_now(user: User) -> bool:
    if not user.quiet_hours:
        return False
    local = datetime.now(ZoneInfo(user.timezone))
    return local.hour >= 22 or local.hour < 7


async def get_user(tg_id: int) -> User | None:
    async with Session() as session:
        return await session.get(User, tg_id)


async def ensure_user(tg_id: int) -> User:
    async with Session() as session:
        user = await session.get(User, tg_id)
        if not user:
            user = User(telegram_id=tg_id)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


async def set_place(tg_id: int, lat: float, lon: float, place: str, tz: str) -> None:
    async with Session() as session:
        user = await session.get(User, tg_id)
        if not user:
            user = User(telegram_id=tg_id)
            session.add(user)
        user.latitude, user.longitude, user.place, user.timezone = lat, lon, place[:160], tz
        await session.commit()


def render_event(event: dict, user: User, lang: str, statuses: dict) -> str:
    qualifies, dist, confidence = assess(event, user.latitude, user.longitude)
    describe(event, user.timezone)
    depth = f"{event['depth']:.0f} km" if event.get("depth") is not None else "unknown"
    return tr(lang, "event", place=event["place"], mag=event["display_mag"], depth=depth,
              distance=f"{dist:.0f} km", time=local_time(event, user.timezone),
              sources=event["source_names"], confidence=confidence,
              feed_status=feed_status_text(statuses, lang))


async def matching(user: User, minutes: int = 30) -> tuple[list[dict], list[str], dict]:
    events, errors, statuses = await fetch_near(user.latitude, user.longitude, minutes)
    found = []
    for event in events:
        qualifies, _, _ = assess(event, user.latitude, user.longitude)
        if qualifies:
            found.append(event)
    return sorted(found, key=lambda e: e["time"], reverse=True), errors, statuses


async def do_check(message: Message, user: User, lang: str) -> None:
    await message.answer(tr(lang, "checking"))
    try:
        events, errors, statuses = await matching(user)
    except Exception:
        events, errors, statuses = [], ["feeds"], {"USGS": {"state": "unavailable"}, "EMSC": {"state": "unavailable"}}
    if events:
        text = "\n\n".join(render_event(e, user, lang, statuses) for e in events[:5])
        await message.answer(text, reply_markup=source_links(lang))
    else:
        # Explicitly describe source unavailability separately; silence is never presented as proof.
        text = tr(lang, "quiet", place=user.place or "your place") + "\n\n" + feed_status_text(statuses, lang)
        if errors:
            text += "\n\n" + {"ru": "Один или оба источника сейчас недоступны.", "uz": "Manbalardan biri yoki ikkalasi hozir ishlamayapti.", "en": "One or both data sources are currently unavailable."}[lang]
        sent = await message.answer(text, reply_markup=source_links(lang))
        async with Session() as session:
            session.add(PendingCheck(telegram_id=user.telegram_id, since=datetime.now(timezone.utc)-timedelta(minutes=30),
                                     expires_at=datetime.now(timezone.utc)+timedelta(minutes=10), message_id=sent.message_id))
            await session.commit()


@dp.message(CommandStart())
async def start(message: Message):
    async with Session() as session:
        user = await session.get(User, message.from_user.id)
        if not user:
            user = User(telegram_id=message.from_user.id, has_started=False)
            session.add(user)
            await session.flush()
        first_visit = not user.has_started
        user.has_started = True
        await session.commit()
        await session.refresh(user)
    lang = user.language
    await message.answer(tr(lang, "welcome_first" if first_visit else "welcome_returning"), reply_markup=menu(lang))
    if user.latitude is None:
        await message.answer(tr(lang, "choose_loc"), reply_markup=location_prompt(lang))


@dp.message(Command("help"))
@dp.message(F.text.in_(["❓ Помощь", "❓ Yordam", "❓ Help"]))
async def help_message(message: Message):
    user = await ensure_user(message.from_user.id)
    await message.answer(tr(user.language, "help"), reply_markup=menu(user.language))


@dp.message(Command("language"))
@dp.message(F.text.in_(["🌍 Язык", "🌍 Til", "🌍 Language"]))
async def language_menu(message: Message):
    await ensure_user(message.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Русский", callback_data="lang:ru"),
        InlineKeyboardButton(text="O‘zbekcha", callback_data="lang:uz"), InlineKeyboardButton(text="English", callback_data="lang:en")]])
    user = await get_user(message.from_user.id)
    await message.answer(tr(user.language, "choose_lang"), reply_markup=keyboard)


@dp.callback_query(F.data.startswith("lang:"))
async def choose_language(query: CallbackQuery):
    lang = query.data.split(":", 1)[1]
    async with Session() as session:
        user = await session.get(User, query.from_user.id)
        if user:
            user.language = lang
            await session.commit()
    await query.answer()
    await query.message.answer(tr(lang, "language"), reply_markup=menu(lang))


@dp.callback_query(F.data.startswith("prefs:"))
async def update_subscription_preferences(query: CallbackQuery):
    parts = query.data.split(":")
    async with Session() as session:
        user = await session.get(User, query.from_user.id)
        if not user:
            await query.answer(); return
        if parts[1] == "sub":
            if not user.subscribed and user.latitude is None:
                await query.answer(tr(user.language, "location_required"), show_alert=True)
                return
            user.subscribed = not user.subscribed
        elif parts[1] == "mag" and len(parts) == 3:
            user.min_magnitude = float(parts[2])
        elif parts[1] == "dist" and len(parts) == 3:
            user.max_distance_km = int(parts[2])
        elif parts[1] == "quiet":
            user.quiet_hours = not user.quiet_hours
        await session.commit()
        await session.refresh(user)
        response_text = subscription_text(user, user.language)
        keyboard = subscription_keyboard(user, user.language)
    await query.answer()
    await query.message.edit_text(response_text, reply_markup=keyboard)


@dp.message(Command("location"))
@dp.message(F.text.in_(["📍 Место", "📍 Joy", "📍 Place"]))
async def request_place(message: Message):
    user = await ensure_user(message.from_user.id)
    await message.answer(tr(user.language, "choose_loc"), reply_markup=location_prompt(user.language))


@dp.message(F.location)
async def location_received(message: Message):
    user = await ensure_user(message.from_user.id)
    lat, lon = message.location.latitude, message.location.longitude
    place, tz = await coords_to_place(lat, lon, settings.user_agent)
    await set_place(user.telegram_id, lat, lon, place, tz)
    await message.answer(tr(user.language, "location_saved", place=place, tz=tz), reply_markup=menu(user.language))


@dp.message(F.text)
async def text_router(message: Message):
    user = await ensure_user(message.from_user.id)
    lang, text = user.language, message.text.strip()
    if text in [tr(l, "menu_lang") for l in ("ru", "uz", "en")]:
        await language_menu(message); return
    if text in [tr(l, "menu_loc") for l in ("ru", "uz", "en")]:
        await request_place(message); return
    if text in [tr(lang, "menu_check") for lang in ("ru", "uz", "en")] or text in {
        "🌐 Was that an earthquake?", "🌐 Zilzila bo'ldimi?"
    }:
        if user.latitude is None:
            await message.answer(tr(lang, "ask_location")); return
        await do_check(message, user, lang); return
    if text in [tr(lang, "menu_sub") for lang in ("ru", "uz", "en")]:
        await show_subscription(message, user); return
    if text.startswith("/"):
        return
    # Free text is a place lookup, which also lets the user replace a saved city.
    try:
        result = await city_to_place(text, settings.user_agent)
    except Exception:
        result = None
    if result:
        lat, lon, place, tz = result
        await set_place(user.telegram_id, lat, lon, place, tz)
        await message.answer(tr(lang, "location_saved", place=place, tz=tz), reply_markup=menu(lang)); return
    await message.answer(tr(lang, "ask_location" if user.latitude is None else "geo_error"), reply_markup=menu(lang))


async def background_worker():
    while True:
        await asyncio.sleep(settings.check_interval_seconds)
        async with Session() as session:
            users = list((await session.scalars(select(User).where(User.subscribed.is_(True), User.latitude.is_not(None)))).all())
            pending = list((await session.scalars(select(PendingCheck).where(PendingCheck.expires_at > datetime.now(timezone.utc)))).all())
        for user in users:
            try:
                if is_quiet_now(user):
                    continue
                events, _, statuses = await matching(user, minutes=720 if user.quiet_hours else 30)
                for event in events:
                    if not assess(event, user.latitude, user.longitude, user.min_magnitude, user.max_distance_km)[0]:
                        continue
                    # Event identity is shared across feeds and recipients to prevent duplicate alerts.
                    key = f"{user.telegram_id}:{round(event['lat'],2)}:{round(event['lon'],2)}:{int(event['time'].timestamp()//90)}"
                    async with Session() as session:
                        previous = await session.get(SeenEvent, key)
                        if previous and previous.delivered:
                            continue
                        if not previous:
                            session.add(SeenEvent(event_key=key, delivered=False))
                            await session.commit()
                    lang = user.language
                    try:
                        await bot.send_message(user.telegram_id, "🔔 " + render_event(event, user, lang, statuses), reply_markup=source_links(lang))
                    except Exception:
                        # Keep the row pending; the next polling cycle will retry this alert.
                        continue
                    async with Session() as session:
                        delivered = await session.get(SeenEvent, key)
                        if delivered:
                            delivered.delivered = True
                            await session.commit()
            except Exception:
                continue
        # A quiet check gets one follow-up if an event lands in the feed before the watch window ends.
        for check in pending:
            try:
                user = await get_user(check.telegram_id)
                if not user or user.latitude is None:
                    continue
                events, _, statuses = await fetch_near(user.latitude, user.longitude, 30)
                events = [e for e in events if e["time"] >= check.since and assess(e, user.latitude, user.longitude)[0]]
                if events:
                    response = tr(user.language, "later") + "\n\n" + "\n\n".join(
                        render_event(event, user, user.language, statuses) for event in sorted(events, key=lambda e: e["time"], reverse=True)[:3])
                    if check.message_id:
                        await bot.edit_message_text(response, chat_id=check.telegram_id, message_id=check.message_id,
                                                    reply_markup=source_links(user.language))
                    else:
                        await bot.send_message(check.telegram_id, response, reply_markup=source_links(user.language))
                    async with Session() as session:
                        await session.delete(await session.get(PendingCheck, check.id))
                        await session.commit()
            except Exception:
                continue
        async with Session() as session:
            await session.execute(delete(PendingCheck).where(PendingCheck.expires_at <= datetime.now(timezone.utc)))
            await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    polling_task = None
    worker_task = asyncio.create_task(background_worker())
    if settings.polling:
        polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    worker_task.cancel()
    if polling_task:
        polling_task.cancel()
    await bot.session.close()


app = FastAPI(title="QuakeWatch Telegram Bot", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}
