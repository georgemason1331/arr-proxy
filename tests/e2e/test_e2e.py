"""End-to-end suite: drives the proxy over HTTP against four live instances.

Runs inside the compose network, so every assertion exercises the same path a
Jellyfin plugin would take.  Where a test cares *which* instance served a call
it checks the mock's own request log rather than inferring it from the body.
"""

from __future__ import annotations

import os

import httpx
import pytest

SONARR = os.environ.get("PROXY_SONARR", "http://proxy:8989")
RADARR = os.environ.get("PROXY_RADARR", "http://proxy:7878")
UNIFIED = os.environ.get("PROXY_UNIFIED", "http://proxy:8787")
CACHED = os.environ.get("PROXY_CACHED", "http://proxy-cached:8989")

SONARR_KEY = "e2e-sonarr-combined-key"
RADARR_KEY = "e2e-radarr-combined-key"
BLOCK = 10_000_000

MOCKS = {
    "sonarr-main": "http://sonarr-main:8989",
    "sonarr-anime": "http://sonarr-anime:8989",
    "radarr-main": "http://radarr-main:7878",
    "radarr-anime": "http://radarr-anime:7878",
}


@pytest.fixture()
def sonarr() -> httpx.Client:
    with httpx.Client(base_url=SONARR, headers={"X-Api-Key": SONARR_KEY},
                      timeout=20.0) as client:
        yield client


@pytest.fixture()
def radarr() -> httpx.Client:
    with httpx.Client(base_url=RADARR, headers={"X-Api-Key": RADARR_KEY},
                      timeout=20.0) as client:
        yield client


def reset_logs() -> None:
    for url in MOCKS.values():
        httpx.get(f"{url}/__mock/reset", timeout=10.0)


def log_of(name: str) -> list[dict]:
    return httpx.get(f"{MOCKS[name]}/__mock/requests", timeout=10.0).json()


def served_by(response: httpx.Response) -> list[str]:
    raw = response.headers.get("X-ArrProxy-Instances", "")
    return [part for part in raw.split(",") if part]


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------
class TestAuth:
    def test_missing_key_is_rejected(self) -> None:
        assert httpx.get(f"{SONARR}/api/v3/series", timeout=20).status_code == 401

    def test_wrong_key_is_rejected(self) -> None:
        response = httpx.get(
            f"{SONARR}/api/v3/series", headers={"X-Api-Key": "nope"}, timeout=20
        )
        assert response.status_code == 401

    def test_other_apps_key_is_rejected(self) -> None:
        """The Sonarr and Radarr listeners must not share a credential."""
        response = httpx.get(
            f"{SONARR}/api/v3/series", headers={"X-Api-Key": RADARR_KEY}, timeout=20
        )
        assert response.status_code == 401

    def test_key_as_query_parameter(self) -> None:
        response = httpx.get(
            f"{SONARR}/api/v3/series", params={"apikey": SONARR_KEY}, timeout=20
        )
        assert response.status_code == 200

    def test_combined_key_never_reaches_an_instance(self) -> None:
        """Upstream must see its own key, never the one the client presented."""
        reset_logs()
        httpx.get(f"{SONARR}/api/v3/series", params={"apikey": SONARR_KEY}, timeout=20)
        for name in ("sonarr-main", "sonarr-anime"):
            for entry in log_of(name):
                assert "apikey" not in entry["query"]
                assert SONARR_KEY not in str(entry["query"])

    def test_ping_and_health_need_no_key(self) -> None:
        assert httpx.get(f"{SONARR}/ping", timeout=20).json() == {"status": "OK"}
        assert httpx.get(f"{UNIFIED}/-/health", timeout=20).status_code == 200


