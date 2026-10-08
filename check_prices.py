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

MSK = timezone(timedelta(hours=3))
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
        offers += fetch_cheapest(token, origin, route["destination"], month,
                                 direct=route.get("direct", False))
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


def alert_buttons(item):
    return [[("⚙️ Настроить", f"i:{item['id']}"), ("⏸ Пауза", f"iz:{item['id']}")]]


def remember_price(item, offers, now):
    if offers:
        item["last_price"] = min(o["price"] for o in offers)
        item["last_ts"] = now.isoformat()


def check_prices(tg, token, cfg, settings, history, sent, now):
    alerts = 0
    for route in settings["routes"]:
        if route.get("paused"):
            continue
        origin = route["origin"]
        key = f"{origin}-{route['destination']}"
        offers = route_offers(token, origin, route, cfg)
        if not offers:
            continue
        remember_price(route, offers, now)

        route_history = history.get(key, [])
        deals = sorted(find_deals(route, offers, route_history, cfg, now), key=lambda d: d[0]["price"])
        # Одно сообщение на направление за запуск: самый дешёвый подходящий билет.
        for o, reason in deals[:1]:
            # Не повторяем один и тот же билет по той же цене чаще, чем раз в realert_hours.
            sent_key = f"{key}|{o['departure_at'][:10]}|{o['price']}"
            last = sent.get(sent_key)
            if last and now - datetime.fromisoformat(last) < timedelta(hours=cfg["realert_hours"]):
                continue
            tg.send(format_alert(route, o, reason, route["origin_name"]), alert_buttons(route))
            sent[sent_key] = now.isoformat()
            alerts += 1

        cheapest = min(o["price"] for o in offers)
        route_history.append({"ts": now.isoformat(), "price": cheapest})
        cutoff = (now - timedelta(days=cfg["history_days"])).isoformat()
        history[key] = [p for p in route_history if p["ts"] >= cutoff]
        print(f"{key}: минимум {cheapest} ₽")

    for f in list(settings.get("filters", [])):
        if filter_expired(f, now.date()):
            settings["filters"].remove(f)
            tg.send(f"⌛ Даты прошли, убрал подбор: {filter_text(f)}")
            continue
        if f.get("paused"):
            continue
        origin = f["origin"]
        all_offers = filter_offers(token, origin, f)
        remember_price(f, all_offers, now)
        offers = [o for o in all_offers if o["price"] <= f["max_price"]]
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
                f"Подбор: {filter_text(f)}", alert_buttons(f))
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
MONTH_NOM = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
             "сентябрь", "октябрь", "ноябрь", "декабрь"]
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


def dates_text(f):
    def fmt(d):
        return f"{d[8:]}.{d[5:7]}" if len(d) == 10 else "в " + MONTH_IN[int(d[5:])]
    text = f"вылет {fmt(f['depart'])}"
    if f.get("return"):
        text += f", обратно {fmt(f['return'])}"
    return text


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

# ---------- Популярные города (для кнопок) ----------

POPULAR_ORIGINS = [("MOW", "Москва"), ("LED", "Санкт-Петербург"), ("KZN", "Казань"),
                   ("SVX", "Екатеринбург"), ("OVB", "Новосибирск"), ("AER", "Сочи"),
                   ("KRR", "Краснодар"), ("KUF", "Самара"), ("UFA", "Уфа")]
POPULAR_DESTINATIONS = [
    ("HKT", "Пхукет", "Таиланд"), ("BKK", "Бангкок", "Таиланд"), ("DPS", "Бали", "Индонезия"),
    ("CXR", "Нячанг", "Вьетнам"), ("DXB", "Дубай", "ОАЭ"), ("AYT", "Анталья", "Турция"),
    ("IST", "Стамбул", "Турция"), ("HRG", "Хургада", "Египет"), ("MLE", "Мальдивы", "Мальдивы"),
    ("EVN", "Ереван", "Армения"), ("TBS", "Тбилиси", "Грузия"), ("AER", "Сочи", "Россия"),
]
ROUTE_PRICES = [5000, 10000, 15000, 20000, 25000, 30000, 40000, 50000, 70000]
FILTER_PRICES = [10000, 20000, 30000, 40000, 50000, 60000, 80000, 100000, 150000]

ADD_HINT = ("Можно и одной строкой: «Пхукет 20000, Бали 35000» "
            "или «Пхукет, Бангкок 25000» — одна цена на всех.")


