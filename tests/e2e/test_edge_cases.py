"""Edge cases across the proxy, the *arr API, and the Jellyfin plugins.

Each test here corresponds to a way one of these pieces can surprise another:
an id that names an instance nobody configured, a status code that must not
carry a body, a header cased the way one specific plugin cases it, a title in
a script that is not Latin-1.
"""

from __future__ import annotations

import concurrent.futures
import os

import httpx
import pytest

SONARR = os.environ.get("PROXY_SONARR", "http://proxy:8989")
RADARR = os.environ.get("PROXY_RADARR", "http://proxy:7878")
UNIFIED = os.environ.get("PROXY_UNIFIED", "http://proxy:8787")
SONARR_KEY = "e2e-sonarr-combined-key"
RADARR_KEY = "e2e-radarr-combined-key"
BLOCK = 10_000_000

# Decodes to instance 9, which is not configured.
GHOST_ID = 9 * BLOCK + 1

MOCKS = {
    "sonarr-main": "http://sonarr-main:8989",
    "sonarr-anime": "http://sonarr-anime:8989",
    "radarr-main": "http://radarr-main:7878",
    "radarr-anime": "http://radarr-anime:7878",
}


def reset_logs() -> None:
    for url in MOCKS.values():
        httpx.get(f"{url}/__mock/reset", timeout=10.0)


def log_of(name: str) -> list[dict]:
    return httpx.get(f"{MOCKS[name]}/__mock/requests", timeout=10.0).json()


@pytest.fixture()
def sonarr() -> httpx.Client:
    with httpx.Client(base_url=SONARR, headers={"X-Api-Key": SONARR_KEY},
                      timeout=30.0) as client:
        yield client


@pytest.fixture()
def radarr() -> httpx.Client:
    with httpx.Client(base_url=RADARR, headers={"X-Api-Key": RADARR_KEY},
                      timeout=30.0) as client:
        yield client


# ---------------------------------------------------------------------------
class TestIdsNamingUnknownInstances:
    """An id can decode to an instance index that was never configured.

    That happens when the config is reordered or an instance is removed while a
    client still holds ids from it.  The dangerous failure is silently widening
    the request instead of narrowing it.
    """

    def test_query_id_for_an_unknown_instance_returns_nothing(self, sonarr) -> None:
        reset_logs()
        response = sonarr.get("/api/v3/episode", params={"seriesId": GHOST_ID})
        assert response.status_code == 200
        assert response.json() == [], "must not fall back to an unfiltered fan-out"
        # The decisive check: no instance should have been asked at all.
        assert log_of("sonarr-main") == []
        assert log_of("sonarr-anime") == []

    def test_path_id_for_an_unknown_instance_is_a_404(self, sonarr) -> None:
        assert sonarr.get(f"/api/v3/series/{GHOST_ID}").status_code == 404

    def test_paged_endpoint_returns_an_empty_envelope(self, sonarr) -> None:
        body = sonarr.get("/api/v3/queue", params={"seriesId": GHOST_ID}).json()
        assert body["totalRecords"] == 0 and body["records"] == []

    def test_command_for_an_unknown_instance_is_not_broadcast(self, sonarr) -> None:
        reset_logs()
        response = sonarr.post(
            "/api/v3/command", json={"name": "RefreshSeries", "seriesId": GHOST_ID}
        )
        assert response.status_code == 404
        assert log_of("sonarr-main") == [] and log_of("sonarr-anime") == []

    def test_a_real_id_still_scopes_correctly(self, sonarr) -> None:
        """The guard must not break the normal narrowing path."""
        reset_logs()
        rows = sonarr.get("/api/v3/episode", params={"seriesId": BLOCK + 1}).json()
        assert rows and log_of("sonarr-main") == []


