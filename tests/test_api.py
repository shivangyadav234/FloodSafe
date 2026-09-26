"""
HTTP surface: input validation, abuse limits, durability and the
quota-shaped bugs that emptied the live rainfall column.
"""

import json
import os
import time

import pytest


class TestPublicEndpoints:

    @pytest.mark.parametrize("path", [
        "/", "/app", "/ffgs", "/status", "/shelters",
        "/ffgs/zones", "/town-rainfall", "/flood-guidance-zones",
    ])
    def test_returns_ok(self, client, path):
        assert client.get(path).status_code == 200

    def test_removed_weather_proxy_is_gone(self, client):
        """/weather was an unreferenced open proxy onto a shared quota.

        It accepted any coordinate on Earth, issued one upstream call
        per distinct ~1km cell and cached them unbounded. It must stay
        deleted, not quietly reappear.
        """
        assert client.get("/weather?lat=30.3&lon=78.0").status_code == 404


class TestFfgsZones:

    def test_every_zone_is_classified_and_attributed(self, client):
        payload = client.get("/ffgs/zones").get_json()
        zones = payload["zones"]

        assert len(zones) > 100
        for zone in zones:
            assert zone["effective_class"] in (
                "LOW", "MODERATE", "SIGNIFICANT", "EXTREME")
            assert zone["hazard_source"] in ("atlas", "ffpi")

    def test_reports_why_rainfall_is_missing(self, client):
        """An empty rainfall column must be distinguishable from dry weather.

        Without this block the two look identical from outside, which
        during a demo reads as "no rain anywhere" and in use would hide
        an outage on the one number the page exists to show.
        """
        payload = client.get("/ffgs/zones").get_json()
        assert "rainfall" in payload
        assert set(payload["rainfall"]) >= {
            "zones_with_data", "age_seconds", "last_error"}

    def test_picker_needs_kind_to_sort_towns_first(self, client):
        """`kind` is what lets the zone picker list towns above localities."""
        zones = client.get("/ffgs/zones").get_json()["zones"]
        assert {z["kind"] for z in zones} == {"town", "locality"}

    def test_full_zone_table_is_collapsed_behind_the_cards(self, client):
        """All 127 rows used to render open, making the page scroll forever.

        Five summary cards come first; the full table sits in a <details>
        that starts closed.
        """
        import re

        page = client.get("/ffgs").get_data(as_text=True)
        details = re.search(r'<details id="ffgsOtherZones"[^>]*>', page)

        assert 'id="zoneCards"' in page
        assert details and "open" not in details.group(0)
        assert page.index('id="zoneCards"') < details.start()
        assert page.index('id="ffgsTableBody"') > details.start()


class TestFfgsPoint:

    @pytest.mark.parametrize("query", [
        "lat=abc&lon=78", "lat=30", "lon=78", "",
        "lat=200&lon=78", "lat=30&lon=400",
    ])
    def test_rejects_bad_coordinates(self, client, query):
        assert client.get(f"/ffgs/point?{query}").status_code == 400

    def test_classifies_an_arbitrary_point(self, client):
        body = client.get("/ffgs/point?lat=30.5551&lon=79.5643").get_json()
        assert body["effective_class"] is not None
        assert body["ffpi"] is not None


class TestRainfallDeduplication:

    def test_fetches_one_point_per_weather_model_cell(self, server):
        """Zones are collapsed onto the weather model's ~11km grid.

        Open-Meteo bills each location in a multi-location request
        separately against a 10,000/day per-IP quota. Fetching all 127
        zones every 10 minutes cost 18,288 calls/day -- 83% over -- and
        emptied the live rainfall column. The 25 localities around
        Haldwani share one model cell and one reading.
        """
        cells = {
            (round(z["lat"] / server.FFGS_RAINFALL_GRID_DEG),
             round(z["lon"] / server.FFGS_RAINFALL_GRID_DEG))
            for z in server.FFGS_ZONES if z.get("effective_class")
        }
        zones = [z for z in server.FFGS_ZONES if z.get("effective_class")]

        assert len(cells) < len(zones) / 2, "dedup should cut fetches at least in half"

        refreshes_per_day = 86400 / server.FFGS_RAINFALL_CACHE_TTL_SECONDS
        # Budget for the town-rainfall refresh alongside it.
        daily = len(cells) * refreshes_per_day + 9 * refreshes_per_day
        assert daily < 10000, f"{daily:.0f} calls/day exceeds the free-tier quota"

    def test_landing_page_station_list_matches_the_server_towns(self, client, server):
        """The two town lists are duplicated and synced by hand.

        RAINFALL_STATIONS lives in the landing page's JavaScript and
        GUIDANCE_TOWNS in Python, and a comment in server.py notes they
        are kept in step manually. They already drifted into fetching
        the same nine places twice per page load. If someone edits one
        list, this fails rather than the duplication going unnoticed.
        """
        import re

        page = client.get("/").get_data(as_text=True)
        block = re.search(r"const RAINFALL_STATIONS = \[(.*?)\n\s*\];", page, re.S)
        assert block, "RAINFALL_STATIONS not found in the landing page"

        station_names = set(re.findall(r"name:\s*'([^']+)'", block.group(1)))
        town_names = {t["name"] for t in server.GUIDANCE_TOWNS}

        assert station_names == town_names, (
            "landing-page stations and GUIDANCE_TOWNS have drifted apart"
        )

    def test_landing_page_makes_no_direct_open_meteo_calls(self, client):
        """Shared readings are fetched server-side, once, not per browser.

        Eighteen calls per page load for nine distinct points spends
        the visitor's own per-IP quota, so a roomful of people behind
        one venue NAT exhausts it for all of them.
        """
        page = client.get("/").get_data(as_text=True)

        # "check my location" is a genuinely per-visitor coordinate and
        # is allowed to call out directly; the shared panels are not.
        shared_panel_calls = page.count("api.open-meteo.com")
        assert shared_panel_calls <= 1, (
            f"landing page makes {shared_panel_calls} direct Open-Meteo "
            "calls; shared data belongs behind /town-rainfall"
        )


class TestRainfallFailureBackoff:
    """A failing rainfall cache must not turn every poll into an upstream call.

    Before the backoff, a cache with no successful reading -- every cache
    right after a restart -- retried Open-Meteo on every request. With the
    pages polling every 60s, a quota block sustained itself: each open tab
    spent quota that the block was waiting to recover.
    """

    class _Quota429:
        status_code = 429

        def json(self):
            return {"error": True, "reason": "Hourly API request limit exceeded."}

        def raise_for_status(self):
            import requests
            raise requests.HTTPError("429 Client Error: Too Many Requests")

    @pytest.fixture()
    def upstream(self, server, monkeypatch):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return self._Quota429()

        monkeypatch.setattr(server.requests, "get", fake_get)
        return calls

    @pytest.fixture(autouse=True)
    def empty_caches(self, server, monkeypatch):
        for name in ("_ffgs_rainfall_cache", "_town_rainfall_cache"):
            monkeypatch.setattr(server, name, {
                "timestamp": 0.0, "data": {}, "last_error": None, "last_attempt": 0.0,
            })

    @pytest.mark.parametrize("path", ["/ffgs/zones", "/town-rainfall"])
    def test_failure_is_fetched_once_per_backoff_window(self, client, upstream, path):
        for _ in range(5):
            assert client.get(path).status_code == 200
        assert len(upstream) == 1

    @pytest.mark.parametrize("path, cache", [
        ("/ffgs/zones", "_ffgs_rainfall_cache"),
        ("/town-rainfall", "_town_rainfall_cache"),
    ])
    def test_retries_once_the_window_has_passed(self, client, server, upstream, path, cache):
        client.get(path)
        getattr(server, cache)["last_attempt"] -= server.RAINFALL_FAILURE_BACKOFF_SECONDS + 1
        client.get(path)
        assert len(upstream) == 2

    def test_reports_which_limit_was_hit(self, client, upstream):
        """"Daily" was hard-coded for every 429; the minutely and hourly
        limits have different causes and different recovery times."""
        payload = client.get("/ffgs/zones").get_json()
        assert "Hourly API request limit exceeded" in payload["rainfall"]["last_error"]


