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

BTN_FROM = "🛫 Откуда лечу"
BTN_ADD = "➕ Добавить направление"
BTN_LIST = "📋 Мои направления"
BTN_REMOVE = "➖ Удалить направление"
BTN_PRICES = "💰 Цены сейчас"
BTN_FILTER = "🎯 Подбор по датам"
MENU = [[BTN_FROM, BTN_PRICES], [BTN_ADD, BTN_FILTER], [BTN_LIST, BTN_REMOVE]]


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

    def send(self, text):
        keyboard = {"keyboard": MENU, "resize_keyboard": True}
        self.call("sendMessage", chat_id=self.chat_id, text=text,
                  reply_markup=keyboard, disable_web_page_preview="true")

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
    origin = settings["origin"]
    alerts = 0
    for route in settings["routes"]:
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
            tg.send(format_alert(route, o, reason, settings["origin_name"]))
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
                f"{offer_line(o, settings['origin_name'], f)}\n"
                f"Подбор: {filter_text(f)}")
        sent[sent_key] = now.isoformat()
        alerts += 1
    print(f"Отправлено алертов: {alerts}")


def prices_now(token, cfg, settings):
    lines = [f"💰 Самые дешёвые билеты из {settings['origin_name']} на {cfg['months_ahead']} мес.:"]
    for route in settings["routes"]:
        offers = route_offers(token, settings["origin"], route, cfg)
        if not offers:
            lines.append(f"\n{route['name']}: билетов не нашёл")
            continue
        best = min(offers, key=lambda o: o["price"])
        lines.append(f"\n{route['name']}: {fmt_price(best['price'])} ₽\n"
                     f"{offer_line(best, settings['origin_name'], route)}")
    for f in settings.get("filters", []):
        offers = filter_offers(token, settings["origin"], f)
        if not offers:
            lines.append(f"\n🎯 {filter_text(f)}: билетов не нашёл")
            continue
        best = min(offers, key=lambda o: o["price"])
        lines.append(f"\n🎯 {filter_text(f)}: {fmt_price(best['price'])} ₽\n"
                     f"{offer_line(best, settings['origin_name'], f)}")
    return "\n".join(lines)


# ---------- Команды ----------

def routes_text(settings):
    filters = settings.get("filters", [])
    if not settings["routes"] and not filters:
        return "Направлений пока нет. Нажми «➕ Добавить направление»."
    lines = [f"Вылет из: {settings['origin_name']}"]
    if settings["routes"]:
        lines.append("Направления:")
    for i, r in enumerate(settings["routes"], 1):
        limit = f"до {fmt_price(r['max_price'])} ₽" if r.get("max_price") else "только резкие падения"
        lines.append(f"{i}. {r['name']}, {limit}")
    if filters:
        lines.append("Подбор по датам:")
    for i, f in enumerate(filters, len(settings["routes"]) + 1):
        lines.append(f"{i}. 🎯 {filter_text(f)}")
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


def add_filter(settings, text):
    city, f = parse_filter(text, date.today())
    if city is None:
        return f, False
    place = find_place(city)
    if isinstance(place, str):
        return place, False
    country = place["country"].upper()
    f.update(name=f"{country} ({place['name']})" if country else place["name"].upper(),
             city=place["name"], destination=place["code"])
    settings.setdefault("filters", []).append(f)
    return f"✅ Добавил подбор: {filter_text(f)}\nСообщу, как только найду такой билет.", True


def split_price(text):
    """«Пхукет 20000» / «Пхукет 20к» → ("Пхукет", 20000)."""
    m = re.match(r"^(.*?)[\s,]+(\d[\d\s]*)\s*(к|k|тыс\.?)?\s*(₽|р|руб\.?)?$", text.strip(), re.I)
    if not m:
        return text.strip(), None
    price = int(m.group(2).replace(" ", ""))
    if m.group(3):
        price *= 1000
    return m.group(1).strip(), price


def set_origin(settings, text):
    place = find_place(text)
    if isinstance(place, str):
        return place, False
    settings["origin"] = place["code"]
    settings["origin_name"] = place["name"]
    return f"✅ Теперь ищу билеты из города {place['name']} ({place['code']}).", True


