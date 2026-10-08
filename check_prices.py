"""Проверяет цены на авиабилеты через Travelpayouts и шлёт алерты в Telegram.

Запускается по расписанию из GitHub Actions. История цен и уже отправленные
алерты хранятся в data/*.json и коммитятся обратно в репозиторий.
"""
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.json"
HISTORY = ROOT / "data" / "history.json"
SENT = ROOT / "data" / "sent.json"

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


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
    data = http_get_json(API_URL + "?" + urllib.parse.urlencode(params))
    if not data.get("success"):
        print(f"API error {origin}->{destination} {month}: {data}", file=sys.stderr)
        return []
    return data.get("data", [])


def find_deals(route, offers, history, cfg, now):
    """Возвращает (offer, причина) для предложений, о которых стоит сообщить."""
    cutoff = (now - timedelta(days=cfg["history_days"])).isoformat()
    past = [p["price"] for p in history if p["ts"] >= cutoff]
    median = statistics.median(past) if len(past) >= 5 else None

    deals = []
    for o in offers:
        price = o["price"]
        reasons = []
        if price <= route["max_price"]:
            reasons.append(f"ниже порога {route['max_price']:,} ₽".replace(",", " "))
        if median and price <= median * (1 - cfg["drop_percent"] / 100):
            drop = round(100 * (1 - price / median))
            reasons.append(f"на {drop}% дешевле обычного")
        if reasons:
            deals.append((o, ", ".join(reasons)))
    return deals


def format_alert(route, offer, reason, origin):
    price = f"{offer['price']:,}".replace(",", " ")
    dep = offer["departure_at"][:10]
    stops = offer.get("transfers", 0)
    stops_txt = "прямой" if stops == 0 else f"пересадок: {stops}"
    link = "https://www.aviasales.ru" + offer.get("link", "")
    return (
        f"🔥 ГОША СРОЧНО {route['name']} {price} ₽\n"
        f"{origin} → {offer.get('destination', route['destination'])}, вылет {dep}, {stops_txt}\n"
        f"Почему: {reason}\n"
        f"{link}"
    )


def send_telegram(bot_token, chat_id, text):
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    with urllib.request.urlopen(url, data=body, timeout=30) as resp:
        resp.read()


def main():
    token = os.environ["TRAVELPAYOUTS_TOKEN"]
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    cfg = load(CONFIG, {})
    history = load(HISTORY, {})
    sent = load(SENT, {})
    now = datetime.now(timezone.utc)
    origin = cfg["origin"]

    alerts = 0
    for route in cfg["routes"]:
        key = f"{origin}-{route['destination']}"
        offers = []
        for month in months(cfg["months_ahead"]):
            offers += fetch_cheapest(token, origin, route["destination"], month)
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
            send_telegram(bot_token, chat_id, format_alert(route, o, reason, origin))
            sent[sent_key] = now.isoformat()
            alerts += 1

        cheapest = min(o["price"] for o in offers)
        route_history.append({"ts": now.isoformat(), "price": cheapest})
        cutoff = (now - timedelta(days=90)).isoformat()
        history[key] = [p for p in route_history if p["ts"] >= cutoff]
        print(f"{key}: минимум {cheapest} ₽")

    week_ago = now - timedelta(days=7)
    sent = {k: v for k, v in sent.items() if datetime.fromisoformat(v) >= week_ago}
    save(HISTORY, history)
    save(SENT, sent)
    print(f"Отправлено алертов: {alerts}")


if __name__ == "__main__":
    main()
