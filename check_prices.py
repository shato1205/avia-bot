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
        "limit": 30,  # с запасом: часть билетов отсеется из-за долгих пересадок
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
                                 direct=route.get("direct", False))
    return comfort(offers, route.get("max_hours")) if comfort_filter else offers


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
        return all_offers, comfort(all_offers, item.get("max_hours"))
    all_offers = filter_offers(token, item["origin"], item, comfort_filter=False)
    return all_offers, comfort(all_offers, item.get("max_hours"), 2 if item.get("round_trip") else 1)


def skipped_note(all_offers, offers):
    """Про отсеянные билеты: «дешевле есть, но 31 ч в пути»."""
    best = min(o["price"] for o in offers) if offers else None
    cheaper = [o for o in all_offers if o not in offers and (best is None or o["price"] < best)]
    if not cheaper:
        return ""
    o = min(cheaper, key=lambda x: x["price"])
    return (f"💤 Дешевле есть: {fmt_price(o['price'])} ₽, но в пути {fmt_minutes(travel_minutes(o))} "
            "— такие не показываю")


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
    offers = fetch_cheapest(token, origin, f["destination"], f["depart"], f.get("return"),
                            one_way=not f.get("round_trip"), direct=f.get("direct", False))
    if not comfort_filter:
        return offers
    return comfort(offers, f.get("max_hours"), 2 if f.get("round_trip") else 1)


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
            rt += fetch_cheapest(token, origin, destination, dep.isoformat(), q, one_way=False)
            back += fetch_cheapest(token, destination, origin, q)
        rt = comfort([o for o in rt if inside(o.get("return_at", ""))], item.get("max_hours"), 2)
        back = comfort([o for o in back if inside(o["departure_at"])], item.get("max_hours"))
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
            "Выбери срок кнопкой или нажми «✏️ Вписать свою дату».")
    btns = [(label, f"iy:{i}:{lo}-{hi}") for label, lo, hi in BACK_PRESETS]
    return text, rows(btns, 3) + [[("✏️ Вписать свою дату", f"ie:{i}")], [("⬅️ Отмена", f"i:{i}")]]


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


def format_alert(route, offer, reason, origin_name, extra=""):
    return (
        f"🔥 ГОША СРОЧНО {route['name']} {fmt_price(offer['price'])} ₽\n"
        f"{offer_line(offer, origin_name, route)}\n"
        + (f"{extra}\n" if extra else "")
        + f"Почему: {reason}"
    )


def alert_buttons(item, link=None, rt=None):
    buy = [[("🎫 Купить билет", link)]] if link else []
    if rt:
        buy.append([("🔁 Купить туда-обратно", offer_link(rt))])
    return buy + [[("⚙️ Настроить", f"i:{item['id']}"), ("⏸ Пауза", f"iz:{item['id']}")]]


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
            rt, back = return_info(token, origin, route["destination"], o, route)
            tg.send(format_alert(route, o, reason, route["origin_name"], return_text(o, rt, back, route)),
                    alert_buttons(route, offer_link(o), rt))
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
        rt = extra = None
        if not f.get("round_trip"):
            rt, back = return_info(token, origin, f["destination"], o, f)
            extra = return_text(o, rt, back, f)
        tg.send(f"🎯 ГОША СРОЧНО {f['name']} {fmt_price(o['price'])} ₽{kind}\n"
                f"{offer_line(o, f['origin_name'], f)}\n" + (f"{extra}\n" if extra else "")
                + f"Подбор: {filter_text(f)}", alert_buttons(f, offer_link(o), rt))
        sent[sent_key] = now.isoformat()
        alerts += 1
    alerts += check_trains(tg, cfg, settings, history, sent, now)
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
        offers = train_offers(t)
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
            tg.send(f"🚆 ГОША СРОЧНО ПОЕЗД {t['from_name']} → {t['to_name']} "
                    f"{fmt_price(o['price'])} ₽\n{train_offer_line(o)}\nПочему: {reason}",
                    alert_buttons(t, o["link"]))
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


def prices_now(token, cfg, settings, origin=None):
    """Цены по направлениям. origin — только этот город вылета (без поездов)."""
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
            note = skipped_note(all_offers, offers)
            if not offers:
                lines.append(f"{title}: билетов не нашёл" + (f"\n{note}" if note else ""))
                continue
            best = min(offers, key=lambda o: o["price"])
            back_line = ""
            if not item.get("round_trip"):
                rt, back = return_info(token, item["origin"], item["destination"], best, item)
                back_line = "\n" + return_text(best, rt, back, item)
            lines.append(f"{title}: {fmt_price(best['price'])} ₽\n"
                         f"{offer_line(best, origin_name, item)}{back_line}" + (f"\n{note}" if note else ""))
    if trains:
        lines.append("\n" + train_prices(settings))
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


def train_link(frm, to, day=None):
    link = f"{TUTU}/poezda/rasp_d.php?nnst1={frm}&nnst2={to}"
    if day:
        link += "&date=" + date.fromisoformat(day).strftime("%d.%m.%Y")
    return link