class TestBrowserRainfallFallback:
    """When Open-Meteo refuses the server, browsers fetch the same points.

    Render's outbound IP is shared with other customers, whose calls can
    exhaust Open-Meteo's per-IP daily quota before this server makes
    one. The pages then fetch under the visitor's own IP, using the grid
    cells /ffgs/zones publishes. If those drift from what the server
    fetches, the fallback reads rainfall for the wrong places.
    """

    def test_published_cells_are_what_the_server_fetches(self, client, server, monkeypatch):
        sent = {}

        def fake_get(url, params=None, **kwargs):
            sent.update(params or {})
            raise server.requests.ConnectionError("offline")

        monkeypatch.setattr(server.requests, "get", fake_get)
        monkeypatch.setattr(server, "_ffgs_rainfall_cache", {
            "timestamp": 0.0, "data": {}, "last_error": None, "last_attempt": 0.0,
        })

        cells = client.get("/ffgs/zones").get_json()["rainfall"]["cells"]

        assert sent["latitude"] == ",".join(str(c[0]) for c in cells)
        assert sent["longitude"] == ",".join(str(c[1]) for c in cells)

    def test_every_classified_zone_points_at_its_own_cell(self, client, server):
        payload = client.get("/ffgs/zones").get_json()
        cells = payload["rainfall"]["cells"]
        grid = server.FFGS_RAINFALL_GRID_DEG

        def key(lat, lon):
            return (round(lat / grid), round(lon / grid))

        for zone in payload["zones"]:
            if not zone["effective_class"]:
                assert zone["rain_cell"] is None
                continue
            assert 0 <= zone["rain_cell"] < len(cells)
            cell = cells[zone["rain_cell"]]
            assert key(zone["lat"], zone["lon"]) == key(cell[0], cell[1])

    def test_script_is_served(self, client):
        response = client.get("/rainfall-fallback.js")
        assert response.status_code == 200
        assert response.mimetype == "application/javascript"
        assert "window.FloodSafeRainfall" in response.get_data(as_text=True)

    @pytest.mark.parametrize("path", ["/", "/ffgs", "/app"])
    def test_every_rainfall_page_loads_it(self, client, path):
        page = client.get(path).get_data(as_text=True)
        assert '<script src="/rainfall-fallback.js"></script>' in page


class TestReportValidation:

    def test_rejects_non_json(self, client):
        assert client.post("/report", data="nope").status_code == 400

    @pytest.mark.parametrize("body", [
        {"lat": "x", "lon": 78.0},
        {"lat": 91.0, "lon": 78.0},
        {"lat": 30.0, "lon": 181.0},
        {},
    ])
    def test_rejects_bad_coordinates(self, client, body):
        assert client.post("/report", json=body).status_code == 400

    def test_truncates_overlong_text(self, client, server):
        body = {
            "lat": 30.3165, "lon": 78.0322,
            "description": "x" * 5000,
            "reporter_name": "y" * 500,
        }
        assert client.post("/report", json=body).status_code in (200, 201)

        stored = server._reports[-1]
        assert len(stored["description"]) <= server.REPORT_DESCRIPTION_MAX_LENGTH
        assert len(stored["reporter_name"]) <= server.REPORT_NAME_MAX_LENGTH

    def test_dangerous_text_is_stored_verbatim_for_escaping_at_render(self, client, server):
        """Storage keeps the raw text; the client escapes on render.

        Asserting it round-trips unmodified guards against someone
        "sanitising" here and assuming that makes rendering safe.
        """
        payload = "<script>alert(1)</script>"
        client.post("/report", json={
            "lat": 30.3165, "lon": 78.0322, "description": payload})
        assert server._reports[-1]["description"] == payload


class TestRateLimiting:

    def test_report_flood_is_blocked(self, client, server):
        limit, _ = server.RATE_LIMITS["report"]
        body = {"lat": 30.3165, "lon": 78.0322, "description": "test"}

        for _ in range(limit):
            assert client.post("/report", json=body).status_code in (200, 201)

        blocked = client.post("/report", json=body)
        assert blocked.status_code == 429
        assert "Too many requests" in blocked.get_json()["error"]

    def test_limit_is_per_client_ip(self, client, server):
        limit, _ = server.RATE_LIMITS["report"]
        body = {"lat": 30.3165, "lon": 78.0322, "description": "test"}

        for _ in range(limit):
            client.post("/report", json=body)
        assert client.post("/report", json=body).status_code == 429

        # A different forwarded address gets its own allowance.
        fresh = client.post("/report", json=body,
                            headers={"X-Forwarded-For": "203.0.113.9"})
        assert fresh.status_code in (200, 201)

    def test_spoofed_forwarded_for_does_not_reset_the_limit(self, client, server):
        """The client writes the *first* X-Forwarded-For entry.

        Keying on it let a fresh fake address through on every request --
        confirmed against the live site, 22 of 22 resolves went through
        while a fixed header was blocked after 20. Proxies append, so
        only the last entry identifies the caller.
        """
        limit, _ = server.RATE_LIMITS["report_action"]

        statuses = [
            client.post("/report/nonexistent/resolve",
                        headers={"X-Forwarded-For": f"203.0.113.{i}, 10.0.0.1"}).status_code
            for i in range(limit + 2)
        ]

        assert statuses[:limit] == [404] * limit
        assert statuses[limit:] == [429, 429]

    def test_cloudflare_address_is_trusted_over_forwarded_for(self, client, server):
        """Cloudflare overwrites CF-Connecting-IP, so a client cannot forge it."""
        limit, _ = server.RATE_LIMITS["report_action"]

        for i in range(limit):
            client.post("/report/nonexistent/resolve", headers={
                "CF-Connecting-IP": "198.51.100.1",
                "X-Forwarded-For": f"203.0.113.{i}",
            })

        blocked = client.post("/report/nonexistent/resolve", headers={
            "CF-Connecting-IP": "198.51.100.1",
            "X-Forwarded-For": "203.0.113.250",
        })
        assert blocked.status_code == 429

    def test_routing_is_limited(self, client, server):
        """Each route takes seconds on the single worker; a loop could stall it.

        Uses an off-network start, which is refused before any routing
        work, so this runs fast while still spending the allowance.
        """
        limit, _ = server.RATE_LIMITS["routing"]
        body = {"lat": 28.6139, "lon": 77.2090}

        for _ in range(limit):
            assert client.post("/evacuate", json=body).status_code == 422
        assert client.post("/evacuate", json=body).status_code == 429

    def test_reads_are_never_limited(self, client, server):
        """Rate limiting must not touch the pages people need in an emergency."""
        for _ in range(server.RATE_LIMITS["report"][0] + 5):
            assert client.get("/ffgs/zones").status_code == 200