class TestAuthEdges:
    # Non-ASCII cannot travel in an HTTP header at all -- httpx refuses to send
    # it -- so the query parameter is the only vector that can actually deliver
    # one. hmac.compare_digest raises TypeError on non-ASCII str, which would
    # surface as a 500; comparing as bytes keeps it a plain 401.
    def test_non_ascii_key_in_the_query_is_rejected_not_crashed(self) -> None:
        response = httpx.get(
            f"{SONARR}/api/v3/series", params={"apikey": "kéy-wîth-áccents"}, timeout=30
        )
        assert response.status_code == 401

    def test_emoji_key_in_the_query_is_rejected_not_crashed(self) -> None:
        response = httpx.get(
            f"{SONARR}/api/v3/series", params={"apikey": "🔑🔑🔑"}, timeout=30
        )
        assert response.status_code == 401

    def test_whitespace_key_is_rejected(self) -> None:
        response = httpx.get(
            f"{SONARR}/api/v3/series", params={"apikey": "   "}, timeout=30
        )
        assert response.status_code == 401

    def test_empty_key_header_falls_through_to_unauthorized(self) -> None:
        response = httpx.get(
            f"{SONARR}/api/v3/series", headers={"X-Api-Key": ""}, timeout=30
        )
        assert response.status_code == 401

    def test_uppercase_header_casing_works(self) -> None:
        """Home Screen Sections sends the header as X-API-KEY, not X-Api-Key."""
        response = httpx.get(
            f"{SONARR}/api/v3/series", headers={"X-API-KEY": SONARR_KEY}, timeout=30
        )
        assert response.status_code == 200

    def test_bearer_token_works(self) -> None:
        response = httpx.get(
            f"{SONARR}/api/v3/series",
            headers={"Authorization": f"Bearer {SONARR_KEY}"}, timeout=30,
        )
        assert response.status_code == 200

    def test_key_is_stripped_from_a_media_cover_request(self, sonarr) -> None:
        reset_logs()
        httpx.get(f"{SONARR}/MediaCover/{BLOCK + 1}/poster.jpg",
                  params={"apikey": SONARR_KEY}, timeout=30)
        for entry in log_of("sonarr-anime"):
            assert "apikey" not in entry["query"]


