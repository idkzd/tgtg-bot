"""Ultimate TGTG Vienna Telegram bot — full service.

Features
--------
- Live location -> top stores within a radius (1/3/5/10 km).
- Chain search: type "HOFER" (or pick from the menu) -> best-rated stores
  of that chain across the whole of Vienna, sorted / filtered by you.
- Origins: live pin, saved home, saved work, or any named place.
- Distance (km) or driving time (Google Distance Matrix, optional key;
  without a key a city-average estimate is used).
- Sort by rating / reviews / distance / value (rating per km or per minute).
- Watch a store -> bot polls it and notifies the moment a bag is back in
  stock. Notification only; it never reserves or buys.
- Everything persisted in SQLite (tgtg_bot.db): users, places, watches,
  the Vienna store cache, travel-time cache.

Run:  TGTG_BOT_TOKEN=<token> python bot.py   (token also read from .env)
"""
import asyncio
import html
import logging
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import DB
from geo import (Geo, TRAVEL_LABELS, estimate_minutes, haversine_km,
                  value_score)
import keep_alive
import wl
from tgtg_client import TgtgClient

TOKEN = os.environ.get("TGTG_BOT_TOKEN", "")
if os.path.exists(os.path.join(os.path.dirname(__file__), ".env")):
    for line in open(os.path.join(os.path.dirname(__file__), ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.environ.get("TGTG_BOT_TOKEN", "")

RADII = [1.0, 3.0, 5.0, 10.0]
SORTS = {"rating": "⭐ рейтинг", "reviews": "💬 отзывы",
         "distance": "📍 ближе", "value": "⚖️ ценность"}
SORT_CB = {"rating": "sort:rating", "reviews": "sort:reviews",
           "distance": "sort:distance", "value": "sort:value"}
LIMIT = 8
CHAIN_TTL_S = int(os.environ.get("TGTG_CHAIN_TTL_S", "1800"))  # cache freshness
COMMON_CHAINS = ["HOFER", "SPAR", "BIPA", "Anker", "INTERSPAR", "EUROSPAR",
                 "Denns BioMarkt", "Ströck", "Felber", "Aida", "Tchibo", "Starbucks"]
# rating thresholds for the "рейтинг ≥ X" filter (0 = any)
MIN_RATINGS = [(0.0, "любой рейтинг"), (4.5, "от 4.5"),
               (4.8, "от 4.8"), (4.9, "от 4.9")]


# ---- glue ----------------------------------------------------------------
def get_db() -> DB:
    return DB()


def client(context: ContextTypes.DEFAULT_TYPE) -> TgtgClient:
    c = context.bot_data.get("tgtg")
    if c is None:
        c = TgtgClient()
        context.bot_data["tgtg"] = c
    return c


def geo(context: ContextTypes.DEFAULT_TYPE) -> Geo:
    g = context.bot_data.get("geo")
    if g is None:
        g = Geo(get_db())
        context.bot_data["geo"] = g
    return g


TRAVEL_NAMES = {"driving": "машина", "transit": "транспорт",
                "walking": "пешком", "bike": "вело"}


def prefs(ud):
    return {
        "radius": ud.get("radius", 3.0),
        "sort": ud.get("sort", "rating"),
        "minrev": ud.get("minrev", 0),
        "minrating": ud.get("minrating", 0.0),
        "stock_only": ud.get("stock_only", True),
        "travel": ud.get("travel", "driving"),
        "origin": ud.get("origin", "live"),
    }


def resolve_origin(db, chat_id, ud):
    """(label, lat, lng) for the chosen origin, falling back home->live->work."""
    mode = ud.get("origin", "live")
    u = db.get_user(chat_id) or {}
    live = ud.get("live")
    cands = []
    if mode == "home" and u.get("home_lat") is not None:
        cands = [("🏠 дом", u["home_lat"], u["home_lng"])]
    elif mode == "work" and u.get("work_lat") is not None:
        cands = [("💼 работа", u["work_lat"], u["work_lng"])]
    elif live:
        cands = [("📍 текущая", live[0], live[1])]
    if not cands:
        if u.get("home_lat") is not None:
            cands = [("🏠 дом", u["home_lat"], u["home_lng"])]
        elif u.get("work_lat") is not None:
            cands = [("💼 работа", u["work_lat"], u["work_lng"])]
        elif live:
            cands = [("📍 текущая", live[0], live[1])]
    if not cands:
        return None, None, None
    return cands[0]


# ---- keyboards -----------------------------------------------------------
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗺️ Топ рядом", callback_data="search"),
         InlineKeyboardButton("🛒 HOFER", callback_data="chain:HOFER")],
        [InlineKeyboardButton("🏪 Сеть магазинов", callback_data="chain"),
         InlineKeyboardButton("🔔 Слежки", callback_data="watches")],
        [InlineKeyboardButton("📍 Мои адреса", callback_data="places"),
         InlineKeyboardButton("🚏 Отправления", callback_data="departures")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
    ])


