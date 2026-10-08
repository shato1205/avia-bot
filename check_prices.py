"""Бот дешёвых авиабилетов: команды в Telegram + проверка цен через Travelpayouts.

Два режима:
- `python check_prices.py` — один проход (для GitHub Actions по расписанию);
- `python check_prices.py --serve` — работает постоянно на сервере и отвечает
  на сообщения сразу.
За проход бот читает новые сообщения в Telegram, отвечает на команды меню и,
если с прошлой проверки прошло check_every_minutes, проверяет цены и шлёт алерты.
Настройки, история цен и состояние хранятся в data/*.json.
"""
import json
import os
import re
import statistics
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.json"
SETTINGS = ROOT / "data" / "settings.json"
STATE = ROOT / "data" / "state.json"
HISTORY = ROOT / "data" / "history.json"
SENT = ROOT / "data" / "sent.json"

PRICES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
PLACES_URL = "https://autocomplete.travelpayouts.com/places2"

BTN_HOME = "🏠 Меню"
BTN_PRICES = "💰 Цены сейчас"
MENU = [[BTN_HOME, BTN_PRICES]]
# Кнопки из старой версии меню: могут остаться на клавиатуре до первого ответа бота.
OLD_BUTTONS = {"🛫 Откуда лечу", "➕ Добавить направление", "📋 Мои направления",
               "➖ Удалить направление", "🎯 Подбор по датам"}


def load(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def http_get_json(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fmt_price(n):
    return f"{n:,}".replace(",", " ")


# ---------- Telegram ----------

class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id)

    def call(self, method, http_timeout=30, **params):
        body = urllib.parse.urlencode(
            {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
             for k, v in params.items()}
        ).encode()
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        with urllib.request.urlopen(url, data=body, timeout=http_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _markup(buttons):
        """buttons: [[(текст, callback_data), ...], ...] → кнопки под сообщением."""
        if buttons is None:
            return {"keyboard": MENU, "resize_keyboard": True}
        return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                    for row in buttons]}

    def send(self, text, buttons=None):
        self.call("sendMessage", chat_id=self.chat_id, text=text,
                  reply_markup=self._markup(buttons), disable_web_page_preview="true")

    def edit(self, message_id, text, buttons):
        try:
            self.call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text,
                      reply_markup=self._markup(buttons), disable_web_page_preview="true")
        except urllib.error.HTTPError as e:
            if e.code != 400:  # 400 — текст не изменился, это нормально
                raise

    def answer(self, callback_id, text=""):
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def updates(self, offset, wait=0):
        """wait > 0 — long polling: ждём новых сообщений до wait секунд."""
        return self.call("getUpdates", http_timeout=wait + 15,
                         offset=offset, timeout=wait).get("result", [])


# ---------- Справочник городов ----------

def find_place(name):
    """Ищет город по названию. Возвращает dict(code, name, country) или строку-ошибку."""
    params = {"term": name, "locale": "ru", "types[]": ["city", "country"]}
    url = PLACES_URL + "?" + urllib.parse.urlencode(params, doseq=True)
    try:
        places = http_get_json(url)
    except Exception as e:  # noqa: BLE001
        print(f"places error: {e}", file=sys.stderr)
        return "Не получилось найти город, попробуй ещё раз позже."
    for p in places:
        if p.get("type") == "city" and p.get("code"):
            return {"code": p["code"], "name": p.get("name", p["code"]),
                    "country": p.get("country_name", "")}
    if places and places[0].get("type") == "country":
        return f"«{name}» это страна. Напиши город, например Пхукет или Бангкок."
    return f"Не нашёл город «{name}». Проверь написание."


# ---------- Цены ----------

def months(n):
    """Текущий месяц и n-1 следующих в формате YYYY-MM."""
    d = date.today().replace(day=1)
    out = []
    for _ in range(n):
        out.append(d.strftime("%Y-%m"))
        d = (d + timedelta(days=32)).replace(day=1)
    return out