class TestHttpSemantics:
    def test_no_body_on_a_204(self, sonarr) -> None:
        """Serialising "null" into a 204 is a protocol violation."""
        created = sonarr.post(
            "/api/v3/series",
            json={"title": "Ephemeral", "tvdbId": 1, "qualityProfileId": BLOCK + 1,
                  "seasons": []},
        ).json()
        response = sonarr.request("DELETE", f"/api/v3/series/{created['id']}")
        assert response.status_code in (200, 204)
        if response.status_code == 204:
            assert response.content == b""

    def test_head_request_does_not_error(self, sonarr) -> None:
        response = sonarr.request("HEAD", "/api/v3/series")
        assert response.status_code < 500

    def test_malformed_json_body_is_relayed_as_a_400(self, sonarr) -> None:
        response = sonarr.post(
            "/api/v3/series", content=b"{not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400

    def test_unknown_endpoint_relays_the_real_404(self, sonarr) -> None:
        """A missing endpoint is an answer, not an outage.

        Every instance is up and says 404, so reporting 502 would wrongly
        implicate the backends.
        """
        response = sonarr.get("/api/v3/completely/made/up")
        assert response.status_code == 404

    def test_non_numeric_id_in_the_path(self, sonarr) -> None:
        assert sonarr.get("/api/v3/series/not-a-number").status_code == 404

    def test_non_numeric_id_in_the_query(self, sonarr) -> None:
        """A non-numeric value stays a plain parameter and reaches upstream."""
        assert sonarr.get(
            "/api/v3/episode", params={"seriesId": "abc"}
        ).status_code < 500

    def test_zero_id_is_not_routed_anywhere_odd(self, sonarr) -> None:
        assert sonarr.get("/api/v3/series/0").status_code in (404, 400)

    def test_negative_id(self, sonarr) -> None:
        assert sonarr.get("/api/v3/series/-5").status_code == 404

    def test_disagreeing_errors_are_reported_as_a_gateway_error(self, sonarr) -> None:
        """Only a unanimous status is relayed verbatim.

        When one instance says 404 and another says 500 there is no single
        honest answer to forward, so it stays a 502 that names both.
        """
        response = sonarr.get("/api/v3/__mixed")
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "sonarr-main" in detail and "sonarr-anime" in detail


class TestUnicode:
    """An anime library is full of titles that are not Latin-1."""

    def test_japanese_titles_survive_the_merge(self, sonarr) -> None:
        created = sonarr.post(
            "/api/v3/series",
            json={"title": "葬送のフリーレン", "tvdbId": 424536,
                  "qualityProfileId": BLOCK + 1, "seasons": []},
        )
        assert created.status_code == 201
        assert created.json()["title"] == "葬送のフリーレン"

        rows = sonarr.get("/api/v3/series").json()
        assert any(r["title"] == "葬送のフリーレン" for r in rows)

    def test_unicode_query_terms_round_trip(self, sonarr) -> None:
        response = sonarr.get("/api/v3/series/lookup", params={"term": "フリーレン"})
        assert response.status_code == 200

    def test_emoji_and_accents_in_a_title(self, sonarr) -> None:
        response = sonarr.post(
            "/api/v3/series",
            json={"title": "Amélie ★ 日本 🎬", "tvdbId": 2,
                  "qualityProfileId": BLOCK + 1, "seasons": []},
        )
        assert response.json()["title"] == "Amélie ★ 日本 🎬"


class TestPagingEdges:
    def test_page_zero_is_treated_as_page_one(self, sonarr) -> None:
        body = sonarr.get("/api/v3/queue", params={"page": 0, "pageSize": 2}).json()
        assert body["page"] == 1 and len(body["records"]) == 2

    def test_negative_page(self, sonarr) -> None:
        body = sonarr.get("/api/v3/queue", params={"page": -3, "pageSize": 2}).json()
        assert body["page"] == 1

    def test_absurd_page_size_is_survivable(self, sonarr) -> None:
        body = sonarr.get("/api/v3/queue", params={"page": 1, "pageSize": 100000}).json()
        assert body["totalRecords"] == len(body["records"])

    def test_page_far_past_the_end_is_empty_not_an_error(self, sonarr) -> None:
        body = sonarr.get("/api/v3/queue", params={"page": 500, "pageSize": 20}).json()
        assert body["records"] == []

    def test_garbage_page_values_do_not_crash(self, sonarr) -> None:
        response = sonarr.get("/api/v3/queue", params={"page": "abc", "pageSize": "xyz"})
        assert response.status_code == 200

    def test_unresolvable_sort_key_does_not_crash(self, sonarr) -> None:
        body = sonarr.get(
            "/api/v3/queue", params={"page": 1, "pageSize": 20, "sortKey": "nope.nope"}
        ).json()
        assert len(body["records"]) == body["totalRecords"]


class TestBrowserDeepLinks:
    """SeerrFin renders "Open in Sonarr" buttons pointing at the configured base.

    Pointed at the proxy those would 404, because the proxy serves no web UI.
    """

    def test_series_deep_link_redirects_to_the_owning_instance(self) -> None:
        response = httpx.get(f"{SONARR}/series/cowboy-bebop",
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302
        assert response.headers["X-ArrProxy-Instances"] == "sonarr-anime"
        assert response.headers["location"].endswith("/series/cowboy-bebop")
        assert "sonarr-anime" in response.headers["location"]

    def test_deep_link_for_a_primary_title(self) -> None:
        response = httpx.get(f"{SONARR}/series/breaking-bad",
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302
        assert response.headers["X-ArrProxy-Instances"] == "sonarr-main"

    def test_movie_deep_link(self) -> None:
        response = httpx.get(f"{RADARR}/movie/your-name",
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302
        assert response.headers["X-ArrProxy-Instances"] == "radarr-anime"

    def test_add_new_link_goes_to_the_default_instance(self) -> None:
        """A title no instance has, and no rule claims, keeps the add form."""
        response = httpx.get(f"{SONARR}/add/new", params={"term": "tmdb:1234"},
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302
        location = response.headers["location"]
        assert "sonarr-main" in location and "term=tmdb%3A1234" in location
        assert response.headers["X-ArrProxy-Resolution"] == "default"

    # SeerrFin emits /add/new?term=tmdb:N even for titles already in a library:
    # it drops a monitored title's progress entry (and its link) while nothing
    # is downloaded yet.  Sending those to the default instance put the user on
    # an add form in an instance that did not have the show at all.
    def test_add_new_for_a_title_already_on_the_anime_instance(self) -> None:
        response = httpx.get(f"{SONARR}/add/new", params={"term": "tmdb:30991"},
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302
        assert response.headers["X-ArrProxy-Instances"] == "sonarr-anime"
        assert response.headers["X-ArrProxy-Resolution"] == "library"
        location = response.headers["location"]
        assert location.endswith("/series/cowboy-bebop"), "open the show, not an add form"
        assert "term=" not in location

    def test_add_new_by_tvdb_id(self) -> None:
        response = httpx.get(f"{SONARR}/add/new", params={"term": "tvdb:424536"},
                             follow_redirects=False, timeout=30)
        assert response.headers["X-ArrProxy-Instances"] == "sonarr-anime"
        assert response.headers["location"].endswith("/series/frieren")

    def test_add_new_for_a_title_on_the_main_instance(self) -> None:
        response = httpx.get(f"{SONARR}/add/new", params={"term": "tmdb:1396"},
                             follow_redirects=False, timeout=30)
        assert response.headers["X-ArrProxy-Instances"] == "sonarr-main"
        assert response.headers["X-ArrProxy-Resolution"] == "library"
        assert response.headers["location"].endswith("/series/breaking-bad")

    def test_add_new_movie_already_on_the_anime_instance(self) -> None:
        response = httpx.get(f"{RADARR}/add/new", params={"term": "tmdb:372058"},
                             follow_redirects=False, timeout=30)
        assert response.headers["X-ArrProxy-Instances"] == "radarr-anime"
        assert response.headers["X-ArrProxy-Resolution"] == "library"
        assert response.headers["location"].endswith("/movie/your-name")

    def test_add_new_for_an_unowned_title_follows_routing_rules(self) -> None:
        """Nobody has Perfect Blue yet; radarr-anime's Animation rule claims it."""
        response = httpx.get(f"{RADARR}/add/new", params={"term": "tmdb:10494"},
                             follow_redirects=False, timeout=30)
        assert response.headers["X-ArrProxy-Instances"] == "radarr-anime"
        assert response.headers["X-ArrProxy-Resolution"] == "rule"
        assert "/add/new?term=tmdb%3A10494" in response.headers["location"]

    def test_add_new_with_free_text_is_not_guessed(self) -> None:
        response = httpx.get(f"{SONARR}/add/new", params={"term": "cowboy"},
                             follow_redirects=False, timeout=30)
        assert response.headers["X-ArrProxy-Instances"] == "sonarr-main"
        assert response.headers["X-ArrProxy-Resolution"] == "default"

    def test_slug_links_report_how_they_resolved(self) -> None:
        found = httpx.get(f"{SONARR}/series/cowboy-bebop", follow_redirects=False, timeout=30)
        missing = httpx.get(f"{SONARR}/series/no-such-show", follow_redirects=False, timeout=30)
        assert found.headers["X-ArrProxy-Resolution"] == "library"
        assert missing.headers["X-ArrProxy-Resolution"] == "default"

    def test_unknown_title_falls_back_to_the_default_instance(self) -> None:
        response = httpx.get(f"{SONARR}/series/nothing-here",
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302
        assert response.headers["X-ArrProxy-Instances"] == "sonarr-main"

    def test_deep_links_need_no_api_key(self) -> None:
        """A browser following the button has no key to present."""
        response = httpx.get(f"{SONARR}/series/cowboy-bebop",
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302

    def test_deep_link_works_through_the_unified_port(self) -> None:
        response = httpx.get(f"{UNIFIED}/sonarr/series/cowboy-bebop",
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302
        assert response.headers["X-ArrProxy-Instances"] == "sonarr-anime"

    def test_api_paths_are_still_authenticated(self) -> None:
        """The deep-link exemption must not widen to the API."""
        assert httpx.get(f"{SONARR}/api/v3/series", timeout=30).status_code == 401


class TestConcurrency:
    def test_parallel_identical_requests_are_consistent(self, sonarr) -> None:
        def fetch() -> tuple[int, int]:
            response = httpx.get(f"{SONARR}/api/v3/series",
                                 headers={"X-Api-Key": SONARR_KEY}, timeout=30)
            return response.status_code, len(response.json())

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = [f.result() for f in [pool.submit(fetch) for _ in range(24)]]

        assert all(status == 200 for status, _ in results)
        assert len({count for _, count in results}) == 1, "inconsistent row counts"

    def test_mixed_apps_in_parallel_do_not_cross_over(self) -> None:
        def sonarr_call() -> set[str]:
            rows = httpx.get(f"{SONARR}/api/v3/series",
                             headers={"X-Api-Key": SONARR_KEY}, timeout=30).json()
            return {r["title"] for r in rows}

        def radarr_call() -> set[str]:
            rows = httpx.get(f"{RADARR}/api/v3/movie",
                             headers={"X-Api-Key": RADARR_KEY}, timeout=30).json()
            return {r["title"] for r in rows}

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(sonarr_call if n % 2 else radarr_call) for n in range(16)]
            results = [f.result() for f in jobs]

        for titles in results:
            assert not ({"Dune", "Arrival"} & titles and {"Breaking Bad"} & titles), \
                "a Sonarr response leaked into a Radarr one"


class TestSlowInstance:
    """An instance that is reachable but hung is worse than one that is down.

    A dead instance fails the connect in milliseconds; a hung one holds the
    whole merged response open, which on a Jellyfin home screen looks like the
    page itself is broken.
    """

    def test_one_hung_instance_does_not_stall_the_merge(self, sonarr) -> None:
        import time

        started = time.monotonic()
        response = sonarr.get("/api/v3/__slow")
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        assert elapsed < 20, f"waited {elapsed:.1f}s for a hung instance"
        # The healthy instance's row still comes back, and the caller is told
        # which instance was dropped.
        assert response.json() == [{"id": 1, "instance": "sonarr-main"}]
        assert response.headers["X-ArrProxy-Degraded"] == "sonarr-anime"

    def test_the_other_app_is_unaffected_by_a_hang(self, radarr) -> None:
        assert radarr.get("/api/v3/movie").status_code == 200
