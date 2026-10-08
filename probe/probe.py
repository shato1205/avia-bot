"""Разовая загрузка справочников станций Travelpayouts/Tutu (удалить после проверки)."""
import json
import os
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
os.makedirs("probe/data", exist_ok=True)


def get(url):
    r = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(r, timeout=40) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except Exception as e:  # noqa: BLE001
        return "ERR", None, repr(e).encode()


FILES = {
    "tutu_routes.csv.zip": "https://support.travelpayouts.com/hc/ru/article_attachments/360031345731",
    "stations_ids.xlsx": "https://drive.google.com/uc?export=download&id=17YapFD0StwdwpdwPPJeDn9SDvAhmNkIf",
    "stations_routes.xlsx": "https://drive.google.com/uc?export=download&id=13_zsY1ZUejnwjg_QfdGlbr9LWujB_bWW",
}
for name, url in FILES.items():
    st, ct, body = get(url)
    print(name, st, ct, len(body), body[:120] if st != 200 else "", flush=True)
    if st == 200:
        open(f"probe/data/{name}", "wb").write(body)

for a, b in [("2000000", "2004000"), ("2004000", "2000000"), ("2000000", "2064130")]:
    st, ct, body = get(f"https://suggest.travelpayouts.com/search?service=tutu_trains&term={a}&term2={b}")
    print("\ntutu_trains", a, b, st)
    try:
        d = json.loads(body)
        print("keys", list(d), "url", d.get("url"), "trips", len(d.get("trips", [])))
        open(f"probe/data/trains_{a}_{b}.json", "wb").write(body)
    except Exception as e:  # noqa: BLE001
        print("bad", e, body[:300])
