"""Временная проверка: время вылета, багаж и календарь подбора на живых ценах."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_prices as c


class TG(c.Telegram):
    def call(self, method, http_timeout=30, **p):
        if method in ("sendMessage", "editMessageText"):
            print(f"--- {method} ({len(p['text'])} знаков)\n{p['text']}")
            for row in (p.get("reply_markup") or {}).get("inline_keyboard", []):
                print("   [" + "] [".join(b["text"] for b in row) + "]")
        return {"ok": True}


tg = TG("x", "1")
token = os.environ["TRAVELPAYOUTS_TOKEN"]
cfg = {"months_ahead": 3, "history_days": 30, "drop_percent": 30, "realert_hours": 24}
hkt = {"id": "r1", "name": "ТАИЛАНД (Пхукет)", "destination": "HKT", "city": "Пхукет", "origin": "MOW",
       "origin_name": "Москва", "max_price": 25000}
ayt = {"id": "r2", "name": "ТУРЦИЯ (Анталья)", "destination": "AYT", "city": "Анталья", "origin": "MOW",
       "origin_name": "Москва", "max_price": 10000}
flt = {"id": "f1", "name": "ОАЭ (Дубай)", "destination": "DXB", "city": "Дубай", "origin": "MOW",
       "origin_name": "Москва", "max_price": 60000, "depart": "2026-11-14", "return": "2026-11-21",
       "round_trip": True}
settings = {"origin": "MOW", "origin_name": "Москва", "routes": [hkt, ayt], "filters": [flt], "trains": []}
c.ensure_origins(settings)
for title, setup in (("все", {}), ("Анталья только утро+день, обратно вечер", {"times": ["m", "d"], "back_times": ["e"]}),
                     ("Анталья только с багажом", {"bag": True})):
    ayt.pop("times", None); ayt.pop("back_times", None); ayt.pop("bag", None)
    ayt.update(setup)
    print(f"########## {title}")
    c.handle_button("in:r2", 1, {}, settings, tg, token, cfg)
print("########## Пхукет все")
c.handle_button("in:r1", 1, {}, settings, tg, token, cfg)
print("########## Подбор Дубай 14.11-21.11")
c.handle_button("in:f1", 1, {}, settings, tg, token, cfg)
flt["times"] = ["e"]
print("########## Подбор Дубай, вылет вечером")
c.handle_button("in:f1", 1, {}, settings, tg, token, cfg)
# багаж: статистика по тарифам
offers = c.route_offers(token, "MOW", {"destination": "AYT", "max_hours": 0}, {"months_ahead": 2})
print("BAG", {k: sum(1 for o in offers if c.has_bag(o) is k) for k in (True, False, None)}, len(offers))
