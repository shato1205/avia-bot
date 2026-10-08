"""Бот дешёвых авиабилетов: команды в Telegram + проверка цен через Travelpayouts.

Запускается по расписанию из GitHub Actions. За один запуск:
1. читает новые сообщения в Telegram и отвечает на команды меню;
2. если с прошлой проверки прошло больше check_every_minutes, проверяет цены
   и шлёт алерты.
Настройки, история цен и состояние хранятся в data/*.json и коммитятся обратно.
"""
import json
import os
import re
import statistics
import sys
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
MENU = [[BTN_FROM, BTN_PRICES], [BTN_ADD, BTN_REMOVE], [BTN_LIST]]


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

    def call(self, method, **params):
        body = urllib.parse.urlencode(
            {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
             for k, v in params.items()}
        ).encode()
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        with urllib.request.urlopen(url, data=body, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send(self, text):
        keyboard = {"keyboard": MENU, "resize_keyboard": True}
        self.call("sendMessage", chat_id=self.chat_id, text=text,
                  reply_markup=keyboard, disable_web_page_preview="true")

    def updates(self, offset):
        return self.call("getUpdates", offset=offset, timeout=0).get("result", [])


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


def fetch_cheapest(token, origin, destination, month):
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": month,
        "currency": "rub",
        "sorting": "price",
        "one_way": "true",
        "limit": 5,
        "token": token,
    }
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


def offer_line(offer, origin_name, route):
    dep = offer["departure_at"][:10]
    stops = offer.get("transfers", 0)
    stops_txt = "прямой" if stops == 0 else f"пересадок: {stops}"
    link = "https://www.aviasales.ru" + offer.get("link", "")
    return f"{origin_name} → {route['city']}, вылет {dep}, {stops_txt}\n{link}"


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
    return "\n".join(lines)


# ---------- Команды ----------

def routes_text(settings):
    if not settings["routes"]:
        return "Направлений пока нет. Нажми «➕ Добавить направление»."
    lines = [f"Вылет из: {settings['origin_name']}", "Направления:"]
    for i, r in enumerate(settings["routes"], 1):
        limit = f"до {fmt_price(r['max_price'])} ₽" if r.get("max_price") else "только резкие падения"
        lines.append(f"{i}. {r['name']}, {limit}")
    return "\n".join(lines)


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
    if not 0 <= i < len(settings["routes"]):
        return "Нет направления с таким номером.", False
    r = settings["routes"].pop(i)
    return f"🗑 Удалил {r['name']}.", True


HELP = (
    "Я слежу за ценами на авиабилеты и пишу, когда находится дешёвый билет.\n\n"
    "Можно нажимать кнопки меню или писать сразу одной строкой:\n"
    "• откуда Санкт-Петербург\n"
    "• добавить Пхукет 20000\n"
    "• удалить 2\n\n"
    "Я отвечаю не мгновенно, а при очередной проверке (раз в ~30 минут)."
)


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
    if text == BTN_REMOVE:
        if not settings["routes"]:
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

    for prefix, action in (("откуда ", "origin"), ("добавить ", "add"), ("удалить ", "remove")):
        if low.startswith(prefix):
            awaiting, text = action, text[len(prefix):]
            break

    actions = {"origin": set_origin, "add": add_route, "remove": remove_route}
    if awaiting in actions:
        reply, changed = actions[awaiting](settings, text)
        tg.send(reply)
        return changed

    tg.send("Не понял 🤔 Нажми кнопку в меню.\n\n" + HELP)
    return False


def process_messages(tg, token, cfg, settings, state):
    changed = False
    for upd in tg.updates(state.get("offset", 0)):
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


def main():
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    tg = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])

    cfg = load(CONFIG, {})
    settings = load(SETTINGS, None) or {
        "origin": cfg["origin"],
        "origin_name": cfg.get("origin_name", cfg["origin"]),
        "routes": [dict(r, city=r.get("city", r["destination"])) for r in cfg["routes"]],
    }
    state = load(STATE, {})
    history = load(HISTORY, {})
    sent = load(SENT, {})
    now = datetime.now(timezone.utc)

    settings_changed = process_messages(tg, token, cfg, settings, state)

    last = state.get("last_check")
    due = not last or now - datetime.fromisoformat(last) >= timedelta(minutes=cfg["check_every_minutes"] - 5)
    if due or settings_changed:
        check_prices(tg, token, cfg, settings, history, sent, now)
        state["last_check"] = now.isoformat()

    week_ago = now - timedelta(days=7)
    sent = {k: v for k, v in sent.items() if datetime.fromisoformat(v) >= week_ago}
    save(SETTINGS, settings)
    save(STATE, state)
    save(HISTORY, history)
    save(SENT, sent)


if __name__ == "__main__":
    main()