class TestOffNetworkRouting:
    """Points far from the road graph are refused, not silently moved.

    A route starting in Delhi came back "ok" as a 97 km route from the
    nearest Uttarakhand road, with nothing telling the user their start
    point had been moved about 200 km.
    """

    DELHI = (28.6139, 77.2090)
    DEHRADUN = (30.3165, 78.0322)

    def test_road_snap_distance_is_small_inside_the_state(self, server):
        assert server.road_snap_km(*self.DEHRADUN) < 1.0
        assert server.road_snap_km(*self.DELHI) > server.MAX_SNAP_KM

    @pytest.mark.parametrize("path, body, label", [
        ("/route", {"start_lat": 28.6139, "start_lon": 77.2090,
                    "end_lat": 30.3165, "end_lon": 78.0322}, "starting point"),
        ("/route", {"start_lat": 30.3165, "start_lon": 78.0322,
                    "end_lat": 28.6139, "end_lon": 77.2090}, "destination"),
        ("/compare", {"start_lat": 28.6139, "start_lon": 77.2090,
                      "end_lat": 30.3165, "end_lon": 78.0322}, "starting point"),
        ("/evacuate", {"lat": 28.6139, "lon": 77.2090}, "starting point"),
        ("/nearest-hospital", {"lat": 28.6139, "lon": 77.2090}, "starting point"),
    ])
    def test_refused_with_a_reason(self, client, path, body, label):
        response = client.post(path, json=body)
        assert response.status_code == 422
        assert label in response.get_json()["error"]


class TestNearestTargetSearchReuse:
    """Evacuation reuses one Dijkstra per weighting across its candidates.

    It used to rerun a full 2M-node search for each of five candidates
    (up to three per candidate in SAFEST), taking 20+ seconds on Render
    against the map's 25-second timeout. Reuse must not change the answer.
    """

    def test_reuse_picks_the_same_shelter_and_path(self, monkeypatch):
        import routing_engine

        start = dict(start_lon=78.2676, start_lat=30.0869, mode="SAFEST",
                     report_points=[[78.27, 30.09]], live_rain_mm=None)

        reused = routing_engine.find_nearest_shelter(**start)

        original = routing_engine.calculate_route

        def without_reuse(**kwargs):
            kwargs.pop("dijkstra_cache", None)
            return original(**kwargs)

        monkeypatch.setattr(routing_engine, "calculate_route", without_reuse)
        separate = routing_engine.find_nearest_shelter(**start)

        assert reused["route"]["path"] == separate["route"]["path"]
        assert reused["route"]["distance_m"] == separate["route"]["distance_m"]
        assert reused["route"]["risk_counts"] == separate["route"]["risk_counts"]


TEST_DB_ADMIN_URL = os.environ.get(
    "FLOODSAFE_TEST_DB_ADMIN_URL", "postgresql://postgres@127.0.0.1:5433/postgres")


@pytest.fixture()
def report_db(server, monkeypatch):
    """A real Postgres (the local cluster from floodsafe/db/setup_local_db.py)
    in its own floodsafe_test database, with an empty reports table.
    Skipped when no server is reachable."""
    psycopg = pytest.importorskip("psycopg")

    try:
        with psycopg.connect(TEST_DB_ADMIN_URL, connect_timeout=3, autocommit=True) as admin:
            exists = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = 'floodsafe_test'").fetchone()
            if not exists:
                admin.execute("CREATE DATABASE floodsafe_test")
    except psycopg.OperationalError:
        pytest.skip("no local Postgres for report storage tests")

    url = TEST_DB_ADMIN_URL.rsplit("/", 1)[0] + "/floodsafe_test"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS flask_hazard_reports")

    monkeypatch.setattr(server, "DATABASE_URL", url)
    monkeypatch.setattr(server, "_reports", [])
    monkeypatch.setattr(server, "_report_storage_state",
                        {"last_error": None, "loaded": False, "last_load_attempt": 0.0})
    server._reports.extend(server._load_reports())
    return url


def _restart(server):
    """What a redeploy does to the in-memory state: start again from storage."""
    server._reports[:] = server._load_reports()
    return {r["id"]: r for r in server._reports}


class TestReportDatabase:
    """Reports survive restarts when DATABASE_URL is set.

    Render's free disk is wiped on every deploy and wake-up, so the JSON
    file alone lost every live hazard report each time.
    """

    BODY = {"lat": 30.3165, "lon": 78.0322, "description": "Road washed out"}

    def test_report_survives_a_restart(self, client, server, report_db):
        report = client.post("/report", json=self.BODY).get_json()
        stored = _restart(server)
        assert stored[report["id"]]["description"] == "Road washed out"

    def test_confirmation_is_stored(self, client, server, report_db):
        report = client.post("/report", json=self.BODY).get_json()
        client.post(f"/report/{report['id']}/confirm")
        assert _restart(server)[report["id"]]["confirmations"] == 1

    def test_resolved_report_stays_gone(self, client, server, report_db):
        report = client.post("/report", json=self.BODY).get_json()
        client.post(f"/report/{report['id']}/resolve")
        assert report["id"] not in _restart(server)

    def test_expired_report_is_deleted(self, client, server, report_db):
        report = client.post("/report", json=self.BODY).get_json()
        server._reports[0]["timestamp"] -= server.REPORT_EXPIRY_SECONDS + 1
        client.get("/reports")
        assert report["id"] not in _restart(server)

    def test_outage_at_startup_never_erases_stored_reports(self, client, server, report_db, monkeypatch):
        """The trap in mirroring memory to the database wholesale.

        If the database is down at startup the app begins with nothing
        in memory; a later "save everything" would then delete every
        stored report. Writes are per report, and the load is retried.
        """
        kept = client.post("/report", json=self.BODY).get_json()

        # Restart while the database is unreachable.
        monkeypatch.setattr(server, "DATABASE_URL", "postgresql://postgres@127.0.0.1:1/none")
        monkeypatch.setattr(server, "_report_storage_state",
                            {"last_error": None, "loaded": False, "last_load_attempt": 0.0})
        server._reports[:] = server._load_reports()
        assert server._reports == []
        assert client.get("/status").get_json()["report_storage_error"]

        during_outage = client.post("/report", json=self.BODY).get_json()

        # Database back; the next read retries the load and merges.
        monkeypatch.setattr(server, "DATABASE_URL", report_db)
        server._report_storage_state["last_load_attempt"] = 0.0
        ids = {r["id"] for r in client.get("/reports").get_json()}
        assert {kept["id"], during_outage["id"]} <= ids

        stored = _restart(server)
        assert kept["id"] in stored, "a report stored before the outage was erased"
        assert during_outage["id"] in stored, "the report made during the outage was never saved"


class TestReportPersistence:

    def test_save_is_atomic(self, server, tmp_path, monkeypatch):
        """A crash mid-write must not truncate the live report file.

        Writing straight into reports.json left a half-written file
        that failed to parse on next boot, silently resetting every
        active hazard report to an empty list.
        """
        target = tmp_path / "reports.json"
        monkeypatch.setattr(server, "REPORTS_FILE", str(target))

        server._save_reports([{"id": "a", "description": "first"}])
        assert json.loads(target.read_text())[0]["id"] == "a"

        # A failure during the write must leave the previous file intact.
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated crash during rename")

        monkeypatch.setattr(os, "replace", boom)
        server._save_reports([{"id": "b", "description": "second"}])
        monkeypatch.setattr(os, "replace", real_replace)

        assert json.loads(target.read_text())[0]["id"] == "a", (
            "the previous file must survive a failed write"
        )
        assert not (tmp_path / "reports.json.tmp").exists(), (
            "the temporary file must be cleaned up"
        )


# ============================================================
# Rainfall relay and push alerts
# ============================================================

def _open_meteo_location(lat, lon, hourly_mm=None, current_mm=0.0):
    """One location in Open-Meteo's response shape. Test fixture only."""
    loc = {"latitude": lat, "longitude": lon,
           "current": {"time": "2026-09-23T12:00", "precipitation": current_mm}}
    if hourly_mm is not None:
        times = [f"2026-09-{21 + (h // 24):02d}T{h % 24:02d}:00" for h in range(len(hourly_mm))]
        loc["hourly"] = {"time": times, "precipitation": hourly_mm}
        loc["current"]["time"] = times[-1]
    return loc