def k(n):
    return f"{n / 1000:g}к" if n >= 1000 else str(n)


def rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def items_of(settings, code):
    return ([("route", r) for r in settings["routes"] if r["origin"] == code]
            + [("filter", f) for f in settings.get("filters", []) if f["origin"] == code])


def origin_name(settings, code):
    return next((o["name"] for o in settings["origins"] if o["code"] == code), code)


def find_item(settings, item_id):
    for kind, key in (("route", "routes"), ("filter", "filters")):
        for item in settings.get(key, []):
            if item["id"] == item_id:
                return kind, item
    return None, None


def short_dates(f):
    def one(d):
        return f"{d[8:]}.{d[5:7]}" if len(d) == 10 else MONTH_NOM[int(d[5:])]
    text = one(f["depart"])
    if f.get("return"):
        text += "–" + one(f["return"])
    return text


def item_label(kind, item):
    pause = "⏸ " if item.get("paused") else ""
    last = f" · сейчас {k(item['last_price'])}" if item.get("last_price") else ""
    if kind == "route":
        limit = f"до {k(item['max_price'])}" if item.get("max_price") else "падения"
        return f"{pause}📍 {item['city']} · {limit}{last}"
    return f"{pause}🎯 {item['city']} {short_dates(item)} · до {k(item['max_price'])}{last}"


def new_item_fields(settings, place):
    return {"name": place_name(place), "city": place["name"], "destination": place["code"],
            "origin": settings["origin"], "origin_name": settings["origin_name"],
            "id": uuid.uuid4().hex[:8]}


def parse_dates(text, today):
    """«15.12-25.12», «20.11», «декабрь» → поля дат подбора или строка-ошибка."""
    city, f = parse_filter(f"город {text} до 1", today)
    if city is None:
        return f.replace("Пхукет ", "").replace(" до 60000", "")
    f.pop("max_price")
    return f


def parse_amount(text):
    m = re.match(r"^\s*(\d[\d\s]*)\s*(к|k|тыс\.?)?\s*(₽|р|руб\.?)?\s*$", text, re.I)
    if not m:
        return None
    n = int(m.group(1).replace(" ", ""))
    return n * 1000 if m.group(2) else n


# ---------- Экраны ----------

def home_screen(settings):
    lines = ["✈️ Главное меню\n"]
    buttons = []
    for o in settings["origins"]:
        items = items_of(settings, o["code"])
        n_r = sum(1 for kind, _ in items if kind == "route")
        lines.append(f"🛫 {o['name']}: направлений {n_r}, подборов {len(items) - n_r}")
        buttons.append([(f"🛫 {o['name']}  ({len(items)})", f"o:{o['code']}")])
    if not settings["origins"]:
        lines.append("Пока нет городов вылета.")
    lines.append("\nНажми на город вылета, чтобы настроить направления.")
    buttons.append([("➕ Город вылета", "no"), ("💰 Все цены", "pa")])
    return "\n".join(lines), buttons


def origin_screen(settings, code):
    name = origin_name(settings, code)
    items = items_of(settings, code)
    lines = [f"🛫 Вылет: {name}\n"]
    if items:
        lines.append("📍 — сообщу, когда цена упадёт ниже порога\n"
                     "🎯 — подбор на конкретные даты\n"
                     "Нажми на направление, чтобы изменить его.")
    else:
        lines.append("Направлений пока нет. Добавь первое кнопкой ниже 👇")
    buttons = [[(item_label(kind, item), f"i:{item['id']}")] for kind, item in items]
    buttons += [
        [("➕ Направление", f"a:{code}"), ("🎯 Подбор по датам", f"f:{code}")],
        [("💰 Цены сейчас", f"p:{code}"), ("🗑 Удалить город", f"xq:{code}")],
        [("⬅️ Все города", "home")],
    ]
    return "\n".join(lines), buttons


