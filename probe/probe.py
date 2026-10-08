"""Разовая проверка источников цен на поезда (удалить после проверки)."""
import io
import signal
import json
import sys
import urllib.request
import uuid
import zipfile
from datetime import date, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _alarm(signum, frame):
    raise TimeoutError("hard timeout")


signal.signal(signal.SIGALRM, _alarm)


def req(url, body=None, headers=None, raw=False):
    signal.alarm(25)
    try:
        return _req(url, body, headers, raw)
    finally:
        signal.alarm(0)


def _req(url, body=None, headers=None, raw=False):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            b = resp.read()
            return resp.status, b if raw else b.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:800]
    except Exception as e:  # noqa: BLE001
        return "ERR", repr(e)


def schema(obj, path="", out=None, limit=400):
    out = {} if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            schema(v, f"{path}.{k}", out, limit)
    elif isinstance(obj, list):
        for v in obj[:3]:
            schema(v, f"{path}[]", out, limit)
    else:
        out.setdefault(path, repr(obj)[:90])
    return out


def section(title):
    print("\n" + "=" * 20, title, "=" * 20, flush=True)


d1 = (date.today() + timedelta(days=14)).isoformat()

section(f"tutu offers MOW->SPB {d1}")
body = {"routes": [{"departureStationCode": "2000000", "arrivalStationCode": "2004000",
                    "departureDate": d1}],
        "searchId": str(uuid.uuid4()), "source": "trainOffers"}
st, txt = req("https://offers-api.tutu.ru/railway/offers", body,
              {"Origin": "https://www.tutu.ru", "Referer": "https://www.tutu.ru/"})
print("status", st, "len", len(txt))
try:
    data = json.loads(txt)
    for p, v in schema(data).items():
        print(p, "=", v)
    open("tutu_offers.json", "w").write(txt)
except Exception as e:  # noqa: BLE001
    print("not json", e, txt[:1500])

section("travelpayouts tutu_trains MOW->SPB")
st, txt = req("https://suggest.travelpayouts.com/search?service=tutu_trains&term=2000000&term2=2004000")
print("status", st)
print(txt[:2500])

section("station suggest candidates")
for u in [
    "https://www.tutu.ru/suggest/railway_simple/?name=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C",
    "https://www.tutu.ru/suggest/railway/?name=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C",
    "https://www.tutu.ru/station/suggest.php?name=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C",
    "https://suggest.travelpayouts.com/search?service=tutu_stations&term=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C",
    "https://ticket.rzd.ru/api/v1/suggests?Query=%D0%BA%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C&TransportType=rail&GroupResults=true&RailwaySortPriority=true&MergeSuburban=true",
    "https://pass.rzd.ru/suggester?stationNamePart=%D0%9A%D0%90%D0%97%D0%90%D0%9D%D0%AC&lang=ru&compactMode=y",
]:
    st, txt = req(u)
    print("\n--", u, "\nstatus", st, "\n", txt[:700])

section("travelpayouts tutu_routes.csv.zip")
st, b = req("https://support.travelpayouts.com/hc/ru/article_attachments/360031345731", raw=True)
print("status", st)
if st == 200:
    try:
        z = zipfile.ZipFile(io.BytesIO(b))
        for n in z.namelist():
            print("file", n, z.getinfo(n).file_size)
            lines = z.read(n).decode("utf-8", "replace").splitlines()
            print("rows", len(lines))
            for line in lines[:8]:
                print(line)
            for line in lines:
                if "Казан" in line:
                    print("KZN:", line)
                    break
    except Exception as e:  # noqa: BLE001
        print("zip err", e, b[:300])
else:
    print(str(b)[:500])

section("rzd ticket pricing reachability")
body = {"Origin": "2000000", "Destination": "2004000", "DepartureDate": d1 + "T00:00:00",
        "TimeFrom": 0, "TimeTo": 24, "CarGrouping": "DontGroup", "GetByLocalTime": True,
        "SpecialPlacesDemand": "StandardPlacesAndForDisabledPersons", "CarIssuingType": "All",
        "GetTrainsFromSchedule": True}
st, txt = req("https://ticket.rzd.ru/apib2b/p/Railway/V1/Search/TrainPricing?service_provider=B2B_RZD", body,
              {"Origin": "https://ticket.rzd.ru", "Referer": "https://ticket.rzd.ru/"})
print("status", st, txt[:500])
sys.exit(0)
