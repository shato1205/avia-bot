#!/bin/bash
# Разовая проверка: какие сайты с ценами на поезда открываются с зарубежного сервера.
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
D=$(date -d "+14 days" +%Y-%m-%d)
try() {
  echo; echo "== $*"
  curl -sS -m 12 -A "$UA" -o /tmp/r -w "HTTP %{http_code} %{content_type} %{size_download}b %{time_total}s\n" "$@" 2>&1
  head -c 400 /tmp/r 2>/dev/null | tr '\n' ' '; echo
}
curl -sS -m 10 https://ipinfo.io/json; echo
try https://www.tutu.travel/
try https://www.tutu.travel/poezda/
try -X POST -H "Content-Type: application/json" -H "Origin: https://www.tutu.travel" -d "{\"routes\":[{\"departureStationCode\":\"2000000\",\"arrivalStationCode\":\"2004000\",\"departureDate\":\"$D\"}],\"searchId\":\"6b1f0c2e-1111-4a2b-9c3d-123456789abc\",\"source\":\"trainOffers\"}" https://offers-api.tutu.travel/railway/offers
try https://travel.yandex.ru/trains/
try "https://api.rasp.yandex.net/v3.0/search/?apikey=test&from=c213&to=c2&transport_types=train&date=$D"
try https://rasp.yandex.ru/
try https://www.ufs-online.ru/
try https://www.onetwotrip.com/ru/poezda/
try https://www.kupibilet.ru/
try https://poezd.ru/
try https://www.rzd.ru/
try https://ticket.rzd.ru/
try https://www.tutu.ru/
try https://offers-api.tutu.ru/
try https://bilet.ru/
try https://www.biletix.ru/
try https://www.ostrovok.ru/
try https://trains.travelpayouts.com/
