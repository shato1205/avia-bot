"""Временная проверка: поезд туда-обратно на живых данных Туту.ру."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_prices as c

for frm, to, car in (("2000000", "2004000", "any"), ("2000000", "2060615", "coupe")):
    item = {"id": "x", "from": frm, "from_name": c.station_name(frm), "to": to,
            "to_name": c.station_name(to), "car": car,
            "dates": ["2026-10-23", "2026-10-23"], "back_dates": ["2026-10-25", "2026-10-26"]}
    offers = c.train_trip_offers(item)
    print("===", c.train_title(item), "->", len(offers))
    for o in offers:
        print(f"{o['price']} ₽ туда-обратно\n{c.train_pair_text(o)}\n")