def train_key(t):
    dates = "-".join(t["dates"]) if t.get("dates") else "any"
    return f"T|{t['from']}-{t['to']}|{t.get('car', 'any')}|{dates}"


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
    return offers


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
    title = f"{t['from_name']} → {t['to_name']}"
    if t.get("dates"):
        title += " " + train_dates_short(t)
    if t.get("car", "any") != "any":
        title += f", {CARS[t['car']]}"
    return title


def train_dates_short(t):
    a, b = (f"{d[8:]}.{d[5:7]}" for d in t["dates"])
    return a if a == b else f"{a}–{b}"


def train_prices(settings):
    lines = ["🚆 Поезда:"]
    trains = settings.get("trains", [])
    if not trains:
        return "Поездов пока нет, добавь их в меню 🏠 → 🚆 Поезда"
    for t in trains:
        offers = train_offers(t)
        if not offers:
            lines.append(f"{train_title(t)}: билетов не нашёл")
            continue
        best = min(offers, key=lambda o: o["price"])
        lines.append(f"{train_title(t)}: {fmt_price(best['price'])} ₽\n{train_offer_line(best)}")
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
        car = f" · {CARS[item['car']]}" if item.get("car", "any") != "any" else ""
        limit = f"до {k(item['max_price'])}" if item.get("max_price") else "падения"
        return f"{pause}🚆 {item['from_name']} → {item['to_name']}{dates}{car} · {limit}{last}"
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


def last_price_line(item):
    ts = datetime.fromisoformat(item["last_ts"]).astimezone(MSK).strftime("%d.%m %H:%M")
    return f"Последняя цена: {fmt_price(item['last_price'])} ₽ (мск {ts})"


def train_screen(item, note=""):
    i = item["id"]
    lines = [note] if note else []
    lines.append(f"🚆 {item['from_name']} → {item['to_name']}")
    if item.get("dates"):
        lines.append(f"Даты: {train_dates_short(item)} (точные цены и места по данным Туту.ру)")
    else:
        lines.append("Даты: любые (цены «от» по данным Туту.ру, без конкретной даты)")
    lines.append("Вагон: " + CARS[item.get("car", "any")])
    lines.append(f"Сообщу, когда билет дешевле {fmt_price(item['max_price'])} ₽ "
                 "или резко подешевеет." if item.get("max_price")
                 else "Сообщу, когда билет резко подешевеет.")
    if item.get("last_price"):
        lines.append(last_price_line(item))
    lines.append("⏸ На паузе: не присылаю уведомления" if item.get("paused") else "✅ Слежу")
    buttons = [
        [("− 1к", f"ip:{i}:-1000"), ("− 500", f"ip:{i}:-500"),
         ("+ 500", f"ip:{i}:500"), ("+ 1к", f"ip:{i}:1000")],
        [("✏️ Своя цена", f"it:{i}"), ("📅 Даты", f"id:{i}")],
        [("🛏 Вагон: " + CARS[item.get("car", "any")], f"iv:{i}"),
         ("▶️ Возобновить" if item.get("paused") else "⏸ Пауза", f"iz:{i}")],
        [("🔎 Цена сейчас", f"in:{i}"), ("🗑 Удалить", f"iq:{i}")],
        [("⬅️ Поезда", "t")],
    ]
    return "\n".join(lines), buttons


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
    lines.append("Только прямые рейсы ✈️" if item.get("direct") else "С пересадками тоже")
    lines.append("⏱ Время в пути: " + hours_text(item.get("max_hours")))
    if not item.get("round_trip"):
        lines.append("↩️ Обратно: " + back_text(item) + " (покажу цену туда-обратно)")
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
         ("⏱ В пути: " + hours_label(item.get("max_hours")), f"ih:{i}")],
        ([] if item.get("round_trip") else [("↩️ Когда обратно", f"ib:{i}")])
        + [("▶️ Возобновить" if item.get("paused") else "⏸ Пауза", f"iz:{i}")],
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


def wiz_cancel(wiz):
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
TRAIN_DATES_HINT = ("Нажми на дату или напиши свою: 15.12 — один день, "
                    "15.12-20.12 — несколько дней подряд (до 7).\n"
                    "«Любая дата» — слежу за ценами «от» без конкретной даты.")


def pick_train_dates_screen(title, prefix, cancel):
    """Даты для поезда: ближайшие пятницы–воскресенья кнопками, свои даты текстом."""
    days, d = [], datetime.now(MSK).date() + timedelta(days=1)
    while len(days) < 6:
        if d.weekday() >= 4:
            days.append(d)
        d += timedelta(days=1)
    btns = [(f"{WEEKDAYS[d.weekday()]} {d:%d.%m}", f"{prefix}{d.isoformat()}") for d in days]
    return (f"{title}: когда едем?\n\n{TRAIN_DATES_HINT}",
            rows(btns, 3) + [[("📅 Любая дата", f"{prefix}any")], cancel])


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
        offers = train_offers(wiz)
        return min((o["price"] for o in offers), default=None)
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


