"""Временная выгрузка: живые билеты через новый код бота (без токена в выводе)."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_prices as c

token = os.environ["TRAVELPAYOUTS_TOKEN"]
cfg = json.load(open("config.json", encoding="utf-8"))
routes = cfg["routes"] + [
    {"name": "ИНДОНЕЗИЯ (Бали)", "destination": "DPS", "city": "Бали"},
    {"name": "ЕГИПЕТ (Хургада)", "destination": "HRG", "city": "Хургада"},
    {"name": "ТУРЦИЯ (Стамбул)", "destination": "IST", "city": "Стамбул"},
    {"name": "ШРИ-ЛАНКА (Коломбо)", "destination": "CMB", "city": "Коломбо"},
    {"name": "ВЬЕТНАМ (Хошимин)", "destination": "SGN", "city": "Хошимин"},
]

def show(label, o, route):
    print(f"  [{label}] {o['price']} ₽ | {c.offer_line(o, 'Москва', route)}".replace("\n", " || "))

for r in routes:
    r["origin"] = "MOW"
    try:
        all_offers, offers = c.item_offers(token, "route", r, cfg)
    except Exception as e:  # noqa: BLE001
        print(f"=== {r['city']}: ошибка {type(e).__name__}")
        continue
    print(f"\n=== {r['city']} ({r['destination']}): всего {len(all_offers)}, после отсева {len(offers)}")
    if not all_offers:
        continue
    cheapest = min(all_offers, key=lambda o: o["price"])
    show("самый дешёвый вообще", cheapest, r)
    if offers:
        best = min(offers, key=lambda o: o["price"])
        show("лучший без долгих пересадок", best, r)
        fast = min(offers, key=lambda o: (c.travel_minutes(o) or 10**6, o["price"]))
        show("самый быстрый", fast, r)
    direct = [o for o in all_offers if o.get("transfers") == 0]
    if direct:
        show("самый дешёвый прямой", min(direct, key=lambda o: o["price"]), r)
    note = c.skipped_note(all_offers, offers)
    if note:
        print("  " + note)
    time.sleep(0.5)

print("\n##### ТУДА-ОБРАТНО")
for r in routes[:5] + routes[5:6]:
    for month in c.months(3)[1:]:
        f = dict(r, origin="MOW", depart=month, round_trip=True)
        try:
            all_offers, offers = c.item_offers(token, "filter", f, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"=== {r['city']} {month}: ошибка {type(e).__name__}")
            continue
        print(f"\n=== {r['city']} {month} туда-обратно: всего {len(all_offers)}, после отсева {len(offers)}")
        if all_offers:
            show("самый дешёвый вообще", min(all_offers, key=lambda o: o["price"]), f)
        if offers:
            show("лучший без долгих пересадок", min(offers, key=lambda o: o["price"]), f)
        time.sleep(0.5)