def _relay_body(server, hourly_mm=0.0):
    return {
        "ffgs": [_open_meteo_location(z[0]["lat"], z[0]["lon"], [hourly_mm] * 49)
                 for z in server.FFGS_RAINFALL_CELLS],
        "towns": [_open_meteo_location(t["lat"], t["lon"]) for t in server.GUIDANCE_TOWNS],
    }


@pytest.fixture()
def relay(server, monkeypatch):
    monkeypatch.setattr(server, "RAINFALL_RELAY_SECRET", "test-secret")
    for name in ("_ffgs_rainfall_cache", "_town_rainfall_cache"):
        monkeypatch.setattr(server, name, {
            "timestamp": 0.0, "data": {}, "last_error": None, "last_attempt": 0.0, "source": None})
    return {"Authorization": "Bearer test-secret"}


class TestRainfallRelay:
    """GitHub Actions relays Open-Meteo readings the server can't fetch itself."""

    def test_points_are_the_servers_own(self, client, server):
        plan = client.get("/rainfall/relay-points").get_json()
        cells = client.get("/ffgs/zones").get_json()["rainfall"]["cells"]
        assert plan["ffgs"]["points"] == cells
        assert plan["towns"]["points"] == [[t["lat"], t["lon"]] for t in server.GUIDANCE_TOWNS]
        assert plan["ffgs"]["query"] == server.FFGS_RAINFALL_QUERY

    def test_disabled_without_a_secret(self, client, server, monkeypatch):
        monkeypatch.setattr(server, "RAINFALL_RELAY_SECRET", "")
        assert client.post("/rainfall/relay", json={}).status_code == 503

    @pytest.mark.parametrize("auth", [None, "Bearer wrong", "test-secret"])
    def test_rejects_a_bad_secret(self, client, server, relay, auth):
        headers = {"Authorization": auth} if auth else {}
        response = client.post("/rainfall/relay", json=_relay_body(server), headers=headers)
        assert response.status_code == 401
        assert not server._ffgs_rainfall_cache["data"]

    def test_accepted_readings_fill_every_zone(self, client, server, relay):
        response = client.post("/rainfall/relay", json=_relay_body(server, 0.5), headers=relay)
        assert response.get_json()["accepted"] == ["ffgs", "towns"]

        status = client.get("/ffgs/zones").get_json()["rainfall"]
        assert status["zones_with_data"] == len(server.FFGS_RAIN_CELL_BY_POINT)
        assert status["source"] == "relay"
        assert client.get("/town-rainfall").get_json()["source"] == "relay"

    @pytest.mark.parametrize("corrupt", ["reversed", "short", "negative", "absurd"])
    def test_rejects_malformed_data_without_storing_any(self, client, server, relay, corrupt):
        body = _relay_body(server, 0.5)
        if corrupt == "reversed":
            body["ffgs"].reverse()
        elif corrupt == "short":
            body["ffgs"].pop()
        elif corrupt == "negative":
            body["ffgs"][0]["hourly"]["precipitation"][-1] = -1.0
        elif corrupt == "absurd":
            body["towns"][0]["current"]["precipitation"] = 1e9

        assert client.post("/rainfall/relay", json=body, headers=relay).status_code == 400
        assert not server._ffgs_rainfall_cache["data"]
        assert not server._town_rainfall_cache["data"]


