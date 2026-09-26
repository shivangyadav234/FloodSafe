"""
End-to-end check of the live FloodSafe site, run from GitHub Actions
(.github/workflows/live-check.yml).

Read-only: it calls every GET endpoint and the four routing POSTs, then
opens each page in Chromium and records script errors and failed
requests. It never files, confirms or resolves a hazard report and never
sends a push, so running it leaves the live site unchanged.

Exits 1 if any check fails; the log ends with a PASS/WARN/FAIL table.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

SITE = os.environ.get("FLOODSAFE_URL", "https://floodsafe-u207.onrender.com").rstrip("/")

DEHRADUN = (30.3165, 78.0322)
RISHIKESH = (30.0869, 78.2676)
MUSSOORIE = (30.4598, 78.0664)
DELHI = (28.6139, 77.2090)

results = []


def record(name, level, detail):
    results.append((name, level, detail))
    print(f"[{level}] {name}: {detail}", flush=True)


def call(path, body=None, timeout=120):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(SITE + path, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "FloodSafe-live-check"})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw, code, headers = response.read(), response.status, response.headers
    except urllib.error.HTTPError as e:
        raw, code, headers = e.read(), e.code, e.headers
    elapsed = time.time() - start
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    return code, payload, raw, headers, elapsed


def check(name, path, body=None, validate=None, slow=None):
    try:
        code, payload, raw, headers, elapsed = call(path, body)
    except Exception as e:
        record(name, "FAIL", f"no response ({type(e).__name__}: {e})")
        return None
    if code != 200:
        record(name, "FAIL", f"HTTP {code} in {elapsed:.1f}s: {raw[:200]!r}")
        return None
    problem = None
    if validate:
        try:
            problem = validate(payload if payload is not None else raw)
        except Exception as e:
            problem = f"unexpected response shape ({type(e).__name__}: {e})"
    if problem:
        record(name, "FAIL", f"{problem} ({elapsed:.1f}s)")
    elif slow and elapsed > slow:
        record(name, "WARN", f"OK but slow: {elapsed:.1f}s (expected under {slow}s)")
    else:
        record(name, "PASS", f"{elapsed:.1f}s, {len(raw)} bytes")
    return payload


# ------------------------------------------------------------
# Server and data
# ------------------------------------------------------------

def check_api():
    # Warm-up: a cold start loads the road graph; give it room.
    status = check("status", "/status", validate=lambda s: (
        None if s["status"] == "ok" and s["routing"] == "online"
        else f"status={s['status']} routing={s['routing']} error={s.get('routing_error')}"))
    if status:
        record("status: routing graph", "PASS" if status.get("node_count") else "FAIL",
               f"{status.get('node_count')} nodes")
        storage_ok = status.get("report_storage") == "postgres" and not status.get("report_storage_error")
        record("status: report storage", "PASS" if storage_ok else "FAIL",
               f"{status.get('report_storage')}, error={status.get('report_storage_error')}")
        record("status: client IP source", "PASS", str(status.get("client_ip_source")))

    for path in ("/", "/app", "/ffgs", "/reports-view"):
        check(f"page {path}", path, validate=lambda raw: (
            None if b"</html>" in raw.lower() else "response is not a complete HTML page"))

    check("manifest", "/manifest.webmanifest", validate=lambda m: (
        None if m.get("icons") and m.get("start_url") else "manifest missing icons/start_url"))
    for icon in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        check(f"icon {icon}", f"/static/icons/{icon}", validate=lambda raw: (
            None if raw[:8] == b"\x89PNG\r\n\x1a\n" else "not a PNG"))
    check("service worker", "/sw.js", validate=lambda raw: None if len(raw) > 100 else "empty")
    check("rainfall fallback script", "/rainfall-fallback.js",
          validate=lambda raw: None if len(raw) > 100 else "empty")

    zones = check("FFGS zones", "/ffgs/zones", validate=lambda z: (
        None if z["available"] and len(z["zones"]) >= 150
        else f"available={z['available']} zones={len(z['zones'])} error={z.get('error')}"))
    if zones:
        rain = zones["rainfall"]
        with_data, total = rain["zones_with_data"], len(zones["zones"])
        age = rain["age_seconds"]
        fresh = age is not None and age < 30 * 60
        level = "PASS" if with_data == total and fresh else ("WARN" if with_data else "FAIL")
        age_text = f"{age / 60:.0f} min old" if age is not None else "never fetched"
        record("FFGS rainfall on server", level,
               f"{with_data}/{total} zones have rainfall, {age_text}, source={rain.get('source')}, "
               f"last_error={rain.get('last_error')}")
        districts = {z.get("district") for z in zones["zones"] if z.get("district")}
        record("FFGS districts covered", "PASS" if len(districts) >= 13 else "WARN",
               f"{len(districts)} districts")

    town = check("town rainfall", "/town-rainfall", validate=lambda t: (
        None if t["towns"] else f"no town data, last_error={t.get('last_error')}"))
    if town:
        age = town["age_seconds"]
        record("town rainfall freshness", "PASS" if age is not None and age < 30 * 60 else "WARN",
               f"{'%.0f min old' % (age / 60) if age is not None else 'never fetched'}, "
               f"source={town.get('source')}")

    check("FFGS point (Dehradun)", f"/ffgs/point?lat={DEHRADUN[0]}&lon={DEHRADUN[1]}",
          validate=lambda p: None if p else "empty")
    check("FFGS point outside state (Delhi)", f"/ffgs/point?lat={DELHI[0]}&lon={DELHI[1]}",
          validate=lambda p: None if p is not None else "empty")
    check("flood guidance zones", "/flood-guidance-zones",
          validate=lambda g: None if g.get("zones") else "no zones")
    check("flood guidance (Mussoorie)", f"/flood-guidance?lat={MUSSOORIE[0]}&lon={MUSSOORIE[1]}",
          validate=lambda g: None if g else "empty")
    check("hazard atlas", "/ffgs/hazard-atlas.geojson",
          validate=lambda g: None if g.get("features") else "no features")
    check("watersheds", "/ffgs/watersheds.geojson",
          validate=lambda g: None if g.get("features") else "no features")

    warnings = check("official warnings (SACHET)", "/official-warnings",
                     validate=lambda w: None if "alerts" in w else "no alerts key")
    if warnings is not None:
        record("official warnings: active alerts", "PASS",
               f"{len(warnings['alerts'])} active, refreshing={warnings.get('refreshing')}, "
               f"checked_at={warnings.get('checked_at')}, error={warnings.get('last_error')}")

    check("shelters", "/shelters", validate=lambda s: None if len(s) > 0 else "no shelters")
    reports = check("hazard reports", "/reports", validate=lambda r: None if isinstance(r, list) else "not a list")
    if reports is not None:
        record("hazard reports: count", "PASS", f"{len(reports)} reports stored")

    push = check("push config", "/push/config", validate=lambda p: (
        None if p.get("available") else f"push unavailable: {p}"))

    plan = check("relay plan", "/rainfall/relay-points",
                 validate=lambda p: None if p["configured"] else "server has no RAINFALL_RELAY_SECRET")

    # Routing -- the expensive calls, one at a time.
    route_body = {"start_lat": DEHRADUN[0], "start_lon": DEHRADUN[1],
                  "end_lat": RISHIKESH[0], "end_lon": RISHIKESH[1]}
    check("route Dehradun -> Rishikesh", "/route", route_body, slow=10,
          validate=lambda r: None if r.get("status") == "ok" else f"status={r.get('status')} error={r.get('error')}")
    check("compare routes", "/compare", route_body, slow=15,
          validate=lambda r: None if r.get("status") == "ok" else f"status={r.get('status')} error={r.get('error')}")
    check("nearest hospital (Dehradun)", "/nearest-hospital", {"lat": DEHRADUN[0], "lon": DEHRADUN[1]}, slow=10,
          validate=lambda r: None if r.get("status") == "ok" else f"status={r.get('status')} error={r.get('error')}")
    check("evacuate (Dehradun)", "/evacuate", {"lat": DEHRADUN[0], "lon": DEHRADUN[1]}, slow=15,
          validate=lambda r: None if r.get("status") == "ok" else f"status={r.get('status')} error={r.get('error')}")

    # Security headers and cross-site write blocking.
    _, _, _, headers, _ = call("/")
    for header in ("X-Frame-Options", "X-Content-Type-Options", "Content-Security-Policy"):
        record(f"header {header}", "PASS" if headers.get(header) else "FAIL", str(headers.get(header)))
    req = urllib.request.Request(SITE + "/report/nonexistent/resolve", data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "Origin": "https://evil.example", "User-Agent": "FloodSafe-live-check"})
    try:
        urllib.request.urlopen(req, timeout=60)
        record("cross-site write blocked", "FAIL", "foreign-origin POST was accepted")
    except urllib.error.HTTPError as e:
        record("cross-site write blocked", "PASS" if e.code == 403 else "FAIL", f"HTTP {e.code}")


# ------------------------------------------------------------
# Pages in a real browser
# ------------------------------------------------------------

def check_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        record("browser checks", "FAIL", "playwright not installed")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for path, wait_s in (("/", 12), ("/app", 15), ("/ffgs", 15), ("/reports-view", 8)):
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            errors, failed = [], []
            page.on("pageerror", lambda e: errors.append(f"uncaught: {e}"))
            page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
            page.on("requestfailed", lambda r: failed.append(f"{r.url} ({r.failure})"))
            page.on("response", lambda r: failed.append(f"{r.url} HTTP {r.status}")
                    if r.status >= 400 and r.url.startswith(SITE) else None)
            start = time.time()
            try:
                page.goto(SITE + path, wait_until="load", timeout=90_000)
                load_s = time.time() - start
                page.wait_for_timeout(wait_s * 1000)
                text = page.inner_text("body")
                os.makedirs("live-check", exist_ok=True)
                page.screenshot(path=f"live-check/{path.strip('/') or 'home'}.png", full_page=False)
            except Exception as e:
                record(f"browser {path}", "FAIL", f"did not load ({type(e).__name__}: {e})")
                context.close()
                continue

            name = f"browser {path}"
            detail = f"loaded in {load_s:.1f}s, {len(text)} chars of text"
            if errors:
                record(name, "FAIL", detail + "; errors: " + " | ".join(errors[:5]))
            elif failed:
                record(name, "WARN", detail + "; failed requests: " + " | ".join(failed[:5]))
            else:
                record(name, "PASS", detail + ", no script errors")
            snippet = " ".join(text.split())[:400]
            print(f"    text: {snippet}", flush=True)
            if path == "/ffgs":
                panel = " ".join(page.inner_text("#officialWarnings").split())
                shown = page.locator("#officialWarnings .official-alert").count()
                record("browser /ffgs: official warnings panel", "PASS" if shown or "No current" in panel else "FAIL",
                       f"{shown} alert(s) shown: {panel[:300]}")
            context.close()
        browser.close()


def main():
    check_api()
    check_browser()

    print("\n" + "=" * 72)
    width = max(len(name) for name, _, _ in results)
    for name, level, detail in results:
        print(f"{level:4}  {name:<{width}}  {detail}")
    counts = {level: sum(1 for _, l, _ in results if l == level) for level in ("PASS", "WARN", "FAIL")}
    print(f"\n{counts['PASS']} passed, {counts['WARN']} warnings, {counts['FAIL']} failed")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