def item_screen(settings, item_id, note=""):
    kind, item = find_item(settings, item_id)
    if not item:
        return home_screen(settings)
    i = item["id"]
    lines = [note] if note else []
    if kind == "route":
        country = item["name"].split(" (")[0].title() if " (" in item["name"] else ""
        lines.append(f"📍 {item['origin_name']} → {item['city']}" + (f" ({country})" if country else ""))
        lines.append(f"Сообщу, когда билет дешевле {fmt_price(item['max_price'])} ₽ "
                     "или резко подешевеет." if item.get("max_price")
                     else "Сообщу, когда билет резко подешевеет.")
    else:
        trip = "туда-обратно" if item.get("round_trip") else "в одну сторону"
        lines.append(f"🎯 {item['origin_name']} → {item['city']}, {trip}")
        lines.append("Даты: " + dates_text(item))
        lines.append(f"Сообщу, когда билет дешевле {fmt_price(item['max_price'])} ₽.")
    if item.get("last_price"):
        ts = datetime.fromisoformat(item["last_ts"]).astimezone(MSK).strftime("%d.%m %H:%M")
        lines.append(f"Последняя цена: {fmt_price(item['last_price'])} ₽ (мск {ts})")
    lines.append("Только прямые рейсы ✈️" if item.get("direct") else "С пересадками тоже")
    lines.append("⏸ На паузе: не присылаю уведомления" if item.get("paused") else "✅ Слежу")

    buttons = [
        [("− 5к", f"ip:{i}:-5000"), ("− 1к", f"ip:{i}:-1000"),
         ("+ 1к", f"ip:{i}:1000"), ("+ 5к", f"ip:{i}:5000")],
        [("✏️ Своя цена", f"it:{i}")],
    ]
    if kind == "filter":
        trip_btn = "🔁 Сделать в одну сторону" if item.get("round_trip") else "🔁 Сделать туда-обратно"
        row = [("📅 Изменить даты", f"id:{i}")]
        if not item.get("return"):
            row.append((trip_btn, f"ir:{i}"))
        buttons.append(row)
    buttons += [
        [("✈️ Только прямые: " + ("вкл" if item.get("direct") else "выкл"), f"ic:{i}"),
         ("▶️ Возобновить" if item.get("paused") else "⏸ Пауза", f"iz:{i}")],
        [("🔎 Цена сейчас", f"in:{i}"), ("🗑 Удалить", f"iq:{i}")],
        [(f"⬅️ {item['origin_name']}", f"o:{item['origin']}")],
    ]
    return "\n".join(lines), buttons


def pick_city_screen(settings, kind):
    what = "Направление" if kind == "r" else "Подбор по датам"
    text = (f"{what} из города {settings['origin_name']}.\n\nКуда летим? "
            "Нажми на город или напиши свой.")
    if kind == "r":
        text += "\n\n" + ADD_HINT
    btns = [(name, f"wc:{code}") for code, name, _ in POPULAR_DESTINATIONS
            if code != settings["origin"]]
    return text, rows(btns, 3) + [[("⬅️ Отмена", f"o:{settings['origin']}")]]


def pick_dates_screen(wiz):
    today = date.today()
    btns = []
    d = today.replace(day=1)
    for _ in range(6):
        btns.append((MONTH_NOM[d.month].capitalize(), f"wm:{d.strftime('%Y-%m')}"))
        d = (d + timedelta(days=32)).replace(day=1)
    text = (f"🎯 {wiz['city']}: когда летим?\n\n"
            "Выбери месяц или напиши точные даты:\n"
            "• 15.12-25.12 — туда-обратно\n"
            "• 20.11 — один день вылета")
    return text, rows(btns, 3) + [[("⬅️ Отмена", f"o:{wiz['origin']}")]]


def pick_trip_screen(wiz):
    return (f"🎯 {wiz['city']}: билет нужен туда-обратно или в одну сторону?",
            [[("🔁 Туда-обратно", "wt:rt"), ("➡️ В одну сторону", "wt:ow")],
             [("⬅️ Отмена", f"o:{wiz['origin']}")]])


def pick_price_screen(wiz, current=None):
    prices = ROUTE_PRICES if wiz["kind"] == "r" else FILTER_PRICES
    what = "билет" if wiz["kind"] == "r" else (
        "билеты туда-обратно" if wiz.get("round_trip") else "билет")
    text = f"{wiz['city']}: при какой цене за {what} прислать уведомление?"
    if current:
        text += f"\n\nСейчас самый дешёвый: {fmt_price(current)} ₽"
    text += "\n\nВыбери порог или напиши свою сумму, например 23500."
    btns = [(f"до {k(p)}", f"wp:{p}") for p in prices]
    buttons = rows(btns, 3)
    if wiz["kind"] == "r":
        buttons.append([("Без порога — только резкие падения", "wp:0")])
    buttons.append([("⬅️ Отмена", f"o:{wiz['origin']}")])
    return text, buttons