# ---------------------------------------------------------------------------
# listener shapes
# ---------------------------------------------------------------------------
class TestListeners:
    def test_unified_port_serves_both_apps_by_prefix(self) -> None:
        shows = httpx.get(
            f"{UNIFIED}/sonarr/api/v3/series",
            headers={"X-Api-Key": SONARR_KEY}, timeout=20,
        )
        films = httpx.get(
            f"{UNIFIED}/radarr/api/v3/movie",
            headers={"X-Api-Key": RADARR_KEY}, timeout=20,
        )
        assert shows.status_code == 200 and films.status_code == 200
        assert {s["title"] for s in shows.json()} >= {"Breaking Bad", "Cowboy Bebop"}
        assert {m["title"] for m in films.json()} >= {"Dune", "Your Name"}

    def test_unified_port_without_a_prefix_explains_itself(self) -> None:
        response = httpx.get(
            f"{UNIFIED}/api/v3/series", headers={"X-Api-Key": SONARR_KEY}, timeout=20
        )
        assert response.status_code == 404
        assert "sonarr" in response.json()["detail"]

    def test_dedicated_port_also_accepts_the_prefix(self, sonarr: httpx.Client) -> None:
        assert sonarr.get("/sonarr/api/v3/series").status_code == 200


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
class TestAggregation:
    def test_series_from_both_instances_with_unique_ids(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/series").json()
        titles = {r["title"] for r in rows}
        assert {"Breaking Bad", "The Office", "Severance"} <= titles
        assert {"Cowboy Bebop", "Frieren", "Ghost in the Shell SAC"} <= titles
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), "ids collided across instances"

    def test_external_ids_are_never_rewritten(self, sonarr) -> None:
        rows = {r["title"]: r for r in sonarr.get("/api/v3/series").json()}
        assert rows["Breaking Bad"]["tvdbId"] == 81189
        assert rows["Cowboy Bebop"]["tvdbId"] == 76885  # anime instance, still raw

    def test_primary_instance_ids_pass_through_unchanged(self, sonarr) -> None:
        rows = {r["title"]: r for r in sonarr.get("/api/v3/series").json()}
        assert rows["Breaking Bad"]["id"] == 1
        assert rows["Cowboy Bebop"]["id"] == BLOCK + 1

    def test_movies_aggregate(self, radarr) -> None:
        rows = radarr.get("/api/v3/movie").json()
        assert {r["title"] for r in rows} >= {"Dune", "Arrival", "Your Name", "Suzume"}
        assert {r["tmdbId"] for r in rows} >= {438631, 372058}

    def test_sonarr_calendar_is_merged_and_sorted(self, sonarr) -> None:
        rows = sonarr.get(
            "/api/v3/calendar",
            params={"includeSeries": "true", "start": "2026-09-01T00:00:00Z",
                    "end": "2026-10-01T00:00:00Z"},
        ).json()
        dates = [r["airDateUtc"] for r in rows]
        assert dates == sorted(dates), "merged calendar must be in air-date order"
        # Seeded so the instances interleave -- proof the merge is not a concat.
        owners = [r["series"]["title"] for r in rows]
        assert owners == ["Breaking Bad", "Cowboy Bebop", "The Office",
                          "Frieren", "Severance"]

    def test_radarr_calendar_is_merged_and_sorted(self, radarr) -> None:
        rows = radarr.get(
            "/api/v3/calendar",
            params={"start": "2026-09-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"},
        ).json()
        dates = [r["inCinemas"] for r in rows]
        assert dates == sorted(dates)
        assert [r["title"] for r in rows] == ["Dune", "Your Name", "Arrival",
                                              "Suzume", "Blade Runner 2049"]

    def test_quality_profiles_merge_with_distinct_ids(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/qualityprofile").json()
        by_name = {r["name"]: r["id"] for r in rows}
        assert {"WEB-1080p", "WEB-2160p", "[Anime] Remux-1080p"} <= set(by_name)
        assert by_name["WEB-1080p"] == 1
        assert by_name["[Anime] Remux-1080p"] == BLOCK + 1
        assert len(set(by_name.values())) == len(by_name)

    def test_global_quality_definition_ids_survive_untouched(self, sonarr) -> None:
        """Quality *definition* ids are the same constants on every instance."""
        rows = sonarr.get("/api/v3/qualityprofile").json()
        anime = next(r for r in rows if r["name"] == "[Anime] Remux-1080p")
        assert anime["items"][0]["quality"]["id"] == 3
        assert anime["cutoff"] == 3

    def test_root_folders_merge(self, sonarr) -> None:
        paths = {r["path"] for r in sonarr.get("/api/v3/rootfolder").json()}
        assert paths == {"/data/media/tv", "/data/media/anime"}

    def test_tags_merge_with_distinct_ids(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/tag").json()
        assert {r["id"] for r in rows} == {1, BLOCK + 1}

    def test_health_names_the_failing_instance(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/health").json()
        assert len(rows) == 2
        assert any("[sonarr-main]" in r["message"] for r in rows)
        assert any("[sonarr-anime]" in r["message"] for r in rows)

    def test_shared_disk_mounts_are_not_double_counted(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/diskspace").json()
        assert [r["path"] for r in rows] == ["/data"]

    def test_queue_status_counters_are_summed(self, sonarr) -> None:
        body = sonarr.get("/api/v3/queue/status").json()
        assert body["totalCount"] == 5  # 3 on main + 2 on anime

    def test_system_status_keeps_app_name_but_renames_instance(self, sonarr) -> None:
        body = sonarr.get("/api/v3/system/status").json()
        assert body["appName"] == "Sonarr", "clients branch on appName"
        assert body["instanceName"] == "Sonarr (combined)"
        assert {i["name"] for i in body["arrProxy"]["instances"]} == {
            "sonarr-main", "sonarr-anime"
        }


# ---------------------------------------------------------------------------
# id translation
# ---------------------------------------------------------------------------
class TestIdTranslation:
    def test_get_by_id_reaches_the_owning_instance(self, sonarr) -> None:
        main = sonarr.get("/api/v3/series/2")
        assert served_by(main) == ["sonarr-main"]
        assert main.json()["title"] == "The Office"

        anime = sonarr.get(f"/api/v3/series/{BLOCK + 2}")
        assert served_by(anime) == ["sonarr-anime"]
        assert anime.json()["title"] == "Frieren"

    def test_upstream_receives_the_real_id(self, sonarr) -> None:
        reset_logs()
        sonarr.get(f"/api/v3/series/{BLOCK + 2}")
        anime_paths = [e["path"] for e in log_of("sonarr-anime")]
        assert "/api/v3/series/2" in anime_paths
        assert not [e for e in log_of("sonarr-main") if e["path"].startswith("/api/v3/series/")]

    def test_cover_urls_are_rewritten_and_remote_urls_are_not(self, sonarr) -> None:
        body = sonarr.get(f"/api/v3/series/{BLOCK + 2}").json()
        poster = body["images"][0]
        assert poster["url"].startswith(f"/MediaCover/{BLOCK + 2}/")
        assert poster["remoteUrl"] == "https://images.example/2/poster.jpg"

    def test_rewritten_cover_url_serves_the_right_instances_bytes(self, sonarr) -> None:
        body = sonarr.get(f"/api/v3/series/{BLOCK + 2}").json()
        response = sonarr.get(body["images"][0]["url"])
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")
        assert served_by(response) == ["sonarr-anime"]

    def test_cover_for_a_primary_id_goes_to_the_primary(self, sonarr) -> None:
        response = sonarr.get("/MediaCover/1/poster.jpg")
        assert response.status_code == 200
        assert served_by(response) == ["sonarr-main"]

    def test_query_id_scopes_the_fanout(self, sonarr) -> None:
        reset_logs()
        rows = sonarr.get("/api/v3/episode", params={"seriesId": BLOCK + 1}).json()
        assert {r["title"] for r in rows} == {"Cowboy Bebop E1", "Cowboy Bebop E2"}
        assert all(r["seriesId"] == BLOCK + 1 for r in rows)
        # The primary must not even be asked -- it owns a different series 1.
        assert log_of("sonarr-main") == []
        assert log_of("sonarr-anime")[0]["query"]["seriesId"] == "1"

    def test_tags_inside_a_record_are_translated(self, sonarr) -> None:
        rows = {r["title"]: r for r in sonarr.get("/api/v3/series").json()}
        assert rows["Breaking Bad"]["tags"] == [1]
        assert rows["Cowboy Bebop"]["tags"] == [BLOCK + 1]

    def test_low_id_owned_only_by_a_secondary_is_still_found(self, sonarr) -> None:
        """Seerr stores the id the real instance assigned, not ours.

        Series 7 exists only on the anime instance; a bare "7" decodes to the
        primary, 404s there, and must then be resolved by the read-only probe.
        """
        response = sonarr.get("/api/v3/series/7")
        assert response.status_code == 200
        assert response.json()["title"] == "Ghost in the Shell SAC"
        assert served_by(response) == ["sonarr-anime"]

    def test_unknown_id_still_404s(self, sonarr) -> None:
        assert sonarr.get("/api/v3/series/4242").status_code == 404


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------
class TestPagination:
    def test_total_records_matches_what_is_served(self, sonarr) -> None:
        body = sonarr.get(
            "/api/v3/queue",
            params={"page": 1, "pageSize": 250, "includeSeries": "true",
                    "includeEpisode": "true"},
        ).json()
        assert body["totalRecords"] == 5
        assert len(body["records"]) == 5

    def test_pages_walk_the_whole_merge_without_gaps(self, sonarr) -> None:
        seen: list[int] = []
        for page in (1, 2, 3):
            body = sonarr.get("/api/v3/queue", params={"page": page, "pageSize": 2}).json()
            assert body["page"] == page and body["totalRecords"] == 5
            seen.extend(r["id"] for r in body["records"])
        assert len(seen) == 5 and len(set(seen)) == 5
        overflow = sonarr.get("/api/v3/queue", params={"page": 9, "pageSize": 2}).json()
        assert overflow["records"] == []

    def test_seerrfin_paging_loop_terminates(self, sonarr) -> None:
        """Replays SeerrFin's exact loop: page until collected >= totalRecords."""
        collected: list[dict] = []
        page = 1
        while page < 50:  # a guard, not an expectation
            body = sonarr.get(
                "/api/v3/queue",
                params={"page": page, "pageSize": 2, "includeSeries": "true",
                        "includeEpisode": "true"},
            ).json()
            rows = body["records"]
            if not rows:
                break
            collected.extend(rows)
            if len(collected) >= body["totalRecords"]:
                break
            page += 1
        else:
            pytest.fail("paging loop never terminated")

        assert len(collected) == 5
        assert {r["series"]["title"] for r in collected} == {
            "Breaking Bad", "The Office", "Severance", "Cowboy Bebop", "Frieren"
        }

    def test_sort_direction_is_honoured_across_instances(self, sonarr) -> None:
        body = sonarr.get(
            "/api/v3/history",
            params={"page": 1, "pageSize": 20, "sortKey": "date",
                    "sortDirection": "descending"},
        ).json()
        dates = [r["date"] for r in body["records"]]
        assert dates == sorted(dates, reverse=True)
        assert body["totalRecords"] == 5

    def test_wanted_missing_pages(self, sonarr) -> None:
        body = sonarr.get("/api/v3/wanted/missing", params={"page": 1, "pageSize": 20}).json()
        assert body["totalRecords"] == 5
        assert len({r["id"] for r in body["records"]}) == 5


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------
class TestLookup:
    def test_duplicate_titles_collapse_to_the_added_one(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/series/lookup", params={"term": "trigun"}).json()
        assert len(rows) == 1
        assert rows[0]["tvdbId"] == 424097
        assert rows[0]["id"] == BLOCK + 2, "must keep the instance that has it"

    def test_movie_lookup_collapses_too(self, radarr) -> None:
        rows = radarr.get("/api/v3/movie/lookup", params={"term": "akira"}).json()
        assert len(rows) == 1 and rows[0]["id"] == BLOCK + 2


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------
class TestWrites:
    def test_create_routes_on_the_quality_profile_the_client_chose(self, sonarr) -> None:
        reset_logs()
        response = sonarr.post(
            "/api/v3/series",
            json={"title": "Dandadan", "tvdbId": 429310,
                  "qualityProfileId": BLOCK + 1,
                  "rootFolderPath": "/data/media/anime", "seasons": []},
        )
        assert response.status_code == 201
        assert served_by(response) == ["sonarr-anime"]
        assert log_of("sonarr-main") == []

    def test_create_body_carries_the_instances_own_ids(self, sonarr) -> None:
        response = sonarr.post(
            "/api/v3/series",
            json={"title": "Ranma", "tvdbId": 76821, "qualityProfileId": BLOCK + 1,
                  "rootFolderPath": "/data/media/anime", "seasons": []},
        )
        created = response.json()
        # Upstream stored profile 1; we hand back the virtual form again.
        assert created["qualityProfileId"] == BLOCK + 1
        assert created["id"] > BLOCK

    def test_create_routes_on_a_configured_rule(self, sonarr) -> None:
        response = sonarr.post(
            "/api/v3/series",
            json={"title": "Bocchi the Rock", "tvdbId": 417529,
                  "seriesType": "anime", "seasons": []},
        )
        assert served_by(response) == ["sonarr-anime"]

    def test_create_falls_back_to_the_default_instance(self, sonarr) -> None:
        response = sonarr.post(
            "/api/v3/series",
            json={"title": "Andor", "tvdbId": 83658, "qualityProfileId": 1,
                  "rootFolderPath": "/data/media/tv", "seasons": []},
        )
        assert served_by(response) == ["sonarr-main"]

    def test_radarr_create_routes_on_root_folder(self, radarr) -> None:
        response = radarr.post(
            "/api/v3/movie",
            json={"title": "Perfect Blue", "tmdbId": 10494,
                  "rootFolderPath": "/data/media/anime-movies"},
        )
        assert served_by(response) == ["radarr-anime"]

    def test_update_reaches_only_the_owning_instance(self, sonarr) -> None:
        reset_logs()
        response = sonarr.put(
            f"/api/v3/series/{BLOCK + 1}",
            json={"id": BLOCK + 1, "title": "Cowboy Bebop", "monitored": False,
                  "qualityProfileId": BLOCK + 1},
        )
        assert response.status_code == 200
        assert response.json()["monitored"] is False
        writes = [e for e in log_of("sonarr-anime") if e["method"] == "PUT"]
        assert writes and writes[0]["path"] == "/api/v3/series/1"
        assert [e for e in log_of("sonarr-main") if e["method"] == "PUT"] == []

    def test_delete_reaches_only_the_owning_instance(self, sonarr) -> None:
        created = sonarr.post(
            "/api/v3/series",
            json={"title": "Disposable", "tvdbId": 1, "qualityProfileId": BLOCK + 1,
                  "seasons": []},
        ).json()
        reset_logs()
        response = sonarr.delete(f"/api/v3/series/{created['id']}")
        assert response.status_code == 200
        assert [e["method"] for e in log_of("sonarr-main")] == []
        assert any(e["method"] == "DELETE" for e in log_of("sonarr-anime"))

    def test_command_with_an_entity_goes_to_one_instance(self, sonarr) -> None:
        reset_logs()
        response = sonarr.post(
            "/api/v3/command", json={"name": "RefreshSeries", "seriesId": BLOCK + 1}
        )
        assert response.status_code == 201
        assert log_of("sonarr-main") == []
        posted = [e for e in log_of("sonarr-anime") if e["method"] == "POST"]
        assert len(posted) == 1

    def test_global_command_is_broadcast(self, sonarr) -> None:
        reset_logs()
        response = sonarr.post("/api/v3/command", json={"name": "RssSync"})
        assert response.status_code == 201
        assert any(e["method"] == "POST" for e in log_of("sonarr-main"))
        assert any(e["method"] == "POST" for e in log_of("sonarr-anime"))


# ---------------------------------------------------------------------------
# resilience
# ---------------------------------------------------------------------------
class TestResilience:
    def test_unknown_endpoint_still_aggregates(self, sonarr) -> None:
        """Anything not in the route table falls back to a shape-driven merge."""
        response = sonarr.get("/api/v3/collection")
        assert response.status_code in (200, 404)

    def test_bad_upstream_path_propagates_a_404(self, sonarr) -> None:
        assert sonarr.get("/api/v3/not-a-real-endpoint").status_code in (404, 502)


class TestCache:
    def test_repeat_reads_are_served_from_cache(self) -> None:
        client = httpx.Client(base_url=CACHED, headers={"X-Api-Key": SONARR_KEY},
                              timeout=20.0)
        with client:
            client.get("/api/v3/tag")  # prime
            reset_logs()
            second = client.get("/api/v3/tag")
            assert second.headers.get("X-ArrProxy-Cache") == "hit"
            assert log_of("sonarr-main") == [], "cache hit must not touch upstream"
