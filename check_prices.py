"""Бот дешёвых авиабилетов и билетов на поезда: команды в Telegram + цены через Travelpayouts.

Два режима:
- `python check_prices.py` — один проход (для GitHub Actions по расписанию);
- `python check_prices.py --serve` — работает постоянно на сервере и отвечает
  на сообщения сразу.
За проход бот читает новые сообщения в Telegram, отвечает на команды меню и,
если с прошлой проверки прошло check_every_minutes, проверяет цены и шлёт алерты.
Настройки, история цен и состояние хранятся в data/*.json.
"""
import gzip
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
ACCESS = ROOT / "data" / "access.json"  # кто ещё может пользоваться ботом
USERS_DIR = ROOT / "data" / "users"  # настройки остальных людей: data/users/<chat id>/
STATIONS_FILE = ROOT / "stations.json"

MSK = timezone(timedelta(hours=3))
PRICES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
PLACES_URL = "https://autocomplete.travelpayouts.com/places2"
# Расписание и цены «от» Туту.ру (без даты). Сайты РЖД и Туту с зарубежных серверов не открываются.
TRAINS_URL = "https://suggest.travelpayouts.com/search"
# Точные цены и места на конкретную дату — тот же запрос, что делает сайт Туту.ру.
TUTU_OFFERS_URL = "https://offers-api.tutu.ru/railway/offers"
TUTU = "https://www.tutu.ru"

BTN_HOME = "🏠 Меню"
BTN_PRICES = "💰 Цены сейчас"
BTN_SAVED = "⭐ Отложенные"
MENU = [[BTN_HOME, BTN_PRICES, BTN_SAVED]]
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


def http_post_json(url, body, headers):
    h = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36",
         "Accept": "application/json", "Accept-Encoding": "gzip", "Content-Type": "application/json"}
    h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def fmt_price(n):
    return f"{n:,}".replace(",", " ")


# ---------- Telegram ----------

def split_text(text, limit=4000):
    """Делит длинный текст на части по абзацам (а если абзац длинный — по строкам)."""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        pieces = [block] if len(block) <= limit else \
            [line[i:i + limit] for line in block.split("\n") for i in range(0, len(line) or 1, limit)]
        sep = "\n\n"
        for piece in pieces:
            if cur and len(cur) + len(sep) + len(piece) > limit:
                parts.append(cur)
                cur = ""
            cur = cur + sep + piece if cur else piece
            sep = "\n"
    return parts + [cur] if cur or not parts else parts


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
        """buttons: [[(текст, callback_data или https-ссылка), ...], ...] → кнопки под сообщением."""
        if buttons is None:
            return {"keyboard": MENU, "resize_keyboard": True}
        return {"inline_keyboard": [[{"text": t, "url" if d.startswith("https://") else "callback_data": d}
                                     for t, d in row] for row in buttons]}

    def send(self, text, buttons=None):
        parts = split_text(text)
        for part in parts[:-1]:  # Телеграм не принимает сообщения длиннее 4096 знаков
            self.call("sendMessage", chat_id=self.chat_id, text=part, disable_web_page_preview="true")
        self.call("sendMessage", chat_id=self.chat_id, text=parts[-1],
                  reply_markup=self._markup(buttons), disable_web_page_preview="true")

    def send_plain(self, text):
        """Сообщение без кнопок и без нижнего меню (для тех, кому бот ещё не открыт)."""
        self.call("sendMessage", chat_id=self.chat_id, text=text,
                  reply_markup={"remove_keyboard": True})

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


def fetch_cheapest(token, origin, destination, month, return_at=None, one_way=True, direct=False,
                   limit=30):
    """month — YYYY-MM или YYYY-MM-DD. При one_way=False цена за туда-обратно."""
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": month,
        "currency": "rub",
        "sorting": "price",
        "one_way": "true" if one_way else "false",
        "direct": "true" if direct else "false",
        "limit": limit,  # с запасом: часть билетов отсеется (долгие пересадки, время вылета)
        "token": token,
    }
    if return_at:
        params["return_at"] = return_at
    data = http_get_json(PRICES_URL + "?" + urllib.parse.urlencode(params))
    if not data.get("success"):
        print(f"API error {origin}->{destination} {month}: {data}", file=sys.stderr)
        return []
    return data.get("data", [])


def route_offers(token, origin, route, cfg, comfort_filter=True):
    offers = []
    for month in months(cfg["months_ahead"]):
        offers += fetch_cheapest(token, origin, route["destination"], month,
                                 direct=route.get("direct", False), limit=api_limit(route))
    return flight_filter(offers, route) if comfort_filter else offers


def api_limit(item):
    """Сколько билетов просить у API: с фильтрами по времени и багажу — побольше."""
    return 100 if item.get("times") or item.get("back_times") or item.get("bag") else 30


# ---------- Удобство: пересадки, время в пути, багаж ----------

AIRLINES = {
    "SU": "Аэрофлот", "S7": "S7", "U6": "Уральские авиалинии", "UT": "ЮТэйр", "DP": "Победа",
    "WZ": "Red Wings", "5N": "Smartavia", "N4": "Nordwind", "ZF": "Azur Air", "2S": "Southwind",
    "Y7": "NordStar", "DV": "SCAT", "C6": "Centrum Air", "HY": "Uzbekistan Airways",
    "KC": "Air Astana", "J2": "AZAL", "CZ": "China Southern", "CA": "Air China",
    "MU": "China Eastern", "HU": "Hainan Airlines", "EK": "Emirates", "FZ": "flydubai",
    "QR": "Qatar Airways", "EY": "Etihad", "TK": "Turkish Airlines", "PC": "Pegasus",
    "VF": "AJet", "G9": "Air Arabia", "W5": "Mahan Air", "TG": "Thai Airways",
    "VN": "Vietnam Airlines", "VJ": "VietJet", "FD": "Thai AirAsia", "AK": "AirAsia",
    "J9": "Jazeera Airways", "XY": "flynas", "OV": "SalamAir", "6E": "IndiGo", "UL": "SriLankan",
    "AI": "Air India", "MS": "EgyptAir", "B2": "Belavia", "XQ": "SunExpress", "IR": "Iran Air",
    "JD": "Capital Airlines", "3U": "Sichuan Airlines", "ZH": "Shenzhen Airlines",
    "SC": "Shandong Airlines", "MF": "Xiamen Airlines", "GS": "Tianjin Airlines",
    "EO": "Pegas Fly", "IO": "ИрАэро", "R3": "Якутия", "A4": "Азимут", "YC": "Ямал",
}
# Лоукостеры: в самый дешёвый тариф багаж обычно не входит.
LOWCOST = {"DP", "G9", "3L", "PC", "VF", "FZ", "XY", "J9", "OV", "VJ", "FD", "AK", "D7", "TR",
           "3K", "JQ", "5J", "6E", "SG", "DD", "SL", "QG", "7C", "MM", "9C", "XQ", "W6", "FR",
           "U2", "HV", "VY", "FS"}
AIRPORT_CITIES = {
    "SVO": "Москва", "DME": "Москва", "VKO": "Москва", "LED": "Петербург", "KZN": "Казань",
    "SVX": "Екатеринбург", "OVB": "Новосибирск", "KJA": "Красноярск", "IKT": "Иркутск",
    "VVO": "Владивосток", "AER": "Сочи", "MRV": "Минводы", "OGZ": "Владикавказ", "UFA": "Уфа",
    "KUF": "Самара", "ROV": "Ростов", "KRR": "Краснодар", "MCX": "Махачкала", "GRV": "Грозный",
    "MSQ": "Минск", "TAS": "Ташкент", "SKD": "Самарканд", "ALA": "Алматы", "NQZ": "Астана",
    "FRU": "Бишкек", "OSS": "Ош", "DYU": "Душанбе", "GYD": "Баку", "EVN": "Ереван",
    "TBS": "Тбилиси", "IST": "Стамбул", "SAW": "Стамбул", "AYT": "Анталья", "DXB": "Дубай",
    "DWC": "Дубай", "SHJ": "Шарджа", "AUH": "Абу-Даби", "DOH": "Доха", "BAH": "Бахрейн",
    "MCT": "Маскат", "KWI": "Кувейт", "RUH": "Эр-Рияд", "JED": "Джидда", "IKA": "Тегеран",
    "CAI": "Каир", "HRG": "Хургада", "DEL": "Дели", "BOM": "Мумбаи", "CMB": "Коломбо",
    "MLE": "Мале", "CAN": "Гуанчжоу", "SZX": "Шэньчжэнь", "HAK": "Хайкоу", "WUH": "Ухань",
    "PEK": "Пекин", "PKX": "Пекин", "PVG": "Шанхай", "SHA": "Шанхай", "CTU": "Чэнду",
    "TFU": "Чэнду", "KMG": "Куньмин", "XIY": "Сиань", "URC": "Урумчи", "HKG": "Гонконг",
    "BKK": "Бангкок", "DMK": "Бангкок", "HKT": "Пхукет", "SGN": "Хошимин", "HAN": "Ханой",
    "DAD": "Дананг", "CXR": "Нячанг", "KUL": "Куала-Лумпур", "SIN": "Сингапур",
    "DPS": "Бали", "CGK": "Джакарта", "MNL": "Манила", "ICN": "Сеул", "NRT": "Токио",
    "HGH": "Ханчжоу", "TAO": "Циндао", "XMN": "Сямэнь", "TSN": "Тяньцзинь", "CKG": "Чунцин",
    "SYX": "Санья", "ZIA": "Москва", "KUT": "Кутаиси", "GOI": "Гоа", "GOX": "Гоа",
}
AUTO_EXTRA_MIN = 600  # «авто»: не дольше самого быстрого варианта + 10 часов на каждую сторону
AUTO_MAX_MIN = 30 * 60  # и не дольше 30 часов на сторону, если есть варианты быстрее
HOURS_STEPS = [None, 12, 16, 20, 24, 30, 0]  # None — авто, 0 — любое время в пути


def travel_minutes(o):
    """Время в пути от вылета до прилёта с пересадками (для туда-обратно — обе стороны)."""
    return int(o.get("duration") or o.get("duration_to") or 0)


def comfort(offers, max_hours, legs=1):
    """Отсеивает билеты с долгими пересадками. max_hours: None — авто, 0 — не отсеивать."""
    timed = [travel_minutes(o) for o in offers if travel_minutes(o)]
    if max_hours == 0 or not timed:
        return offers
    if max_hours:
        limit = max_hours * 60 * legs
    else:
        fastest = min(timed)
        limit = max(fastest, min(fastest + AUTO_EXTRA_MIN * legs, AUTO_MAX_MIN * legs))
    return [o for o in offers if not travel_minutes(o) or travel_minutes(o) <= limit]


def has_bag(o):
    """Багаж по тарифу: True — включён, False — нет, None — неизвестно."""
    m = re.search(r"static_fare_key=([^&]+)", o.get("link", ""))
    if m:
        parts = urllib.parse.unquote(m.group(1)).split("|")
        if "L0" in parts:
            return False
        if any(part.startswith("L1") for part in parts):
            return True
    return None


def bag_ok(o):
    """Есть ли багаж (если тариф неизвестен — у лоукостеров считаем, что нет)."""
    bag = has_bag(o)
    return bag if bag is not None else o.get("airline", "") not in LOWCOST


def time_ok(times, iso):
    """Подходит ли время вылета (…T23:15…) под выбранные части суток."""
    part = part_of(iso[11:16])
    return not times or not part or part in times


def flight_filter(offers, item, legs=1, back=False):
    """Билеты под настройки направления: время в пути, время вылета, багаж.

    back=True — это обратные рейсы: время вылета сверяем с «обратно».
    """
    times = item.get("back_times" if back else "times")
    return [o for o in comfort(offers, item.get("max_hours"), legs)
            if time_ok(times, o.get("departure_at", ""))
            and (back or not o.get("return_at") or time_ok(item.get("back_times"), o["return_at"]))
            and (not item.get("bag") or bag_ok(o))]


def skip_reason(o, item):
    """Почему билет не показываем: «вылет в 03:10», «без багажа», «в пути 31 ч»."""
    if not time_ok(item.get("times"), o.get("departure_at", "")):
        return f"вылет в {o['departure_at'][11:16]}"
    if o.get("return_at") and not time_ok(item.get("back_times"), o["return_at"]):
        return f"обратно вылет в {o['return_at'][11:16]}"
    if item.get("bag") and not bag_ok(o):
        return "без багажа"
    return f"в пути {fmt_minutes(travel_minutes(o))}"


def hours_label(max_hours):
    return {None: "авто", 0: "любое"}.get(max_hours, f"до {max_hours} ч")


def hours_text(max_hours):
    if max_hours is None:
        return ("авто — отсеиваю билеты, где лететь на 10+ часов дольше самого быстрого "
                "или больше 30 ч")
    if max_hours == 0:
        return "любое — показываю все пересадки"
    return f"не дольше {max_hours} ч в одну сторону"


def fmt_minutes(m):
    hours, minutes = divmod(m, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


def transfer_cities(o):
    """(города пересадок, меняется ли аэропорт) для билета в одну сторону.

    Аэропорты зашиты в ссылку Aviasales: …SVOCANHKT_… Если прилетаешь в один аэропорт,
    а улетаешь из другого (…VKODWCDXBCMB_…), аэропортов больше, чем пересадок.
    """
    m = re.search(r"[?&]t=[A-Z0-9]{2}\d+([A-Z]{6,})_", o.get("link", ""))
    if not m:
        return [], False
    middle = [m.group(1)[i:i + 3] for i in range(3, len(m.group(1)) - 3, 3)]
    cities = []
    for c in middle:
        city = AIRPORT_CITIES.get(c, c)
        if not cities or cities[-1] != city:
            cities.append(city)
    return cities, len(middle) > o.get("transfers", 0)


def baggage(o):
    """Багаж по тарифу из ссылки Aviasales (static_fare_key=…|L1_1_23|…); None — неизвестно."""
    m = re.search(r"static_fare_key=([^&]+)", o.get("link", ""))
    if not m:
        return None
    for part in urllib.parse.unquote(m.group(1)).split("|"):
        if part == "L0":
            return "🧳 Без багажа: чемодан за доплату"
        if part.startswith("L1"):
            nums = part.split("_")[1:]
            if len(nums) == 2 and nums[1] != "0":
                pieces, kg = nums
                return f"🧳 Багаж включён: {kg} кг" + (f" ({pieces} места)" if pieces != "1" else "")
            return "🧳 Багаж включён"
    return None


def trip_details(o):
    """«✈️ Аэрофлот · прямой · в пути 9 ч 30 мин» + пометка про багаж у лоукостеров."""
    code = o.get("airline", "")
    parts = [f"✈️ {AIRLINES.get(code, code)}" if code else "✈️"]
    transfers = o.get("transfers", 0)
    if o.get("return_at"):
        back = o.get("return_transfers", 0)
        parts.append("прямые" if transfers + back == 0 else f"пересадок: туда {transfers}, обратно {back}")
        if travel_minutes(o):
            parts.append(f"в пути всего {fmt_minutes(travel_minutes(o))}")
    else:
        if transfers == 0:
            parts.append("прямой")
        else:
            cities, change = transfer_cities(o)
            where = ", ".join(cities)
            parts.append(f"пересадок: {transfers}" + (f" ({where})" if where else "")
                         + (", ⚠️ смена аэропорта" if change else ""))
        if travel_minutes(o):
            parts.append(f"в пути {fmt_minutes(travel_minutes(o))}")
    line = " · ".join(parts)
    bag = baggage(o)
    if bag:
        line += "\n" + bag
    elif code in LOWCOST:
        line += "\n🧳 Лоукостер: багаж, скорее всего, за доплату"
    return line


def item_offers(token, kind, item, cfg):
    """(все билеты, билеты без долгих пересадок) для направления или подбора."""
    if kind == "route":
        all_offers = route_offers(token, item["origin"], item, cfg, comfort_filter=False)
        return all_offers, flight_filter(all_offers, item)
    all_offers = filter_offers(token, item["origin"], item, comfort_filter=False)
    return all_offers, flight_filter(all_offers, item, 2 if item.get("round_trip") else 1)


def skipped_note(all_offers, offers, item=None):
    """Про отсеянные билеты: «дешевле есть, но 31 ч в пути» (или вылет ночью, или без багажа)."""
    best = min(o["price"] for o in offers) if offers else None
    cheaper = [o for o in all_offers if o not in offers and (best is None or o["price"] < best)]
    if not cheaper:
        return ""
    o = min(cheaper, key=lambda x: x["price"])
    return f"💤 Дешевле есть: {fmt_price(o['price'])} ₽, но {skip_reason(o, item or {})} — такие не показываю"


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


def filter_offers(token, origin, f, comfort_filter=True):
    offers = fetch_cheapest(token, origin, f["destination"], f["depart"],
                            f.get("return") if f.get("round_trip") else None,
                            one_way=not f.get("round_trip"), direct=f.get("direct", False),
                            limit=api_limit(f))
    if not comfort_filter:
        return offers
    return flight_filter(offers, f, 2 if f.get("round_trip") else 1)


def offer_link(offer):
    return "https://www.aviasales.ru" + offer.get("link", "")


def short_date(iso):
    """2026-12-06T23:15:00+03:00 → «06.12 в 23:15» (или «06.12», если времени нет)."""
    text = f"{iso[8:10]}.{iso[5:7]}"
    return text + f" в {iso[11:16]}" if len(iso) >= 16 else text


def offer_line(offer, origin_name, route):
    ret = f", обратно {short_date(offer['return_at'])}" if offer.get("return_at") else ""
    return (f"{origin_name} → {route['city']}, вылет {short_date(offer['departure_at'])}{ret}\n"
            f"{trip_details(offer)}\n{offer_link(offer)}")


# ---------- Обратный билет ----------

RETURN_DAYS = (5, 30)  # по умолчанию обратный рейс ищем через 5–30 дней после вылета
BACK_PRESETS = [("Неделя", 6, 8), ("10 дней", 9, 11), ("2 недели", 13, 15),
                ("3 недели", 20, 22), ("Месяц", 28, 32), ("Любой срок", 5, 30)]


def back_window(item, dep):
    """Когда лететь обратно: (первый день, последний день) для вылета dep."""
    back = item.get("back") or {}
    if back.get("dates"):
        first, last = (date.fromisoformat(d) for d in back["dates"])
        return max(first, dep + timedelta(days=1)), last
    lo, hi = back.get("days") or RETURN_DAYS
    return dep + timedelta(days=lo), dep + timedelta(days=hi)


def back_text(item):
    """«через 5–30 дней», «через 2 недели», «25.12», «20.12–28.12»."""
    back = item.get("back") or {}
    if back.get("dates"):
        first, last = back["dates"]
        short = lambda d: f"{d[8:]}.{d[5:7]}"  # noqa: E731
        return short(first) if first == last else f"{short(first)}–{short(last)}"
    lo, hi = back.get("days") or RETURN_DAYS
    for label, plo, phi in BACK_PRESETS[:-1]:
        if (lo, hi) == (plo, phi):
            return "через " + {"Неделя": "неделю", "Месяц": "месяц"}.get(label, label)
    if hi - lo == 2:
        return f"примерно через {days_word(lo + 1)}"
    return f"через {days_word(lo)}" if lo == hi else f"через {lo}–{hi} дней"


def days_word(n):
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} дня"
    return f"{n} дней"