def pick_origin_screen():
    btns = [(name, f"po:{code}") for code, name in POPULAR_ORIGINS]
    return ("Откуда будешь улетать? Нажми на город или напиши свой.",
            rows(btns, 3) + [[("⬅️ Отмена", "home")]])


def back_button(settings, code):
    if code:
        return [[(f"⬅️ {origin_name(settings, code)}", f"o:{code}")]]
    return [[("⬅️ В меню", "home")]]


# ---------- Мастер добавления ----------

def wizard_current_price(token, cfg, settings, wiz):
    try:
        if wiz["kind"] == "r":
            offers = route_offers(token, settings["origin"], {"destination": wiz["dest"]}, cfg)
        else:
            offers = filter_offers(token, settings["origin"], dict(wiz, destination=wiz["dest"]))
    except Exception as e:  # noqa: BLE001
        print(f"price error: {e}", file=sys.stderr)
        return None
    return min((o["price"] for o in offers), default=None)


def wizard_set_city(state, settings, place):
    wiz = state.setdefault("wiz", {})
    wiz.update(dest=place["code"], city=place["name"], country=place["country"])


def wizard_next(state, settings, tg, token, cfg, message_id=None):
    """Показывает следующий шаг мастера (или создаёт направление на последнем шаге)."""
    wiz = state["wiz"]
    show = (lambda t, b: tg.edit(message_id, t, b)) if message_id else tg.send
    if wiz["kind"] == "f" and "depart" not in wiz:
        state["awaiting"] = {"type": "wiz_dates"}
        return show(*pick_dates_screen(wiz))
    if wiz["kind"] == "f" and "round_trip" not in wiz:
        return show(*pick_trip_screen(wiz))
    if "price" not in wiz:
        state["awaiting"] = {"type": "wiz_price"}
        return show(*pick_price_screen(wiz, wizard_current_price(token, cfg, settings, wiz)))

    place = {"code": wiz["dest"], "name": wiz["city"], "country": wiz.get("country", "")}
    item = new_item_fields(settings, place)
    if wiz["kind"] == "r":
        item["max_price"] = wiz["price"] or None
        settings["routes"] = [r for r in settings["routes"]
                              if not (r["destination"] == item["destination"]
                                      and r["origin"] == item["origin"])]
        settings["routes"].append(item)
    else:
        item.update(max_price=wiz["price"], depart=wiz["depart"], round_trip=wiz["round_trip"],
                    direct=wiz.get("direct", False))
        if wiz.get("return"):
            item["return"] = wiz["return"]
        settings["filters"].append(item)
    state.pop("wiz", None)
    state.pop("awaiting", None)
    state["changed"] = True
    show(*item_screen(settings, item["id"], "✅ Добавлено! Я уже проверяю цены.\n"))


# ---------- Обработка сообщений ----------

