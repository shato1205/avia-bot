"""Разовая проверка парсера поездов бота на живых данных (удалить после проверки)."""
import sys
import time
from datetime import date, timedelta
sys.path.insert(0, "probe")
import check_prices as c  # noqa: E402

for frm, to, days in (("2000000", "2004000", 3), ("2000000", "2064130", 30), ("2060615", "2000000", 60)):
    day = (date.today() + timedelta(days=days)).isoformat()
    t = time.time()
    try:
        offers = c.fetch_train_day(frm, to, day)
    except Exception as e:  # noqa: BLE001
        print(frm, to, day, "ERROR", repr(e), f"{time.time() - t:.1f}s", flush=True)
        continue
    print(f"\n{frm}->{to} {day}: {len(offers)} offers, {time.time() - t:.1f}s", flush=True)
    for o in sorted(offers, key=lambda o: o["price"])[:4]:
        print(" ", o["price"], c.train_offer_line(o).replace("\n", " | "))
    print("  cars:", sorted({o["car"] for o in offers}))
item = {"from": "2000000", "to": "2004000", "car": "coupe",
        "dates": [(date.today() + timedelta(days=5)).isoformat(), (date.today() + timedelta(days=7)).isoformat()]}
t = time.time()
offers = c.train_offers(item)
print(f"\nrange {item['dates']} coupe: {len(offers)} offers {time.time() - t:.1f}s, min",
      min((o["price"] for o in offers), default=None))
print("undated:", len(c.fetch_trains("2000000", "2004000")))