def fetch_cheapest(token, origin, destination, month, return_at=None, one_way=True, direct=False):
    """month — YYYY-MM или YYYY-MM-DD. При one_way=False цена за туда-обратно."""
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": month,
        "currency": "rub",
        "sorting": "price",
        "one_way": "true" if one_way else "false",
        "direct": "true" if direct else "false",
        "limit": 5,
        "token": token,
    }
    if return_at:
        params["return_at"] = return_at
    data = http_get_json(PRICES_URL + "?" + urllib.parse.urlencode(params))
    if not data.get("success"):
        print(f"API error {origin}->{destination} {month}: {data}", file=sys.stderr)
        return []
    return data.get("data", [])


def route_offers(token, origin, route, cfg):
    offers = []
    for month in months(cfg["months_ahead"]):
        offers += fetch_cheapest(token, origin, route["destination"], month)
    return offers


def find_deals(route, offers, history, cfg, now):
    """Возвращает (offer, причина) для предложений, о которых стоит сообщить."""
    cutoff = (now - timedelta(days=cfg["history_days"])).isoformat()
    past = [p["price"] for p in history if p["ts"] >= cutoff]
    median = statistics.median(past) if len(past) >= 5 else None

    deals = []
    for o in offers:
        price = o["price"]
        reasons = []
        if route.get("max_price") and price <= route["max_price"]:
            reasons.append(f"ниже порога {fmt_price(route['max_price'])} ₽")
        if median and price <= median * (1 - cfg["drop_percent"] / 100):
            drop = round(100 * (1 - price / median))
            reasons.append(f"на {drop}% дешевле обычного")
        if reasons:
            deals.append((o, ", ".join(reasons)))
    return deals


def filter_offers(token, origin, f):
    return fetch_cheapest(token, origin, f["destination"], f["depart"], f.get("return"),
                          one_way=not f.get("round_trip"), direct=f.get("direct", False))


def offer_line(offer, origin_name, route):
    dep = offer["departure_at"][:10]
    stops = offer.get("transfers", 0) + offer.get("return_transfers", 0)
    stops_txt = "прямой" if stops == 0 else f"пересадок: {stops}"
    ret = f", обратно {offer['return_at'][:10]}" if offer.get("return_at") else ""
    link = "https://www.aviasales.ru" + offer.get("link", "")
    return f"{origin_name} → {route['city']}, вылет {dep}{ret}, {stops_txt}\n{link}"


def format_alert(route, offer, reason, origin_name):
    return (
        f"🔥 ГОША СРОЧНО {route['name']} {fmt_price(offer['price'])} ₽\n"
        f"{offer_line(offer, origin_name, route)}\n"
        f"Почему: {reason}"
    )


def check_prices(tg, token, cfg, settings, history, sent, now):
    alerts = 0
    for route in settings["routes"]:
        origin = route["origin"]
        key = f"{origin}-{route['destination']}"
        offers = route_offers(token, origin, route, cfg)
        if not offers:
            continue

        route_history = history.get(key, [])
        deals = sorted(find_deals(route, offers, route_history, cfg, now), key=lambda d: d[0]["price"])
        # Одно сообщение на направление за запуск: самый дешёвый подходящий билет.
        for o, reason in deals[:1]:
            # Не повторяем один и тот же билет по той же цене чаще, чем раз в realert_hours.
            sent_key = f"{key}|{o['departure_at'][:10]}|{o['price']}"
            last = sent.get(sent_key)
            if last and now - datetime.fromisoformat(last) < timedelta(hours=cfg["realert_hours"]):
                continue
            tg.send(format_alert(route, o, reason, route["origin_name"]))
            sent[sent_key] = now.isoformat()
            alerts += 1

        cheapest = min(o["price"] for o in offers)
        route_history.append({"ts": now.isoformat(), "price": cheapest})
        cutoff = (now - timedelta(days=90)).isoformat()
        history[key] = [p for p in route_history if p["ts"] >= cutoff]
        print(f"{key}: минимум {cheapest} ₽")

    for f in list(settings.get("filters", [])):
        if filter_expired(f, now.date()):
            settings["filters"].remove(f)
            tg.send(f"⌛ Даты прошли, убрал подбор: {filter_text(f)}")
            continue
        origin = f["origin"]
        offers = [o for o in filter_offers(token, origin, f) if o["price"] <= f["max_price"]]
        if not offers:
            continue
        o = min(offers, key=lambda x: x["price"])
        sent_key = f"F|{origin}-{f['destination']}|{o['departure_at'][:10]}|{o.get('return_at', '')[:10]}|{o['price']}"
        last = sent.get(sent_key)
        if last and now - datetime.fromisoformat(last) < timedelta(hours=cfg["realert_hours"]):
            continue
        kind = " туда-обратно" if f.get("round_trip") else ""
        tg.send(f"🎯 ГОША СРОЧНО {f['name']} {fmt_price(o['price'])} ₽{kind}\n"
                f"{offer_line(o, f['origin_name'], f)}\n"
                f"Подбор: {filter_text(f)}")
        sent[sent_key] = now.isoformat()
        alerts += 1
    print(f"Отправлено алертов: {alerts}")