def handle(text, state, settings, tg, token, cfg):
    """Обрабатывает текстовое сообщение. Возвращает True, если появились новые направления."""
    text = text.strip()
    low = text.lower()
    awaiting = state.pop("awaiting", None)
    if isinstance(awaiting, str):
        awaiting = {"type": awaiting}
    awaiting = awaiting or {}

    if low in ("/start", "/help", "помощь"):
        state.pop("wiz", None)
        tg.send(HELP)
        tg.send(*home_screen(settings))
        return False
    if low in ("меню", "/menu") or text == BTN_HOME or text in OLD_BUTTONS:
        state.pop("wiz", None)
        tg.send(*home_screen(settings))
        return False
    if text == BTN_PRICES:
        tg.send("🔎 Ищу цены…")
        tg.send(prices_now(token, cfg, settings), back_button(settings, None))
        return False

    for prefix, action in (("откуда ", "origin"), ("добавить ", "add"), ("подбор ", "filter")):
        if low.startswith(prefix):
            awaiting, text = {"type": action}, text[len(prefix):]
            state.pop("wiz", None)
            break

    kind = awaiting.get("type")
    wiz = state.get("wiz")

    if kind == "wiz_city" and wiz:
        if re.search(r"\d", text) or "," in text:
            # Всё одной строкой: «Пхукет 20000, Бали 35000» или «Пхукет 15.12-25.12 до 60000».
            state.pop("wiz", None)
            kind = "add" if wiz["kind"] == "r" else "filter"
        else:
            place = find_place(text)
            if isinstance(place, str):
                state["awaiting"] = awaiting
                tg.send(place)
                return False
            wizard_set_city(state, settings, place)
            wizard_next(state, settings, tg, token, cfg)
            return False
    if kind == "wiz_dates" and wiz:
        fields = parse_dates(text, date.today())
        if isinstance(fields, str):
            state["awaiting"] = awaiting
            tg.send(fields)
            return False
        wiz.update(fields)
        if not fields.get("return") and len(fields["depart"]) == 10:
            wiz.pop("round_trip", None)  # один день: спросим, нужен ли обратный билет
        wizard_next(state, settings, tg, token, cfg)
        return False
    if kind == "wiz_price" and wiz:
        amount = parse_amount(text)
        if not amount:
            state["awaiting"] = awaiting
            tg.send("Напиши сумму цифрами, например 23500 или 25к.")
            return False
        wiz["price"] = amount
        wizard_next(state, settings, tg, token, cfg)
        return state.pop("changed", False)
    if kind in ("item_price", "item_dates"):
        _, item = find_item(settings, awaiting.get("id"))
        if not item:
            tg.send(*home_screen(settings))
            return False
        if kind == "item_price":
            amount = parse_amount(text)
            if not amount:
                state["awaiting"] = awaiting
                tg.send("Напиши сумму цифрами, например 23500 или 25к.")
                return False
            item["max_price"] = amount
        else:
            fields = parse_dates(text, date.today())
            if isinstance(fields, str):
                state["awaiting"] = awaiting
                tg.send(fields)
                return False
            item.pop("return", None)
            fields.setdefault("round_trip", item.get("round_trip", True))
            item.update(fields)
        tg.send(*item_screen(settings, item["id"], "✅ Сохранил.\n"))
        return True

    actions = {"origin": set_origin, "add": add_route, "filter": add_filter}
    if kind in actions:
        reply, changed = actions[kind](settings, text)
        tg.send(reply)
        if "✅" in reply:
            tg.send(*origin_screen(settings, settings["origin"]))
        return changed

    tg.send("Не понял 🤔 Открой меню кнопкой «🏠 Меню» внизу.")
    return False


