"""
Relay Open-Meteo rainfall to the FloodSafe server.

Open-Meteo's free quota is per IP, and the server's Render IP is shared
with other customers who usually exhaust it. This runs on GitHub's
runners instead: it asks the server which readings it is missing
(/rainfall/relay-points), fetches exactly those points with exactly the
server's own query parameters, and posts the raw responses back to
/rainfall/relay, authenticated with RAINFALL_RELAY_SECRET. See RAINFALL
RELAY in data/flood/uttarakhand/server.py.

Standard library only, so the workflow needs no install step.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SITE = os.environ.get("FLOODSAFE_URL", "https://floodsafe-u207.onrender.com").rstrip("/")
SECRET = os.environ.get("RAINFALL_RELAY_SECRET", "").strip()


def request_json(url, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "FloodSafe-rainfall-relay", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def fetch_open_meteo(points, query):
    params = {
        "latitude": ",".join(str(p[0]) for p in points),
        "longitude": ",".join(str(p[1]) for p in points),
        **{key: str(value) for key, value in query.items()},
    }
    payload = request_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params))
    return payload if isinstance(payload, list) else [payload]


def main():
    if not SECRET:
        print("::warning::RAINFALL_RELAY_SECRET is not set in this repository's secrets; nothing relayed.")
        return 0

    plan = request_json(SITE + "/rainfall/relay-points")

    if not plan.get("configured"):
        print("::warning::The server has no RAINFALL_RELAY_SECRET set; nothing relayed.")
        return 0

    body, failures = {}, []

    for name in ("ffgs", "towns"):
        part = plan[name]
        if not part["needed"]:
            print(f"{name}: server reading is fresh, skipped")
            continue
        try:
            body[name] = fetch_open_meteo(part["points"], part["query"])
            print(f"{name}: fetched {len(body[name])} locations from Open-Meteo")
        except urllib.error.HTTPError as e:
            reason = e.read().decode("utf-8", "replace")[:200]
            failures.append(f"{name}: Open-Meteo HTTP {e.code} {reason}")

    if body:
        result = request_json(SITE + "/rainfall/relay", body=body,
                              headers={"Authorization": f"Bearer {SECRET}"})
        print("server accepted:", result.get("accepted"))

    for failure in failures:
        print(f"::error::{failure}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
