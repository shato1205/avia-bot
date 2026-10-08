"""Разовая проверка цен Туту.ру на дату (удалить после проверки)."""
import json
import os
import time
import urllib.request
import uuid
from datetime import date, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
os.makedirs("probe/out", exist_ok=True)
d = date.today() + timedelta(days=14)


def req(name, url, body=None, headers=None, timeout=60):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
         "Accept-Language": "ru-RU,ru;q=0.9"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=h, method="POST" if body is not None else "GET")
    t = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            b = resp.read()
            st = resp.status
    except urllib.error.HTTPError as e:
        b, st = e.read(), e.code
    except Exception as e:  # noqa: BLE001
        b, st = repr(e).encode(), "ERR"
    print(f"{name}: {st} {len(b)}b {time.time() - t:.1f}s {b[:200]!r}", flush=True)
    open(f"probe/out/{name}", "wb").write(b)


TUTU = {"Origin": "https://www.tutu.ru", "Referer": "https://www.tutu.ru/"}
for fmt in (d.isoformat(), d.strftime("%d.%m.%Y")):
    body = {"routes": [{"departureStationCode": "2000000", "arrivalStationCode": "2004000",
                        "departureDate": fmt}],
            "searchId": str(uuid.uuid4()), "source": "trainOffers"}
    req(f"offers_{fmt}.json", "https://offers-api.tutu.ru/railway/offers", body, TUTU)
req("rasp_d.html", f"https://www.tutu.ru/poezda/rasp_d.php?nnst1=2000000&nnst2=2004000&date={d.strftime('%d.%m.%Y')}",
    headers={"Accept": "text/html"})
req("rasp_d_sochi.html", f"https://www.tutu.ru/poezda/rasp_d.php?nnst1=2000000&nnst2=2064130&date={d.strftime('%d.%m.%Y')}",
    headers={"Accept": "text/html"})
req("ufs.html", f"https://www.ufs-online.ru/kupit-zhd-bilety/2000000/2004000?date={d.strftime('%d.%m.%Y')}",
    headers={"Accept": "text/html"})