def add_route(settings, text):
    city, price = split_price(text)
    place = find_place(city)
    if isinstance(place, str):
        return place, False
    if any(r["destination"] == place["code"] for r in settings["routes"]):
        for r in settings["routes"]:
            if r["destination"] == place["code"]:
                r["max_price"] = price
        return f"✅ Обновил порог для {place['name']}.", True
    country = place["country"].upper()
    name = f"{country} ({place['name']})" if country else place["name"].upper()
    settings["routes"].append({"name": name, "city": place["name"],
                               "destination": place["code"], "max_price": price})
    limit = f"дешевле {fmt_price(price)} ₽" if price else "при резком падении цены"
    return f"✅ Добавил {name}. Сообщу, когда билет будет {limit}.", True


def remove_route(settings, text):
    if not text.strip().isdigit():
        return "Напиши номер направления из списка.", False
    i = int(text.strip()) - 1
    routes, filters = settings["routes"], settings.get("filters", [])
    if 0 <= i < len(routes):
        r = routes.pop(i)
        return f"🗑 Удалил {r['name']}.", True
    if 0 <= i - len(routes) < len(filters):
        f = filters.pop(i - len(routes))
        return f"🗑 Удалил подбор: {filter_text(f)}", True
    return "Нет направления с таким номером.", False


HELP = (
    "Я слежу за ценами на авиабилеты и пишу, когда находится дешёвый билет.\n\n"
    "Можно нажимать кнопки меню или писать сразу одной строкой:\n"
    "• откуда Санкт-Петербург\n"
    "• добавить Пхукет 20000\n"
    "• подбор Пхукет 15.12-25.12 до 60000\n"
    "• подбор Бали декабрь до 70000 прямой\n"
    "• удалить 2"
)
SERVE = "--serve" in sys.argv
if not SERVE:
    HELP += "\n\nЯ отвечаю не мгновенно, а при очередной проверке (раз в ~30 минут)."


def handle(text, state, settings, tg, token, cfg):
    """Обрабатывает одно сообщение. Возвращает True, если настройки изменились."""
    text = text.strip()
    low = text.lower()
    awaiting = state.pop("awaiting", None)

    if low in ("/start", "/help", "меню", "помощь"):
        tg.send(HELP)
        return False
    if text == BTN_FROM:
        state["awaiting"] = "origin"
        tg.send(f"Сейчас вылет из: {settings['origin_name']}.\nНапиши город, откуда хочешь улететь.")
        return False
    if text == BTN_ADD:
        state["awaiting"] = "add"
        tg.send("Напиши город и максимальную цену, например: Пхукет 20000\n"
                "Без цены буду сообщать только о резких падениях.")
        return False
    if text == BTN_FILTER:
        state["awaiting"] = "filter"
        tg.send("Напиши город, даты и максимальную цену за билет:\n"
                "• Пхукет 15.12-25.12 до 60000 — туда-обратно в эти даты\n"
                "• Стамбул 20.11 до 8000 — в одну сторону\n"
                "• Бали декабрь до 70000 — туда-обратно с вылетом в декабре\n"
                "Добавь «прямой», если нужны только рейсы без пересадок.")
        return False
    if text == BTN_REMOVE:
        if not settings["routes"] and not settings.get("filters"):
            tg.send(routes_text(settings))
            return False
        state["awaiting"] = "remove"
        tg.send(routes_text(settings) + "\n\nНапиши номер, который удалить.")
        return False
    if text == BTN_LIST:
        tg.send(routes_text(settings))
        return False
    if text == BTN_PRICES:
        tg.send(prices_now(token, cfg, settings))
        return False

    for prefix, action in (("откуда ", "origin"), ("добавить ", "add"), ("подбор ", "filter"), ("удалить ", "remove")):
        if low.startswith(prefix):
            awaiting, text = action, text[len(prefix):]
            break

    actions = {"origin": set_origin, "add": add_route, "filter": add_filter, "remove": remove_route}
    if awaiting in actions:
        reply, changed = actions[awaiting](settings, text)
        tg.send(reply)
        return changed

    tg.send("Не понял 🤔 Нажми кнопку в меню.\n\n" + HELP)
    return False


def process_messages(tg, token, cfg, settings, state, wait=0):
    changed = False
    for upd in tg.updates(state.get("offset", 0), wait):
        state["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        # Слушаемся только владельца бота.
        if str(msg.get("chat", {}).get("id")) != tg.chat_id or "text" not in msg:
            continue
        try:
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