def settings_keyboard(p, has_live, has_home, has_work):
    rows = [[InlineKeyboardButton(f"📏 {int(r)} км" if r >= 1 else f"📏 {r} км",
                                  callback_data=f"radius:{r}") for r in RADII]]
    rows.append([InlineKeyboardButton(
        ("✅ " if p["sort"] == k else "") + v, callback_data=SORT_CB[k])
        for k, v in SORTS.items()])
    rows.append([
        InlineKeyboardButton(("✅ " if p["minrev"] == 0 else "") + "любое кол-во отзывов",
                             callback_data="minrev:0"),
        InlineKeyboardButton(("✅ " if p["minrev"] == 100 else "") + "от 100 отзывов",
                             callback_data="minrev:100"),
    ])
    t = p["travel"]
    rows.append([InlineKeyboardButton(
        ("✅ " if t == k else "") + TRAVEL_LABELS[k] + " " + TRAVEL_NAMES[k],
        callback_data=f"travel:{k}")
        for k in ("driving", "transit", "walking", "bike")])
    mr = p["minrating"]
    rows.append([InlineKeyboardButton(
        ("✅ " if mr == v else "") + label, callback_data=f"minrating:{v}")
        for v, label in MIN_RATINGS])
    rows.append([
        InlineKeyboardButton(("✅ " if p["stock_only"] else "") + "только в наличии",
                             callback_data="stock:1"),
        InlineKeyboardButton(("✅ " if not p["stock_only"] else "") + "вкл. распроданное",
                             callback_data="stock:0"),
    ])
    o = p["origin"]
    rows.append([
        InlineKeyboardButton(("✅ " if o == "live" and has_live else "") + "📍 текущая",
                             callback_data="origin:live"),
        InlineKeyboardButton(("✅ " if o == "home" and has_home else "") + "🏠 дом",
                             callback_data="origin:home"),
        InlineKeyboardButton(("✅ " if o == "work" and has_work else "") + "💼 работа",
                             callback_data="origin:work"),
    ])
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def chain_keyboard(db):
    rows = []
    names = [r["store_name"] for r in db.store_names()[:10]]
    names = names or COMMON_CHAINS
    for i in range(0, len(names), 2):
        rows.append([InlineKeyboardButton(n, callback_data=f"chain:{n}")
                     for n in names[i:i + 2]])
    rows.append([InlineKeyboardButton("✏️ Своё название", callback_data="chain_custom")])
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def places_keyboard(db, chat_id):
    rows = []
    for pl in db.places(chat_id):
        rows.append([InlineKeyboardButton(f"❌ {pl['name']}", callback_data=f"delplace:{pl['id']}")])
    rows.append([InlineKeyboardButton("➕ Добавить место", callback_data="addplace")])
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def watches_keyboard(db, chat_id):
    rows = []
    for w in db.watches(chat_id):
        rows.append([InlineKeyboardButton(f"🔕 {w['store_name']}",
                                          callback_data=f"unwatch:{w['id']}")])
    rows.append([InlineKeyboardButton("❌ Снять все", callback_data="unwatchall")])
    rows.append([InlineKeyboardButton("◀️ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def result_keyboard(items):
    rows = [[InlineKeyboardButton(f"🔔 {it['store_name'][:28]}",
                                  callback_data=f"watch:{it['item_id']}")]
            for it in items]
    rows.append([InlineKeyboardButton("🔄", callback_data="refresh"),
                 InlineKeyboardButton("⚙️", callback_data="settings"),
                 InlineKeyboardButton("◀️ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def item_link(it):
    """Deep link that opens the exact store/item inside the TGTG app.
    Format confirmed from the app: AppConstants.SHARE_URL_ITEM_VIEW =
    https://share.toogoodtogo.com/item/ (AndroidManifest declares the host)."""
    iid = it.get("item_id")
    if iid:
        return f"https://share.toogoodtogo.com/item/{iid}"
    return None


# ---- rendering -----------------------------------------------------------
def esc(s):
    """HTML-escape for parse_mode='HTML' — store names/addresses contain & and "
    would otherwise break Telegram entity parsing."""
    return html.escape(str(s)) if s is not None else ""


def fmt_line(it, minutes=None, origin_label=None):
    rating = f"{it['rating']:.2f}" if it.get("rating") is not None else "—"
    revs = it.get("rating_count")
    revs_s = str(revs) if revs is not None else "—"
    status = "✅ есть" if it.get("in_stock") else "⛔ распродано"
    parts = [f"⭐ {rating} ({revs_s})"]
    if it.get("km") is not None:
        parts.append(f"{it['km']:.1f} км")
    if minutes is not None:
        label = it.get("travel_label", "🚗")
        if it.get("minutes_src") == "est":
            parts.append(f"≈ {minutes:.0f} мин")
        else:
            parts.append(f"{label} {minutes:.0f} мин")
    parts.append(status)
    if it.get("price") is not None:
        parts.append(f"{it['price']:.2f} {it.get('currency', 'EUR')}")
    head = " · ".join(parts)
    name = esc(it.get("store_name") or "")
    url = item_link(it)
    store_line = f"🏪 <a href=\"{url}\">{name}</a>" if url else f"🏪 {name}"
    lines = [head, store_line]
    if it.get("address"):
        lines.append(f"📍 {esc(it['address'])}")
    if origin_label:
        lines[0] += f"\n      (от: {esc(origin_label)})"
    return "\n".join(lines)


def enrich_and_sort(rows, p, db, chat_id, ud):
    """Add km/minutes/value to each row, resolve origin, sort, cap."""
    origin_label, olat, olon = resolve_origin(db, chat_id, ud)
    enriched = []
    for r in rows:
        km = r.get("km")
        if km is None and olat is not None:
            km = haversine_km(olat, olon, r.get("lat"), r.get("lng"))
        travel = p["travel"]
        minutes = None
        minutes_src = None
        if km is not None and origin_label and olat is not None:
            minutes, minutes_src = geo_travel_minutes(
                olat, olon, r.get("lat"), r.get("lng"), travel)
            if minutes is None:
                minutes = estimate_minutes(km, travel)
                minutes_src = "est"
        r = dict(r)
        # cached chain rows use in_sales_window; live radius rows use in_stock
        if "in_stock" not in r and "in_sales_window" in r:
            r["in_stock"] = bool(r["in_sales_window"])
        r["km"] = km
        r["minutes"] = minutes
        r["minutes_src"] = minutes_src
        r["travel_label"] = TRAVEL_LABELS.get(travel, "🚗")
        r["value"] = value_score(r.get("rating"), km, minutes)
        enriched.append(r)
    if p["minrating"] > 0:
        enriched = [r for r in enriched if (r.get("rating") or 0) >= p["minrating"]]
    if p["minrev"] > 0:
        enriched = [r for r in enriched if (r.get("rating_count") or 0) >= p["minrev"]]
    if p["stock_only"]:
        enriched = [r for r in enriched if r.get("in_stock")]
    if p["sort"] == "rating":
        enriched.sort(key=lambda r: (-(r.get("rating") or 0), -(r.get("rating_count") or 0)))
    elif p["sort"] == "reviews":
        enriched.sort(key=lambda r: (-(r.get("rating_count") or 0), -(r.get("rating") or 0)))
    elif p["sort"] == "distance":
        enriched.sort(key=lambda r: (r.get("km") if r.get("km") is not None else 1e9,
                                     -(r.get("rating") or 0)))
    elif p["sort"] == "value":
        enriched.sort(key=lambda r: (-(r.get("value") or 0)))
    return enriched[:LIMIT], origin_label


_geo_ctx = {}


def geo_travel_minutes(olat, olon, dlat, dlon, travel="driving"):
    g = _geo_ctx.get("g")
    if g is None:
        g = Geo(get_db())
        _geo_ctx["g"] = g
    return g.travel_minutes(olat, olon, dlat, dlon, travel)


async def send_result(chat_id, context, items, origin_label, header):
    text = [esc(header)]
    for it in items:
        text.append(fmt_line(it, minutes=it.get("minutes"), origin_label=origin_label))
        text.append("")
    kb = result_keyboard(items)
    try:
        await context.bot.send_message(chat_id, "\n".join(text), reply_markup=kb,
                                       parse_mode="HTML")
    except Exception as e:  # message too long etc.
        await context.bot.send_message(chat_id, f"⚠️ {e}")


def filter_str(p):
    """Human-readable summary of active filters, for result headers."""
    parts = []
    if p["minrating"] > 0:
        parts.append(f"рейтинг ≥ {p['minrating']:g}")
    if p["minrev"] > 0:
        parts.append(f"отзывов ≥ {p['minrev']}")
    if p["stock_only"]:
        parts.append("в наличии")
    return (" · " + " · ".join(parts)) if parts else ""


# ---- radius search (live API call) --------------------------------------
async def run_radius(chat_id, context, message=None, edit_msg=None):
    ud = context.user_data
    db = get_db()
    p = prefs(ud)
    origin_label, olat, olon = resolve_origin(db, chat_id, ud)
    if olat is None:
        text = "Нет точки отсчёта. Пришли геолокацию 📍 (скрепка → Location), или задай дом/работу: /home /work"
        if message:
            await message.reply_text(text)
        else:
            await context.bot.send_message(chat_id, text)
        return
    try:
        raw = await asyncio.to_thread(
            client(context).top_stores, olat, olon, p["radius"], p["sort"], p["minrev"],
            LIMIT * 4, with_stock_only=p["stock_only"])
    except Exception as e:
        await context.bot.send_message(chat_id, f"⚠️ Ошибка запроса: {e}")
        return
    if not raw:
        await context.bot.send_message(
            chat_id, f"В радиусе {p['radius']:.0f} км от {origin_label} ничего не нашлось.")
        return
    # top_stores already sorted/filtered by API-side fields; re-enrich with time
    rows = []
    for it in raw:
        rows.append({"rating": it["rating"], "rating_count": it["rating_count"],
                     "km": it["distance_km"], "in_stock": it["in_stock"],
                     "price": it["price"], "currency": it["price_currency"],
                     "store_name": it["store_name"], "address": it["address"],
                     "item_id": it["item_id"], "store_id": it["store_id"],
                     "lat": it.get("lat"), "lng": it.get("lng")})
    ud["last_items"] = {str(it["item_id"]): it for it in raw}
    items, origin_label2 = enrich_and_sort(rows, p, db, chat_id, ud)
    header = (f"Топ {len(items)} · радиус {p['radius']:.0f} км · от {origin_label2} · "
              f"{SORTS[p['sort']]}{filter_str(p)}"
              f" · {TRAVEL_LABELS[p['travel']]} {TRAVEL_NAMES[p['travel']]}")
    await send_result(chat_id, context, items, origin_label2, header)


# ---- chain search (cached Vienna scan) -----------------------------------
def scan_stale(db):
    ts = db.get_meta("last_scan_ts")
    if not ts:
        return True
    try:
        return time.time() - float(ts) > CHAIN_TTL_S
    except ValueError:
        return True


async def ensure_scan(context):
    if context.bot_data.get("scanning"):
        return
    context.bot_data["scanning"] = True
    context.application.create_task(run_scan(context))


async def run_scan(context):
    db = get_db()
    try:
        def prog(step, total, found):
            if step % 10 == 0 or step == total:
                context.bot_data["scan_progress"] = (step, total, found)

        entries = await asyncio.to_thread(client(context).scan_vienna, prog)
        db.upsert_stores(entries)
        db.set_meta("last_scan_ts", str(time.time()))
        db.set_meta("last_scan_count", str(len(entries)))
    finally:
        context.bot_data["scanning"] = False
    for chat_id, query in db.pop_pending_chain():
        try:
            await chain_search(chat_id, query, context)
        except Exception:
            pass


async def chain_search(chat_id, query, context, reply=None):
    db = get_db()
    ud = context.user_data
    if scan_stale(db):
        db.add_pending_chain(chat_id, query)
        msg = (f"Кэш Вены устарел — сканирую город заново (~3-5 мин). "
               f"Пришлю топ по «{query}», как будет готово.")
        if reply:
            await reply.reply_text(msg)
        else:
            await context.bot.send_message(chat_id, msg)
        await ensure_scan(context)
        return
    rows = db.chain_stores(query)
    if not rows:
        await context.bot.send_message(
            chat_id, f"По запросу «{query}» в Вене ничего не нашлось. "
                     f"Попробуй другое название или /chain.")
        return
    ud["view"] = {"type": "chain", "query": query}
    await send_chain_result(chat_id, query, context)


async def send_chain_result(chat_id, query, context):
    ud = context.user_data
    db = get_db()
    p = prefs(ud)
    rows = db.chain_stores(query)
    items, origin_label = enrich_and_sort(rows, p, db, chat_id, ud)
    ts = db.get_meta("last_scan_ts")
    when = "—"
    if ts:
        when = time.strftime("%H:%M", time.localtime(float(ts)))
    header = (f"🏪 «{query}» по всей Вене ({len(rows)} шт.) · данные {when} · "
              f"{SORTS[p['sort']]}{filter_str(p)}"
              f" · {TRAVEL_LABELS[p['travel']]} {TRAVEL_NAMES[p['travel']]}"
              f"{' · от ' + origin_label if origin_label else ''}")
    await send_result(chat_id, context, items, origin_label, header)


# ---- watches -------------------------------------------------------------
async def add_watch_cb(update, context):
    q = update.callback_query
    await q.answer()
    item_id = q.data.split(":", 1)[1]
    db = get_db()
    chat_id = update.effective_chat.id
    row = db._one("SELECT * FROM stores WHERE item_id=?", (item_id,))
    if row is None:
        ud = context.user_data
        row = (ud.get("last_items") or {}).get(item_id)
    if row is None:
        await q.message.reply_text("Не нашёл магазин — обнови список (🔄).")
        return
    db.add_watch(chat_id, item_id, row.get("store_id"), row.get("store_name"),
                 row.get("address"), row.get("lat"), row.get("lng"))
    if db.has_watch(chat_id, item_id):
        await q.message.reply_text(f"🔔 Уже слежу за «{row.get('store_name')}». "
                                   f"Напишу, как только товар появится.")
    else:
        await q.message.reply_text(f"🔔 Теперь слежу за «{row.get('store_name')}».")


async def watch_job(context):
    db = get_db()
    watches = db.all_watches()
    if not watches:
        return
    c = client(context)
    for w in watches:
        if w["lat"] is None or w["lng"] is None:
            continue
        try:
            state = await asyncio.to_thread(c.item_status, w["item_id"], w["lat"], w["lng"])
        except Exception:
            continue
        if state is None:
            continue
        if w["last_in_stock"] == 0 and state:
            try:
                await context.bot.send_message(
                    w["telegram_id"],
                    f"🟢 «{w['store_name']}» снова в наличии!\n{w['address'] or ''}\n"
                    f"Открывай приложение и резервируй.")
            except Exception:
                pass
        db.set_watch_state(w["id"], state)


# ---- handlers ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    db = get_db()
    db.upsert_user(chat.id, getattr(chat, "username", None), getattr(chat, "first_name", None))
    await update.message.reply_text(
        "👋 Too Good To Go · Вена\n\n"
        "• 🗺️ Топ рядом — пришли геолокацию 📍 (скрепка → Location), только в наличии\n"
        "• 🛒 HOFER — сразу топ HOFER по рейтингу (фильтруй рейтинг в ⚙️)\n"
        "• 🏪 Сеть магазинов — топ по рейтингу по всей Вене (HOFER, SPAR, BIPA…)\n"
        "• 📍 Мои адреса — дом / работа / свои места\n"
        "• ⚙️ Настройки — радиус, сортировка, километры или время\n"
        "• 🔔 Слежка за магазином — уведомление, когда товар появился\n\n"
        "Кидай геолокацию или жми кнопки 👇",
        reply_markup=main_keyboard())


async def help_cmd(update, context):
    await update.message.reply_text(
        "/menu — главное меню\n/chain <название> — топ сети по Вене (напр. /chain HOFER)\n"
        "/home, /work — задать адрес (пришли геолокацию или текст)\n"
        "/places — мои адреса\n/watches — слежки\n/unwatchall — снять все слежки\n"
        "/departures <остановка> — когда придёт U-Bahn/трамвай/автобус (Wiener Linien, реальное время)\n"
        "⚙️ Настройки: радиус, рейтинг ≥ X, отзывы ≥ N, только в наличии, способ передвижения")


async def on_location(update, context):
    loc = update.message.location
    ud = context.user_data
    chat_id = update.effective_chat.id
    db = get_db()
    pending = ud.get("pending")
    if pending == "set_home":
        db.set_home(chat_id, loc.latitude, loc.longitude)
        ud["pending"] = None
        ud["origin"] = "home"
        await update.message.reply_text("🏠 Домашний адрес сохранён.")
        return
    if pending == "set_work":
        db.set_work(chat_id, loc.latitude, loc.longitude)
        ud["pending"] = None
        ud["origin"] = "work"
        await update.message.reply_text("💼 Рабочий адрес сохранён.")
        return
    if pending == "new_place:loc":
        ud["place_temp"] = (loc.latitude, loc.longitude)
        ud["pending"] = "new_place:name"
        await update.message.reply_text("Теперь напиши название места (например «Тренажёрка»).")
        return
    if pending == "new_place:name":
        ud["pending"] = None
        await update.message.reply_text("Сначала напиши название, потом пришли геолокацию.")
        return
    ud["live"] = (loc.latitude, loc.longitude)
    ud["origin"] = "live"
    await update.message.reply_text("🔎 Ищу магазины рядом…")
    await run_radius(chat_id, context)


async def on_text(update, context):
    text = update.message.text.strip()
    ud = context.user_data
    chat_id = update.effective_chat.id
    db = get_db()
    pending = ud.get("pending")
    if pending in ("set_home", "set_work") or pending == "new_place:name":
        # try geocoding the typed address (needs GOOGLE_MAPS_API_KEY)
        g = geo(context)
        res = g.geocode(text)
        if res:
            lat, lng = res
            if pending == "set_home":
                db.set_home(chat_id, lat, lng)
                ud["pending"] = None
                await update.message.reply_text(f"🏠 Дом сохранён: {text}")
            elif pending == "set_work":
                db.set_work(chat_id, lat, lng)
                ud["pending"] = None
                await update.message.reply_text(f"💼 Работа сохранена: {text}")
            else:
                name = text
                db.add_place(chat_id, name, lat, lng)
                ud["pending"] = None
                await update.message.reply_text(f"📍 «{name}» сохранено.")
            return
        await update.message.reply_text(
            "Не смог распознать адрес текстом (нужен GOOGLE_MAPS_API_KEY). "
            "Пришли геолокацию 📍 (скрепка → Location).")
        return
    if text.lower().startswith("/chain"):
        text = text[len("/chain"):].strip()
    if text:
        await chain_search(chat_id, text, context, reply=update.message)


async def on_button(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    ud = context.user_data
    chat_id = update.effective_chat.id
    db = get_db()
    if data == "menu":
        await q.edit_message_text("Главное меню:", reply_markup=main_keyboard())
        return
    if data == "search":
        if ud.get("live"):
            await run_radius(chat_id, context)
        else:
            await q.edit_message_text(
                "Пришли геолокацию 📍 (скрепка → Location) — покажу топ магазинов вокруг.",
                reply_markup=main_keyboard())
        return
    if data == "chain":
        await q.edit_message_text("Выбери сеть или введи название текстом:",
                                  reply_markup=chain_keyboard(db))
        return
    if data == "chain_custom":
        await q.edit_message_text("Напиши название сети текстом, например: HOFER")
        return
    if data.startswith("chain:"):
        query = data.split(":", 1)[1]
        ud["view"] = {"type": "chain", "query": query}
        await q.edit_message_text(f"Ищу «{query}»…")
        await chain_search(chat_id, query, context)
        return
    if data == "places":
        await q.edit_message_text("Мои адреса:", reply_markup=places_keyboard(db, chat_id))
        return
    if data == "addplace":
        ud["pending"] = "new_place:loc"
        await q.edit_message_text(
            "Пришли геолокацию 📍 нового места, потом напиши его название.")
        return
    if data.startswith("delplace:"):
        db.del_place(int(data.split(":", 1)[1]), chat_id)
        await q.edit_message_text("Мои адреса:", reply_markup=places_keyboard(db, chat_id))
        return
    if data == "watches":
        mine = db.watches(chat_id)
        txt = ("Твои слежки:" if mine else "Слежек нет. Нажми 🔔 на магазине в списке.")
        await q.edit_message_text(txt, reply_markup=watches_keyboard(db, chat_id))
        return
    if data.startswith("unwatch:"):
        db.unwatch(int(data.split(":", 1)[1]), chat_id)
        mine = db.watches(chat_id)
        await q.edit_message_text("Твои слежки:",
                                  reply_markup=watches_keyboard(db, chat_id) if mine else None)
        return
    if data == "unwatchall":
        db.unwatch_all(chat_id)
        await q.edit_message_text("Все слежки сняты.", reply_markup=main_keyboard())
        return
    if data == "settings":
        u = db.get_user(chat_id) or {}
        await q.edit_message_text("Настройки:", reply_markup=settings_keyboard(
            prefs(ud), bool(ud.get("live")), u.get("home_lat") is not None,
            u.get("work_lat") is not None))
        return
    if data == "departures":
        await departures_button(update, context)
        return
    if data.startswith("radius:"):
        ud["radius"] = float(data.split(":", 1)[1])
        await settings_save_and_show(q, context, ud, chat_id, db)
        return
    if data.startswith("sort:"):
        ud["sort"] = data.split(":", 1)[1]
        await settings_save_and_show(q, context, ud, chat_id, db)
        return
    if data.startswith("minrev:"):
        ud["minrev"] = int(data.split(":", 1)[1])
        await settings_save_and_show(q, context, ud, chat_id, db)
        return
    if data.startswith("minrating:"):
        ud["minrating"] = float(data.split(":", 1)[1])
        await settings_save_and_show(q, context, ud, chat_id, db)
        return
    if data.startswith("stock:"):
        ud["stock_only"] = data.split(":", 1)[1] == "1"
        await settings_save_and_show(q, context, ud, chat_id, db)
        return
    if data.startswith("travel:"):
        ud["travel"] = data.split(":", 1)[1]
        await settings_save_and_show(q, context, ud, chat_id, db)
        return
    if data.startswith("origin:"):
        ud["origin"] = data.split(":", 1)[1]
        await settings_save_and_show(q, context, ud, chat_id, db)
        return
    if data == "refresh":
        view = ud.get("view")
        if view and view.get("type") == "chain":
            await chain_search(chat_id, view["query"], context)
        else:
            await run_radius(chat_id, context)
        return
    if data.startswith("watch:"):
        await add_watch_cb(update, context)
        return


async def settings_save_and_show(q, context, ud, chat_id, db):
    u = db.get_user(chat_id) or {}
    await q.edit_message_text("Настройки:", reply_markup=settings_keyboard(
        prefs(ud), bool(ud.get("live")), u.get("home_lat") is not None,
        u.get("work_lat") is not None))


async def set_home_cmd(update, context):
    context.user_data["pending"] = "set_home"
    await update.message.reply_text("Пришли геолокацию дома 📍 (или адрес текстом).")


async def set_work_cmd(update, context):
    context.user_data["pending"] = "set_work"
    await update.message.reply_text("Пришли геолокацию работы 📍 (или адрес текстом).")


async def watches_cmd(update, context):
    db = get_db()
    chat_id = update.effective_chat.id
    mine = db.watches(chat_id)
    txt = "Твои слежки:" if mine else "Слежек нет."
    await update.message.reply_text(txt, reply_markup=watches_keyboard(db, chat_id))


async def unwatchall_cmd(update, context):
    get_db().unwatch_all(update.effective_chat.id)
    await update.message.reply_text("Все слежки сняты.")


# ---- Wiener Linien departures -------------------------------------------
async def departures_cmd(update, context):
    args = update.message.text.split(None, 1)
    name = args[1].strip() if len(args) > 1 else ""
    chat_id = update.effective_chat.id
    if name:
        hits = wl.find_stops(name)
        if not hits:
            await update.message.reply_text(f"Остановка «{name}» не найдена. Попробуй другое написание.")
            return
        stop = hits[0]
        await update.message.reply_text("🔎 Смотрю ближайшие отправления…")
        await send_departures(chat_id, context, stop)
        return
    live = context.user_data.get("live")
    if not live:
        await update.message.reply_text(
            "Укажи остановку: /departures Rochusgasse — или пришли геолокацию 📍, и покажу ближайшую.")
        return
    stop = wl.nearest_stop(live[0], live[1])
    await send_departures(chat_id, context, stop)


async def departures_button(update, context):
    q = update.callback_query
    await q.answer()
    live = context.user_data.get("live")
    if not live:
        await q.edit_message_text(
            "Пришли геолокацию 📍 — покажу ближайшую остановку и её отправления.\n"
            "Или напиши /departures <название>.")
        return
    stop = wl.nearest_stop(live[0], live[1])
    await q.edit_message_text("🔎 Ближайшая остановка: " + stop["n"] + "…")
    await send_departures(q.message.chat_id, context, stop)


async def send_departures(chat_id, context, stop):
    if stop is None:
        await context.bot.send_message(chat_id, "Не нашёл остановку рядом.")
        return
    try:
        deps = await asyncio.to_thread(wl.departures_parallel, stop)
    except Exception as e:
        await context.bot.send_message(chat_id, f"⚠️ Ошибка запроса к Wiener Linien: {e}")
        return
    if not deps:
        await context.bot.send_message(chat_id, f"По «{stop['n']}» сейчас нет данных. Попробуй позже.")
        return
    lines = [f"🚏 {stop['n']} — ближайшие отправления:"]
    for d in deps:
        rt = " 🟢" if d["realtime"] else ""
        lines.append(f"{d['line']} → {d['towards'] or '—'} — через {d['countdown']} мин{rt}")
    await context.bot.send_message(chat_id, "\n".join(lines))


# ---- main ----------------------------------------------------------------
def _start_health_server():
    """Tiny HTTP server bound to $PORT so Railway's healthcheck sees a live
    service instead of killing the (portless, long-polling) bot. Runs only
    when $PORT is set (Railway injects it); locally it stays off."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    port = int(os.environ.get("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"healthcheck on :{port}", flush=True)


def main():
    if not TOKEN:
        raise SystemExit("Set TGTG_BOT_TOKEN (or put it in .env) — create a bot via @BotFather.")
    if os.environ.get("PORT"):  # Railway/cloud: keep the healthcheck alive
        _start_health_server()
    keep_alive.start()  # self-ping so Render's free tier never sleeps
    app = (ApplicationBuilder().token(TOKEN)
           .get_updates_read_timeout(20)
           .get_updates_connect_timeout(15)
           .get_updates_write_timeout(20)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", lambda u, c: u.message.reply_text("Главное меню:", reply_markup=main_keyboard())))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("chain", lambda u, c: on_text(u, c)))
    app.add_handler(CommandHandler("home", set_home_cmd))
    app.add_handler(CommandHandler("work", set_work_cmd))
    app.add_handler(CommandHandler("places", lambda u, c: u.message.reply_text("Мои адреса:", reply_markup=places_keyboard(get_db(), u.effective_chat.id))))
    app.add_handler(CommandHandler("watches", watches_cmd))
    app.add_handler(CommandHandler("unwatchall", unwatchall_cmd))
    app.add_handler(CommandHandler("departures", departures_cmd))
    app.add_handler(CommandHandler("next", departures_cmd))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    app.job_queue.run_repeating(watch_job, interval=60, first=10)

    async def on_error(update, context):
        # network hiccups (TimedOut etc.) are retried by PTB; just log, don't spam a traceback
        logging.getLogger(__name__).warning("poll error: %s", context.error)

    app.add_error_handler(on_error)
    print("bot started.", flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