def window_queries(lo, hi):
    """Даты или месяцы для запроса к API, покрывающие окно [lo, hi]."""
    if lo == hi:
        return [lo.isoformat()]
    out, d = [], lo.replace(day=1)
    while d <= hi:
        out.append(d.strftime("%Y-%m"))
        d = (d + timedelta(days=32)).replace(day=1)
    return out


def return_info(token, origin, destination, offer, item):
    """Сколько стоит вернуться: (туда-обратно одним билетом, обратный билет отдельно)."""
    try:
        dep = date.fromisoformat(offer["departure_at"][:10])
        lo, hi = back_window(item, dep)
        if hi < lo:
            return None, None
        inside = lambda d: lo.isoformat() <= d[:10] <= hi.isoformat()  # noqa: E731
        rt, back = [], []
        for q in window_queries(lo, hi):
            rt += fetch_cheapest(token, origin, destination, dep.isoformat(), q, one_way=False,
                                 limit=api_limit(item))
            back += fetch_cheapest(token, destination, origin, q, limit=api_limit(item))
        rt = flight_filter([o for o in rt if inside(o.get("return_at", ""))], item, 2)
        back = flight_filter([o for o in back if inside(o["departure_at"])], item, back=True)
    except Exception as e:  # noqa: BLE001
        print(f"return_info {origin}-{destination}: {e!r}", file=sys.stderr)
        return None, None
    cheapest = lambda offers: min(offers, key=lambda o: o["price"]) if offers else None  # noqa: E731
    return cheapest(rt), cheapest(back)