def ensure_origins(settings):
    """Дополняет старые настройки: город вылета и id у каждого направления, список городов."""
    origins = settings.setdefault("origins", [])
    known = {o["code"] for o in origins}
    for item in settings["routes"] + settings.setdefault("filters", []):
        item.setdefault("origin", settings["origin"])
        item.setdefault("origin_name", settings["origin_name"])
        item.setdefault("id", uuid.uuid4().hex[:8])
        if item["origin"] not in known:
            origins.append({"code": item["origin"], "name": item["origin_name"]})
            known.add(item["origin"])
    if settings["origin"] not in known:
        origins.insert(0, {"code": settings["origin"], "name": settings["origin_name"]})


def grouped_items(settings):
    """[(origin_name, [(kind, item), ...]), ...] — по городам вылета, текущий первым.

    Порядок общий для списка и для удаления по номеру.
    """
    groups = {}
    order = [settings["origin"]]
    for kind, items in (("route", settings["routes"]), ("filter", settings.get("filters", []))):
        for item in items:
            if item["origin"] not in order:
                order.append(item["origin"])
            groups.setdefault(item["origin"], (item["origin_name"], []))[1].append((kind, item))
    return [groups[o] for o in order if o in groups]


def prices_now(token, cfg, settings, origin=None):
    lines = [f"💰 Самые дешёвые билеты на {cfg['months_ahead']} мес.:"]
    groups = grouped_items(settings)
    if origin:
        groups = [g for g in groups if g[1][0][1]["origin"] == origin]
    if not groups:
        return "Направлений пока нет, добавь их в меню 🏠"
    for origin_name, pairs in groups:
        lines.append(f"\n🛫 Вылет: {origin_name}")
        for kind, item in pairs:
            if kind == "route":
                offers, title = route_offers(token, item["origin"], item, cfg), item["name"]
            else:
                offers, title = filter_offers(token, item["origin"], item), "🎯 " + filter_text(item)
            if not offers:
                lines.append(f"{title}: билетов не нашёл")
                continue
            best = min(offers, key=lambda o: o["price"])
            lines.append(f"{title}: {fmt_price(best['price'])} ₽\n"
                         f"{offer_line(best, origin_name, item)}")
    return "\n".join(lines)


# ---------- Подбор по датам ----------

MONTH_FORMS = {
    1: "январь января январе янв", 2: "февраль февраля феврале фев",
    3: "март марта марте мар", 4: "апрель апреля апреле апр", 5: "май мая мае",
    6: "июнь июня июне июн", 7: "июль июля июле июл", 8: "август августа августе авг",
    9: "сентябрь сентября сентябре сен сент", 10: "октябрь октября октябре окт",
    11: "ноябрь ноября ноябре ноя", 12: "декабрь декабря декабре дек",
}
MONTHS = {form: num for num, forms in MONTH_FORMS.items() for form in forms.split()}
MONTH_IN = ["", "январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе",
            "сентябре", "октябре", "ноябре", "декабре"]


def _future_date(day, month, year, today):
    d = date(year or today.year, month, day)
    if not year and d < today:
        d = d.replace(year=d.year + 1)
    return d


def _future_month(month, today):
    year = today.year + (1 if month < today.month else 0)
    return f"{year}-{month:02d}"