def _browser_subscription(endpoint="https://fcm.googleapis.com/fcm/send/test-endpoint"):
    """A subscription with a real P-256 key pair, like a browser creates,
    so the server's encryption can be checked by decrypting it."""
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def b64(raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return key, auth, {"endpoint": endpoint, "keys": {"p256dh": b64(public), "auth": b64(auth)}}


@pytest.fixture()
def push(server, report_db, monkeypatch):
    """Push against the real test database, with delivery captured instead
    of sent and alert threads run inline."""
    monkeypatch.setattr(server, "_push_state", {
        "vapid": None, "public_key": None, "last_error": None, "last_check": None, "alerts_sent": 0})

    sent = []
    reply = {"status": 201}

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status

    def fake_post(url, data=None, headers=None, timeout=None):
        sent.append({"url": url, "body": data, "headers": headers})
        return FakeResponse(reply["status"])

    class InlineThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(server.requests, "post", fake_post)
    monkeypatch.setattr(server.threading, "Thread", InlineThread)

    import psycopg
    with psycopg.connect(report_db, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS push_subscriptions")

    return {"sent": sent, "reply": reply}


def _critical_readings(server, zone):
    """Readings with this zone's cell at 1.5x its 1h critical level."""
    critical = zone["thresholds_mm"]["1h"]["critical"] * 1.5
    return {(z["lat"], z["lon"]): {"1h": critical if z is zone else 0.0, "3h": 0.0, "24h": 0.0,
                                   "antecedent_48h": 0.0}
            for z in server.FFGS_ZONE_BY_KEY.values()}


def _forecast_readings(server, zone, critical_in):
    """Dry now everywhere; this zone's 1h window reaches 1.5x critical
    `critical_in` hours ahead in the forecast."""
    critical = zone["thresholds_mm"]["1h"]["critical"] * 1.5
    dry = {"1h": 0.0, "3h": 0.0, "24h": 0.0}
    readings = {}
    for z in server.FFGS_ZONE_BY_KEY.values():
        forecast = [dict(dry) for _ in range(server.FFGS_FORECAST_HOURS)]
        if z is zone:
            forecast[critical_in - 1] = {"1h": critical, "3h": critical, "24h": critical}
        readings[(z["lat"], z["lon"])] = dict(dry, antecedent_48h=0.0, forecast=forecast)
    return readings


class TestPushAlerts:

    ZONE_NAME = "Dehradun"

    def zone_key(self, server):
        zone = next(z for z in server.FFGS_ZONE_BY_KEY.values() if z["name"] == self.ZONE_NAME)
        return server._zone_key(zone), zone

    def test_unavailable_without_the_database(self, client, server, monkeypatch):
        monkeypatch.setattr(server, "DATABASE_URL", "")
        assert client.get("/push/config").get_json()["available"] is False

    @pytest.mark.parametrize("endpoint", [
        "https://evil.example.com/steal",
        "http://fcm.googleapis.com/fcm/send/x",
        "https://fcm.googleapis.com.evil.example/x",
        "https://169.254.169.254/latest/meta-data",
    ])
    def test_only_browser_push_services_are_accepted(self, client, server, push, endpoint):
        """The server posts to the endpoint it is given, so anything else
        would let a caller aim this server's requests anywhere."""
        key, zone = self.zone_key(server)
        _, _, sub = _browser_subscription(endpoint)
        response = client.post("/push/subscribe", json={"zone": key, "subscription": sub})
        assert response.status_code == 400

    def test_unknown_zone_is_rejected(self, client, server, push):
        _, _, sub = _browser_subscription()
        response = client.post("/push/subscribe", json={"zone": "0.00000,0.00000", "subscription": sub})
        assert response.status_code == 400

    def test_subscribe_list_unsubscribe(self, client, server, push):
        key, zone = self.zone_key(server)
        _, _, sub = _browser_subscription()

        assert client.post("/push/subscribe", json={"zone": key, "subscription": sub}).status_code == 201
        listed = client.post("/push/subscriptions", json={"endpoint": sub["endpoint"]}).get_json()
        assert listed["zones"] == [key]

        client.post("/push/unsubscribe", json={"zone": key, "endpoint": sub["endpoint"]})
        listed = client.post("/push/subscriptions", json={"endpoint": sub["endpoint"]}).get_json()
        assert listed["zones"] == []

    def test_critical_zone_alert_is_delivered_encrypted_and_signed(self, client, server, push):
        import http_ece

        key, zone = self.zone_key(server)
        browser_key, auth, sub = _browser_subscription()
        client.post("/push/subscribe", json={"zone": key, "subscription": sub})
        public_key = client.get("/push/config").get_json()["public_key"]

        server._store_ffgs_readings(_critical_readings(server, zone), time.time(), "relay")

        assert len(push["sent"]) == 1
        delivery = push["sent"][0]
        assert delivery["url"] == sub["endpoint"]
        assert delivery["headers"]["Content-Encoding"] == "aes128gcm"
        assert delivery["headers"]["Authorization"].startswith("vapid t=")
        assert f"k={public_key}" in delivery["headers"]["Authorization"]

        # Only the subscriber's own private key can read it.
        message = json.loads(http_ece.decrypt(
            delivery["body"], private_key=browser_key, auth_secret=auth, version="aes128gcm"))
        assert message["title"] == f"Flash-flood CRITICAL: {zone['name']}"
        assert "not an official IMD/CWC warning" in message["body"]
        assert message["url"] == f"/ffgs?zone={key}"

    def test_one_alert_per_cooldown_while_it_stays_critical(self, client, server, push):
        key, zone = self.zone_key(server)
        _, _, sub = _browser_subscription()
        client.post("/push/subscribe", json={"zone": key, "subscription": sub})

        readings = _critical_readings(server, zone)
        server._store_ffgs_readings(readings, time.time(), "relay")
        server._store_ffgs_readings(readings, time.time(), "relay")
        assert len(push["sent"]) == 1

    def test_no_alert_when_nothing_is_critical(self, client, server, push):
        key, zone = self.zone_key(server)
        _, _, sub = _browser_subscription()
        client.post("/push/subscribe", json={"zone": key, "subscription": sub})

        dry = {k: {"1h": 0.0, "3h": 0.0, "24h": 0.0, "antecedent_48h": 0.0}
               for k in _critical_readings(server, zone)}
        server._store_ffgs_readings(dry, time.time(), "relay")
        assert push["sent"] == []

    def test_zone_forecast_to_turn_critical_is_warned_ahead(self, client, server, push):
        key, zone = self.zone_key(server)
        _, _, sub = _browser_subscription()
        client.post("/push/subscribe", json={"zone": key, "subscription": sub})

        server._store_ffgs_readings(_forecast_readings(server, zone, critical_in=2), time.time(), "relay")

        assert len(push["sent"]) == 1
        assert push["sent"][0]["url"] == sub["endpoint"]

    def test_forecast_beyond_the_push_horizon_is_not_pushed(self, client, server, push):
        key, zone = self.zone_key(server)
        _, _, sub = _browser_subscription()
        client.post("/push/subscribe", json={"zone": key, "subscription": sub})

        later = server.PUSH_FORECAST_HOURS + 1
        server._store_ffgs_readings(_forecast_readings(server, zone, critical_in=later), time.time(), "relay")
        assert push["sent"] == []

    def test_expired_subscription_is_removed(self, client, server, push):
        key, zone = self.zone_key(server)
        _, _, sub = _browser_subscription()
        client.post("/push/subscribe", json={"zone": key, "subscription": sub})

        push["reply"]["status"] = 410
        server._store_ffgs_readings(_critical_readings(server, zone), time.time(), "relay")

        listed = client.post("/push/subscriptions", json={"endpoint": sub["endpoint"]}).get_json()
        assert listed["zones"] == []

    def test_test_notification_needs_a_subscription(self, client, server, push):
        key, zone = self.zone_key(server)
        _, _, sub = _browser_subscription()
        assert client.post("/push/test", json={"zone": key, "endpoint": sub["endpoint"]}).status_code == 404

        client.post("/push/subscribe", json={"zone": key, "subscription": sub})
        assert client.post("/push/test", json={"zone": key, "endpoint": sub["endpoint"]}).status_code == 200
        assert len(push["sent"]) == 1

    def test_service_worker_is_served_from_the_root(self, client):
        response = client.get("/sw.js")
        assert response.mimetype == "application/javascript"
        assert "showNotification" in response.get_data(as_text=True)


class TestPageScriptsParse:
    """Every page's JavaScript must at least parse.

    The page HTML lives in ordinary Python strings, so an escape like \\"
    in the source reaches the browser as a bare quote. That once ended a
    translation string early and threw a SyntaxError that stopped the
    whole /ffgs script -- no zones, no picker -- while every Python test
    still passed.
    """

    @pytest.mark.parametrize("path", ["/", "/ffgs", "/app", "/reports-view",
                                      "/rainfall-fallback.js", "/sw.js"])
    def test_scripts_parse(self, client, path, tmp_path):
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")

        body = client.get(path).get_data(as_text=True)

        if path.endswith(".js"):
            scripts = [body]
        else:
            scripts = [m.group(1) for m in re.finditer(
                r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", body, re.S)]
            assert scripts, f"no inline scripts found on {path}"

        for i, script in enumerate(scripts):
            target = tmp_path / f"script_{i}.js"
            target.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(target)],
                                    capture_output=True, text=True)
            assert result.returncode == 0, f"{path} script {i}: {result.stderr[:500]}"


class TestOfflineCache:
    """The service worker keeps pages and shelters for offline use -- and
    nothing that changes minute to minute, which offline would read as
    current."""

    def test_only_stable_paths_are_kept_offline(self, client):
        import re

        worker = client.get("/sw.js").get_data(as_text=True)
        listed = re.search(r"var OFFLINE_PATHS = \[(.*?)\];", worker).group(1)
        paths = set(re.findall(r'"([^"]+)"', listed))

        assert "/shelters" in paths and "/app" in paths
        for live in ("/ffgs/zones", "/town-rainfall", "/reports", "/status", "/push/config"):
            assert live not in paths, f"{live} is live data and must not be served stale offline"

    def test_map_page_registers_the_worker_and_saves_routes(self, client):
        page = client.get("/app").get_data(as_text=True)
        assert 'navigator.serviceWorker.register("/sw.js")' in page
        for kind in ("route", "evacuate", "hospital"):
            assert f'saveRouteForOffline("{kind}"' in page


class TestWebAppManifest:
    """Installable to the home screen -- which on an iPhone is the only
    way to get push alerts at all."""

    def test_manifest_is_installable(self, client):
        response = client.get("/manifest.webmanifest")
        assert response.mimetype == "application/manifest+json"
        manifest = json.loads(response.get_data(as_text=True))

        assert manifest["display"] == "standalone"
        assert manifest["start_url"] == "/" and manifest["short_name"]
        # Chrome's install prompt wants a 192 and a 512 icon.
        sizes = {icon["sizes"] for icon in manifest["icons"] if icon["purpose"] == "any"}
        assert {"192x192", "512x512"} <= sizes

    def test_every_icon_is_served_at_its_size(self, client, server):
        import struct

        paths = [(icon["src"], icon["sizes"]) for icon in server.WEB_APP_MANIFEST["icons"]]
        paths.append(("/static/icons/apple-touch-icon.png", "180x180"))
        for src, sizes in paths:
            response = client.get(src)
            assert response.status_code == 200, src
            body = response.get_data()
            assert body[:8] == b"\x89PNG\r\n\x1a\n", src
            width, height = struct.unpack(">II", body[16:24])
            assert f"{width}x{height}" == sizes, src

    @pytest.mark.parametrize("path", ["/", "/ffgs", "/app", "/reports-view"])
    def test_every_page_links_the_manifest(self, client, server, path):
        head = client.get(path).get_data(as_text=True).split("</head>")[0]
        # The map page is built by map_app.py, which repeats these tags.
        for tag in server.APP_HEAD_TAGS.strip().splitlines():
            assert tag in head, f"{path} is missing {tag}"

    def test_iphone_visitors_are_told_how_to_get_alerts(self, client):
        page = client.get("/ffgs").get_data(as_text=True)
        assert page.count("pushInstallIos:") == 2
        assert 't(iosOutsideHomeScreen() ? "pushInstallIos" : "pushUnsupported")' in page


# ============================================================
# Official warnings (NDMA SACHET)
# ============================================================

def _cap(identifier, area, *, sender="Uttarakhand-SDMA", event="Thunder shower", severity="Moderate",
         expires="2099-01-01T00:00:00+05:30", status="Actual", msg_type="Alert", references="",
         headline_hi="अगले 3 घंटो के दौरान वर्षा"):
    """A CAP 1.2 document in SACHET's shape. Test fixture only."""
    def info(lang, headline):
        return f"""<cap:info><cap:language>{lang}</cap:language><cap:category>Met</cap:category>
<cap:event>{event}</cap:event><cap:urgency>Expected</cap:urgency><cap:severity>{severity}</cap:severity>
<cap:certainty>Likely</cap:certainty><cap:effective>2026-09-23T21:17:00+05:30</cap:effective>
<cap:expires>{expires}</cap:expires><cap:headline>{headline}</cap:headline><cap:description/>
<cap:instruction>Please follow SDMA guidelines.</cap:instruction>
<cap:area><cap:areaDesc>{area}</cap:areaDesc></cap:area></cap:info>"""
    return f"""<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
<cap:identifier>{identifier}</cap:identifier><cap:sender>{sender}</cap:sender>
<cap:sent>2026-09-23T21:22:04+05:30</cap:sent><cap:status>{status}</cap:status>
<cap:msgType>{msg_type}</cap:msgType><cap:scope>Public</cap:scope><cap:references>{references}</cap:references>
{info("en-IN", "Rain likely over " + area)}{info("HI", headline_hi)}</cap:alert>""".encode("utf-8")


def _feed(items):
    rows = "".join(
        f"<item><title>{title}</title><link>https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier={guid}</link>"
        f"<author>controlroom@ndma.gov.in ({office})</author><guid>{guid}</guid></item>"
        for guid, office, title in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{rows}</channel></rss>'.encode("utf-8")


@pytest.fixture()
def sachet(server, monkeypatch):
    """Serves a fixed feed and CAP documents in place of SACHET."""
    documents = {
        "1": _cap("IN-1", "Bageshwar, Almora and Pithoragarh"),
        "2": _cap("IN-2", "Jalaka, Mathani Road Bridge, Balasore, Odisha", sender="CWC", event="Flood"),
        "3": _cap("IN-3", "Alaknanda, Rudraprayag, Rudraprayag, Uttarakhand", sender="CWC", event="Flood",
                  severity="Severe"),
        "4": _cap("IN-4", "Dehradun", expires="2020-01-01T00:00:00+05:30"),
        "5": _cap("IN-5", "Nainital"),
        "6": _cap("IN-6", "Nainital", msg_type="Update", references="Uttarakhand-SDMA,IN-5,2026-09-23T20:00:00+05:30"),
        "7": _cap("IN-7", "Haridwar", status="Exercise"),
        "8": _cap("IN-8", "Some tehsil name"),
    }
    feed = _feed([
        ("1", "IMD Dehradun", "Thunder shower"), ("2", "CWC", "River Jalaka"), ("3", "CWC", "River Alaknanda"),
        ("4", "IMD Dehradun", "Old"), ("5", "IMD Dehradun", "First"), ("6", "IMD Dehradun", "Update"),
        ("7", "IMD Dehradun", "Drill"), ("8", "Uttarakhand SDMA", "Local"), ("9", "IMD Mumbai", "Mumbai rain"),
    ])
    calls = []

    class Reply:
        def __init__(self, body):
            self.content = body

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        calls.append(url)
        if url == server.NDMA_FEED_URL:
            return Reply(feed)
        return Reply(documents[url.rsplit("=", 1)[1]])

    monkeypatch.setattr(server.requests, "get", fake_get)
    monkeypatch.setattr(server, "_ndma_state",
                        {"alerts": [], "checked_at": None, "last_error": None, "last_attempt": 0.0})
    monkeypatch.setattr(server, "_ndma_cap_cache", {})
    # Refreshes run in a background thread; run them inline here so a
    # request sees its own refresh's result.
    monkeypatch.setattr(server, "_run_in_background", lambda fn: fn())
    return calls


class TestOfficialWarnings:

    def alerts(self, client):
        return {a["identifier"]: a for a in client.get("/official-warnings").get_json()["alerts"]}

    def test_keeps_only_current_uttarakhand_alerts(self, client, server, sachet):
        alerts = self.alerts(client)
        # Odisha river, expired, superseded (5 by 6) and an exercise are all dropped.
        assert set(alerts) == {"IN-1", "IN-3", "IN-6", "IN-8"}

    def test_districts_come_from_the_area_text(self, client, server, sachet):
        alerts = self.alerts(client)
        assert alerts["IN-1"]["districts"] == ["Almora", "Bageshwar", "Pithoragarh"]
        assert alerts["IN-3"]["districts"] == ["Rudraprayag"]
        assert not alerts["IN-1"]["statewide"]

    def test_unrecognised_area_from_the_state_is_shown_statewide(self, client, server, sachet):
        """Dropping an official warning is worse than showing it too broadly."""
        alert = self.alerts(client)["IN-8"]
        assert alert["statewide"] and len(alert["districts"]) == 13

    def test_text_is_passed_through_in_both_languages(self, client, server, sachet):
        alert = self.alerts(client)["IN-1"]
        assert alert["headline"] == "Rain likely over Bageshwar, Almora and Pithoragarh"
        assert alert["headline_hi"] == "अगले 3 घंटो के दौरान वर्षा"
        assert alert["office"] == "IMD Dehradun"

    def test_severe_alerts_come_first(self, client, server, sachet):
        alerts = client.get("/official-warnings").get_json()["alerts"]
        assert alerts[0]["identifier"] == "IN-3"

    def test_only_plausible_items_are_opened_and_documents_are_reused(self, client, server, sachet):
        client.get("/official-warnings")
        opened = {url.rsplit("=", 1)[1] for url in sachet if url != server.NDMA_FEED_URL}
        assert "9" not in opened, "an IMD Mumbai alert should not be fetched"

        server._ndma_state["last_attempt"] = 0.0
        before = len(sachet)
        client.get("/official-warnings")
        assert len(sachet) == before + 1, "a refresh should re-read only the feed"

    def test_an_outage_is_reported_and_retried_once_per_ttl(self, client, server, monkeypatch):
        attempts = []

        def down(url, **kwargs):
            attempts.append(url)
            raise server.requests.ConnectionError("unreachable")

        monkeypatch.setattr(server.requests, "get", down)
        monkeypatch.setattr(server, "_ndma_state",
                            {"alerts": [], "checked_at": None, "last_error": None, "last_attempt": 0.0})
        monkeypatch.setattr(server, "_run_in_background", lambda fn: fn())

        for _ in range(3):
            body = client.get("/official-warnings").get_json()
        assert body["last_error"] and body["alerts"] == []
        assert len(attempts) == 1

    def test_a_page_request_never_waits_for_sachet(self, client, server, sachet, monkeypatch):
        """A slow SACHET once held /official-warnings for 30 s."""
        queued = []
        monkeypatch.setattr(server, "_run_in_background", queued.append)

        body = client.get("/official-warnings").get_json()
        assert sachet == [], "the request itself must not contact SACHET"
        assert body["refreshing"] and body["alerts"] == [] and body["checked_at"] is None

        client.get("/official-warnings")
        assert len(queued) == 1, "one refresh per TTL, however many requests"

        queued.pop()()
        body = client.get("/official-warnings").get_json()
        assert not body["refreshing"] and len(body["alerts"]) == 4

    def test_the_last_list_is_served_while_a_refresh_runs(self, client, server, sachet, monkeypatch):
        client.get("/official-warnings")
        queued = []
        monkeypatch.setattr(server, "_run_in_background", queued.append)
        server._ndma_state["last_attempt"] = 0.0

        body = client.get("/official-warnings").get_json()
        assert len(queued) == 1
        assert body["refreshing"] and len(body["alerts"]) == 4

    def test_the_page_waits_for_the_first_read_and_asks_again(self, client):
        page = client.get("/ffgs").get_data(as_text=True)
        assert "officialData.refreshing && !officialData.checked_at" in page
        assert "setTimeout(loadOfficialWarnings" in page


class TestZoneDistricts:

    def test_every_zone_has_a_district_by_location(self, client, server):
        zones = client.get("/ffgs/zones").get_json()["zones"]
        names = {name for name, _ in server.UTTARAKHAND_DISTRICTS}
        assert len(names) == 13
        assert all(z["district"] in names for z in zones)

    def test_a_town_can_span_districts(self, client):
        """Why the district comes from location, not the parent town."""
        zones = client.get("/ffgs/zones").get_json()["zones"]
        rishikesh = {z["district"] for z in zones if z.get("parent_town") == "Rishikesh"}
        assert {"Dehradun", "Tehri Garhwal"} <= rishikesh


# ============================================================
# Hardening: cross-site writes, headers, limits, concurrency
# ============================================================

class TestCrossSiteWrites:
    """Any web page used to be able to act on FloodSafe through its
    visitors' browsers -- confirmed live: a form-style POST from a foreign
    origin was processed, and CORS reflected every origin for every method."""

    @pytest.mark.parametrize("headers", [
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "https://evil.example", "Content-Type": "text/plain"},
    ])
    def test_foreign_writes_are_refused(self, client, headers):
        assert client.post("/report/anything/resolve", headers=headers).status_code == 403
        assert client.post("/report", data='{"lat": 30.3, "lon": 78.0}', headers=headers).status_code == 403

    def test_same_site_and_non_browser_writes_still_work(self, client):
        same_site = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}
        assert client.post("/report/anything/resolve", headers=same_site).status_code == 404
        assert client.post("/report/anything/resolve").status_code == 404

    def test_reads_stay_open_to_other_sites(self, client):
        response = client.get("/ffgs/zones", headers={"Origin": "https://other.example"})
        assert response.status_code == 200
        assert response.headers.get("Access-Control-Allow-Origin") in ("*", "https://other.example")

    def test_cors_never_offers_write_methods(self, client):
        preflight = client.options("/report", headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        })
        allowed = preflight.headers.get("Access-Control-Allow-Methods", "")
        assert "POST" not in allowed