def train_wizard_next(state, settings, tg, token, cfg, show):
    wiz = state["wiz"]
    if "to" not in wiz:
        state["awaiting"] = {"type": "wiz_station"}
        return show(*pick_station_screen(wiz))
    if "car" not in wiz:
        return show(*pick_car_screen(wiz))
    if "dates" not in wiz:
        state["awaiting"] = {"type": "wiz_tdates"}
        return show(*pick_train_dates_screen(f"🚆 {wiz['city']}", "wd:", wiz_cancel(wiz)))
    if "price" not in wiz:
        state["awaiting"] = {"type": "wiz_price"}
        return show(*pick_price_screen(wiz, wizard_current_price(token, cfg, settings, wiz)))

    item = {"id": uuid.uuid4().hex[:8], "from": wiz["from"], "from_name": wiz["from_name"],
            "to": wiz["to"], "to_name": wiz["to_name"], "city": wiz["city"],
            "car": wiz["car"], "dates": wiz["dates"], "max_price": wiz["price"] or None}
    same = ("from", "to", "car", "dates")
    settings["trains"] = [t for t in settings["trains"]
                          if [t.get(f) for f in same] != [item[f] for f in same]]
    settings["trains"].append(item)
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
            item.pop("last_price", None)
        elif kind == "item_price":
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
    elif cmd == "t":
        edit(*trains_screen(settings))
    elif cmd == "tp":
        tg.send("🔎 Ищу цены…")
        tg.send(train_prices(settings), [[("⬅️ Поезда", "t")]])
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
    elif cmd == "wd" and state.get("wiz", {}).get("kind") == "t":
        state["wiz"]["dates"] = None if arg == "any" else [arg, arg]
        wizard_next(state, settings, tg, token, cfg, message_id)
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
    elif cmd in ("i", "ip", "it", "id", "ia", "ir", "ic", "ih", "ib", "ie", "iy", "iv", "iz", "in", "iq",
                 "ix"):
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
        elif cmd == "id" and kind == "train":
            state["awaiting"] = {"type": "item_dates", "id": item_id}
            edit(*pick_train_dates_screen(f"🚆 {item['city']}", f"ia:{item_id}:",
                                          [("⬅️ Отмена", f"i:{item_id}")]))
            return False
        elif cmd == "ia":
            item["dates"] = None if extra == "any" else [extra, extra]
            item.pop("last_price", None)
            note = "✅ Даты: " + (train_dates_short(item) if item["dates"] else "любые") + "\n"
        elif cmd == "id":
            state["awaiting"] = {"type": "item_dates", "id": item_id}
            tg.send(f"🎯 {item['city']}: напиши новые даты, например 15.12-25.12, 20.11 или декабрь.",
                    [[("⬅️ Отмена", f"i:{item_id}")]])
            return False
        elif cmd == "ir":
            item["round_trip"] = not item.get("round_trip")
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
            offers = train_offers(item)
            remember_price(item, offers, datetime.now(timezone.utc))
            back = [[("⬅️ К направлению", f"i:{item_id}")]]
            if not offers:
                tg.send(f"{train_title(item)}: билетов не нашёл 😔", back)
                return False
            best = []  # три самых дешёвых разных поезда
            for o in sorted(offers, key=lambda o: o["price"]):
                if all((o["train"], o.get("date")) != (b["train"], b.get("date")) for b in best):
                    best.append(o)
            best = best[:3]
            note = ("" if item.get("dates")
                    else "\n\nЭто цены «от»: даты и места смотри на Туту.ру")
            tg.send(f"💰 {train_title(item)}: от {fmt_price(best[0]['price'])} ₽\n\n"
                    + "\n\n".join(f"{fmt_price(o['price'])} ₽ · " + train_offer_line(o, False, False)
                                  for o in best) + note,
                    [[("🎫 Купить билет", best[0]["link"])]] + back)
            return False
        elif cmd == "in":
            tg.send("🔎 Ищу цены…")
            all_offers, offers = item_offers(token, kind, item, cfg)
            remember_price(item, offers, datetime.now(timezone.utc))
            back = [[("⬅️ К направлению", f"i:{item_id}")]]
            note = skipped_note(all_offers, offers)
            if not offers:
                tg.send(f"{item['city']}: билетов не нашёл 😔" + (f"\n\n{note}" if note else ""), back)
                return False
            best = min(offers, key=lambda o: o["price"])
            text = f"💰 {item['city']}: {fmt_price(best['price'])} ₽\n{offer_line(best, item['origin_name'], item)}"
            buttons = [[("🎫 Купить билет", offer_link(best))]]
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
            if note:
                text += f"\n\n{note}"
            tg.send(text, buttons + back)
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
        return cmd in ("ip", "ir", "ic", "ih", "iv", "ia")

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