def parse_filter(text, today):
    """«Пхукет 15.12-25.12 до 60000 прямой» → (город, dict фильтра) или (None, ошибка)."""
    low = " " + text.lower() + " "
    direct = bool(re.search(r"прям|без пересад", low))
    one_way_word = bool(re.search(r"в одну сторону|только туда", low))
    low = re.sub(r"прям\w*|без пересадок|в одну сторону|только туда|туда[- ]обратно", " ", low)

    dates = []
    for m in re.finditer(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", low):
        year = int(m.group(3)) if m.group(3) else None
        if year and year < 100:
            year += 2000
        try:
            dates.append(_future_date(int(m.group(1)), int(m.group(2)), year, today))
        except ValueError:
            return None, f"Не понял дату «{m.group(0)}». Пиши так: 15.12"
    low = re.sub(r"\d{1,2}\.\d{1,2}(?:\.\d{2,4})?", " ", low)

    months = []
    for word in re.findall(r"[а-яё]+", low):
        if word in MONTHS:
            months.append(MONTHS[word])
            low = re.sub(rf"\b{word}\b", " ", low, count=1)

    city, price = split_price(re.sub(r"\bдо\b|\s[-–—]+\s", " ", low))
    city = city.strip(" ,")
    if not city:
        return None, "Не вижу город. Пример: Пхукет 15.12-25.12 до 60000"
    if not price:
        return None, "Укажи максимальную цену, например: Пхукет 15.12-25.12 до 60000"

    f = {"max_price": price, "direct": direct}
    if dates:
        f["depart"] = dates[0].isoformat()
        if len(dates) > 1:
            if dates[1] < dates[0]:
                return None, "Дата обратно получилась раньше даты вылета. Проверь даты."
            f["return"] = dates[1].isoformat()
        f["round_trip"] = len(dates) > 1
    elif months:
        f["depart"] = _future_month(months[0], today)
        if len(months) > 1:
            f["return"] = _future_month(months[1], today)
        f["round_trip"] = not one_way_word
    else:
        return None, "Укажи даты или месяц. Пример: Пхукет 15.12-25.12 до 60000"
    return city, f


def filter_text(f):
    def fmt(d):
        if len(d) == 7:
            return "в " + MONTH_IN[int(d[5:])]
        return f"{d[8:]}.{d[5:7]}"
    dates = f"вылет {fmt(f['depart'])}"
    if f.get("return"):
        dates += f", обратно {fmt(f['return'])}"
    elif f.get("round_trip"):
        dates += ", туда-обратно"
    direct = ", прямой" if f.get("direct") else ""
    return f"{f['name']}: {dates}{direct}, до {fmt_price(f['max_price'])} ₽"


def filter_expired(f, today):
    d = f["depart"]
    if len(d) == 7:
        return d < today.strftime("%Y-%m")
    return d < today.isoformat()


def place_name(place):
    country = place["country"].upper()
    return f"{country} ({place['name']})" if country else place["name"].upper()


def split_items(text):
    """Несколько городов через запятую, «;», «и» или с новой строки."""
    parts = re.split(r"[,;\n]+|\s+и\s+", text)
    return [p.strip() for p in parts if p.strip()]


def add_filter(settings, text):
    # Один набор дат и цены на все города: «Пхукет, Бангкок 15.12-25.12 до 60000».
    cities, f = parse_filter(text, date.today())
    if cities is None:
        return f, False
    replies, changed = [], False
    for city in split_items(cities):
        place = find_place(city)
        if isinstance(place, str):
            replies.append("❌ " + place)
            continue
        item = dict(f, name=place_name(place), city=place["name"], destination=place["code"],
                    origin=settings["origin"], origin_name=settings["origin_name"],
                    id=uuid.uuid4().hex[:8])
        settings.setdefault("filters", []).append(item)
        replies.append(f"✅ Добавил подбор: {filter_text(item)}")
        changed = True
    if changed:
        replies.append("Сообщу, как только найду такой билет.")
    return "\n".join(replies), changed


def split_price(text):
    """«Пхукет 20000» / «Пхукет 20к» → ("Пхукет", 20000)."""
    m = re.match(r"^(.*?)[\s,]+(\d[\d\s]*)\s*(к|k|тыс\.?)?\s*(₽|р|руб\.?)?$", text.strip(), re.I)
    if not m:
        return text.strip(), None
    price = int(m.group(2).replace(" ", ""))
    if m.group(3):
        price *= 1000
    return m.group(1).strip(), price


def select_origin(settings, code):
    for o in settings["origins"]:
        if o["code"] == code:
            settings["origin"], settings["origin_name"] = o["code"], o["name"]
            return True
    return False


def set_origin(settings, text):
    place = find_place(text)
    if isinstance(place, str):
        return place, False
    if not any(o["code"] == place["code"] for o in settings["origins"]):
        settings["origins"].append({"code": place["code"], "name": place["name"]})
    select_origin(settings, place["code"])
    return f"✅ Город вылета {place['name']} добавлен.", False


def add_route(settings, text):
    """«Пхукет 20000, Бали 35к» или «Пхукет, Бали, Дубай 30000» (одна цена на всех)."""
    items = [split_price(t) for t in split_items(text)]
    priced = [p for _, p in items if p]
    if len(priced) == 1 and items[-1][1]:
        items = [(c, items[-1][1]) for c, _ in items]
    replies, changed = [], False
    for city, price in items:
        place = find_place(city)
        if isinstance(place, str):
            replies.append("❌ " + place)
            continue
        limit = (f"сообщу, когда билет будет дешевле {fmt_price(price)} ₽" if price
                 else "сообщу о резком падении цены")
        existing = [r for r in settings["routes"]
                    if r["destination"] == place["code"] and r["origin"] == settings["origin"]]
        if existing:
            existing[0]["max_price"] = price
            replies.append(f"✅ {existing[0]['name']}: теперь {limit}.")
        else:
            name = place_name(place)
            settings["routes"].append({"name": name, "city": place["name"],
                                       "destination": place["code"], "max_price": price,
                                       "origin": settings["origin"],
                                       "origin_name": settings["origin_name"],
                                       "id": uuid.uuid4().hex[:8]})
            replies.append(f"✅ Добавил {name}: {limit}.")
        changed = True
    return "\n".join(replies) or "Напиши город, например: Пхукет 20000", changed


HELP = (
    "Я слежу за ценами на авиабилеты и пишу, когда находится дешёвый билет.\n\n"
    "Всё настраивается кнопками в меню 🏠. А ещё можно писать одной строкой:\n"
    "• откуда Казань\n"
    "• добавить Пхукет 20000, Бали 35000\n"
    "• подбор Пхукет 15.12-25.12 до 60000\n"
    "Новые направления добавляются в город вылета, который открыт в меню."
)
SERVE = "--serve" in sys.argv
if not SERVE:
    HELP += "\n\nЯ отвечаю не мгновенно, а при очередной проверке (раз в ~30 минут)."

ADD_PROMPT = ("Напиши город и максимальную цену:\n"
              "• Пхукет 20000\n"
              "• Пхукет 20000, Бали 35000 — у каждого своя цена\n"
              "• Пхукет, Бангкок, Нячанг 25000 — одна цена на всех\n"
              "Без цены сообщу только о резком падении.")
FILTER_PROMPT = ("Напиши город, даты и максимальную цену за билет:\n"
                 "• Пхукет 15.12-25.12 до 60000 — туда-обратно\n"
                 "• Стамбул 20.11 до 8000 — в одну сторону\n"
                 "• Бали декабрь до 70000 — туда-обратно, вылет в декабре\n"
                 "• Пхукет, Бангкок 15.12-25.12 до 60000 — несколько городов\n"
                 "Добавь «прямой», если нужны рейсы без пересадок.")


# ---------- Экраны меню ----------

def k(n):
    return f"{round(n / 1000)}к" if n >= 1000 else str(n)


def items_of(settings, code):
    return ([("route", r) for r in settings["routes"] if r["origin"] == code]
            + [("filter", f) for f in settings.get("filters", []) if f["origin"] == code])


def origin_name(settings, code):
    return next((o["name"] for o in settings["origins"] if o["code"] == code), code)


def home_screen(settings):
    lines = ["✈️ Главное меню\n", "Выбери город вылета, чтобы посмотреть и настроить направления:"]
    buttons = []
    for o in settings["origins"]:
        items = items_of(settings, o["code"])
        n_routes = sum(1 for kind, _ in items if kind == "route")
        n_filters = len(items) - n_routes
        lines.append(f"🛫 {o['name']}: направлений {n_routes}, подборов {n_filters}")
        buttons.append([(f"🛫 {o['name']}", f"o:{o['code']}")])
    buttons.append([("➕ Добавить город вылета", "neworigin")])
    buttons.append([("💰 Цены по всем городам", "pa")])
    return "\n".join(lines), buttons


def origin_screen(settings, code):
    name = origin_name(settings, code)
    items = items_of(settings, code)
    routes = [r for kind, r in items if kind == "route"]
    filters = [f for kind, f in items if kind == "filter"]
    lines = [f"🛫 Вылет: {name}\n", "📍 Направления — сообщу, когда билет подешевеет:"]
    for r in routes:
        limit = f"до {fmt_price(r['max_price'])} ₽" if r.get("max_price") else "резкие падения"
        lines.append(f"• {r['name']} — {limit}")
    if not routes:
        lines.append("пока нет")
    lines.append("\n🎯 Подборы по датам:")
    for f in filters:
        lines.append("• " + filter_text(f))
    if not filters:
        lines.append("пока нет")
    buttons = [
        [("➕ Направление", f"a:{code}"), ("🎯 Подбор по датам", f"f:{code}")],
        [("💰 Цены сейчас", f"p:{code}"), ("🗑 Удалить", f"d:{code}")],
        [("⬅️ Все города", "home")],
    ]
    return "\n".join(lines), buttons


def delete_screen(settings, code):
    name = origin_name(settings, code)
    buttons = []
    for kind, item in items_of(settings, code):
        if kind == "route":
            label = f"❌ {item['city']}" + (f" до {k(item['max_price'])}" if item.get("max_price") else "")
        else:
            dates = item["depart"][8:] + "." + item["depart"][5:7] if len(item["depart"]) == 10 \
                else "в " + MONTH_IN[int(item["depart"][5:])]
            if item.get("return") and len(item["return"]) == 10:
                dates += f"–{item['return'][8:]}.{item['return'][5:7]}"
            label = f"❌ 🎯 {item['city']} {dates} до {k(item['max_price'])}"
        buttons.append([(label[:60], f"r:{item['id']}")])
    buttons.append([(f"🗑 Удалить город {name} целиком", f"x:{code}")])
    buttons.append([("⬅️ Назад", f"o:{code}")])
    return f"🗑 Вылет: {name}\nНажми на то, что нужно удалить:", buttons


def back_button(settings, code):
    if code:
        return [[(f"⬅️ {origin_name(settings, code)}", f"o:{code}")]]
    return [[("⬅️ В меню", "home")]]


# ---------- Обработка сообщений и кнопок ----------

def handle(text, state, settings, tg, token, cfg):
    """Обрабатывает текстовое сообщение. Возвращает True, если появились новые направления."""
    text = text.strip()
    low = text.lower()
    awaiting = state.pop("awaiting", None)

    if low in ("/start", "/help", "помощь"):
        tg.send(HELP)
        tg.send(*home_screen(settings))
        return False
    if low in ("меню", "/menu") or text == BTN_HOME or text in OLD_BUTTONS:
        tg.send(*home_screen(settings))
        return False
    if text == BTN_PRICES:
        tg.send("🔎 Ищу цены…")
        tg.send(prices_now(token, cfg, settings), back_button(settings, None))
        return False

    for prefix, action in (("откуда ", "origin"), ("добавить ", "add"), ("подбор ", "filter")):
        if low.startswith(prefix):
            awaiting, text = action, text[len(prefix):]
            break

    actions = {"origin": set_origin, "add": add_route, "filter": add_filter}
    if awaiting in actions:
        reply, changed = actions[awaiting](settings, text)
        tg.send(reply)
        if "✅" in reply:
            tg.send(*origin_screen(settings, settings["origin"]))
        return changed

    tg.send("Не понял 🤔 Открой меню кнопкой «🏠 Меню» внизу.")
    return False


def handle_button(data, message_id, state, settings, tg, token, cfg):
    """Обрабатывает нажатие кнопки под сообщением."""
    state.pop("awaiting", None)
    cmd, _, arg = data.partition(":")
    if cmd == "home":
        tg.edit(message_id, *home_screen(settings))
    elif cmd == "o" and select_origin(settings, arg):
        tg.edit(message_id, *origin_screen(settings, arg))
    elif cmd == "a" and select_origin(settings, arg):
        state["awaiting"] = "add"
        tg.send(f"🛫 Вылет: {settings['origin_name']}\n" + ADD_PROMPT, back_button(settings, arg))
    elif cmd == "f" and select_origin(settings, arg):
        state["awaiting"] = "filter"
        tg.send(f"🛫 Вылет: {settings['origin_name']}\n" + FILTER_PROMPT, back_button(settings, arg))
    elif cmd == "neworigin":
        state["awaiting"] = "origin"
        tg.send("Напиши город, откуда хочешь улетать, например: Казань", back_button(settings, None))
    elif cmd in ("p", "pa"):
        tg.send("🔎 Ищу цены…")
        tg.send(prices_now(token, cfg, settings, arg or None), back_button(settings, arg or None))
    elif cmd == "d":
        tg.edit(message_id, *delete_screen(settings, arg))
    elif cmd == "r":
        for key in ("routes", "filters"):
            for item in settings[key]:
                if item["id"] == arg:
                    settings[key].remove(item)
                    tg.edit(message_id, *delete_screen(settings, item["origin"]))
                    return
        tg.edit(message_id, *home_screen(settings))
    elif cmd == "x":
        settings["routes"] = [r for r in settings["routes"] if r["origin"] != arg]
        settings["filters"] = [f for f in settings["filters"] if f["origin"] != arg]
        settings["origins"] = [o for o in settings["origins"] if o["code"] != arg]
        if settings["origin"] == arg and settings["origins"]:
            select_origin(settings, settings["origins"][0]["code"])
        tg.edit(message_id, *home_screen(settings))
    else:
        tg.edit(message_id, *home_screen(settings))


def process_messages(tg, token, cfg, settings, state, wait=0):
    changed = False
    for upd in tg.updates(state.get("offset", 0), wait):
        state["offset"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        msg = (cq or {}).get("message") or upd.get("message") or {}
        # Слушаемся только владельца бота.
        if str(msg.get("chat", {}).get("id")) != tg.chat_id:
            continue
        try:
            if cq:
                tg.answer(cq["id"])
                handle_button(cq.get("data", ""), msg["message_id"], state, settings, tg, token, cfg)
            elif "text" in msg:
                changed |= handle(msg["text"], state, settings, tg, token, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"handle error: {e}", file=sys.stderr)
            tg.send("Что-то пошло не так, попробуй ещё раз.")
    return changed


class Bot:
    def __init__(self):
        self.token = os.environ["TRAVELPAYOUTS_TOKEN"]
        self.tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
        self.cfg = load(CONFIG, {})
        cfg = self.cfg
        self.settings = load(SETTINGS, None) or {
            "origin": cfg["origin"],
            "origin_name": cfg.get("origin_name", cfg["origin"]),
            "routes": [dict(r, city=r.get("city", r["destination"])) for r in cfg["routes"]],
        }
        ensure_origins(self.settings)
        self.state = load(STATE, {})
        self.history = load(HISTORY, {})
        self.sent = load(SENT, {})

    def step(self, wait=0):
        """Один проход: сообщения, затем (если пора) проверка цен, затем сохранение."""
        changed = process_messages(self.tg, self.token, self.cfg, self.settings, self.state, wait)
        now = datetime.now(timezone.utc)
        last = self.state.get("last_check")
        due = not last or now - datetime.fromisoformat(last) >= timedelta(
            minutes=self.cfg["check_every_minutes"] - 5)
        if due or changed:
            check_prices(self.tg, self.token, self.cfg, self.settings, self.history, self.sent, now)
            self.state["last_check"] = now.isoformat()
            week_ago = now - timedelta(days=7)
            self.sent = {k: v for k, v in self.sent.items() if datetime.fromisoformat(v) >= week_ago}
        save(SETTINGS, self.settings)
        save(STATE, self.state)
        save(HISTORY, self.history)
        save(SENT, self.sent)


def main():
    bot = Bot()
    if not SERVE:
        bot.step()
        return
    print("Бот запущен и ждёт сообщений", flush=True)
    while True:
        try:
            bot.step(wait=50)
        except Exception as e:  # noqa: BLE001
            print(f"loop error: {e}", file=sys.stderr, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