class TestResponseHardening:

    @pytest.mark.parametrize("path", ["/", "/ffgs", "/app", "/status"])
    def test_pages_cannot_be_framed_or_sniffed(self, client, path):
        headers = client.get(path).headers
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"

    def test_oversized_bodies_are_refused(self, client, server):
        huge = b"x" * (server.app.config["MAX_CONTENT_LENGTH"] + 1)
        response = client.post("/report", data=huge, content_type="application/json")
        assert response.status_code == 413

    @pytest.mark.parametrize("path, body, target", [
        ("/evacuate", {"lat": 30.3165, "lon": 78.0322}, "find_nearest_shelter"),
        ("/nearest-hospital", {"lat": 30.3165, "lon": 78.0322}, "find_nearest_hospital"),
        ("/route", {"start_lat": 30.3165, "start_lon": 78.0322,
                    "end_lat": 30.0869, "end_lon": 78.2676}, "calculate_route"),
    ])
    def test_errors_do_not_leak_internals(self, client, server, monkeypatch, path, body, target):
        def boom(**kwargs):
            raise RuntimeError(r"C:\\secret\\path\\to\\graph.npz is corrupt")

        monkeypatch.setattr(server, target, boom)
        text = client.post(path, json=body).get_data(as_text=True)
        assert "secret" not in text and "graph.npz" not in text


