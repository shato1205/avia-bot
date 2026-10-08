"""Временная проверка: поезда по времени суток на живых данных Туту.ру."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_prices as c


class TG(c.Telegram):
    def call(self, method, http_timeout=30, **p):
        if method == "sendMessage":
            print(f"--- message ({len(p['text'])} знаков)\n{p['text']}")
            for row in (p.get("reply_markup") or {}).get("inline_keyboard", []):
                print("   [" + "] [".join(b["text"] for b in row) + "]")
        return {"ok": True}


tg = TG("x", "1")
one = {"id": "a1", "from": "2000000", "from_name": "Москва", "to": "2004000", "to_name": "Санкт-Петербург",
       "city": "Москва → Санкт-Петербург", "car": "any", "dates": ["2026-10-23", "2026-10-23"]}
rt = {"id": "b2", "from": "2000000", "from_name": "Москва", "to": "2060615", "to_name": "Казань",
      "city": "Москва → Казань", "car": "coupe", "dates": ["2026-10-23", "2026-10-24"],
      "back_dates": ["2026-10-26", "2026-10-26"], "back_times": ["d", "e"]}
undated = {"id": "c3", "from": "2000000", "from_name": "Москва", "to": "2004000",
           "to_name": "Санкт-Петербург", "city": "Москва → Санкт-Петербург", "car": "plazcard", "dates": None}
settings = {"origin": "MOW", "origin_name": "Москва", "routes": [], "filters": [], "trains": [one, rt, undated]}
c.ensure_origins(settings)
print("########## Цены на поезда")
c.handle_button("tp", 1, {}, settings, tg, "", {})
for item in (one, rt):
    print(f"########## Цена сейчас {item['id']}")
    c.handle_button(f"in:{item['id']}", 1, {}, settings, tg, "", {})
one["times"] = ["e"]
print("########## Цена сейчас a1, только вечер")
c.handle_button("in:a1", 1, {}, settings, tg, "", {})
