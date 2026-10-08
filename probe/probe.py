"""Временная проверка: отложенные билеты находятся снова на живых ценах."""
import os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_prices as c

token = os.environ["TRAVELPAYOUTS_TOKEN"]
now = datetime.now(timezone.utc)
item = {"origin": "MOW", "destination": "AYT", "origin_name": "Москва", "city": "Анталья"}
offers = c.route_offers(token, "MOW", {"destination": "AYT", "max_hours": 0}, {"months_ahead": 2})
offers.sort(key=lambda o: o["price"])
for o in offers[:3] + offers[10:12]:
    snap = c.flight_snapshot(o, "MOW", "AYT", "Москва", "Анталья")
    s = dict(snap, id="x", saved=now.isoformat())
    print("FLIGHT", snap["label"], "->", c.recheck_saved(token, s, now))
rt = c.fetch_cheapest(token, "MOW", "DXB", "2026-11-14", "2026-11-21", one_way=False)
for o in sorted(rt, key=lambda o: o["price"])[:2]:
    s = dict(c.flight_snapshot(o, "MOW", "DXB", "Москва", "Дубай"), id="y", saved=now.isoformat())
    print("RT", s["label"], "->", c.recheck_saved(token, s, now))
t = {"from": "2000000", "to": "2004000", "from_name": "Москва", "to_name": "Санкт-Петербург",
     "car": "any", "dates": ["2026-10-23", "2026-10-23"], "back_dates": ["2026-10-26", "2026-10-26"]}
both = c.train_both(t)
pair = c.train_trip_offers(t, both)[0]
for o, back in [(pair, False), (sorted(both[0], key=lambda o: o["price"])[5], False), (both[1][3], True)]:
    s = dict(c.train_snapshot(o, t, back), id="z", saved=now.isoformat())
    print("TRAIN", s["label"], "->", c.recheck_saved(token, s, now), "seats", s.get("seats"))
