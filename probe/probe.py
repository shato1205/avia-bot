"""Разовая выгрузка живых авиабилетов для анализа (удалить после проверки). Токен не печатается."""
import json
import os
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, timedelta

TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def months(n):
    d = date.today().replace(day=1)
    for _ in range(n):
        yield d.strftime("%Y-%m")
        d = (d + timedelta(days=32)).replace(day=1)


def get(params):
    q = dict(params, currency="rub", sorting="price", limit=30, token=TOKEN)
    try:
        with urllib.request.urlopen(URL + "?" + urllib.parse.urlencode(q), timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}


first = True
out = {}
for dest in ["HKT", "BKK", "CXR", "AYT", "DXB"]:
    rows = []
    for m in months(3):
        d = get({"origin": "MOW", "destination": dest, "departure_at": m, "one_way": "true"})
        if first and d.get("data"):
            print("KEYS:", json.dumps(d["data"][0], ensure_ascii=False))
            first = False
        rows += d.get("data", [])
    rows.sort(key=lambda o: o["price"])
    out[dest] = rows
    print(f"\n=== MOW->{dest}: {len(rows)} offers; airlines {Counter(o.get('airline') for o in rows).most_common(8)}")
    for o in rows[:12]:
        print(f"  {o['price']:>7} {o.get('airline')}{o.get('flight_number')} {o['departure_at'][:16]} "
              f"tr={o.get('transfers')} dur_to={o.get('duration_to')} dur={o.get('duration')} "
              f"{o.get('origin_airport')}->{o.get('destination_airport')} {o.get('link', '')[:60]}")
    if rows:
        fast = min(rows, key=lambda o: o.get("duration_to") or 9999)
        print(f"  fastest: {fast['price']} dur_to={fast.get('duration_to')} tr={fast.get('transfers')} {fast.get('airline')}")
    rt = []
    for m in months(3):
        rt += get({"origin": "MOW", "destination": dest, "departure_at": m, "one_way": "false"}).get("data", [])
    rt.sort(key=lambda o: o["price"])
    for o in rt[:3]:
        print(f"  RT {o['price']:>7} {o.get('airline')} {o['departure_at'][:10]}..{(o.get('return_at') or '')[:10]} "
              f"tr={o.get('transfers')}/{o.get('return_transfers')} dur={o.get('duration')}")
os.makedirs("probe/out", exist_ok=True)
json.dump(out, open("probe/out/avia.json", "w"), ensure_ascii=False)
