from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import json
import time
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
PORT = 5055
CACHE_TTL_SECONDS = 300

BMKG_URLS = {
    "latest_quake": "https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json",
    "felt_quakes": "https://data.bmkg.go.id/DataMKG/TEWS/gempadirasakan.json",
    "weather": "https://api.bmkg.go.id/publik/prakiraan-cuaca?adm4={adm4}",
    "district_weather": "https://www.bmkg.go.id/cuaca/prakiraan-cuaca/{adm3}",
    "weather_alerts": "https://www.bmkg.go.id/alerts/nowcast/id",
}

REGIONS = [
    {"group": "Kota Palu", "adm3": "72.71.01", "name": "Palu Timur"},
    {"group": "Kota Palu", "adm3": "72.71.02", "name": "Palu Barat"},
    {"group": "Kota Palu", "adm3": "72.71.03", "name": "Palu Selatan"},
    {"group": "Kota Palu", "adm3": "72.71.04", "name": "Palu Utara"},
    {"group": "Kota Palu", "adm3": "72.71.05", "name": "Ulujadi"},
    {"group": "Kota Palu", "adm3": "72.71.06", "name": "Tatanga"},
    {"group": "Kota Palu", "adm3": "72.71.07", "name": "Tawaeli"},
    {"group": "Kota Palu", "adm3": "72.71.08", "name": "Mantikulore"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.01", "name": "Sigi Biromaru"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.02", "name": "Palolo"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.03", "name": "Nokilalaki"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.04", "name": "Lindu"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.05", "name": "Kulawi"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.06", "name": "Kulawi Selatan"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.07", "name": "Pipikoro"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.08", "name": "Gumbasa"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.09", "name": "Dolo Selatan"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.10", "name": "Tanambulava"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.11", "name": "Dolo Barat"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.12", "name": "Dolo"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.13", "name": "Kinovaro"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.14", "name": "Marawola"},
    {"group": "Kabupaten Sigi", "adm3": "72.10.15", "name": "Marawola Barat"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.04", "name": "Rio Pakava"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.06", "name": "Dampelas"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.08", "name": "Banawa"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.09", "name": "Labuan"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.10", "name": "Sindue"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.11", "name": "Sirenja"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.12", "name": "Balaesang"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.14", "name": "Sojol"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.18", "name": "Banawa Selatan"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.19", "name": "Tanantovea"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.21", "name": "Pinembani"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.24", "name": "Sindue Tombusabora"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.25", "name": "Sindue Tobata"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.27", "name": "Banawa Tengah"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.30", "name": "Sojol Utara"},
    {"group": "Kabupaten Donggala", "adm3": "72.03.31", "name": "Balaesang Tanjung"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.01", "name": "Parigi"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.02", "name": "Ampibabo"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.03", "name": "Tinombo"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.04", "name": "Moutong"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.05", "name": "Tomini"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.06", "name": "Sausu"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.07", "name": "Bolano Lambunu"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.08", "name": "Kasimbar"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.09", "name": "Torue"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.10", "name": "Tinombo Selatan"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.11", "name": "Parigi Selatan"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.12", "name": "Mepanga"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.13", "name": "Toribulu"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.14", "name": "Taopa"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.15", "name": "Balinggi"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.16", "name": "Parigi Barat"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.17", "name": "Siniu"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.18", "name": "Palasa"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.19", "name": "Parigi Utara"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.20", "name": "Parigi Tengah"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.21", "name": "Bolano"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.22", "name": "Ongka Malino"},
    {"group": "Kabupaten Parigi Moutong", "adm3": "72.08.23", "name": "Sidoan"},
]

DEFAULT_ADM3 = "72.71.08"
DEFAULT_ADM4 = "72.71.08.1002"

SULTENG_KEYWORDS = (
    "palu",
    "sigi",
    "donggala",
    "parigi",
    "poso",
    "sulawesi tengah",
    "sulteng",
)

_cache = {}


def cached_json(key, fetcher):
    now = time.time()
    item = _cache.get(key)
    if item and now - item["at"] < CACHE_TTL_SECONDS:
        return item["value"]

    value = fetcher()
    _cache[key] = {"at": now, "value": value}
    return value


def fetch_text(url):
    request = Request(
        url,
        headers={
            "User-Agent": "curl/8.0",
            "Accept": "application/json, application/xml, text/xml, */*",
        },
    )
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_json(url):
    return json.loads(fetch_text(url))


def region_by_adm3(adm3):
    for region in REGIONS:
        if region["adm3"] == adm3:
            return region
    return next(region for region in REGIONS if region["adm3"] == DEFAULT_ADM3)


def fetch_region_locations(adm3):
    region = region_by_adm3(adm3)
    html = cached_json(
        f"region:{adm3}",
        lambda: fetch_text(BMKG_URLS["district_weather"].format(adm3=adm3)),
    )
    import re

    pattern = (
        r'href="/cuaca/prakiraan-cuaca/('
        + re.escape(adm3)
        + r'\.\d{4})"[^>]*>\s*<span>(.*?)</span>'
    )
    locations = []
    seen = set()
    for adm4, village in re.findall(pattern, html):
        if adm4 in seen:
            continue
        seen.add(adm4)
        village = unescape(re.sub(r"<.*?>", "", village)).strip()
        locations.append(
            {
                "key": adm4,
                "adm4": adm4,
                "adm3": adm3,
                "village": village,
                "district": region["name"],
                "group": region["group"],
                "label": f"{village}, {region['name']}",
            }
        )
    return locations


def location_by_key(key, adm3=None):
    if key and key.count(".") == 3:
        target_adm3 = ".".join(key.split(".")[:3])
        for location in fetch_region_locations(target_adm3):
            if location["adm4"] == key:
                return location

    locations = fetch_region_locations(adm3 or DEFAULT_ADM3)
    for location in locations:
        if location["key"] == key:
            return location

    for location in locations:
        if location["adm4"] == DEFAULT_ADM4:
            return location
    return locations[0]


def flatten_weather(weather_payload):
    forecasts = []
    for area in weather_payload.get("data", []):
        for day_group in area.get("cuaca", []):
            for entry in day_group:
                forecasts.append(entry)
    forecasts.sort(key=lambda item: item.get("local_datetime") or "")
    return forecasts


def parse_weather_alerts(xml_text):
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    items = []
    if channel is None:
        return items

    for item in channel.findall("item"):
        title = item.findtext("title", default="")
        description = item.findtext("description", default="")
        link = item.findtext("link", default="")
        pub_date = item.findtext("pubDate", default="")
        combined = f"{title} {description}".lower()
        is_sulteng = any(keyword in combined for keyword in SULTENG_KEYWORDS)
        items.append(
            {
                "title": title,
                "description": " ".join(description.split()),
                "link": link,
                "pubDate": pub_date,
                "is_sulteng": is_sulteng,
            }
        )
    return items


def relevant_quakes(quakes):
    results = []
    for quake in quakes:
        text = f"{quake.get('Wilayah', '')} {quake.get('Dirasakan', '')}".lower()
        if any(keyword in text for keyword in SULTENG_KEYWORDS):
            results.append(quake)
    return results


def build_local_info(location_key, adm3=None):
    location = location_by_key(location_key, adm3)
    adm4 = location["adm4"]

    latest_quake = cached_json(
        "latest_quake",
        lambda: fetch_json(BMKG_URLS["latest_quake"]),
    )
    felt_quakes = cached_json(
        "felt_quakes",
        lambda: fetch_json(BMKG_URLS["felt_quakes"]),
    )
    weather_payload = cached_json(
        f"weather:{adm4}",
        lambda: fetch_json(BMKG_URLS["weather"].format(adm4=adm4)),
    )
    alert_xml = cached_json(
        "weather_alerts",
        lambda: fetch_text(BMKG_URLS["weather_alerts"]),
    )

    latest = latest_quake.get("Infogempa", {}).get("gempa", {})
    felt = felt_quakes.get("Infogempa", {}).get("gempa", [])
    forecasts = flatten_weather(weather_payload)
    alerts = parse_weather_alerts(alert_xml)

    return {
        "source": "BMKG (Badan Meteorologi, Klimatologi, dan Geofisika)",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "location": {
            "key": location["key"],
            "label": location["label"],
            "adm4": adm4,
            "adm3": location["adm3"],
            "district": location["district"],
            "group": location["group"],
            "bmkg_location": weather_payload.get("lokasi", {}),
        },
        "latest_quake": latest,
        "sulteng_felt_quakes": relevant_quakes(felt),
        "weather_now": forecasts[0] if forecasts else None,
        "weather_next": forecasts[:8],
        "sulteng_weather_alerts": [item for item in alerts if item["is_sulteng"]],
        "all_weather_alert_count": len(alerts),
    }


def response_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def response_file(handler, path):
    body = path.read_bytes()
    content_type = "text/html; charset=utf-8" if path.suffix == ".html" else "text/plain"
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        try:
            if parsed.path in ("/", "/index.html"):
                return response_file(self, ROOT / "index.html")

            if parsed.path == "/api/health":
                return response_json(self, 200, {"status": "ok", "source": "local BMKG test"})

            if parsed.path == "/api/regions":
                return response_json(self, 200, REGIONS)

            if parsed.path == "/api/locations":
                adm3 = query.get("adm3", [DEFAULT_ADM3])[0]
                return response_json(self, 200, fetch_region_locations(adm3))

            if parsed.path == "/api/coverage":
                coverage = []
                total_locations = 0
                errors = []
                for region in REGIONS:
                    try:
                        locations = fetch_region_locations(region["adm3"])
                        total_locations += len(locations)
                        coverage.append({**region, "locations": len(locations), "sample": locations[0] if locations else None})
                    except Exception as exc:
                        errors.append({**region, "error": str(exc)})
                return response_json(
                    self,
                    200 if not errors else 207,
                    {
                        "regions": len(REGIONS),
                        "locations": total_locations,
                        "coverage": coverage,
                        "errors": errors,
                    },
                )

            if parsed.path == "/api/local-info":
                location = query.get("location", ["tondo"])[0]
                adm3 = query.get("adm3", [None])[0]
                return response_json(self, 200, build_local_info(location, adm3))

            return response_json(self, 404, {"error": "Not found"})
        except (HTTPError, URLError, TimeoutError) as exc:
            return response_json(self, 502, {"error": "BMKG request failed", "detail": str(exc)})
        except Exception as exc:
            return response_json(self, 500, {"error": "Local test failed", "detail": str(exc)})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"BMKG local test running at http://127.0.0.1:{PORT}")
    print("Open /api/local-info or the browser page to test.")
    server.serve_forever()