class TestClientIdentity:

    def test_ipv6_is_grouped_by_its_slash_64(self, client, server):
        """A client can pick a fresh address from its /64 on every request."""
        limit, _ = server.RATE_LIMITS["report_action"]
        statuses = [
            client.post("/report/nonexistent/resolve",
                        headers={"CF-Connecting-IP": f"2001:db8:1:2::{i:x}"}).status_code
            for i in range(limit + 1)
        ]
        assert statuses[-1] == 429

    def test_different_ipv6_networks_are_separate(self, client, server):
        limit, _ = server.RATE_LIMITS["report_action"]
        for i in range(limit):
            client.post("/report/nonexistent/resolve", headers={"CF-Connecting-IP": "2001:db8:1:2::1"})
        other = client.post("/report/nonexistent/resolve", headers={"CF-Connecting-IP": "2001:db8:9:9::1"})
        assert other.status_code == 404


class TestReportPlacement:

    def test_reports_far_from_any_road_are_refused(self, client):
        # Mid-Atlantic, then the Nanda Devi sanctuary (30 km from a road).
        for lat, lon in ((0.0, -30.0), (30.376, 79.970)):
            assert client.post("/report", json={"lat": lat, "lon": lon}).status_code == 422


class TestConcurrency:
    """The server now runs threads, so shared state must stay consistent."""

    def test_route_computations_never_overlap(self, server, monkeypatch):
        import threading

        active, overlaps = [0], []

        def slow_route(**kwargs):
            active[0] += 1
            if active[0] > 1:
                overlaps.append(True)
            time.sleep(0.05)
            active[0] -= 1

        guarded = server._one_route_at_a_time(slow_route)
        threads = [threading.Thread(target=guarded) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not overlaps

    def test_simultaneous_reports_are_all_kept(self, server, monkeypatch, tmp_path):
        import threading

        monkeypatch.setattr(server, "DATABASE_URL", "")
        monkeypatch.setattr(server, "REPORTS_FILE", str(tmp_path / "reports.json"))
        monkeypatch.setattr(server, "_reports", [])
        monkeypatch.setitem(server.RATE_LIMITS, "report", (1000, 300))

        def post(i):
            with server.app.test_client() as c:
                c.post("/report", json={"lat": 30.3165, "lon": 78.0322, "description": f"r{i}"},
                       headers={"CF-Connecting-IP": f"198.51.100.{i}"})

        threads = [threading.Thread(target=post, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(server._reports) == 20
        assert len(json.loads((tmp_path / "reports.json").read_text())) == 20

    def test_a_stale_cache_is_refreshed_by_one_request_not_all(self, server, monkeypatch):
        import threading

        calls = []

        class Slow429:
            status_code = 429

            def json(self):
                return {"reason": "Daily API request limit exceeded."}

            def raise_for_status(self):
                raise server.requests.HTTPError("429")

        def slow_get(url, **kwargs):
            calls.append(url)
            time.sleep(0.2)
            return Slow429()

        monkeypatch.setattr(server.requests, "get", slow_get)
        monkeypatch.setattr(server, "_ffgs_rainfall_cache", {
            "timestamp": 0.0, "data": {}, "last_error": None, "last_attempt": 0.0, "source": None})

        threads = [threading.Thread(target=server._fetch_ffgs_live_rainfall) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1


class TestCalibratedThresholds:
    """Zone thresholds come from the flood-calibrated rainfall rule
    (floodsafe/pipeline/calibrate_thresholds.py), not the hand-set table."""

    def test_every_zone_uses_calibrated_thresholds(self, client):
        zones = client.get("/ffgs/zones").get_json()["zones"]
        assert all(z["threshold_source"] == "calibrated" for z in zones)
        for zone in zones:
            for window in ("1h", "3h", "24h"):
                pair = zone["thresholds_mm"][window]
                assert 0 < pair["watch"] < pair["critical"]

    def test_a_zone_takes_its_nearest_grid_points_thresholds(self, server):
        import math

        zone = next(z for z in server.FFGS_ZONES if z["name"] == "Dehradun")
        points = server.CALIBRATED_THRESHOLDS["points"]
        scale = math.cos(math.radians(zone["lat"]))
        nearest = min(points, key=lambda p: (p["lat"] - zone["lat"]) ** 2 + ((p["lon"] - zone["lon"]) * scale) ** 2)
        assert zone["thresholds_mm"] == nearest["thresholds_mm"]

    def test_outside_the_calibrated_area_falls_back(self, client):
        inside = client.get("/ffgs/point?lat=30.3165&lon=78.0322").get_json()
        assert inside["threshold_source"] == "calibrated"
        outside = client.get("/ffgs/point?lat=28.6139&lon=77.2090").get_json()
        assert outside["threshold_source"] != "calibrated"

    def test_page_publishes_the_measured_scores(self, client, server):
        page = client.get("/ffgs").get_data(as_text=True)
        assert "__CRIT_POD__" not in page and "__WATCH_POFD__" not in page
        critical = server.CALIBRATED_THRESHOLDS["validation"]["critical"]["POD"]
        assert f"{round(critical * 100)}%" in page

    def test_calibration_records_its_validation(self, server):
        validation = server.CALIBRATED_THRESHOLDS["validation"]
        assert validation["flood_events_scored"] > 100
        assert validation["critical"]["POFD"] < validation["watch"]["POFD"]
        assert validation["critical"]["TSS"] > 0 and validation["watch"]["TSS"] > 0


class TestLandingAgreesWithFfgs:
    """The landing panel used its own hand-set pair per class against a
    single reading, so it could judge a town differently from /ffgs."""

    def test_same_thresholds_and_rain_for_every_town(self, client):
        landing = {z["name"]: z for z in client.get("/flood-guidance-zones").get_json()["zones"]}
        ffgs = {z["name"]: z for z in client.get("/ffgs/zones").get_json()["zones"] if z["kind"] == "town"}
        assert set(landing) == set(ffgs)
        for name, town in landing.items():
            assert town["thresholds_mm"] == ffgs[name]["thresholds_mm"]
            assert town["live_rainfall"] == ffgs[name]["live_rainfall"]

    def test_panel_supports_the_browser_fallback(self, client):
        data = client.get("/flood-guidance-zones").get_json()
        assert data["rainfall"]["cells"] and all(z["rain_cell"] is not None for z in data["zones"])

    def test_my_location_uses_calibrated_thresholds(self, client):
        point = client.get("/flood-guidance?lat=30.4598&lon=78.0664").get_json()
        assert point["threshold_source"] == "calibrated"
        assert set(point["thresholds_mm"]) == {"1h", "3h", "24h"}


class TestZoneCoverage:
    """Zones followed the hazard atlas, not where floods are recorded."""

    def test_every_district_has_zones(self, client):
        import collections

        zones = client.get("/ffgs/zones").get_json()["zones"]
        per_district = collections.Counter(z["district"] for z in zones)
        assert len(per_district) == 13
        for district in ("Chamoli", "Rudraprayag", "Udham Singh Nagar"):
            assert per_district[district] >= 7, district

    def test_places_named_in_flood_records_are_zones(self, client):
        names = {z["name"] for z in client.get("/ffgs/zones").get_json()["zones"]}
        assert {"Kedarnath", "Gaurikund", "Guptkashi", "Tharali"} <= names


def _hourly_payload(past_mm, future_mm):
    """Open-Meteo shape with `past_mm` up to and including now, then
    `future_mm` as the forecast hours."""
    values = list(past_mm) + list(future_mm)
    times = [f"2026-09-{21 + (h // 24):02d}T{h % 24:02d}:00" for h in range(len(values))]
    return {"current": {"time": times[len(past_mm) - 1], "precipitation": past_mm[-1]},
            "hourly": {"time": times, "precipitation": values}}


class TestForecastOutlook:
    """"Prediction" means warning before the rain has fallen: the same
    calibrated thresholds, applied to the next hours of forecast rain."""

    def zone(self, server):
        return next(z for z in server.FFGS_ZONE_BY_KEY.values() if z["name"] == "Dehradun")

    def test_forecast_windows_mix_fallen_and_forecast_rain(self, server):
        reading = server._parse_open_meteo_durations(_hourly_payload([1.0] * 48, [2.0, 4.0, 0.0] + [0.0] * 30))

        assert reading["1h"] == 1.0 and reading["3h"] == 3.0 and reading["24h"] == 24.0
        assert len(reading["forecast"]) == server.FFGS_FORECAST_HOURS
        first, second, third = reading["forecast"][:3]
        assert first == {"1h": 2.0, "3h": 4.0, "24h": 25.0}
        assert second == {"1h": 4.0, "3h": 7.0, "24h": 28.0}
        assert third == {"1h": 0.0, "3h": 6.0, "24h": 27.0}

    def test_no_forecast_hours_means_no_forecast(self, server):
        assert server._parse_open_meteo_durations(_hourly_payload([1.0] * 48, []))["forecast"] == []

    def test_first_hour_of_the_worst_status_is_reported(self, server):
        zone = self.zone(server)
        th = zone["thresholds_mm"]["1h"]
        watch = (th["watch"] + th["critical"]) / 2
        forecast = [{"1h": 0.0, "3h": 0.0, "24h": 0.0}, {"1h": watch, "3h": 0.0, "24h": 0.0},
                    {"1h": th["critical"] * 2, "3h": 0.0, "24h": 0.0},
                    {"1h": th["critical"] * 3, "3h": 0.0, "24h": 0.0}]
        reading = {"1h": 0.0, "3h": 0.0, "24h": 0.0, "forecast": forecast}

        outlook = server._forecast_outlook(zone, reading)
        assert outlook["status"] == "CRITICAL" and outlook["in_hours"] == 3
        assert outlook["window"] == "1h" and outlook["threshold_mm"] == th["critical"]

    def test_nothing_to_report_when_it_is_already_that_bad(self, server):
        zone = self.zone(server)
        critical = zone["thresholds_mm"]["1h"]["critical"] * 2
        windows = {"1h": critical, "3h": 0.0, "24h": 0.0}
        assert server._forecast_outlook(zone, dict(windows, forecast=[windows] * 6)) is None

    def test_dry_forecast_has_no_outlook(self, server):
        dry = {"1h": 0.0, "3h": 0.0, "24h": 0.0}
        assert server._forecast_outlook(self.zone(server), dict(dry, forecast=[dry] * 6)) is None

    def test_two_days_are_fetched_so_the_evening_still_has_six_hours(self, server):
        assert server.FFGS_RAINFALL_QUERY["forecast_days"] == 2

    def test_relayed_forecast_reaches_the_zones(self, client, server, relay):
        body = _relay_body(server, 0.5)
        for loc in body["ffgs"]:
            loc["hourly"]["precipitation"] += [0.5] * 47
            loc["hourly"]["time"] = [f"2026-09-{21 + (h // 24):02d}T{h % 24:02d}:00" for h in range(96)]
        assert client.post("/rainfall/relay", json=body, headers=relay).status_code == 200

        zone = client.get("/ffgs/zones").get_json()["zones"][0]
        assert len(zone["live_rainfall"]["forecast"]) == server.FFGS_FORECAST_HOURS

    def test_page_and_browser_fallback_show_the_outlook(self, client, server):
        page = client.get("/ffgs").get_data(as_text=True)
        assert "function forecastOutlook" in page and 'data-i18n="colOutlook"' in page
        assert 'id="ffgsForecastAlert"' in page

        script = client.get("/rainfall-fallback.js").get_data(as_text=True)
        assert f"var FORECAST_HOURS = {server.FFGS_FORECAST_HOURS};" in script
        assert "forecast_days=2" in script