def handle_button(data, message_id, state, settings, tg, token, cfg):
    """Обрабатывает нажатие кнопки под сообщением. Возвращает True, если стоит проверить цены."""
    cmd, _, arg = data.partition(":")
    if not cmd.startswith("w"):
        state.pop("awaiting", None)
        state.pop("wiz", None)
    edit = lambda t, b: tg.edit(message_id, t, b)  # noqa: E731

    if cmd == "home":
        edit(*home_screen(settings))
    elif cmd == "o" and select_origin(settings, arg):
        edit(*origin_screen(settings, arg))

    # Город вылета
    elif cmd == "no":
        state["awaiting"] = {"type": "origin"}
        edit(*pick_origin_screen())
    elif cmd == "po":
        name = dict(POPULAR_ORIGINS).get(arg, arg)
        if not any(o["code"] == arg for o in settings["origins"]):
            settings["origins"].append({"code": arg, "name": name})
        select_origin(settings, arg)
        edit(*origin_screen(settings, arg))
    elif cmd == "xq":
        edit(f"Удалить город {origin_name(settings, arg)} и все его направления?",
             [[("🗑 Да, удалить", f"x:{arg}"), ("Отмена", f"o:{arg}")]])
    elif cmd == "x":
        settings["routes"] = [r for r in settings["routes"] if r["origin"] != arg]
        settings["filters"] = [f for f in settings["filters"] if f["origin"] != arg]
        settings["origins"] = [o for o in settings["origins"] if o["code"] != arg]
        if settings["origin"] == arg and settings["origins"]:
            select_origin(settings, settings["origins"][0]["code"])
        edit(*home_screen(settings))

    # Мастер добавления
    elif cmd in ("a", "f") and select_origin(settings, arg):
        state["wiz"] = {"kind": "r" if cmd == "a" else "f", "origin": arg}
        state["awaiting"] = {"type": "wiz_city"}
        edit(*pick_city_screen(settings, state["wiz"]["kind"]))
    elif cmd == "wc" and state.get("wiz"):
        code, name, country = next(d for d in POPULAR_DESTINATIONS if d[0] == arg)
        wizard_set_city(state, settings, {"code": code, "name": name, "country": country})
        wizard_next(state, settings, tg, token, cfg, message_id)
    elif cmd == "wm" and state.get("wiz"):
        state["wiz"]["depart"] = arg
        wizard_next(state, settings, tg, token, cfg, message_id)
    elif cmd == "wt" and state.get("wiz"):
        state["wiz"]["round_trip"] = arg == "rt"
        wizard_next(state, settings, tg, token, cfg, message_id)
    elif cmd == "wp" and state.get("wiz"):
        state["wiz"]["price"] = int(arg)
        wizard_next(state, settings, tg, token, cfg, message_id)
        return state.pop("changed", False)
    elif cmd.startswith("w"):
        edit(*home_screen(settings))  # мастер устарел (например, после перезапуска)

    # Карточка направления
    elif cmd in ("i", "ip", "it", "id", "ir", "ic", "iz", "in", "iq", "ix"):
        item_id, _, extra = arg.partition(":")
        kind, item = find_item(settings, item_id)
        if not item:
            edit("Этого направления уже нет.", back_button(settings, None))
            return False
        select_origin(settings, item["origin"])
        note = ""
        if cmd == "ip":
            base = item.get("max_price") or item.get("last_price") or 20000
            item["max_price"] = max(1000, base + int(extra))
            note = f"✅ Порог: {fmt_price(item['max_price'])} ₽\n"
        elif cmd == "it":
            state["awaiting"] = {"type": "item_price", "id": item_id}
            tg.send(f"{item['city']}: напиши новую цену, например 23500 или 25к.",
                    [[("⬅️ Отмена", f"i:{item_id}")]])
            return False
        elif cmd == "id":
            state["awaiting"] = {"type": "item_dates", "id": item_id}
            tg.send(f"🎯 {item['city']}: напиши новые даты, например 15.12-25.12, 20.11 или декабрь.",
                    [[("⬅️ Отмена", f"i:{item_id}")]])
            return False
        elif cmd == "ir":
            item["round_trip"] = not item.get("round_trip")
        elif cmd == "ic":
            item["direct"] = not item.get("direct")
        elif cmd == "iz":
            item["paused"] = not item.get("paused")
            note = "⏸ Поставил на паузу.\n" if item["paused"] else "▶️ Снова слежу.\n"
        elif cmd == "in":
            tg.send("🔎 Ищу цены…")
            offers = (route_offers(token, item["origin"], item, cfg) if kind == "route"
                      else filter_offers(token, item["origin"], item))
            remember_price(item, offers, datetime.now(timezone.utc))
            if offers:
                best = min(offers, key=lambda o: o["price"])
                tg.send(f"💰 {item['city']}: {fmt_price(best['price'])} ₽\n"
                        f"{offer_line(best, item['origin_name'], item)}",
                        [[("⬅️ К направлению", f"i:{item_id}")]])
            else:
                tg.send(f"{item['city']}: билетов не нашёл 😔", [[("⬅️ К направлению", f"i:{item_id}")]])
            return False
        elif cmd == "iq":
            edit(f"Удалить {item_label(kind, item)}?",
                 [[("🗑 Да, удалить", f"ix:{item_id}"), ("Отмена", f"i:{item_id}")]])
            return False
        elif cmd == "ix":
            settings["routes" if kind == "route" else "filters"].remove(item)
            edit(*origin_screen(settings, item["origin"]))
            return False
        edit(*item_screen(settings, item_id, note))
        return cmd in ("ip", "ir", "ic")

    # Цены
    elif cmd in ("p", "pa"):
        tg.send("🔎 Ищу цены…")
        tg.send(prices_now(token, cfg, settings, arg or None), back_button(settings, arg or None))
    else:
        edit(*home_screen(settings))
    return False


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
                changed |= bool(handle_button(cq.get("data", ""), msg["message_id"],
                                              state, settings, tg, token, cfg))
            elif "text" in msg:
                changed |= bool(handle(msg["text"], state, settings, tg, token, cfg))
        except Exception as e:  # noqa: BLE001
            print(f"handle error: {e!r}", file=sys.stderr)
            tg.send("Что-то пошло не так, попробуй ещё раз.", back_button(settings, None))
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