def parse_back(text, today):
    """«12», «10-14», «2 недели», «25.12», «20.12-28.12», «декабрь» → поле back или строка-ошибка."""
    low = _norm(text)
    words = {"неделя": (6, 8), "неделю": (6, 8), "1 неделя": (6, 8), "10 дней": (9, 11),
             "2 недели": (13, 15), "две недели": (13, 15), "3 недели": (20, 22),
             "три недели": (20, 22), "месяц": (28, 32), "любой": (5, 30), "любой срок": (5, 30)}
    low = re.sub(r"^через ", "", low)
    if low in words:
        return {"days": list(words[low])}
    m = None if re.search(r"\d\.\d", text) else re.fullmatch(r"(\d{1,2})(?: (\d{1,2}))?(?: дн\w*| день)?", low)
    if m:
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else None
        lo, hi = (min(a, b), max(a, b)) if b else (max(1, a - 1), a + 1)
        if hi > 60:
            return "Можно не больше 60 дней. Например: 14 или 10-14."
        return {"days": [lo, hi]}
    fields = parse_dates(text, today)
    if isinstance(fields, str):
        return ("Не понял. Напиши, через сколько дней обратно (14 или 10-14) "
                "или дату (25.12 или 20.12-28.12).")
    first = fields["depart"]
    last = fields.get("return") or first
    if len(first) == 7:
        first = first + "-01"
    if len(last) == 7:
        y, mth = int(last[:4]), int(last[5:])
        last = ((date(y, mth, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).isoformat()
    return {"dates": [first, last]}


def pick_back_screen(item):
    i = item["id"]
    text = (f"↩️ {item['city']}: когда летим обратно?\n\nСейчас: {back_text(item)}.\n\n"
            "Выбери срок кнопкой, точный день в календаре или впиши свою дату.")
    btns = [(label, f"iy:{i}:{lo}-{hi}") for label, lo, hi in BACK_PRESETS]
    return text, rows(btns, 3) + [[("📅 День в календаре", f"ij:{i}"), ("✏️ Вписать свою дату", f"ie:{i}")],
                                  [("⬅️ Отмена", f"i:{i}")]]


BACK_HINT = ("Напиши, когда тебе нужно обратно:\n"
             "• 25.12 — точная дата\n"
             "• 20.12-28.12 — любой день в эти даты\n"
             "• 12 — через 12 дней после вылета\n"
             "• 10-14 — через 10–14 дней после вылета")


def brief(o):
    """«China Southern, пересадок: 1, 18 ч 40 мин» — коротко про рейс."""
    code = o.get("airline", "")
    there, back = o.get("transfers", 0), o.get("return_transfers", 0)
    parts = [AIRLINES.get(code, code)] if code else []
    if o.get("return_at"):
        parts.append("прямые" if there + back == 0 else f"пересадок: туда {there}, обратно {back}")
    else:
        parts.append(f"пересадок: {there}" if there else "прямой")
    if travel_minutes(o):
        parts.append(("всего " if o.get("return_at") else "") + fmt_minutes(travel_minutes(o)))
    return ", ".join(parts)


def return_text(offer, rt, back, item=None):
    """Строки про обратный путь под билетом «туда»."""
    lines = []
    if rt:
        lines.append(f"🔁 Туда-обратно одним билетом: {fmt_price(rt['price'])} ₽, "
                     f"обратно {short_date(rt['return_at'])} ({brief(rt)})")
    if back:
        lines.append(f"↩️ Обратно отдельно: {fmt_price(back['price'])} ₽, {short_date(back['departure_at'])} "
                     f"({brief(back)}), вместе с «туда» {fmt_price(offer['price'] + back['price'])} ₽")
    if not lines:
        lines.append("↩️ Обратных билетов" + (f" ({back_text(item)})" if item else "") + " не нашёл")
    return "\n".join(lines)


def shout(settings):
    """«ГОША СРОЧНО» — у каждого человека своё имя в уведомлениях."""
    return f"{settings.get('shout', 'ГОША')} СРОЧНО"


def format_alert(route, offer, reason, origin_name, extra="", who="ГОША СРОЧНО"):
    return (
        f"🔥 {who} {route['name']} {fmt_price(offer['price'])} ₽\n"
        f"{offer_line(offer, origin_name, route)}\n"
        + (f"{extra}\n" if extra else "")
        + f"Почему: {reason}"
    )


def alert_buttons(item, link=None, rt=None, pick=None):
    buy = [[("🎫 Купить билет", link)]] if link else []
    if rt:
        buy.append([("🔁 Купить туда-обратно", offer_link(rt))])
    buy += pick or []
    return buy + [[("⚙️ Настроить", f"i:{item['id']}"), ("⏸ Пауза", f"iz:{item['id']}")]]


# ---------- ⭐ Отложенные билеты ----------

SAVED_MAX = 50  # больше в корзине не храним
SAVED_RECHECK_HOURS = 2  # как часто сами проверяем цену отложенных


def flight_snapshot(o, origin, dest, from_name, to_name):
    """Билет на самолёт для «⭐ Отложенных»: что показать, где купить и как проверить цену."""
    rt = bool(o.get("return_at"))
    arrow = "⇄" if rt else "→"
    when = short_date(o["departure_at"]) + (f" – {short_date(o['return_at'])}" if rt else "")
    return {"kind": "flight", "title": f"✈️ {from_name} {arrow} {to_name}",
            "label": f"✈️ {from_name}{arrow}{to_name} · {fmt_price(o['price'])} ₽ · {when}",
            "text": f"Вылет {short_date(o['departure_at'])}"
                    + (f", обратно {short_date(o['return_at'])}" if rt else "") + "\n" + trip_details(o),
            "price": o["price"], "date": o["departure_at"][:10],
            "links": [["🎫 Купить билет", offer_link(o)]],
            "check": {"type": "f", "origin": origin, "dest": dest, "dep": o["departure_at"][:16],
                      "ret": o.get("return_at", "")[:16], "airline": o.get("airline", "")}}


def item_flight_snaps(item, offers, back=None):
    """Снимки билетов направления: «туда» (и туда-обратно) из offers, обратные — из back."""
    there = [flight_snapshot(o, item["origin"], item["destination"], item["origin_name"], item["city"])
             for o in offers if o]
    return there + [flight_snapshot(o, item["destination"], item["origin"], item["city"], item["origin_name"])
                    for o in back or [] if o]


def train_snapshot(o, t, back=False):
    """Билет на поезд (или пара туда-обратно) для «⭐ Отложенных». Без даты — None.

    back=True — это одиночный билет обратно (из города t["to"] в t["from"]).
    """
    pair = "there" in o
    legs = [o["there"], o["back"]] if pair else [o]
    if not all(leg.get("date") for leg in legs):
        return None  # цены «от» без даты: откладывать нечего
    names = [(t["from"], t["to"], t["from_name"], t["to_name"]), (t["to"], t["from"], t["to_name"], t["from_name"])]
    if back:
        names = names[1:]
    a, b = names[0][2], names[0][3]
    when = " / ".join(f"{leg['date'][8:]}.{leg['date'][5:7]} {leg['dep_time']}" for leg in legs)
    car = CAR_NAMES.get(o["car"], o["car"])
    return {"kind": "train", "title": f"🚆 {a} {'⇄' if pair else '→'} {b}",
            "label": f"🚆 {a}{'⇄' if pair else '→'}{b} · {fmt_price(o['price'])} ₽ · {when} · {o['train']} {car}",
            "text": train_pair_text(o, link=False) if pair else train_offer_line(o, False, False),
            "price": o["price"], "date": legs[0]["date"],
            "links": ([["🎫 Купить туда", o["there"]["link"]], ["🎫 Купить обратно", o["back"]["link"]]]
                      if pair else [["🎫 Купить билет", o["link"]]]),
            "check": {"type": "t", "legs": [{"from": n[0], "to": n[1], "date": leg["date"],
                                             "train": leg["train"], "car": leg["car"]}
                                            for n, leg in zip(names, legs)]}}


def snap_key(snap):
    return json.dumps(snap["check"], sort_keys=True, ensure_ascii=False)


def pick_button(settings, snaps):
    """Кнопка «⭐ Отложить» под сообщением с билетами (сами билеты запоминаем в настройках)."""
    items, seen = [], set()
    for snap in snaps:
        if snap and snap_key(snap) not in seen:
            seen.add(snap_key(snap))
            items.append(snap)
    if not items:
        return []
    picks = settings.setdefault("picks", {})
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    for token in [t for t, p in picks.items() if p["ts"] < week_ago][:50]:
        del picks[token]
    while len(picks) >= 150:  # старые списки выбрасываем
        del picks[min(picks, key=lambda t: picks[t]["ts"])]
    token = uuid.uuid4().hex[:8]
    picks[token] = {"ts": datetime.now(timezone.utc).isoformat(), "items": items}
    return [[("⭐ Отложить", f"sp:{token}")]]


def save_snap(settings, snap):
    """Кладёт билет в «⭐ Отложенные». Возвращает текст ответа."""
    saved = settings.setdefault("saved", [])
    if any(snap_key(x) == snap_key(snap) for x in saved):
        return "Этот билет уже в отложенных ⭐"
    if len(saved) >= SAVED_MAX:
        return f"В отложенных уже {SAVED_MAX} билетов — убери ненужные и попробуй снова."
    saved.append(dict(snap, id=uuid.uuid4().hex[:6], saved=datetime.now(timezone.utc).isoformat()))
    return f"⭐ Отложил: {snap['label']}"


def find_saved(settings, saved_id):
    return next((x for x in settings.get("saved", []) if x["id"] == saved_id), None)


def saved_now(s):
    """Последняя известная цена отложенного билета (None — не нашёл в продаже)."""
    return s["now_price"] if "now_price" in s else s["price"]


def diff_text(old, new):
    if new == old:
        return "та же цена"
    return f"{'дешевле' if new < old else 'дороже'} на {fmt_price(abs(new - old))} ₽"


def saved_text(s):
    saved_at = datetime.fromisoformat(s["saved"]).astimezone(MSK).strftime("%d.%m")
    lines = [f"⭐ {s['title']}", s["text"], "", f"Когда отложил ({saved_at}): {fmt_price(s['price'])} ₽"]
    if s.get("checked"):
        ts = datetime.fromisoformat(s["checked"]).astimezone(MSK).strftime("%d.%m %H:%M")
        now = saved_now(s)
        if now is None:
            lines.append(("Сейчас: мест в этом вагоне не вижу" if s["kind"] == "train"
                          else "Сейчас: этого рейса нет в свежих ценах — проверь по кнопке «Купить»")
                         + f" (мск {ts})")
        else:
            lines.append(f"Сейчас: {fmt_price(now)} ₽, {diff_text(s['price'], now)} (мск {ts})")
            if s.get("seats"):
                lines.append(f"Свободных мест: {s['seats']}")
    return "\n".join(lines)


def saved_buttons(s):
    return [[(label, url) for label, url in s["links"]],
            [("🔄 Проверить цену", f"sc:{s['id']}"), ("🗑 Убрать", f"sq:{s['id']}")],
            [("⬅️ Отложенные", "sl")]]


def saved_label(s):
    now = saved_now(s)
    mark = "" if now is None or now == s["price"] else (" 📉" if now < s["price"] else " 📈")
    price = "нет мест" if now is None and s["kind"] == "train" else k(now if now is not None else s["price"])
    icon, _, route = s["title"].partition(" ")
    return f"{icon} {s['date'][8:]}.{s['date'][5:7]} {route} · {price}{mark}"


def saved_screen(settings, note=""):
    saved = sorted(settings.get("saved", []), key=lambda x: x["date"])
    lines = [note] if note else []
    lines.append("⭐ Отложенные билеты\n")
    if not saved:
        lines.append("Пока пусто. Под уведомлениями и под «🔎 Цена сейчас» есть кнопка «⭐ Отложить»: "
                     "нажми её, и билет попадёт сюда, чтобы купить его позже.")
    else:
        lines.append("Нажми на билет, чтобы купить его, проверить цену или убрать.\n"
                     "Раз в пару часов сам проверяю цены и напишу, если отложенный билет подешевеет "
                     "или на поезд закончатся места. 📉 — подешевел, 📈 — подорожал.")
    buttons = [[(saved_label(x), f"so:{x['id']}")] for x in saved]
    if saved:
        buttons.append([("🔄 Проверить все цены", "sa")])
    return "\n".join(lines), buttons + [[("⬅️ В меню", "home")]]


def pick_screen(settings, token):
    entry = settings.get("picks", {}).get(token)
    if not entry:
        return None
    keys = {snap_key(x) for x in settings.get("saved", [])}
    buttons = [[(("✅ " if snap_key(it) in keys else "") + it["label"], f"sv:{token}:{i}")]
               for i, it in enumerate(entry["items"])]
    return ("⭐ Какой билет отложить? Можно несколько. Нажми ещё раз, чтобы убрать.",
            buttons + [[("⭐ Все отложенные", "sl")]])


def recheck_saved(token, s, now):
    """Узнаёт свежую цену отложенного билета. (старая цена, новая цена) или None при ошибке."""
    c = s["check"]
    try:
        if c["type"] == "f":
            offers = fetch_cheapest(token, c["origin"], c["dest"], c["dep"][:10], c["ret"][:10] or None,
                                    one_way=not c["ret"], limit=100)
            same = [o for o in offers if o["departure_at"][:16] == c["dep"]
                    and o.get("airline", "") == c["airline"] and o.get("return_at", "")[:16] == c["ret"]]
            new = min((o["price"] for o in same), default=None)
            if same:
                s["links"][0][1] = offer_link(min(same, key=lambda o: o["price"]))
        else:
            new, seats = 0, []
            for i, leg in enumerate(c["legs"]):
                if i:
                    time.sleep(1)
                same = [o for o in fetch_train_day(leg["from"], leg["to"], leg["date"])
                        if o["train"] == leg["train"] and o["car"] == leg["car"]]
                if not same:
                    new = None
                    break
                cheapest = min(same, key=lambda o: o["price"])
                new += cheapest["price"]
                seats.append(cheapest.get("seats", 0))
            s["seats"] = min(seats) if new else 0
    except Exception as e:  # noqa: BLE001
        print(f"saved check error {s.get('title')}: {e!r}", file=sys.stderr)
        return None
    old = saved_now(s)
    s["now_price"] = new
    s["checked"] = now.isoformat()
    return old, new


def check_saved(tg, token, settings, now):
    """Следит за отложенными: убирает прошедшие, пишет, если подешевели или кончились места."""
    today = now.astimezone(MSK).date().isoformat()
    sent, checked = 0, 0
    for s in list(settings.get("saved", [])):
        if s["date"] < today:
            settings["saved"].remove(s)
            tg.send(f"⌛ Убрал из отложенных, дата прошла: {saved_label(s)}")
            continue
        last = s.get("checked")
        if checked >= 15 or last and now - datetime.fromisoformat(last) < timedelta(hours=SAVED_RECHECK_HOURS):
            continue
        checked += 1
        res = recheck_saved(token, s, now)
        if not res:
            continue
        old, new = res
        head = ""
        if new is not None and old is not None and new <= old - 100:
            head = f"📉 Отложенный билет подешевел: {fmt_price(old)} → {fmt_price(new)} ₽"
        elif new is None and old is not None and s["kind"] == "train":
            head = "⚠️ На отложенный поезд в этом вагоне больше нет мест"
        elif new is not None and old is None and s["kind"] == "train":
            head = f"✅ На отложенный поезд снова есть места: {fmt_price(new)} ₽"
        if head:
            tg.send(f"{head}\n\n{saved_text(s)}", saved_buttons(s))
            sent += 1
    return sent


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
        key = f"{origin}-{route['destination']}" + "".join(
            f"|{f}:{''.join(route[f]) if isinstance(route[f], list) else 1}"
            for f in ("times", "back_times", "bag") if route.get(f))
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
            rt, back = return_info(token, origin, route["destination"], o, route)
            tg.send(format_alert(route, o, reason, route["origin_name"], return_text(o, rt, back, route),
                                 shout(settings)),
                    alert_buttons(route, offer_link(o), rt,
                                  pick_button(settings, item_flight_snaps(route, [o, rt], [back]))))
            sent[sent_key] = now.isoformat()
            alerts += 1

        add_history(history, key, offers, cfg, now)
        print(f"{key}: минимум {min(o['price'] for o in offers)} ₽")

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
        rt = back = extra = None
        if not f.get("round_trip"):
            rt, back = return_info(token, origin, f["destination"], o, f)
            extra = return_text(o, rt, back, f)
        tg.send(f"🎯 {shout(settings)} {f['name']} {fmt_price(o['price'])} ₽{kind}\n"
                f"{offer_line(o, f['origin_name'], f)}\n" + (f"{extra}\n" if extra else "")
                + f"Подбор: {filter_text(f)}",
                alert_buttons(f, offer_link(o), rt, pick_button(settings, item_flight_snaps(f, [o, rt], [back]))))
        sent[sent_key] = now.isoformat()
        alerts += 1
    alerts += check_trains(tg, cfg, settings, history, sent, now)
    alerts += check_saved(tg, token, settings, now)
    print(f"Отправлено алертов: {alerts}")


def add_history(history, key, offers, cfg, now):
    points = history.get(key, [])
    points.append({"ts": now.isoformat(), "price": min(o["price"] for o in offers)})
    cutoff = (now - timedelta(days=cfg["history_days"])).isoformat()
    history[key] = [p for p in points if p["ts"] >= cutoff]


def check_trains(tg, cfg, settings, history, sent, now):
    alerts = 0
    for t in list(settings.get("trains", [])):
        if t.get("dates") and t["dates"][1] < now.astimezone(MSK).date().isoformat():
            settings["trains"].remove(t)
            tg.send(f"⌛ Даты прошли, убрал поезд: {item_label('train', t)}")
            continue
        if t.get("paused"):
            continue
        offers = train_trip_offers(t)
        if not offers:
            continue
        remember_price(t, offers, now)
        key = train_key(t)
        deals = sorted(find_deals(t, offers, history.get(key, []), cfg, now),
                       key=lambda d: d[0]["price"])
        for o, reason in deals[:1]:
            # Цены у поездов меняются редко: тот же поезд по той же цене не повторяем,
            # пока запись хранится в sent (неделю).
            sent_key = f"{key}|{o.get('date', '')}|{o['train']}|{o['car']}|{o['price']}"
            if sent_key in sent:
                continue
            if "there" in o:
                text = (f"🚆 {shout(settings)} ПОЕЗД {t['from_name']} ⇄ {t['to_name']} "
                        f"{fmt_price(o['price'])} ₽ туда-обратно\n\n{train_pair_text(o)}\n\nПочему: {reason}")
            else:
                text = (f"🚆 {shout(settings)} ПОЕЗД {t['from_name']} → {t['to_name']} "
                        f"{fmt_price(o['price'])} ₽\n{train_offer_line(o)}\nПочему: {reason}")
            tg.send(text, train_buy_buttons(o) + alert_buttons(t, pick=pick_button(settings, [train_snapshot(o, t)])))
            sent[sent_key] = now.isoformat()
            alerts += 1
        add_history(history, key, offers, cfg, now)
        print(f"{key}: минимум {min(o['price'] for o in offers)} ₽")
    return alerts


def ensure_origins(settings):
    """Дополняет старые настройки: город вылета и id у каждого направления, список городов."""
    origins = settings.setdefault("origins", [])
    known = {o["code"] for o in origins}
    settings.setdefault("trains", [])
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


def prices_now(token, cfg, settings, origin=None, snaps=None):
    """Цены по направлениям. origin — только этот город вылета (без поездов).

    snaps — список, куда сложить показанные билеты для кнопки «⭐ Отложить».
    """
    snaps = [] if snaps is None else snaps
    lines = [f"💰 Самые дешёвые билеты на {cfg['months_ahead']} мес.:"]
    groups = grouped_items(settings)
    if origin:
        groups = [g for g in groups if g[1][0][1]["origin"] == origin]
    trains = [] if origin else settings.get("trains", [])
    if not groups and not trains:
        return "Направлений пока нет, добавь их в меню 🏠"
    for origin_name, pairs in groups:
        lines.append(f"\n🛫 Вылет: {origin_name}")
        for kind, item in pairs:
            title = item["name"] if kind == "route" else "🎯 " + filter_text(item)
            all_offers, offers = item_offers(token, kind, item, cfg)
            note = skipped_note(all_offers, offers, item)
            if not offers:
                lines.append(f"{title}: билетов не нашёл" + (f"\n{note}" if note else ""))
                continue
            parts = flight_parts(offers)
            if len(parts) > 1:
                note = ("🕐 По времени вылета: "
                        + ", ".join(f"{DAY_PART_NAMES[c].split(' ', 1)[1].lower()} от {k(o['price'])}"
                                    for c, o in parts) + (f"\n{note}" if note else ""))
            best = min(offers, key=lambda o: o["price"])
            back_line = ""
            rt = back = None
            if not item.get("round_trip"):
                rt, back = return_info(token, item["origin"], item["destination"], best, item)
                back_line = "\n" + return_text(best, rt, back, item)
            snaps += item_flight_snaps(item, [best, rt], [back])
            lines.append(f"{title}: {fmt_price(best['price'])} ₽\n"
                         f"{offer_line(best, origin_name, item)}{back_line}" + (f"\n{note}" if note else ""))
    if trains:
        lines.append("\n" + train_prices(settings, snaps))
    return "\n".join(lines)


# ---------- Поезда ----------

def _norm(text):
    text = text.lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


# [[код, название, популярность], ...] — по убыванию популярности.
STATIONS = [(code, name, weight, _norm(name))
            for code, name, weight in load(STATIONS_FILE, [])]
STATION_NAMES = {code: name for code, name, _, _ in STATIONS}
STATION_ALIASES = {"питер": "2004000", "спб": "2004000", "петербург": "2004000",
                   "мск": "2000000", "екб": "2030000", "нижний": "2060000", "ростов": "2064000"}
POPULAR_STATIONS = [
    ("2000000", "Москва"), ("2004000", "Санкт-Петербург"), ("2060615", "Казань"),
    ("2060000", "Нижний Новгород"), ("2030000", "Екатеринбург"), ("2064130", "Сочи"),
    ("2064150", "Адлер"), ("2064788", "Краснодар"), ("2064188", "Анапа"),
    ("2064000", "Ростов-на-Дону"), ("2078001", "Симферополь"), ("2064170", "Минеральные Воды"),
]
CARS = {"any": "любой", "plazcard": "плацкарт", "coupe": "купе", "sedentary": "сидячий", "lux": "СВ"}
CAR_NAMES = dict(CARS, soft="люкс")
# Части суток, как на Туту.ру: код, название, часы отправления [с, до).
DAY_PARTS = [("m", "🌅 Утро", 6, 12), ("d", "☀️ День", 12, 18),
             ("e", "🌆 Вечер", 18, 24), ("n", "🌙 Ночь", 0, 6)]
DAY_PART_NAMES = {code: name for code, name, _, _ in DAY_PARTS}
TRAIN_PRICES = [1000, 1500, 2000, 3000, 4000, 5000, 7000, 10000, 15000]


def station_name(code):
    return dict(POPULAR_STATIONS).get(code) or STATION_NAMES.get(code, code)


def find_station(text):
    """Станция по названию: dict(code, name), список вариантов или строка-ошибка."""
    q = _norm(text)
    if not q:
        return "Напиши название города или станции."
    if q in STATION_ALIASES:
        code = STATION_ALIASES[q]
        return {"code": code, "name": station_name(code)}
    found = ([s for s in STATIONS if s[3] == q]
             or [s for s in STATIONS if s[3].startswith(q)]
             or [s for s in STATIONS if q in s[3]])
    if not found:
        return f"Не нашёл станцию «{text.strip()}». Проверь написание."
    # Явный лидер по популярности (например, «Екатеринбург Пасс.») выбираем сразу.
    if len(found) == 1 or found[0][3] == q or found[0][2] >= 3 * found[1][2]:
        return {"code": found[0][0], "name": station_name(found[0][0])}
    return [{"code": s[0], "name": s[1]} for s in found[:6]]


TUTU_CARS = {"RESERVED_SEAT": "plazcard", "COMPARTMENT": "coupe", "SEDENTARY": "sedentary",
             "LUX": "lux", "SOFT": "soft"}


def part_of(hhmm):
    """«07:30» → часть суток m, d, e, n (или "", если время неизвестно)."""
    if not (hhmm or "")[:2].isdigit():
        return ""
    return next(code for code, _, lo, hi in DAY_PARTS if lo <= int(hhmm[:2]) < hi)


def day_part(o):
    """Часть суток, когда отправляется поезд."""
    return part_of(o.get("dep_time"))


def flight_parts(offers):
    """Самый дешёвый рейс в каждой части суток (по времени вылета): [(код, билет)]."""
    best = {}
    for o in offers:
        part = part_of(o.get("departure_at", "")[11:16])
        if part and (part not in best or o["price"] < best[part]["price"]):
            best[part] = o
    return [(code, best[code]) for code, *_ in DAY_PARTS if code in best]


def flight_short(o):
    """«31 000 ₽ · 15.10 в 01:30 · Аэрофлот, прямой, 9 ч 30 мин»."""
    back = f", обратно {short_date(o['return_at'])}" if o.get("return_at") else ""
    return f"{fmt_price(o['price'])} ₽ · {short_date(o['departure_at'])}{back} · {brief(o)}"


def day_part_label(code):
    name, lo, hi = next((n, a, b) for c, n, a, b in DAY_PARTS if c == code)
    return f"{name} ({lo}–{hi})"


def times_text(times):
    """["m", "e"] → «утро, вечер»; пусто → «любое»."""
    if not times:
        return "любое"
    return ", ".join(DAY_PART_NAMES[c].split(" ", 1)[1].lower() for c, *_ in DAY_PARTS if c in times)


def train_link(frm, to, day=None):
    link = f"{TUTU}/poezda/rasp_d.php?nnst1={frm}&nnst2={to}"
    if day:
        link += "&date=" + date.fromisoformat(day).strftime("%d.%m.%Y")
    return link


def train_key(t):
    dates = "-".join(t["dates"]) if t.get("dates") else "any"
    back = "|back:" + "-".join(t["back_dates"]) if t.get("back_dates") else ""
    if t.get("times"):
        back += "|t:" + "".join(t["times"])
    if t.get("back_dates") and t.get("back_times"):
        back += "|bt:" + "".join(t["back_times"])
    return f"T|{t['from']}-{t['to']}|{t.get('car', 'any')}|{dates}{back}"


def fetch_trains(frm, to):
    """Все поезда между станциями без даты (цены «от»): [{price, car, train, ...}]."""
    url = TRAINS_URL + "?" + urllib.parse.urlencode(
        {"service": "tutu_trains", "term": frm, "term2": to})
    data = http_get_json(url)
    link = train_link(frm, to)
    offers = []
    for t in data.get("trips") or []:
        for c in t.get("categories") or []:
            if c.get("price"):
                offers.append({"price": int(c["price"]), "car": c.get("type", ""),
                               "train": t.get("trainNumber", ""), "name": t.get("name", ""),
                               "station": STATION_NAMES.get(t.get("departureStation"), ""),
                               "dep_time": (t.get("departureTime") or "")[:5],
                               "seconds": int(t.get("travelTimeInSeconds") or 0), "link": link})
    return offers


def fetch_train_day(frm, to, day):
    """Поезда на дату day (YYYY-MM-DD) с точными ценами и свободными местами."""
    body = {"routes": [{"departureStationCode": frm, "arrivalStationCode": to,
                        "departureDate": day}],
            "searchId": str(uuid.uuid4()), "source": "trainOffers"}
    data = http_post_json(TUTU_OFFERS_URL, body, {"Origin": TUTU, "Referer": TUTU + "/"})
    if isinstance(data, list):
        data = data[0] if data else {}
    dic = data.get("dictionary") or {}
    com, tr = dic.get("common") or {}, dic.get("train") or {}
    fares, conditions = com.get("fareApplications") or {}, tr.get("conditions") or {}
    offers = {}  # один поезд, вагон и цена — одна строка, места складываем
    for off in ((data.get("offers") or {}).get("actual") or {}).values():
        try:
            seg = com["segments"][com["routes"][off["routeIds"][0]]["segmentIds"][0]]
        except (KeyError, IndexError, TypeError):
            continue
        voyage = (tr.get("voyages") or {}).get(seg.get("voyageNumber")) or {}
        vehicle = (tr.get("vehicles") or {}).get(seg.get("vehicleId")) or {}
        point = (tr.get("points") or {}).get(str(seg.get("departureGeoPointId"))) or {}
        dep = seg.get("departureDateTime") or ""
        for var in off.get("offerVariants") or []:
            if (var.get("status") or {}).get("saleStatus") != "ALLOWED":
                continue
            # priceOld — обычная цена; price бывает со скидкой только для карты Альфа-банка.
            price = (var.get("priceOld") or var.get("price") or {}).get("value") or {}
            if not price.get("amount"):
                continue
            fas = [fares.get(i) or {} for ids in (var.get("fareApplications") or {}).values()
                   for i in ids]
            cond = conditions.get(fas[0].get("segmentConditions")) or {} if fas else {}
            o = {"price": round(price["amount"] / (price.get("fraction") or 100)),
                 "car": TUTU_CARS.get(cond.get("carType"), ""),
                 "train": voyage.get("numberForPassengers") or voyage.get("number", ""),
                 "name": vehicle.get("name", ""),
                 "station": (point.get("name") or {}).get("nominative", ""),
                 "date": dep[:10] or day, "dep_time": dep[11:16],
                 "seconds": int(seg.get("duration") or 0) * 60,
                 "seats": sum(fa.get("seats", 0) for fa in fas),
                 "link": train_link(frm, to, dep[:10] or day)}
            same = (o["train"], o["date"], o["car"], o["price"])
            if same in offers:
                offers[same]["seats"] += o["seats"]
            else:
                offers[same] = o
    return list(offers.values())


def date_range(first, last):
    d = date.fromisoformat(first)
    while d.isoformat() <= last:
        yield d.isoformat()
        d += timedelta(days=1)


def train_offers(item):
    """Поезда по направлению с учётом дат и типа вагона."""
    offers = []
    if item.get("dates"):
        today = datetime.now(MSK).date().isoformat()
        for i, day in enumerate(d for d in date_range(*item["dates"]) if d >= today):
            if i:
                time.sleep(1)  # не дёргаем Туту.ру слишком часто
            try:
                offers += fetch_train_day(item["from"], item["to"], day)
            except Exception as e:  # noqa: BLE001
                print(f"trains error {item['from']}->{item['to']} {day}: {e}", file=sys.stderr)
    else:
        try:
            offers = fetch_trains(item["from"], item["to"])
        except Exception as e:  # noqa: BLE001
            print(f"trains error {item['from']}->{item['to']}: {e}", file=sys.stderr)
    car = item.get("car", "any")
    if car != "any":
        offers = [o for o in offers if o["car"] == car]
    if item.get("times"):  # только удобное время отправления (если время неизвестно — оставляем)
        offers = [o for o in offers if day_part(o) in item["times"] or not day_part(o)]
    return offers


def train_back_item(item):
    """Направление «обратно» для поезда туда-обратно."""
    return dict(item, **{"from": item["to"], "to": item["from"], "dates": item["back_dates"],
                         "times": item.get("back_times")})


def train_both(item):
    """Поезда туда и (если нужно) обратно: (there, back)."""
    there = train_offers(item)
    if not item.get("back_dates") or not there:
        return there, []
    time.sleep(1)
    return there, train_offers(train_back_item(item))


def train_trip_offers(item, both=None):
    """Билеты по направлению. Для туда-обратно — самая дешёвая пара {price, there, back}."""
    there, back = both or train_both(item)
    if not item.get("back_dates") or not there:
        return there

    def moment(o, extra=0):
        start = datetime.fromisoformat(f"{o['date']}T{o.get('dep_time') or '00:00'}")
        return start + timedelta(seconds=extra)

    best = None
    for a in there:
        arrive = moment(a, a.get("seconds", 0) + 3600)  # час на пересадку с поезда на поезд
        for b in back:
            if moment(b) < arrive:
                continue
            if best is None or a["price"] + b["price"] < best["price"]:
                best = {"price": a["price"] + b["price"], "there": a, "back": b,
                        "train": f"{a['train']}+{b['train']}", "car": a["car"],
                        "date": a["date"], "link": a["link"]}
    return [best] if best else []


def train_pair_text(o, link=True):
    return (f"➡️ Туда: {fmt_price(o['there']['price'])} ₽\n{train_offer_line(o['there'], link=link)}\n\n"
            f"⬅️ Обратно: {fmt_price(o['back']['price'])} ₽\n{train_offer_line(o['back'], link=link)}")


def train_buy_buttons(o):
    if "there" in o:
        return [[("🎫 Купить туда", o["there"]["link"]), ("🎫 Купить обратно", o["back"]["link"])]]
    return [[("🎫 Купить билет", o["link"])]]


def train_short(o, car=True, day=False):
    """Одна строка: «1 087 ₽ · 07:30 · поезд 742У «Ласточка» · сидячий · 4 ч 10 мин · мест 36»."""
    hours, minutes = divmod(o["seconds"] // 60, 60)
    when = (f"{o['date'][8:]}.{o['date'][5:7]} " if day and o.get("date") else "") + (o["dep_time"] or "?")
    parts = [f"{fmt_price(o['price'])} ₽", when,
             f"поезд {o['train']}" + (f" «{o['name']}»" if o["name"] else "")]
    if car:
        parts.append(CAR_NAMES.get(o["car"], o["car"]))
    parts.append(f"{hours} ч {minutes} мин в пути")
    if o.get("seats"):
        parts.append(f"мест {o['seats']}")
    return " · ".join(parts)


def by_day_part(offers, per_part):
    """Самые дешёвые разные поезда в каждой части суток: [(код, [поезда по времени])]."""
    groups = []
    for code in [c for c, *_ in DAY_PARTS] + [""]:
        best = []
        for o in sorted((o for o in offers if day_part(o) == code), key=lambda o: o["price"]):
            if all((o["train"], o.get("date")) != (b["train"], b.get("date")) for b in best):
                best.append(o)
            if len(best) == per_part:
                break
        if best:
            groups.append((code, sorted(best, key=lambda o: (o.get("date", ""), o["dep_time"]))))
    return groups


def day_parts_text(item, offers, per_part):
    """Билеты по частям суток: утро, день, вечер, ночь."""
    car = item.get("car", "any") == "any"
    day = bool(item.get("dates")) and item["dates"][0] != item["dates"][1]
    lines = []
    for code, group in by_day_part(offers, per_part):
        label = day_part_label(code) if code else "🕐 Время не указано"
        if per_part == 1:
            lines.append(f"{label.split(' (')[0]}: {train_short(group[0], car, day)}")
        else:
            lines.append(label)
            lines += [f"• {train_short(o, car, day)}" for o in group]
    return "\n".join(lines)


def train_day_buttons(offers):
    """Кнопки «Купить»: одна, если все поезда в один день, иначе по кнопке на каждый день."""
    links = {}
    for o in sorted(offers, key=lambda o: o.get("date", "")):
        links.setdefault(o["link"], o.get("date"))
    if len(links) == 1:
        return [[("🎫 Купить билет", next(iter(links)))]]
    buttons = [(f"🎫 {d[8:]}.{d[5:7]}" if d else "🎫 Купить", link) for link, d in links.items()]
    return [buttons[i:i + 4] for i in range(0, len(buttons), 4)]


def train_offer_line(o, note=True, link=True):
    hours, minutes = divmod(o["seconds"] // 60, 60)
    name = f" «{o['name']}»" if o["name"] else ""
    when = f"{o['date'][8:]}.{o['date'][5:7]} в {o['dep_time']}" if o.get("date") else f"в {o['dep_time']}"
    lines = [f"{CAR_NAMES.get(o['car'], o['car']).capitalize()}, поезд {o['train']}{name}",
             f"Отправление {when}" + (f" ({o['station']})" if o["station"] else "")
             + f", в пути {hours} ч {minutes} мин"]
    if o.get("seats"):
        lines.append(f"Свободных мест: {o['seats']}")
    if note and not o.get("date"):
        lines.append("Это цена «от»: даты и места смотри на Туту.ру")
    if link:
        lines.append(o["link"])
    return "\n".join(lines)


def train_title(t):
    arrow = "⇄" if t.get("back_dates") else "→"
    title = f"{t['from_name']} {arrow} {t['to_name']}"
    if t.get("dates"):
        title += " " + train_dates_short(t)
    if t.get("back_dates"):
        title += ", обратно " + train_dates_short(t, "back_dates")
    if t.get("car", "any") != "any":
        title += f", {CARS[t['car']]}"
    return title


def train_dates_short(t, field="dates"):
    a, b = (f"{d[8:]}.{d[5:7]}" for d in t[field])
    return a if a == b else f"{a}–{b}"


def train_prices(settings, snaps=None):
    snaps = [] if snaps is None else snaps
    lines = ["🚆 Поезда:"]
    trains = settings.get("trains", [])
    if not trains:
        return "Поездов пока нет, добавь их в меню 🏠 → 🚆 Поезда"
    for t in trains:
        both = train_both(t)
        offers = train_trip_offers(t, both)
        times = f" (отправление: {times_text(t.get('times'))})" if t.get("times") else ""
        if not offers:
            lines.append(f"\n{train_title(t)}{times}: билетов не нашёл")
            continue
        best = min(offers, key=lambda o: o["price"])
        snaps.append(train_snapshot(best, t))
        if "there" in best:
            lines.append(f"\n{train_title(t)}{times}: от {fmt_price(best['price'])} ₽ туда-обратно\n"
                         f"➡️ Туда:\n{day_parts_text(t, both[0], 1)}\n"
                         f"⬅️ Обратно:\n{day_parts_text(t, both[1], 1)}\n"
                         f"Купить туда: {best['there']['link']}\nКупить обратно: {best['back']['link']}")
        else:
            note = "" if best.get("date") else "\nЭто цены «от»: даты и места смотри на Туту.ру"
            lines.append(f"\n{train_title(t)}{times}: от {fmt_price(best['price'])} ₽\n"
                         f"{day_parts_text(t, offers, 1)}{note}\nКупить: {best['link']}")
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


# Что нового: каждый человек один раз получает записи, которых ещё не видел.
NEWS_ITEMS = [
    ("2026-10-08-saved",
     "⭐ Отложенные билеты. Под уведомлениями и под «🔎 Цена сейчас» есть кнопка «⭐ Отложить», "
     "а внизу — кнопка «⭐ Отложенные»: там билеты можно купить, проверить цену или убрать. "
     "Я сам слежу за их ценой и напишу, если отложенный билет подешевеет.\n\n"
     "✈️ В карточке направления есть «🕐 Время вылета» (утро, день, вечер, ночь) "
     "и «🧳 С багажом».\n\n"
     "🚆 У поездов можно выбрать время отправления, а «Цена сейчас» показывает поезда "
     "утром, днём, вечером и ночью."),
    ("2026-10-10-dates",
     "📅 Дату вылета теперь выбираешь прямо при добавлении: «➕ Направление» → город → день "
     "в календаре, весь месяц или «🗓 Любые даты». Потом «Когда обратно?» и порог цены.\n\n"
     "У направлений, которые уже есть, нажми «📅 Вылет: любой день» в карточке, чтобы выбрать дату."),
]
NEWS_ID = NEWS_ITEMS[-1][0]


def news_text(seen):
    """Новости, которых человек ещё не видел (seen — id последней увиденной), или None."""
    ids = [i for i, _ in NEWS_ITEMS]
    if seen == NEWS_ID:
        return None
    start = ids.index(seen) + 1 if seen in ids else 0
    return "🆕 Бот обновился:\n\n" + "\n\n".join(text for _, text in NEWS_ITEMS[start:])


HELP = (
    "Я слежу за ценами на авиабилеты и билеты на поезда и пишу, когда находится дешёвый билет.\n\n"
    "Всё настраивается кнопками в меню 🏠. А ещё можно писать одной строкой:\n"
    "• откуда Казань\n"
    "• добавить Пхукет 20000, Бали 35000\n"
    "• подбор Пхукет 15.12-25.12 до 60000\n"
    "Новые направления добавляются в город вылета, который открыт в меню.\n"
    "Поезда: меню 🏠 → «🚆 Поезда»."
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
    return f"{round(n / 1000, 1):g}к" if n >= 1000 else str(n)


def rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def items_of(settings, code):
    return ([("route", r) for r in settings["routes"] if r["origin"] == code]
            + [("filter", f) for f in settings.get("filters", []) if f["origin"] == code])


def origin_name(settings, code):
    return next((o["name"] for o in settings["origins"] if o["code"] == code), code)


ITEM_LISTS = {"route": "routes", "filter": "filters", "train": "trains"}


def find_item(settings, item_id):
    for kind, key in ITEM_LISTS.items():
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
    if kind == "train":
        dates = " " + train_dates_short(item) if item.get("dates") else ""
        if item.get("back_dates"):
            dates += " ⇄ " + train_dates_short(item, "back_dates")
        car = f" · {CARS[item['car']]}" if item.get("car", "any") != "any" else ""
        limit = f"до {k(item['max_price'])}" if item.get("max_price") else "падения"
        arrow = "⇄" if item.get("back_dates") else "→"
        return f"{pause}🚆 {item['from_name']} {arrow} {item['to_name']}{dates}{car} · {limit}{last}"
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
    lines = ["🏠 Главное меню\n"]
    buttons = []
    for o in settings["origins"]:
        items = items_of(settings, o["code"])
        n_r = sum(1 for kind, _ in items if kind == "route")
        lines.append(f"🛫 {o['name']}: направлений {n_r}, подборов {len(items) - n_r}")
        buttons.append([(f"🛫 {o['name']}  ({len(items)})", f"o:{o['code']}")])
    if not settings["origins"]:
        lines.append("Пока нет городов вылета.")
    trains = settings.get("trains", [])
    lines.append(f"🚆 Поезда: направлений {len(trains)}")
    lines.append("\nНажми на город вылета или на «🚆 Поезда», чтобы настроить направления.")
    buttons.append([("➕ Город вылета", "no"), ("💰 Все цены", "pa")])
    buttons.append([(f"🚆 Поезда  ({len(trains)})", "t")])
    buttons.append([(f"⭐ Отложенные  ({len(settings.get('saved', []))})", "sl")])
    if settings.get("owner"):
        buttons.append([("👥 Друзья", "users")])
    return "\n".join(lines), buttons


def trains_screen(settings):
    trains = settings.get("trains", [])
    lines = ["🚆 Поезда\n"]
    if trains:
        lines.append("Сообщу, когда билет станет дешевле порога или резко подешевеет.\n"
                     "С датами — точные цены и места на эти дни, без дат — цены «от».\n"
                     "Нажми на направление, чтобы изменить его.")
    else:
        lines.append("Направлений пока нет. Добавь первое кнопкой ниже 👇")
    buttons = [[(item_label("train", t), f"i:{t['id']}")] for t in trains]
    buttons += [[("➕ Поезд", "ta"), ("💰 Цены на поезда", "tp")], [("⬅️ В меню", "home")]]
    return "\n".join(lines), buttons


def origin_screen(settings, code):
    name = origin_name(settings, code)
    items = items_of(settings, code)
    lines = [f"🛫 Вылет: {name}\n"]
    if items:
        lines.append("📍 — любые даты: сообщу, когда цена упадёт ниже порога\n"
                     "🎯 — конкретные даты вылета\n"
                     "Нажми на направление, чтобы изменить его.")
    else:
        lines.append("Направлений пока нет. Добавь первое кнопкой ниже 👇")
    buttons = [[(item_label(kind, item), f"i:{item['id']}")] for kind, item in items]
    buttons += [
        [("➕ Направление", f"a:{code}")],
        [("💰 Цены сейчас", f"p:{code}"), ("🗑 Удалить город", f"xq:{code}")],
        [("⬅️ Все города", "home")],
    ]
    return "\n".join(lines), buttons


def last_price_line(item):
    ts = datetime.fromisoformat(item["last_ts"]).astimezone(MSK).strftime("%d.%m %H:%M")
    return f"Последняя цена: {fmt_price(item['last_price'])} ₽ (мск {ts})"


def train_screen(item, note=""):
    i = item["id"]
    lines = [note] if note else []
    rt = bool(item.get("back_dates"))
    lines.append(f"🚆 {item['from_name']} {'⇄' if rt else '→'} {item['to_name']}")
    if item.get("dates"):
        lines.append(f"Туда: {train_dates_short(item)} (точные цены и места по данным Туту.ру)")
        lines.append("Обратно: " + (train_dates_short(item, "back_dates") if rt else "не нужно"))
    else:
        lines.append("Даты: любые (цены «от» по данным Туту.ру, без конкретной даты)")
    lines.append("Вагон: " + CARS[item.get("car", "any")])
    lines.append("Отправление: " + times_text(item.get("times"))
                 + (", обратно: " + times_text(item.get("back_times")) if rt else ""))
    what = "билеты туда и обратно вместе" if rt else "билет"
    lines.append(f"Сообщу, когда {what} дешевле {fmt_price(item['max_price'])} ₽ "
                 "или резко подешевеет." if item.get("max_price")
                 else f"Сообщу, когда {what} резко подешевеет.")
    if item.get("last_price"):
        lines.append(last_price_line(item))
    lines.append("⏸ На паузе: не присылаю уведомления" if item.get("paused") else "✅ Слежу")
    buttons = [
        [("− 1к", f"ip:{i}:-1000"), ("− 500", f"ip:{i}:-500"),
         ("+ 500", f"ip:{i}:500"), ("+ 1к", f"ip:{i}:1000")],
        [("✏️ Своя цена", f"it:{i}")],
        [("📅 Туда: " + train_dates_short(item) if item.get("dates") else "📅 Даты", f"id:{i}")]
        + ([("🔁 Обратно: " + (train_dates_short(item, "back_dates") if rt else "нет"), f"ij:{i}")]
           if item.get("dates") else []),
        [("🕐 Время отправления: " + times_text(item.get("times"))
          + (" / " + times_text(item.get("back_times")) if rt else ""), f"iw:{i}")],
        [("🛏 Вагон: " + CARS[item.get("car", "any")], f"iv:{i}"),
         ("▶️ Возобновить" if item.get("paused") else "⏸ Пауза", f"iz:{i}")],
        [("🔎 Цена сейчас", f"in:{i}"), ("🗑 Удалить", f"iq:{i}")],
        [("⬅️ Поезда", "t")],
    ]
    return "\n".join(lines), buttons


def times_screen(kind, item):
    i = item["id"]
    if kind == "train":
        rt = bool(item.get("back_dates"))
        lines = [f"🕐 {train_title(item)}\n",
                 "Во сколько тебе удобно отправляться? Отметь одно или несколько — "
                 "буду искать и присылать только такие поезда."]
    else:
        rt = True  # у самолётов всегда есть «обратно»: обратный рейс или цена обратного билета
        lines = [f"🕐 {item['origin_name']} → {item['city']}\n",
                 "Во сколько тебе удобно вылетать? Отметь одно или несколько — буду искать "
                 "и присылать только такие рейсы. «Обратно» — время вылета обратного рейса."]
    buttons = []
    for field, title in (("times", "➡️ Туда"), ("back_times", "⬅️ Обратно")):
        if field == "back_times" and not rt:
            break
        chosen = item.get(field) or []
        if rt:
            buttons.append([(f"— {title} —", "noop")])
        parts = [(("✅ " if c in chosen else "") + day_part_label(c), f"iw:{i}:{field}:{c}")
                 for c, *_ in DAY_PARTS]
        buttons += [parts[:2], parts[2:]]
        buttons.append([(("✅ " if not chosen else "") + "Любое время", f"iw:{i}:{field}:any")])
    lines.append("\nСейчас: " + times_text(item.get("times"))
                 + (", обратно: " + times_text(item.get("back_times")) if rt else ""))
    return "\n".join(lines), buttons + [[("✅ Готово", f"if:{i}")]]


def flight_times_label(item):
    """«утро, вечер / любое» — коротко для кнопки."""
    back = item.get("back_times")
    return times_text(item.get("times")) + (" / обратно " + times_text(back) if back else "")


def item_screen(settings, item_id, note=""):
    kind, item = find_item(settings, item_id)
    if not item:
        return home_screen(settings)
    if kind == "train":
        return train_screen(item, note)
    i = item["id"]
    lines = [note] if note else []
    if kind == "route":
        country = item["name"].split(" (")[0].title() if " (" in item["name"] else ""
        lines.append(f"📍 {item['origin_name']} → {item['city']}" + (f" ({country})" if country else ""))
        lines.append("Даты: " + ANY_DATES)
        lines.append(f"Сообщу, когда билет дешевле {fmt_price(item['max_price'])} ₽ "
                     "или резко подешевеет." if item.get("max_price")
                     else "Сообщу, когда билет резко подешевеет.")
    else:
        trip = "туда-обратно" if item.get("round_trip") else "в одну сторону"
        lines.append(f"🎯 {item['origin_name']} → {item['city']}, {trip}")
        lines.append("Даты: " + dates_text(item))
        lines.append(f"Сообщу, когда билет дешевле {fmt_price(item['max_price'])} ₽.")
    if item.get("last_price"):
        lines.append(last_price_line(item))
    lines.append("🕐 Время вылета: " + times_text(item.get("times"))
                 + ", обратно: " + times_text(item.get("back_times")))
    lines.append("✈️ Только прямые рейсы" if item.get("direct") else "✈️ С пересадками тоже")
    lines.append("⏱ Время в пути: " + hours_text(item.get("max_hours")))
    lines.append("🧳 Только билеты с багажом" if item.get("bag") else "🧳 Багаж: не важно")
    if kind == "route":
        lines.append("↩️ Обратно: " + back_text(item) + " (покажу, сколько стоит вернуться)")
    lines.append("⏸ На паузе: не присылаю уведомления" if item.get("paused") else "✅ Слежу")

    buttons = [
        [("− 5к", f"ip:{i}:-5000"), ("− 1к", f"ip:{i}:-1000"),
         ("+ 1к", f"ip:{i}:1000"), ("+ 5к", f"ip:{i}:5000")],
        [("✏️ Своя цена", f"it:{i}")],
    ]
    if kind == "route":
        buttons.append([("📅 Вылет: любой день", f"id:{i}")])
    if kind == "filter":
        dep = item["depart"]
        if len(dep) == 10:
            back = (f"{item['return'][8:]}.{item['return'][5:7]}" if item.get("return")
                    else "любой день" if item.get("round_trip") else "нет")
            buttons.append([(f"📅 Вылет: {dep[8:]}.{dep[5:7]}", f"id:{i}"),
                            ("🔁 Обратно: " + back, f"ij:{i}")])
        else:
            back = (MONTH_NOM[int(item["return"][5:])] if item.get("return")
                    else "да" if item.get("round_trip") else "нет")
            buttons.append([("📅 Вылет: " + MONTH_NOM[int(dep[5:])], f"id:{i}"),
                            ("🔁 Туда-обратно: " + back, f"ir:{i}")])
    buttons += [
        [("🕐 Время вылета: " + flight_times_label(item), f"iw:{i}")],
        [("✈️ Только прямые: " + ("вкл" if item.get("direct") else "выкл"), f"ic:{i}"),
         ("⏱ В пути: " + hours_label(item.get("max_hours")), f"ih:{i}")],
        [("🧳 С багажом: " + ("вкл" if item.get("bag") else "выкл"), f"is:{i}"),
         ("▶️ Возобновить" if item.get("paused") else "⏸ Пауза", f"iz:{i}")],
    ]
    if kind == "route":
        buttons.append([("↩️ Когда обратно: " + back_text(item), f"ib:{i}")])
    buttons += [
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


FLIGHT_DAYS_AHEAD = 330  # цены на авиабилеты бывают примерно на год вперёд
FLIGHT_DATES_HINT = ("Напиши даты:\n"
                     "• 15.12 — день вылета\n"
                     "• 15.12-25.12 — вылет и обратно\n"
                     "• декабрь — любой день месяца, найду самый дешёвый")
FLIGHT_BACK_HINT = ("Напиши дату вылета обратно, например 25.12.\n"
                    "Обратный билет не нужен — напиши «нет».")
BACK_QUICK = [("Неделя", 7), ("10 дней", 10), ("2 недели", 14)]


ANY_DATES = "любой день в ближайшие 3 месяца"  # как months_ahead в config.json
ANY_DATES_WORDS = ("любые", "любые даты", "любая", "любая дата", "любой день", "неважно",
                   "не важно", "без даты", "без дат")


def pick_flight_dates_screen(title, prefix, cancel, nav, write, month=None, any_cb=None):
    """Когда вылет: календарь, весь месяц (гибкие даты), любые даты, своя дата текстом."""
    today = datetime.now(MSK).date()
    hi = today + timedelta(days=FLIGHT_DAYS_AHEAD)
    months_btns, d = [], today.replace(day=1)
    for _ in range(6):
        months_btns.append((MONTH_NOM[d.month].capitalize(), f"{prefix}{d:%Y-%m}"))
        d = (d + timedelta(days=32)).replace(day=1)
    text = (f"{title}: когда вылет?\n\nНажми на день в календаре. Даты гибкие — выбери месяц "
            "под календарём, найду самый дешёвый день в нём.")
    extra = []
    if any_cb:
        text += (" Даты не важны — жми «🗓 Любые даты»: буду следить за ценами на каждый день "
                 "в ближайшие 3 месяца.")
        extra = [[("🗓 Любые даты", any_cb)]]
    return (text, calendar_rows(month or today, prefix, nav, today, hi)
            + [[("— или весь месяц —", "noop")]] + rows(months_btns, 3)
            + extra + [[("✏️ Вписать свою дату", write)], cancel])


def pick_flight_back_screen(title, depart, prefix, cancel, nav, write, month=None):
    """Когда обратно. depart — день вылета «туда» (для подбора) или None (просто день обратно)."""
    today = datetime.now(MSK).date()
    hi = today + timedelta(days=FLIGHT_DAYS_AHEAD)
    if depart:
        lo = date.fromisoformat(depart)
        quick = [(f"{label} · {lo + timedelta(days=n):%d.%m}", f"{prefix}{lo + timedelta(days=n)}")
                 for label, n in BACK_QUICK if lo + timedelta(days=n) <= hi]
        text = (f"{title}: вылет {lo:%d.%m}. Когда обратно?\n\nНажми на день — найду билет "
                "туда-обратно и посчитаю общую цену. Обратный не нужен — жми «➡️ Только туда».")
        top, last = [quick], [("✏️ Вписать свою дату", write), ("➡️ Только туда", f"{prefix}none")]
    else:
        lo = today + timedelta(days=1)
        text = (f"{title}: в какой день летим обратно?\n\nНажми на день — под каждым билетом "
                "покажу, сколько стоит вернуться именно в этот день.")
        top, last = [], [("✏️ Вписать свою дату", write)]
    return text, top + calendar_rows(month or lo, prefix, nav, lo, hi) + [last, cancel]


def parse_flight_back(text, today, depart):
    """Дата обратно для подбора: YYYY-MM-DD, "none" (только туда) или строка-ошибка."""
    if _norm(text) in ("нет", "не нужно", "не надо", "только туда", "без обратного"):
        return "none"
    f = parse_dates(text, today)
    if isinstance(f, str) or len(f["depart"]) != 10:
        return "Напиши дату обратно, например 25.12, или «нет», если обратный билет не нужен."
    if f["depart"] < depart:
        return "Дата обратно получилась раньше вылета. Проверь дату."
    return f["depart"]


def set_filter_back(item, back):
    """Обратно для подбора: день YYYY-MM-DD или "none" (только туда)."""
    item.pop("return", None)
    item["round_trip"] = back != "none"
    if back != "none":
        item["return"] = back
    item.pop("last_price", None)


def flight_dates_wizard_screen(wiz, month=None):
    return pick_flight_dates_screen(f"✈️ {wiz['city']}", "wm:", wiz_cancel(wiz), "wk:", "we", month,
                                    "wm:any" if wiz.get("any_ok") else None)


def flight_back_wizard_screen(wiz, month=None):
    return pick_flight_back_screen(f"✈️ {wiz['city']}", wiz["depart"], "wb:", wiz_cancel(wiz),
                                   "wj:", "wf", month)


# Что переносим, когда направление меняет даты (любые ↔ конкретные): остальные настройки те же.
CARRY_FIELDS = ("name", "times", "back_times", "bag", "direct", "max_hours", "paused", "back")


def redate_wizard(item, kind):
    """Мастер, который заменит направление (тот же id) на такое же, но с другими датами."""
    wiz = {"kind": "r", "origin": item["origin"], "dest": item["destination"],
           "city": item["city"], "replace": item["id"],
           "carry": {f: item[f] for f in CARRY_FIELDS if f in item}}
    if item.get("max_price") and not item.get("round_trip"):
        wiz["old_price"] = item["max_price"]  # цена за билет в одну сторону: можно оставить
    if kind == "route":
        wiz["any_ok"] = True
    else:
        wiz["depart"] = None  # подбор → любые даты
    return wiz


def filter_back_screen(item, month=None):
    i = item["id"]
    return pick_flight_back_screen(f"🎯 {item['city']}", item["depart"], f"ig:{i}:",
                                   [("⬅️ Отмена", f"i:{i}")], f"il:{i}:", f"io:{i}", month)


def pick_trip_screen(wiz):
    return (f"✈️ {wiz['city']}: билет нужен туда-обратно или в одну сторону?",
            [[("🔁 Туда-обратно", "wt:rt"), ("➡️ В одну сторону", "wt:ow")], wiz_cancel(wiz)])


def set_wiz_depart(wiz, depart):
    """Вылет в мастере: None — любые даты (📍 направление), день или месяц — 🎯 подбор."""
    wiz["kind"] = "r" if depart is None else "f"
    wiz["depart"] = depart
    wiz.pop("return", None)
    wiz.pop("round_trip", None)  # для конкретных дат спросим про обратный билет


def wiz_cancel(wiz):
    if wiz.get("replace"):
        return [("⬅️ Отмена", f"i:{wiz['replace']}")]
    return [("⬅️ Отмена", "t" if wiz["kind"] == "t" else f"o:{wiz['origin']}")]


def pick_price_screen(wiz, current=None):
    prices = {"r": ROUTE_PRICES, "f": FILTER_PRICES, "t": TRAIN_PRICES}[wiz["kind"]]
    what = "билеты туда-обратно" if wiz.get("round_trip") else "билет"
    text = f"{wiz['city']}: при какой цене за {what} прислать уведомление?"
    if current:
        text += f"\n\nСейчас самый дешёвый: {fmt_price(current)} ₽"
    example = "2500" if wiz["kind"] == "t" else "23500"
    text += f"\n\nВыбери порог или напиши свою сумму, например {example}."
    btns = [(f"до {k(p)}", f"wp:{p}") for p in prices]
    buttons = rows(btns, 3)
    if wiz.get("old_price") and not wiz.get("round_trip"):
        buttons.insert(0, [(f"Оставить как было: до {k(wiz['old_price'])}", f"wp:{wiz['old_price']}")])
    if wiz["kind"] in ("r", "t"):
        buttons.append([("Без порога — только резкие падения", "wp:0")])
    buttons.append(wiz_cancel(wiz))
    return text, buttons


def pick_station_screen(wiz, choices=None):
    if choices:
        return ("Нашёл несколько станций, выбери нужную:",
                [[(c["name"], f"ws:{c['code']}")] for c in choices] + [wiz_cancel(wiz)])
    if "from" not in wiz:
        text = "🚆 Откуда едем? Нажми на город или напиши свой (можно станцию)."
    else:
        text = f"🚆 {wiz['from_name']} → куда едем? Нажми на город или напиши свой."
    btns = [(name, f"ws:{code}") for code, name in POPULAR_STATIONS if code != wiz.get("from")]
    return text, rows(btns, 3) + [wiz_cancel(wiz)]


def pick_car_screen(wiz):
    return (f"🚆 {wiz['city']}: какой вагон?",
            [[("Любой", "wv:any"), ("Плацкарт", "wv:plazcard"), ("Купе", "wv:coupe")],
             [("Сидячий", "wv:sedentary"), ("СВ", "wv:lux")], wiz_cancel(wiz)])


WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
TRAIN_DATES_HINT = ("Напиши дату поездки:\n"
                    "• 15.12 — один день\n"
                    "• 15.12-20.12 — несколько дней подряд (до 7), найду самый дешёвый\n"
                    "• «любая» — слежу за ценами «от» без конкретной даты")
TRAIN_DAYS_AHEAD = 120  # билеты на поезда продают примерно на 3–4 месяца вперёд


def calendar_rows(month, pick, nav, lo, hi):
    """Календарь месяца кнопками: день → pick + YYYY-MM-DD, листание → nav + YYYY-MM."""
    first = month.replace(day=1)
    rows_ = [[(f"{MONTH_NOM[first.month].capitalize()} {first.year}", "noop")],
             [(w, "noop") for w in WEEKDAYS]]
    row = [(" ", "noop")] * first.weekday()
    d = first
    while d.month == first.month:
        row.append((str(d.day), f"{pick}{d.isoformat()}") if lo <= d <= hi else ("·", "noop"))
        if len(row) == 7:
            rows_.append(row)
            row = []
        d += timedelta(days=1)
    if row:
        rows_.append(row + [(" ", "noop")] * (7 - len(row)))
    prev = (first - timedelta(days=1)).replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    rows_.append([("◀️", f"{nav}{prev:%Y-%m}") if prev >= lo.replace(day=1) else (" ", "noop"),
                  ("▶️", f"{nav}{nxt:%Y-%m}") if nxt <= hi else (" ", "noop")])
    return rows_


def pick_train_dates_screen(title, prefix, cancel, nav, write, month=None, back_from=None):
    """Даты для поезда: календарь, ближайшие выходные, своя дата текстом.

    back_from — дата поездки туда: тогда выбираем дату обратно (или «только туда»).
    """
    today = datetime.now(MSK).date()
    lo = date.fromisoformat(back_from) if back_from else today
    hi = today + timedelta(days=TRAIN_DAYS_AHEAD)
    weekend, d = [], max(lo, today + timedelta(days=1))
    while len(weekend) < 3:
        if d.weekday() >= 4:
            weekend.append(d)
        d += timedelta(days=1)
    btns = [(f"{WEEKDAYS[d.weekday()]} {d:%d.%m}", f"{prefix}{d.isoformat()}") for d in weekend]
    if back_from:
        text = (f"{title}: когда обратно?\n\nНажми на день в календаре — найду билеты и туда, "
                "и обратно и посчитаю общую цену. Обратный билет не нужен — жми «➡️ Только туда».")
        last = [("✏️ Вписать свою дату", write), ("➡️ Только туда", f"{prefix}none")]
    else:
        text = (f"{title}: когда едем?\n\nНажми на день в календаре или на ближайшие выходные. "
                "Несколько дней подряд можно вписать кнопкой «✏️ Вписать свою дату».")
        last = [("✏️ Вписать свою дату", write), ("📅 Любая дата", f"{prefix}any")]
    return (text, [btns] + calendar_rows(month or lo, prefix, nav, lo, hi) + [last, cancel])


TRAIN_BACK_HINT = ("Напиши дату обратно:\n"
                   "• 20.12 — один день\n"
                   "• 20.12-22.12 — несколько дней подряд (до 7), найду самый дешёвый\n"
                   "• «нет» — обратный билет не нужен")


def parse_train_back(text, today, there):
    """Дата обратно для поезда: [первый, последний день], "none" или строка-ошибка."""
    if _norm(text) in ("нет", "не нужно", "не надо", "только туда", "без обратного"):
        return "none"
    dates = parse_train_dates(text, today)
    if dates is None:
        return "Напиши конкретную дату обратно, например 20.12, или «нет»."
    if isinstance(dates, list) and dates[1] < there[0]:
        return "Дата обратно получилась раньше поездки туда. Проверь дату."
    return dates


def parse_train_dates(text, today):
    """«15.12», «15.12-20.12» → [первый, последний день]; «любая» → None; иначе строка-ошибка."""
    if _norm(text) in ("любая", "любая дата", "любой день", "без даты", "любые"):
        return None
    f = parse_dates(text, today)
    if isinstance(f, str):
        return "Не понял дату. Напиши, например, 15.12 или 15.12-20.12."
    if len(f["depart"]) != 10:
        return "Для поезда напиши конкретные дни, например 15.12 или 15.12-20.12."
    first, last = f["depart"], f.get("return") or f["depart"]
    if (date.fromisoformat(last) - date.fromisoformat(first)).days > 6:
        return "Можно выбрать до 7 дней подряд, например 15.12-21.12."
    return [first, last]


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
    if wiz["kind"] == "t":
        offers = train_trip_offers(wiz)
        return min((o["price"] for o in offers), default=None)
    try:
        # Настройки, перенесённые из карточки (время вылета, багаж, прямые), тоже учитываем.
        item = dict(wiz.get("carry", {}), destination=wiz["dest"])
        if wiz["kind"] == "r":
            offers = route_offers(token, wiz.get("origin") or settings["origin"], item, cfg)
        else:
            item.update({f: wiz[f] for f in ("depart", "return", "round_trip") if f in wiz})
            item["direct"] = item.get("direct") or wiz.get("direct", False)
            offers = filter_offers(token, wiz.get("origin") or settings["origin"], item)
    except Exception as e:  # noqa: BLE001
        print(f"price error: {e}", file=sys.stderr)
        return None
    return min((o["price"] for o in offers), default=None)


def wizard_set_city(state, settings, place):
    wiz = state.setdefault("wiz", {})
    wiz.update(dest=place["code"], city=place["name"], country=place["country"])


def wizard_set_station(wiz, station):
    side = "from" if "from" not in wiz else "to"
    wiz[side], wiz[side + "_name"] = station["code"], station["name"]
    if side == "to":
        wiz["city"] = f"{wiz['from_name']} → {wiz['to_name']}"


def wizard_next(state, settings, tg, token, cfg, message_id=None):
    """Показывает следующий шаг мастера (или создаёт направление на последнем шаге)."""
    wiz = state["wiz"]
    show = (lambda t, b: tg.edit(message_id, t, b)) if message_id else tg.send
    if wiz["kind"] == "t":
        return train_wizard_next(state, settings, tg, token, cfg, show)
    if "depart" not in wiz:  # самолёты: когда вылет (день, месяц или любые даты)
        state["awaiting"] = {"type": "wiz_dates"}
        return show(*flight_dates_wizard_screen(wiz))
    if wiz["kind"] == "f" and "round_trip" not in wiz:
        if len(wiz["depart"]) == 10:
            state["awaiting"] = {"type": "wiz_fback"}
            return show(*flight_back_wizard_screen(wiz))
        return show(*pick_trip_screen(wiz))
    if "price" not in wiz:
        state["awaiting"] = {"type": "wiz_price"}
        return show(*pick_price_screen(wiz, wizard_current_price(token, cfg, settings, wiz)))

    place = {"code": wiz["dest"], "name": wiz["city"], "country": wiz.get("country", "")}
    item = new_item_fields(settings, place)
    if wiz.get("replace"):  # то же направление с новыми датами: убираем старое, id тот же
        item["id"] = wiz["replace"]
        settings["routes"] = [r for r in settings["routes"] if r["id"] != item["id"]]
        settings["filters"] = [f for f in settings["filters"] if f["id"] != item["id"]]
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
    item.update(wiz.get("carry", {}))
    if wiz.get("direct"):  # «прямой» в датах, вписанных текстом
        item["direct"] = True
    state.pop("wiz", None)
    state.pop("awaiting", None)
    state["changed"] = True
    done = "✅ Сохранил новые даты!" if wiz.get("replace") else "✅ Добавлено!"
    show(*item_screen(settings, item["id"], done + " Я уже проверяю цены.\n"))


def train_dates_wizard_screen(wiz, month=None):
    return pick_train_dates_screen(f"🚆 {wiz['city']}", "wd:", wiz_cancel(wiz), "wk:", "we", month)


def train_back_wizard_screen(wiz, month=None):
    return pick_train_dates_screen(f"🚆 {wiz['city']}", "wb:", wiz_cancel(wiz), "wj:", "wf", month,
                                   back_from=wiz["dates"][0])


def train_wizard_next(state, settings, tg, token, cfg, show):
    wiz = state["wiz"]
    if "to" not in wiz:
        state["awaiting"] = {"type": "wiz_station"}
        return show(*pick_station_screen(wiz))
    if "car" not in wiz:
        return show(*pick_car_screen(wiz))
    if "dates" not in wiz:
        state["awaiting"] = {"type": "wiz_tdates"}
        return show(*train_dates_wizard_screen(wiz))
    if "back_dates" not in wiz:
        if not wiz["dates"]:
            wiz["back_dates"] = None
        else:
            state["awaiting"] = {"type": "wiz_tback"}
            return show(*train_back_wizard_screen(wiz))
    wiz["round_trip"] = bool(wiz["back_dates"])
    if "price" not in wiz:
        state["awaiting"] = {"type": "wiz_price"}
        return show(*pick_price_screen(wiz, wizard_current_price(token, cfg, settings, wiz)))

    item = {"id": uuid.uuid4().hex[:8], "from": wiz["from"], "from_name": wiz["from_name"],
            "to": wiz["to"], "to_name": wiz["to_name"], "city": wiz["city"],
            "car": wiz["car"], "dates": wiz["dates"], "max_price": wiz["price"] or None}
    if wiz.get("back_dates"):
        item["back_dates"] = wiz["back_dates"]
    same = ("from", "to", "car", "dates", "back_dates")
    settings["trains"] = [t for t in settings["trains"]
                          if [t.get(f) for f in same] != [item.get(f) for f in same]]
    settings["trains"].append(item)
    state.pop("wiz", None)
    state.pop("awaiting", None)
    state["changed"] = True
    show(*item_screen(settings, item["id"], "✅ Добавлено! Я уже проверяю цены.\n"))


def set_train_back(item, back):
    if back:
        item["back_dates"] = back
    else:
        item.pop("back_dates", None)
    item.pop("last_price", None)


def fix_train_back(item):
    """После смены даты «туда»: обратный билет без дат или раньше поездки убираем.

    Возвращает пометку для карточки, если убрали.
    """
    back = item.get("back_dates")
    if back and (not item.get("dates") or back[1] < item["dates"][0]):
        item.pop("back_dates")
        return "↩️ Дата обратно оказалась раньше поездки — выбери её заново кнопкой «🔁 Обратно».\n"
    return ""


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
        snaps = []
        tg.send(prices_now(token, cfg, settings, snaps=snaps),
                pick_button(settings, snaps) + back_button(settings, None))
        return False
    if text == BTN_SAVED:
        tg.send(*saved_screen(settings))
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
    if kind == "wiz_station" and wiz:
        station = find_station(text)
        state["awaiting"] = awaiting
        if isinstance(station, str):
            tg.send(station)
        elif isinstance(station, list):
            tg.send(*pick_station_screen(wiz, station))
        else:
            wizard_set_station(wiz, station)
            wizard_next(state, settings, tg, token, cfg)
        return False
    if kind == "wiz_tdates" and wiz:
        dates = parse_train_dates(text, date.today())
        if isinstance(dates, str):
            state["awaiting"] = awaiting
            tg.send(dates)
            return False
        wiz["dates"] = dates
        wizard_next(state, settings, tg, token, cfg)
        return False
    if kind == "wiz_tback" and wiz and wiz.get("dates"):
        back = parse_train_back(text, date.today(), wiz["dates"])
        if isinstance(back, str) and back != "none":
            state["awaiting"] = awaiting
            tg.send(back)
            return False
        wiz["back_dates"] = None if back == "none" else back
        wizard_next(state, settings, tg, token, cfg)
        return False
    if kind == "wiz_fback" and wiz and len(wiz.get("depart", "")) == 10:
        back = parse_flight_back(text, date.today(), wiz["depart"])
        if back != "none" and len(back) != 10:
            state["awaiting"] = awaiting
            tg.send(back)
            return False
        wiz["round_trip"] = back != "none"
        wiz.pop("return", None)
        if back != "none":
            wiz["return"] = back
        wizard_next(state, settings, tg, token, cfg)
        return False
    if kind == "item_fback":
        item_kind, item = find_item(settings, awaiting.get("id"))
        if not item or item_kind != "filter" or len(item["depart"]) != 10:
            tg.send(*home_screen(settings))
            return False
        back = parse_flight_back(text, date.today(), item["depart"])
        if back != "none" and len(back) != 10:
            state["awaiting"] = awaiting
            tg.send(back)
            return False
        set_filter_back(item, back)
        tg.send(*item_screen(settings, item["id"], "✅ Сохранил.\n"))
        return True
    if kind == "item_tback":
        item_kind, item = find_item(settings, awaiting.get("id"))
        if not item or not item.get("dates"):
            tg.send(*home_screen(settings))
            return False
        back = parse_train_back(text, date.today(), item["dates"])
        if isinstance(back, str) and back != "none":
            state["awaiting"] = awaiting
            tg.send(back)
            return False
        set_train_back(item, None if back == "none" else back)
        tg.send(*item_screen(settings, item["id"], "✅ Сохранил.\n"))
        return True
    if kind == "wiz_dates" and wiz:
        if wiz.get("any_ok") and _norm(text) in ANY_DATES_WORDS:
            if find_item(settings, wiz.get("replace"))[0] == "route":
                state.pop("wiz")
                tg.send(*item_screen(settings, wiz["replace"]))
                return False
            set_wiz_depart(wiz, None)
            wizard_next(state, settings, tg, token, cfg)
            return False
        fields = parse_dates(text, date.today())
        if isinstance(fields, str):
            state["awaiting"] = awaiting
            tg.send(fields)
            return False
        set_wiz_depart(wiz, fields["depart"])
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
    if kind == "item_back":
        item_kind, item = find_item(settings, awaiting.get("id"))
        if not item:
            tg.send(*home_screen(settings))
            return False
        back = parse_back(text, date.today())
        if isinstance(back, str):
            state["awaiting"] = awaiting
            tg.send(back)
            return False
        item["back"] = back
        tg.send(*item_screen(settings, item["id"], "✅ Обратно: " + back_text(item) + "\n"))
        return False
    if kind in ("item_price", "item_dates"):
        item_kind, item = find_item(settings, awaiting.get("id"))
        if not item:
            tg.send(*home_screen(settings))
            return False
        if kind == "item_dates" and item_kind == "train":
            dates = parse_train_dates(text, date.today())
            if isinstance(dates, str):
                state["awaiting"] = awaiting
                tg.send(dates)
                return False
            item["dates"] = dates
            awaiting["note"] = fix_train_back(item)
            item.pop("last_price", None)
        elif kind == "item_price":
            amount = parse_amount(text)
            if not amount:
                state["awaiting"] = awaiting
                tg.send("Напиши сумму цифрами, например 23500 или 25к.")
                return False
            item["max_price"] = amount
        else:
            if _norm(text) in ANY_DATES_WORDS:  # подбор → направление на любые даты
                state["wiz"] = redate_wizard(item, item_kind)
                wizard_next(state, settings, tg, token, cfg)
                return False
            fields = parse_dates(text, date.today())
            if isinstance(fields, str):
                state["awaiting"] = awaiting
                tg.send(fields)
                return False
            item.pop("return", None)
            fields.setdefault("round_trip", item.get("round_trip", True))
            item.update(fields)
            item.pop("last_price", None)
            if len(item["depart"]) == 10 and not item.get("return"):
                # Один день вылета — сразу спросим, когда обратно (цены проверю после ответа).
                state["awaiting"] = {"type": "item_fback", "id": item["id"]}
                tg.send(*filter_back_screen(item))
                return False
        tg.send(*item_screen(settings, item["id"], "✅ Сохранил.\n" + awaiting.get("note", "")))
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
    if data == "noop":  # заголовки и пустые клетки календаря
        return False
    cmd, _, arg = data.partition(":")
    if not cmd.startswith("w"):
        state.pop("awaiting", None)
        state.pop("wiz", None)
    edit = lambda t, b: tg.edit(message_id, t, b)  # noqa: E731

    if cmd == "home":
        edit(*home_screen(settings))
    elif cmd == "t":
        edit(*trains_screen(settings))
    elif cmd == "tp":
        tg.send("🔎 Ищу цены…")
        snaps = []
        text = train_prices(settings, snaps)
        tg.send(text, pick_button(settings, snaps) + [[("⬅️ Поезда", "t")]])
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
        if cmd == "a":
            state["wiz"]["any_ok"] = True  # на экране дат есть «🗓 Любые даты»
        state["awaiting"] = {"type": "wiz_city"}
        edit(*pick_city_screen(settings, state["wiz"]["kind"]))
    elif cmd == "ta":
        state["wiz"] = {"kind": "t"}
        state["awaiting"] = {"type": "wiz_station"}
        edit(*pick_station_screen(state["wiz"]))
    elif cmd == "ws" and state.get("wiz", {}).get("kind") == "t":
        wizard_set_station(state["wiz"], {"code": arg, "name": station_name(arg)})
        wizard_next(state, settings, tg, token, cfg, message_id)
    elif cmd == "wv" and state.get("wiz", {}).get("kind") == "t":
        state["wiz"]["car"] = arg
        wizard_next(state, settings, tg, token, cfg, message_id)
    elif cmd == "wk" and state.get("wiz", {}).get("kind") == "t":
        state["awaiting"] = {"type": "wiz_tdates"}
        edit(*train_dates_wizard_screen(state["wiz"], date.fromisoformat(arg + "-01")))
    elif cmd == "we" and state.get("wiz", {}).get("kind") == "t":
        state["awaiting"] = {"type": "wiz_tdates"}
        edit(f"🚆 {state['wiz']['city']}: {TRAIN_DATES_HINT[0].lower()}{TRAIN_DATES_HINT[1:]}",
             [[("⬅️ К календарю", "wk:" + date.today().strftime("%Y-%m"))]])
    elif cmd == "wj" and state.get("wiz", {}).get("kind") == "t" and state["wiz"].get("dates"):
        state["awaiting"] = {"type": "wiz_tback"}
        edit(*train_back_wizard_screen(state["wiz"], date.fromisoformat(arg + "-01")))
    elif cmd == "wf" and state.get("wiz", {}).get("kind") == "t" and state["wiz"].get("dates"):
        state["awaiting"] = {"type": "wiz_tback"}
        edit(f"🚆 {state['wiz']['city']}: {TRAIN_BACK_HINT[0].lower()}{TRAIN_BACK_HINT[1:]}",
             [[("⬅️ К календарю", "wj:" + state["wiz"]["dates"][0][:7])]])
    elif cmd == "wb" and state.get("wiz", {}).get("kind") == "t" and state["wiz"].get("dates"):
        state["wiz"]["back_dates"] = None if arg == "none" else [arg, arg]
        wizard_next(state, settings, tg, token, cfg, message_id)
    elif cmd == "wd" and state.get("wiz", {}).get("kind") == "t":
        state["wiz"]["dates"] = None if arg == "any" else [arg, arg]
        wizard_next(state, settings, tg, token, cfg, message_id)
    elif cmd == "wk" and state.get("wiz", {}).get("kind") in ("r", "f"):
        state["awaiting"] = {"type": "wiz_dates"}
        edit(*flight_dates_wizard_screen(state["wiz"], date.fromisoformat(arg + "-01")))
    elif cmd == "we" and state.get("wiz", {}).get("kind") in ("r", "f"):
        state["awaiting"] = {"type": "wiz_dates"}
        hint = FLIGHT_DATES_HINT + ("\n• любые — слежу за всеми днями" if state["wiz"].get("any_ok") else "")
        edit(f"✈️ {state['wiz']['city']}: {hint[0].lower()}{hint[1:]}",
             [[("⬅️ К календарю", "wk:" + date.today().strftime("%Y-%m"))]])
    elif cmd in ("wj", "wf", "wb") and state.get("wiz", {}).get("kind") == "f" \
            and len(state["wiz"].get("depart", "")) == 10:
        wiz = state["wiz"]
        if cmd == "wb":
            wiz["round_trip"] = arg != "none"
            wiz.pop("return", None)
            if arg != "none":
                wiz["return"] = arg
            wizard_next(state, settings, tg, token, cfg, message_id)
        else:
            state["awaiting"] = {"type": "wiz_fback"}
            if cmd == "wj":
                edit(*flight_back_wizard_screen(wiz, date.fromisoformat(arg + "-01")))
            else:
                edit(f"✈️ {wiz['city']}: {FLIGHT_BACK_HINT[0].lower()}{FLIGHT_BACK_HINT[1:]}",
                     [[("⬅️ К календарю", "wj:" + wiz["depart"][:7])]])
    elif cmd == "wc" and state.get("wiz"):
        code, name, country = next(d for d in POPULAR_DESTINATIONS if d[0] == arg)
        wizard_set_city(state, settings, {"code": code, "name": name, "country": country})
        wizard_next(state, settings, tg, token, cfg, message_id)
    elif cmd == "wm" and state.get("wiz"):
        wiz = state["wiz"]
        if arg == "any" and find_item(settings, wiz.get("replace"))[0] == "route":
            state.pop("wiz")  # у направления и так любые даты
            edit(*item_screen(settings, wiz["replace"]))
            return False
        set_wiz_depart(wiz, None if arg == "any" else arg)
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
    elif cmd in ("i", "ip", "it", "id", "ik", "iu", "ia", "ij", "il", "io", "ig", "ir", "ic", "ih", "ib",
                 "ie", "iy", "iv", "iz", "in", "iq", "ix", "iw", "if", "is"):
        item_id, _, extra = arg.partition(":")
        kind, item = find_item(settings, item_id)
        if not item:
            edit("Этого направления уже нет.", back_button(settings, None))
            return False
        if kind != "train":
            select_origin(settings, item["origin"])
        note = ""
        if cmd == "ip":
            train = kind == "train"
            base = item.get("max_price") or item.get("last_price") or (3000 if train else 20000)
            item["max_price"] = max(100 if train else 1000, base + int(extra))
            note = f"✅ Порог: {fmt_price(item['max_price'])} ₽\n"
        elif cmd == "it":
            state["awaiting"] = {"type": "item_price", "id": item_id}
            example = "2500 или 3к" if kind == "train" else "23500 или 25к"
            tg.send(f"{item['city']}: напиши новую цену, например {example}.",
                    [[("⬅️ Отмена", f"i:{item_id}")]])
            return False
        elif cmd in ("id", "ik") and kind == "filter":
            state["awaiting"] = {"type": "item_dates", "id": item_id}
            # Календарь открываем на месяце текущей даты вылета.
            month = extra or max(item["depart"][:7], date.today().strftime("%Y-%m"))
            month = date.fromisoformat(month + "-01")
            edit(*pick_flight_dates_screen(f"🎯 {item['city']}", f"ia:{item_id}:",
                                           [("⬅️ Отмена", f"i:{item_id}")], f"ik:{item_id}:",
                                           f"iu:{item_id}", month, f"ia:{item_id}:any"))
            return False
        elif cmd == "id" and kind == "route":
            # Выбрали дату у направления «любые даты»: дальше как при добавлении (обратно, порог).
            state["wiz"] = redate_wizard(item, kind)
            state["awaiting"] = {"type": "wiz_dates"}
            edit(*flight_dates_wizard_screen(state["wiz"]))
            return False
        elif cmd == "ia" and kind == "filter" and extra == "any":
            state["wiz"] = redate_wizard(item, kind)
            wizard_next(state, settings, tg, token, cfg, message_id)
            return False
        elif cmd == "iu" and kind == "filter":
            state["awaiting"] = {"type": "item_dates", "id": item_id}
            hint = FLIGHT_DATES_HINT + "\n• любые — слежу за всеми днями"
            edit(f"🎯 {item['city']}: {hint[0].lower()}{hint[1:]}",
                 [[("⬅️ К календарю", f"id:{item_id}")]])
            return False
        elif cmd == "ia" and kind == "filter" and extra:
            item["depart"] = extra
            item.pop("return", None)
            item.pop("last_price", None)
            if len(extra) == 10:  # день вылета выбран — теперь когда обратно
                state["awaiting"] = {"type": "item_fback", "id": item_id}
                edit(*filter_back_screen(item))
                return False
            note = f"✅ Вылет: в {MONTH_IN[int(extra[5:])]}\n"
        elif cmd in ("ij", "il") and kind == "filter" and len(item["depart"]) == 10:
            state["awaiting"] = {"type": "item_fback", "id": item_id}
            edit(*filter_back_screen(item, date.fromisoformat(extra + "-01") if cmd == "il" else None))
            return False
        elif cmd in ("ij", "il") and kind == "route":
            state["awaiting"] = {"type": "item_back", "id": item_id}
            edit(*pick_flight_back_screen(f"↩️ {item['city']}", None, f"ig:{item_id}:",
                                          [("⬅️ Назад", f"ib:{item_id}")], f"il:{item_id}:",
                                          f"ie:{item_id}",
                                          date.fromisoformat(extra + "-01") if cmd == "il" else None))
            return False
        elif cmd == "io" and kind == "filter" and len(item["depart"]) == 10:
            state["awaiting"] = {"type": "item_fback", "id": item_id}
            edit(f"🎯 {item['city']}: {FLIGHT_BACK_HINT[0].lower()}{FLIGHT_BACK_HINT[1:]}",
                 [[("⬅️ К календарю", f"ij:{item_id}")]])
            return False
        elif cmd == "ig" and kind == "filter" and len(item["depart"]) == 10 and extra:
            set_filter_back(item, extra)
            note = "✅ " + dates_text(item).capitalize() + ("" if item["round_trip"] else ", только туда") + "\n"
        elif cmd == "ig" and kind == "route" and extra:
            item["back"] = {"dates": [extra, extra]}
            note = "✅ Обратно: " + back_text(item) + "\n"
        elif cmd == "is":
            item["bag"] = not item.get("bag")
            if not item["bag"]:
                item.pop("bag")
            item.pop("last_price", None)
            note = ("✅ Ищу только билеты с багажом\n" if item.get("bag")
                    else "✅ Багаж не важен: показываю все билеты\n")
        elif cmd in ("id", "ik") and kind == "train":
            state["awaiting"] = {"type": "item_dates", "id": item_id}
            month = date.fromisoformat(extra + "-01") if cmd == "ik" else None
            edit(*pick_train_dates_screen(f"🚆 {item['city']}", f"ia:{item_id}:",
                                          [("⬅️ Отмена", f"i:{item_id}")], f"ik:{item_id}:",
                                          f"iu:{item_id}", month))
            return False
        elif cmd == "iu":
            state["awaiting"] = {"type": "item_dates", "id": item_id}
            edit(f"🚆 {item['city']}: {TRAIN_DATES_HINT[0].lower()}{TRAIN_DATES_HINT[1:]}",
                 [[("⬅️ К календарю", f"id:{item_id}")]])
            return False
        elif cmd == "ia":
            item["dates"] = None if extra == "any" else [extra, extra]
            dropped = fix_train_back(item)
            item.pop("last_price", None)
            note = "✅ Даты: " + (train_dates_short(item) if item["dates"] else "любые") + "\n" + dropped
        elif cmd in ("ij", "il") and kind == "train" and item.get("dates"):
            state["awaiting"] = {"type": "item_tback", "id": item_id}
            month = date.fromisoformat(extra + "-01") if cmd == "il" else None
            edit(*pick_train_dates_screen(f"🚆 {item['city']}", f"ig:{item_id}:",
                                          [("⬅️ Отмена", f"i:{item_id}")], f"il:{item_id}:",
                                          f"io:{item_id}", month, back_from=item["dates"][0]))
            return False
        elif cmd == "io" and kind == "train" and item.get("dates"):
            state["awaiting"] = {"type": "item_tback", "id": item_id}
            edit(f"🚆 {item['city']}: {TRAIN_BACK_HINT[0].lower()}{TRAIN_BACK_HINT[1:]}",
                 [[("⬅️ К календарю", f"ij:{item_id}")]])
            return False
        elif cmd == "ig" and kind == "train" and item.get("dates"):
            set_train_back(item, None if extra == "none" else [extra, extra])
            note = "✅ Обратно: " + (train_dates_short(item, "back_dates") if item.get("back_dates")
                                    else "не нужно") + "\n"
        elif cmd == "ir":
            item["round_trip"] = not item.get("round_trip")
            if not item["round_trip"]:
                item.pop("return", None)
            item.pop("last_price", None)
        elif cmd == "ic":
            item["direct"] = not item.get("direct")
        elif cmd == "ib":
            state["awaiting"] = {"type": "item_back", "id": item_id}
            edit(*pick_back_screen(item))
            return False
        elif cmd == "ie":
            state["awaiting"] = {"type": "item_back", "id": item_id}
            edit(f"↩️ {item['city']}: {BACK_HINT[0].lower()}{BACK_HINT[1:]}",
                 [[("⬅️ Назад", f"ib:{item_id}")]])
            return False
        elif cmd == "iy":
            lo, _, hi = extra.partition("-")
            item["back"] = {"days": [int(lo), int(hi)]}
            note = "✅ Обратно: " + back_text(item) + "\n"
        elif cmd == "ih":
            steps = HOURS_STEPS
            current = item.get("max_hours")
            item["max_hours"] = steps[(steps.index(current) + 1) % len(steps) if current in steps else 0]
            if item["max_hours"] is None:
                item.pop("max_hours")
            item.pop("last_price", None)
            note = "✅ Время в пути: " + hours_text(item.get("max_hours")) + "\n"
        elif cmd == "iv":
            cars = list(CARS)
            item["car"] = cars[(cars.index(item.get("car", "any")) + 1) % len(cars)]
            item.pop("last_price", None)
            note = f"✅ Вагон: {CARS[item['car']]}\n"
        elif cmd == "iz":
            item["paused"] = not item.get("paused")
            note = "⏸ Поставил на паузу.\n" if item["paused"] else "▶️ Снова слежу.\n"
        elif cmd == "in" and kind == "train":
            tg.send("🔎 Ищу цены…")
            both = train_both(item)
            offers = train_trip_offers(item, both)
            remember_price(item, offers, datetime.now(timezone.utc))
            back = [[("🕐 Время отправления: " + times_text(item.get("times"))
                      + (" / " + times_text(item.get("back_times")) if item.get("back_dates") else ""),
                      f"iw:{item_id}")],
                    [("⬅️ К направлению", f"i:{item_id}")]]
            times = ""
            if item.get("times") or (item.get("back_dates") and item.get("back_times")):
                times = ("\nПоказываю только отправление: " + times_text(item.get("times"))
                         + (", обратно: " + times_text(item.get("back_times")) if item.get("back_dates") else "")
                         + ". Поменять — кнопка «🕐 Время отправления».")
            if not offers:
                tg.send(f"{train_title(item)}: билетов не нашёл 😔{times}", back)
                return False
            if "there" in offers[0]:
                o = offers[0]
                snaps = ([train_snapshot(o, item)]
                         + [train_snapshot(x, item) for _, g in by_day_part(both[0], 2) for x in g]
                         + [train_snapshot(x, item, back=True) for _, g in by_day_part(both[1], 2) for x in g])
                tg.send(f"💰 {train_title(item)}: от {fmt_price(o['price'])} ₽ туда-обратно{times}\n\n"
                        "Самая дешёвая пара:\n" + train_pair_text(o, link=False)
                        + "\n\n➡️ Туда, по времени суток:\n" + day_parts_text(item, both[0], 2)
                        + "\n\n⬅️ Обратно, по времени суток:\n"
                        + day_parts_text(train_back_item(item), both[1], 2),
                        train_buy_buttons(o) + pick_button(settings, snaps) + back)
                return False
            best = min(offers, key=lambda o: o["price"])
            note = ("" if item.get("dates")
                    else "\n\nЭто цены «от»: даты и места смотри на Туту.ру")
            snaps = [train_snapshot(x, item) for _, g in by_day_part(offers, 3) for x in g]
            tg.send(f"💰 {train_title(item)}: от {fmt_price(best['price'])} ₽{times}\n"
                    "Самые дешёвые поезда по времени отправления:\n\n"
                    + day_parts_text(item, offers, 3) + note,
                    train_day_buttons(offers) + pick_button(settings, snaps) + back)
            return False
        elif cmd == "iw":
            field, _, code = extra.partition(":")
            if field in ("times", "back_times") and code:
                chosen = set(item.get(field) or [])
                chosen = set() if code == "any" else chosen ^ {code}
                if len(chosen) == len(DAY_PARTS):
                    chosen = set()  # все части суток = любое время
                if chosen:
                    item[field] = [c for c, *_ in DAY_PARTS if c in chosen]
                else:
                    item.pop(field, None)
                item.pop("last_price", None)
            edit(*times_screen(kind, item))
            return False
        elif cmd == "if":
            note = ("✅ Время отправления: " if kind == "train" else "✅ Время вылета: ") \
                + times_text(item.get("times"))
            if item.get("back_dates") or kind != "train":
                note += ", обратно: " + times_text(item.get("back_times"))
            note += "\n"
        elif cmd == "in":
            tg.send("🔎 Ищу цены…")
            all_offers, offers = item_offers(token, kind, item, cfg)
            remember_price(item, offers, datetime.now(timezone.utc))
            back = [[("🕐 Время вылета: " + flight_times_label(item), f"iw:{item_id}")],
                    [("⬅️ К направлению", f"i:{item_id}")]]
            note = skipped_note(all_offers, offers, item)
            if not offers:
                tg.send(f"{item['city']}: билетов не нашёл 😔" + (f"\n\n{note}" if note else ""), back)
                return False
            best = min(offers, key=lambda o: o["price"])
            text = f"💰 {item['city']}: {fmt_price(best['price'])} ₽\n{offer_line(best, item['origin_name'], item)}"
            buttons = [[("🎫 Купить билет", offer_link(best))]]
            rt = ret = None
            if not item.get("round_trip"):
                rt, ret = return_info(token, item["origin"], item["destination"], best, item)
                text += "\n" + return_text(best, rt, ret, item)
                if rt:
                    buttons.append([("🔁 Купить туда-обратно", offer_link(rt))])
            # Самый быстрый вариант, если он заметно быстрее самого дешёвого.
            fast = min(offers, key=lambda o: (travel_minutes(o) or 10 ** 6, o["price"]))
            if fast is not best and travel_minutes(fast) and travel_minutes(best) - travel_minutes(fast) >= 120:
                text += (f"\n\n⚡ Быстрее: {fmt_price(fast['price'])} ₽\n"
                         f"{offer_line(fast, item['origin_name'], item)}")
                buttons.append([("⚡ Купить быстрый", offer_link(fast))])
            parts = flight_parts(offers)
            if len(parts) > 1:
                text += ("\n\n🕐 Самые дешёвые по времени вылета:\n"
                         + "\n".join(f"{day_part_label(c)}: {flight_short(o)}" for c, o in parts))
                part_btns = [(f"🎫 {DAY_PART_NAMES[c]} · {k(o['price'])}", offer_link(o)) for c, o in parts]
                buttons += rows(part_btns, 2)
            if item.get("times") or item.get("back_times"):
                text += ("\n\n🕐 Показываю только вылет: " + times_text(item.get("times"))
                         + ", обратно: " + times_text(item.get("back_times"))
                         + ". Поменять — кнопка «🕐 Время вылета».")
            if note:
                text += f"\n\n{note}"
            snaps = item_flight_snaps(item, [best, rt, fast] + [o for _, o in parts], [ret])
            tg.send(text, buttons + pick_button(settings, snaps) + back)
            return False
        elif cmd == "iq":
            edit(f"Удалить {item_label(kind, item)}?",
                 [[("🗑 Да, удалить", f"ix:{item_id}"), ("Отмена", f"i:{item_id}")]])
            return False
        elif cmd == "ix":
            settings[ITEM_LISTS[kind]].remove(item)
            edit(*(trains_screen(settings) if kind == "train"
                   else origin_screen(settings, item["origin"])))
            return False
        edit(*item_screen(settings, item_id, note))
        return cmd in ("ip", "ir", "ic", "ih", "iv", "ia", "ig", "if", "is")

    # ⭐ Отложенные
    elif cmd == "sl":
        edit(*saved_screen(settings))
    elif cmd == "sp":
        entry = settings.get("picks", {}).get(arg)
        if not entry:
            tg.send("Этот список устарел. Открой «🔎 Цена сейчас» ещё раз.", [[("⭐ Отложенные", "sl")]])
        elif len(entry["items"]) == 1:
            tg.send(save_snap(settings, entry["items"][0]) + "\nВсе отложенные — кнопка «⭐ Отложенные».",
                    [[("⭐ Отложенные", "sl")]])
        else:
            tg.send(*pick_screen(settings, arg))
    elif cmd == "sv":
        pick_id, _, idx = arg.partition(":")
        entry = settings.get("picks", {}).get(pick_id)
        if not entry or not idx.isdigit() or int(idx) >= len(entry["items"]):
            edit("Этот список устарел. Открой «🔎 Цена сейчас» ещё раз.", [[("⭐ Отложенные", "sl")]])
            return False
        snap = entry["items"][int(idx)]
        same = [x for x in settings.get("saved", []) if snap_key(x) == snap_key(snap)]
        if same:
            settings["saved"].remove(same[0])  # повторное нажатие — убрать из отложенных
        else:
            save_snap(settings, snap)
        edit(*pick_screen(settings, pick_id))
    elif cmd in ("so", "sc", "sq", "sd"):
        s = find_saved(settings, arg)
        if not s:
            edit(*saved_screen(settings, "Этого билета уже нет в отложенных.\n"))
        elif cmd == "so":
            edit(saved_text(s), saved_buttons(s))
        elif cmd == "sc":
            res = recheck_saved(token, s, datetime.now(timezone.utc))
            if not res:
                note = "Не получилось проверить цену, попробуй позже.\n\n"
            elif res[1] is None:
                note = ("😔 Мест в этом вагоне больше нет.\n\n" if s["kind"] == "train"
                        else "😔 Этого рейса сейчас нет в свежих ценах.\n\n")
            else:
                note = (f"🔄 Проверил: {fmt_price(res[1])} ₽ — "
                        + ("столько же, сколько когда отложил" if res[1] == s["price"]
                           else diff_text(s["price"], res[1]) + ", чем когда отложил") + ".\n\n")
            edit(note + saved_text(s), saved_buttons(s))
        elif cmd == "sq":
            edit(f"Убрать из отложенных?\n\n{saved_label(s)}",
                 [[("🗑 Да, убрать", f"sd:{s['id']}"), ("Отмена", f"so:{s['id']}")]])
        else:
            settings["saved"].remove(s)
            edit(*saved_screen(settings, f"🗑 Убрал: {saved_label(s)}\n"))
    elif cmd == "sa":
        tg.send("🔎 Проверяю цены отложенных…")
        now, counts = datetime.now(timezone.utc), {"down": 0, "up": 0, "same": 0, "gone": 0}
        for s in settings.get("saved", []):
            res = recheck_saved(token, s, now)
            if res:
                new = res[1]
                counts["gone" if new is None else "down" if new < s["price"]
                       else "up" if new > s["price"] else "same"] += 1
        note = (f"🔄 Проверил. Подешевели: {counts['down']}, подорожали: {counts['up']}, "
                f"та же цена: {counts['same']}, не нашёл: {counts['gone']}.\n")
        tg.send(*saved_screen(settings, note))

    # Цены
    elif cmd in ("p", "pa"):
        tg.send("🔎 Ищу цены…")
        snaps = []
        text = prices_now(token, cfg, settings, arg or None, snaps)
        tg.send(text, pick_button(settings, snaps) + back_button(settings, arg or None))
    else:
        edit(*home_screen(settings))
    return False


# ---------- Люди: владелец и друзья по приглашению ----------

ACCESS_CMDS = {"users", "ua", "ud", "uq", "ux"}


def person_label(info):
    info = info or {}
    name = info.get("name") or "Без имени"
    return name + (f" (@{info['username']})" if info.get("username") else "")


class User:
    """Настройки, состояние и история одного человека.

    Владелец хранится в data/ (как раньше), остальные — в data/users/<chat id>/.
    """

    def __init__(self, chat_id, folder, tg, default_settings):
        self.chat_id = str(chat_id)
        self.dir = folder
        self.tg = tg
        self.settings = load(folder / "settings.json", None) or default_settings
        ensure_origins(self.settings)
        self.state = load(folder / "state.json", {})
        self.history = load(folder / "history.json", {})
        self.sent = load(folder / "sent.json", {})

    def save(self):
        for name in ("settings", "state", "history", "sent"):
            save(self.dir / f"{name}.json", getattr(self, name))


def users_screen(access, users, bot_link=""):
    lines = ["👥 Друзья\n"]
    buttons = []
    allowed = access["allowed"]
    if allowed:
        lines.append("Пользуются ботом (нажми, чтобы убрать):")
        for uid, info in allowed.items():
            settings = users[uid].settings if uid in users else {}
            n = len(settings.get("routes", [])) + len(settings.get("filters", [])) \
                + len(settings.get("trains", []))
            buttons.append([(f"👤 {person_label(info)} · направлений {n}", f"uq:{uid}")])
    else:
        lines.append("Пока ботом пользуешься только ты.")
    for uid, info in access["pending"].items():
        lines.append(f"\n⏳ Просится: {person_label(info)}")
        buttons.append([(f"✅ Пустить {info.get('name') or ''}".strip(), f"ua:{uid}"),
                        ("🚫 Нет", f"ud:{uid}")])
    for uid, info in access["denied"].items():
        buttons.append([(f"↩️ Всё-таки пустить {person_label(info)}", f"ua:{uid}")])
    lines.append("\nКак пригласить: отправь другу ссылку на бота"
                 + (f" {bot_link}" if bot_link else "")
                 + ". Друг напишет боту, а тебе придёт запрос «Пустить?». "
                 "У каждого свои направления и уведомления, твои они не видят.")
    return "\n".join(lines), buttons + [[("⬅️ В меню", "home")]]


class Bot:
    def __init__(self):
        self.token = os.environ["TRAVELPAYOUTS_TOKEN"]
        self.bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        owner_id = str(os.environ["TELEGRAM_CHAT_ID"])
        self.tg = Telegram(self.bot_token, owner_id)
        self.cfg = load(CONFIG, {})
        cfg = self.cfg
        self.owner = User(owner_id, ROOT / "data", self.tg, {
            "origin": cfg["origin"],
            "origin_name": cfg.get("origin_name", cfg["origin"]),
            "routes": [dict(r, city=r.get("city", r["destination"])) for r in cfg["routes"]],
        })
        self.owner.settings["owner"] = True
        self.access = load(ACCESS, {})
        for key in ("allowed", "pending", "denied"):
            self.access.setdefault(key, {})
        self.users = {owner_id: self.owner}
        for uid in self.access["allowed"]:
            self.users[uid] = self.load_user(uid)
        self.bot_link = ""

    # Старые имена: настройки и состояние владельца.
    settings = property(lambda self: self.owner.settings)
    state = property(lambda self: self.owner.state)
    history = property(lambda self: self.owner.history)
    sent = property(lambda self: self.owner.sent)

    def load_user(self, uid):
        info = self.access["allowed"].get(uid) or {}
        first = (info.get("name") or "").split(" ")[0]
        return User(uid, USERS_DIR / uid, Telegram(self.bot_token, uid), {
            "origin": self.cfg["origin"],
            "origin_name": self.cfg.get("origin_name", self.cfg["origin"]),
            "routes": [], "filters": [], "trains": [],
            "shout": first.upper() if first else "ЭЙ", "news": NEWS_ID,
        })

    def stranger(self, uid, chat):
        """Пишет человек, которому бот ещё не открыт: спрашиваем владельца."""
        if uid in self.access["denied"]:
            return
        tg = Telegram(self.bot_token, uid)
        if uid in self.access["pending"]:
            tg.send_plain("Запрос уже у владельца бота. Как только доступ откроют, я напишу.")
            return
        name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        info = {"name": name, "username": chat.get("username"),
                "ts": datetime.now(timezone.utc).isoformat()}
        self.access["pending"][uid] = info
        tg.send_plain("Привет! Это личный бот для поиска дешёвых билетов. "
                      "Я отправил запрос владельцу бота. Как только доступ откроют, я напишу.")
        self.tg.send(f"👤 {person_label(info)} хочет пользоваться ботом. Пустить?",
                     [[("✅ Пустить", f"ua:{uid}"), ("🚫 Нет", f"ud:{uid}")]])

    def get_bot_link(self):
        if not self.bot_link:
            try:
                name = self.tg.call("getMe").get("result", {}).get("username")
                self.bot_link = f"https://t.me/{name}" if name else ""
            except Exception as e:  # noqa: BLE001
                print(f"getMe error: {e!r}", file=sys.stderr)
        return self.bot_link

    def access_button(self, data, message_id):
        cmd, _, uid = data.partition(":")
        edit = lambda t, b: self.tg.edit(message_id, t, b)  # noqa: E731
        people = [[("👥 Друзья", "users")]]
        acc = self.access
        if cmd == "users":
            edit(*users_screen(acc, self.users, self.get_bot_link()))
        elif cmd == "ua":
            if uid in acc["allowed"]:
                edit(f"{person_label(acc['allowed'][uid])} уже пользуется ботом.", people)
                return
            info = acc["pending"].pop(uid, None) or acc["denied"].pop(uid, None)
            if info is None:
                edit("Этого запроса уже нет.", people)
                return
            acc["allowed"][uid] = info
            user = self.users[uid] = self.load_user(uid)
            user.tg.send("✅ Доступ открыт! Вот как я работаю:\n\n" + HELP)
            user.tg.send(*home_screen(user.settings))
            edit(f"✅ Пустил: {person_label(info)}. Твои направления этот человек не видит.", people)
        elif cmd == "ud":
            info = acc["pending"].pop(uid, None)
            if info is not None:
                acc["denied"][uid] = info
                Telegram(self.bot_token, uid).send_plain("Владелец бота не открыл доступ, извини.")
            edit(f"🚫 Не пустил: {person_label(info)}", people)
        elif cmd == "uq" and uid in acc["allowed"]:
            edit(f"Убрать {person_label(acc['allowed'][uid])}? Бот перестанет отвечать этому человеку "
                 "и присылать ему билеты.", [[("🗑 Да, убрать", f"ux:{uid}"), ("Отмена", "users")]])
        elif cmd == "ux" and uid in acc["allowed"]:
            info = acc["allowed"].pop(uid)
            user = self.users.pop(uid, None)
            if user:
                user.save()  # настройки остаются на диске: если пустишь снова, всё вернётся
            edit(f"🗑 Убрал: {person_label(info)}", people)
        else:
            edit(*users_screen(acc, self.users, self.get_bot_link()))

    def process_messages(self, wait=0):
        """Читает новые сообщения. Возвращает chat id людей, у которых поменялись направления."""
        changed = set()
        state = self.owner.state
        for upd in self.tg.updates(state.get("offset", 0), wait):
            state["offset"] = upd["update_id"] + 1
            cq = upd.get("callback_query")
            msg = (cq or {}).get("message") or upd.get("message") or {}
            chat = msg.get("chat", {})
            if chat.get("type", "private") != "private":
                continue  # в группах бот не работает
            uid = str(chat.get("id"))
            user = self.users.get(uid)
            try:
                if user is None:
                    if cq:
                        self.tg.answer(cq["id"])
                    else:
                        self.stranger(uid, chat)
                    continue
                if cq:
                    user.tg.answer(cq["id"])
                    data = cq.get("data", "")
                    if user is self.owner and data.partition(":")[0] in ACCESS_CMDS:
                        self.access_button(data, msg["message_id"])
                    elif handle_button(data, msg["message_id"], user.state, user.settings,
                                       user.tg, self.token, self.cfg):
                        changed.add(uid)
                elif "text" in msg:
                    if handle(msg["text"], user.state, user.settings, user.tg, self.token, self.cfg):
                        changed.add(uid)
            except Exception as e:  # noqa: BLE001
                print(f"handle error {uid}: {e!r}", file=sys.stderr)
                if user is not None:
                    user.tg.send("Что-то пошло не так, попробуй ещё раз.",
                                 back_button(user.settings, None))
        return changed

    def tell_news(self):
        """Один раз после обновления рассказываем, что нового (и обновляем кнопки внизу)."""
        for user in self.users.values():
            text = news_text(user.settings.get("news"))
            if not text:
                continue
            try:
                user.tg.send(text)
                user.settings["news"] = NEWS_ID
            except Exception as e:  # noqa: BLE001
                print(f"news error {user.chat_id}: {e!r}", file=sys.stderr)

    def step(self, wait=0):
        """Один проход: сообщения, затем (если пора) проверка цен, затем сохранение."""
        if not getattr(self, "news_told", False):
            self.news_told = True
            self.tell_news()
        changed = self.process_messages(wait)
        now = datetime.now(timezone.utc)
        state = self.owner.state
        last = state.get("last_check")
        due = not last or now - datetime.fromisoformat(last) >= timedelta(
            minutes=self.cfg["check_every_minutes"] - 5)
        for uid, user in list(self.users.items()):
            if not (due or uid in changed):
                continue
            try:
                check_prices(user.tg, self.token, self.cfg, user.settings, user.history, user.sent, now)
            except Exception as e:  # noqa: BLE001
                print(f"check error {uid}: {e!r}", file=sys.stderr)
            week_ago = now - timedelta(days=7)
            user.sent = {k: v for k, v in user.sent.items() if datetime.fromisoformat(v) >= week_ago}
        if due or self.owner.chat_id in changed:
            state["last_check"] = now.isoformat()
        for user in self.users.values():
            user.save()
        save(ACCESS, self.access)


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
