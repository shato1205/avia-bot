"""Временная выгрузка: обратные билеты через новый код бота (без токена в выводе)."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_prices as c

token = os.environ["TRAVELPAYOUTS_TOKEN"]
cfg = json.load(open("config.json", encoding="utf-8"))
backs = [{}, {"back": {"days": [13, 15]}}, {"back": {"days": [6, 8]}}]
for r in cfg["routes"]:
    r = dict(r, origin="MOW", id="x")
    all_offers, offers = c.item_offers(token, "route", r, cfg)
    if not offers:
        print(f"=== {r['city']}: нет билетов")
        continue
    best = min(offers, key=lambda o: o["price"])
    print(f"\n=== {r['city']}: {best['price']} ₽ | {c.offer_line(best, 'Москва', r)}".replace("\n", " || "))
    for b in backs:
        item = dict(r, **b)
        rt, back = c.return_info(token, "MOW", r["destination"], best, item)
        print(f"  [{c.back_text(item)}] " + c.return_text(best, rt, back, item).replace("\n", " || "))
        if rt:
            print(f"     rt link: {c.offer_link(rt)[:160]}")
        time.sleep(0.3)
