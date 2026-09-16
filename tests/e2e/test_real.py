"""End-to-end suite against genuine Sonarr and Radarr containers.

The mock suite proves the merge logic in isolation; this one proves the proxy
against the real thing.  Both Sonarr instances are freshly installed, so each
has numbered its series, tags and quality profiles from 1 -- every id collides,
which is precisely the situation the proxy exists to resolve.

Ground truth comes from ``generated/facts.json`` and from querying the
instances directly, never from the proxy itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

FACTS = json.loads(Path("/generated/facts.json").read_text(encoding="utf-8"))

SONARR = os.environ.get("PROXY_SONARR", "http://proxy:8989")
RADARR = os.environ.get("PROXY_RADARR", "http://proxy:7878")
SONARR_KEY = "real-sonarr-combined-key-000001"
RADARR_KEY = "real-radarr-combined-key-000001"
BLOCK = 10_000_000

DIRECT = {
    "sonarr-main": "http://real-sonarr-main:8989",
    "sonarr-anime": "http://real-sonarr-anime:8989",
    "radarr-main": "http://real-radarr-main:7878",
    "radarr-anime": "http://real-radarr-anime:7878",
}


def direct(name: str, path: str, **kwargs) -> httpx.Response:
    """Talk to one instance without the proxy, for independent verification."""
    headers = {
        "X-Api-Key": FACTS[name]["api_key"],
        "Content-Type": "application/json",
    }
    headers.update(kwargs.pop("headers", {}))
    return httpx.request(
        kwargs.pop("method", "GET"),
        f"{DIRECT[name]}{path}",
        headers=headers,
        timeout=60.0,
        **kwargs,
    )


def served_by(response: httpx.Response) -> list[str]:
    raw = response.headers.get("X-ArrProxy-Instances", "")
    return [p for p in raw.split(",") if p]


@pytest.fixture(scope="module")
def sonarr() -> httpx.Client:
    with httpx.Client(base_url=SONARR, headers={"X-Api-Key": SONARR_KEY},
                      timeout=60.0) as client:
        yield client


@pytest.fixture(scope="module")
def radarr() -> httpx.Client:
    with httpx.Client(base_url=RADARR, headers={"X-Api-Key": RADARR_KEY},
                      timeout=60.0) as client:
        yield client


# ---------------------------------------------------------------------------
class TestRealInstancesActuallyCollide:
    """Establishes the premise: without the proxy these ids are ambiguous."""

    # Asserted as "the same id exists on both sides" rather than an exact list:
    # the write tests add titles, so an exact count would only hold on a run
    # against pristine volumes -- while the collision itself always holds.
    def test_both_sonarr_instances_own_a_series_1(self) -> None:
        assert 1 in FACTS["sonarr-main"]["library_ids"]
        assert 1 in FACTS["sonarr-anime"]["library_ids"]

    def test_both_sonarr_instances_own_a_tag_1(self) -> None:
        assert 1 in FACTS["sonarr-main"]["tag_ids"]
        assert 1 in FACTS["sonarr-anime"]["tag_ids"]

    def test_both_sonarr_instances_share_every_profile_id(self) -> None:
        assert FACTS["sonarr-main"]["profile_ids"] == [1, 2, 3, 4, 5, 6]
        assert FACTS["sonarr-anime"]["profile_ids"] == [1, 2, 3, 4, 5, 6]

    def test_both_radarr_instances_own_a_movie_1(self) -> None:
        assert 1 in FACTS["radarr-main"]["library_ids"]
        assert 1 in FACTS["radarr-anime"]["library_ids"]


class TestRealSonarr:
    def test_system_status_reports_a_real_version(self, sonarr) -> None:
        body = sonarr.get("/api/v3/system/status").json()
        assert body["appName"] == "Sonarr"
        assert body["version"].startswith("4.")
        assert body["instanceName"] == "Sonarr (combined)"
        assert len(body["arrProxy"]["instances"]) == 2

    def test_libraries_merge_without_collision(self, sonarr) -> None:
        # Keyed on tvdbId and written as a subset so the suite stays valid after
        # TestRealWrites has added a title, and on repeat runs against the same
        # volumes.
        rows = sonarr.get("/api/v3/series").json()
        by_tvdb = {r["tvdbId"]: r for r in rows}
        assert {81189, 76885} <= set(by_tvdb)
        assert by_tvdb[81189]["id"] == 1, "primary instance ids stay untouched"
        assert by_tvdb[76885]["id"] == BLOCK + 1, "secondary instance is namespaced"

        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)), "ids collided across instances"

        # The merged view must hold exactly what the instances hold, no more.
        upstream = sum(
            len(direct(name, "/api/v3/series").json())
            for name in ("sonarr-main", "sonarr-anime")
        )
        assert len(rows) == upstream

    def test_real_quality_profiles_merge(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/qualityprofile").json()
        assert len(rows) == 12, "6 defaults from each instance"
        ids = [r["id"] for r in rows]
        assert len(set(ids)) == 12, "ids must not collide after merging"
        assert set(ids) == set(range(1, 7)) | {BLOCK + n for n in range(1, 7)}
        # Same names on both sides -- only the ids are namespaced.
        assert [r["name"] for r in rows[:6]] == [r["name"] for r in rows[6:]]

    def test_real_quality_definition_ids_are_left_alone(self, sonarr) -> None:
        """Quality definitions are global constants, not per-instance rows."""
        rows = sonarr.get("/api/v3/qualityprofile").json()
        anime = next(r for r in rows if r["id"] > BLOCK)
        found = [
            item["quality"]["id"]
            for group in anime["items"]
            for item in ([group] if "quality" in group and group["quality"] else group.get("items", []))
            if item.get("quality")
        ]
        assert found, "expected quality entries inside the profile"
        assert max(found) < 100, "definition ids must not be shifted into id space"

    def test_tags_merge(self, sonarr) -> None:
        rows = {r["id"]: r["label"] for r in sonarr.get("/api/v3/tag").json()}
        assert rows == {1: "mainshows", BLOCK + 1: "animeshows"}

    def test_root_folders_merge(self, sonarr) -> None:
        paths = {r["path"] for r in sonarr.get("/api/v3/rootfolder").json()}
        assert paths == {"/data/tv", "/data/anime"}

    def test_get_by_id_reaches_the_right_library(self, sonarr) -> None:
        main = sonarr.get("/api/v3/series/1")
        assert main.json()["title"] == "Breaking Bad"
        assert served_by(main) == ["sonarr-main"]

        anime = sonarr.get(f"/api/v3/series/{BLOCK + 1}")
        assert anime.json()["title"] == "Cowboy Bebop"
        assert served_by(anime) == ["sonarr-anime"]

    def test_home_sections_calendar_call(self, sonarr) -> None:
        """The exact request jellyfin-plugin-home-sections makes."""
        response = sonarr.get(
            "/api/v3/calendar",
            params={"includeSeries": "true", "unmonitored": "true",
                    "start": "1998-01-01T00:00:00Z", "end": "2013-12-31T00:00:00Z"},
        )
        rows = response.json()
        assert len(rows) > 50, "expected real episodes from both instances"
        dates = [r["airDateUtc"] for r in rows]
        assert dates == sorted(dates), "merged calendar must be ordered by air date"
        assert {r["series"]["title"] for r in rows} >= {"Breaking Bad", "Cowboy Bebop"}
        assert {1, BLOCK + 1} <= {r["seriesId"] for r in rows}
        assert sorted(served_by(response)) == ["sonarr-anime", "sonarr-main"]
        # Fields the plugin reads must all survive the merge.
        sample = rows[0]
        for field in ("id", "title", "monitored", "hasFile", "seasonNumber",
                      "episodeNumber", "airDateUtc"):
            assert field in sample
        assert "path" in sample["series"] and "images" in sample["series"]

    def test_episodes_scope_to_one_instance(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/episode", params={"seriesId": BLOCK + 1}).json()
        assert rows, "Cowboy Bebop should have episodes"
        assert {r["seriesId"] for r in rows} == {BLOCK + 1}
        assert all(r["id"] > BLOCK for r in rows)
        # And the real ids upstream are small -- confirm the offset is exact.
        upstream = direct("sonarr-anime", "/api/v3/episode?seriesId=1").json()
        assert len(upstream) == len(rows)
        assert {r["id"] for r in rows} == {r["id"] + BLOCK for r in upstream}

    def test_media_cover_urls_are_rewritten_and_resolve(self, sonarr) -> None:
        body = sonarr.get(f"/api/v3/series/{BLOCK + 1}").json()
        posters = [i for i in body["images"] if i["coverType"] == "poster"]
        assert posters, "real Sonarr returns a poster entry"
        url = posters[0]["url"]
        assert f"/{BLOCK + 1}/" in url, f"cover url was not namespaced: {url}"
        assert posters[0]["remoteUrl"].startswith("http"), "remote url must stay external"

        fetched = sonarr.get(url)
        assert fetched.status_code == 200
        assert fetched.headers["content-type"].startswith("image/")
        assert served_by(fetched) == ["sonarr-anime"]

    def test_seerrfin_queue_call(self, sonarr) -> None:
        """SeerrFin pages until collected >= totalRecords; the envelope must agree."""
        response = sonarr.get(
            "/api/v3/queue",
            params={"page": 1, "pageSize": 250, "includeSeries": "true",
                    "includeEpisode": "true"},
        )
        body = response.json()
        assert set(body) >= {"page", "pageSize", "totalRecords", "records"}
        assert body["totalRecords"] == len(body["records"])
        assert sorted(served_by(response)) == ["sonarr-anime", "sonarr-main"]

    def test_seerrfin_series_list_call(self, sonarr) -> None:
        """SeerrFin matches requests to library rows by tmdbId over the full list."""
        rows = sonarr.get("/api/v3/series").json()
        assert all("tvdbId" in r for r in rows)
        assert all(isinstance(r.get("statistics", {}), dict) for r in rows)

    def test_history_paging_envelope(self, sonarr) -> None:
        body = sonarr.get("/api/v3/history", params={"page": 1, "pageSize": 20}).json()
        assert body["totalRecords"] == len(body["records"])

    def test_lookup_prefers_the_instance_that_has_it(self, sonarr) -> None:
        """Both instances resolve tvdb:76885; only the anime one has it added."""
        rows = sonarr.get("/api/v3/series/lookup", params={"term": "tvdb:76885"}).json()
        assert len(rows) == 1, "the duplicate must collapse"
        assert rows[0]["tvdbId"] == 76885
        assert rows[0]["id"] == BLOCK + 1, "must keep the row that is already added"

    def test_health_is_attributed_per_instance(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/health").json()
        if rows:
            assert any(r["message"].startswith("[sonarr-") for r in rows)

    def test_disk_space_is_not_double_counted(self, sonarr) -> None:
        rows = sonarr.get("/api/v3/diskspace").json()
        paths = [r["path"] for r in rows]
        assert len(paths) == len(set(paths)), "shared mounts must collapse"


class TestRealWrites:
    def test_create_routes_by_the_chosen_quality_profile(self, sonarr) -> None:
        """Adding with an anime-instance profile id must land on that instance."""
        before = {r["tvdbId"] for r in direct("sonarr-anime", "/api/v3/series").json()}
        if 424536 not in before:
            hits = sonarr.get(
                "/api/v3/series/lookup", params={"term": "tvdb:424536"}
            ).json()
            assert hits, "lookup for Frieren failed"
            payload = dict(hits[0])
            payload.update({
                "qualityProfileId": BLOCK + 4,   # HD-1080p on the anime instance
                "rootFolderPath": "/data/anime",
                "monitored": False,
                "seasonFolder": True,
                "addOptions": {"monitor": "none", "searchForMissingEpisodes": False},
            })
            response = sonarr.post("/api/v3/series", json=payload)
            assert response.status_code in (200, 201), response.text
            assert served_by(response) == ["sonarr-anime"]

        anime_ids = {r["tvdbId"] for r in direct("sonarr-anime", "/api/v3/series").json()}
        main_ids = {r["tvdbId"] for r in direct("sonarr-main", "/api/v3/series").json()}
        assert 424536 in anime_ids, "Frieren should be on the anime instance"
        assert 424536 not in main_ids, "and must not have leaked onto the main one"

    def test_update_reaches_only_the_owning_instance(self, sonarr) -> None:
        current = sonarr.get(f"/api/v3/series/{BLOCK + 1}").json()
        target = not current["monitored"]
        updated = dict(current)
        updated["monitored"] = target

        response = sonarr.put(f"/api/v3/series/{BLOCK + 1}", json=updated)
        assert response.status_code in (200, 202), response.text

        # Verify on the instances themselves, not through the proxy.
        anime = direct("sonarr-anime", "/api/v3/series/1").json()
        main = direct("sonarr-main", "/api/v3/series/1").json()
        assert anime["monitored"] is target
        assert main["title"] == "Breaking Bad", "the primary must be untouched"

    def test_global_command_reaches_both_instances(self, sonarr) -> None:
        response = sonarr.post("/api/v3/command", json={"name": "RefreshMonitoredDownloads"})
        assert response.status_code in (200, 201), response.text


class TestRealRadarr:
    def test_movies_merge_without_collision(self, radarr) -> None:
        # Keyed on tmdbId, not title: TMDB owns the display title and renders
        # this one as "Your Name." -- the external id is the stable identity.
        rows = {r["tmdbId"]: r for r in radarr.get("/api/v3/movie").json()}
        assert set(rows) == {438631, 372058}
        assert rows[438631]["id"] == 1, "primary instance ids stay untouched"
        assert rows[372058]["id"] == BLOCK + 1, "secondary instance is namespaced"

    def test_profiles_merge(self, radarr) -> None:
        rows = radarr.get("/api/v3/qualityprofile").json()
        assert len(rows) == 12
        assert len({r["id"] for r in rows}) == 12

    def test_get_by_id_routes(self, radarr) -> None:
        anime = radarr.get(f"/api/v3/movie/{BLOCK + 1}")
        assert anime.json()["tmdbId"] == 372058
        assert served_by(anime) == ["radarr-anime"]

        main = radarr.get("/api/v3/movie/1")
        assert main.json()["tmdbId"] == 438631
        assert served_by(main) == ["radarr-main"]

    def test_lookup_prefers_the_added_instance(self, radarr) -> None:
        rows = radarr.get("/api/v3/movie/lookup", params={"term": "tmdb:372058"}).json()
        assert len(rows) == 1 and rows[0]["id"] == BLOCK + 1

    def test_calendar_shape(self, radarr) -> None:
        response = radarr.get(
            "/api/v3/calendar",
            params={"start": "2016-01-01T00:00:00Z", "end": "2022-12-31T00:00:00Z"},
        )
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_queue_envelope(self, radarr) -> None:
        body = radarr.get(
            "/api/v3/queue", params={"page": 1, "pageSize": 250, "includeMovie": "true"}
        ).json()
        assert body["totalRecords"] == len(body["records"])


class TestRealAuth:
    def test_key_is_required(self) -> None:
        assert httpx.get(f"{SONARR}/api/v3/series", timeout=30).status_code == 401

    def test_instance_keys_are_not_accepted_by_the_proxy(self) -> None:
        """A backing instance's own key must not unlock the combined endpoint."""
        response = httpx.get(
            f"{SONARR}/api/v3/series",
            headers={"X-Api-Key": FACTS["sonarr-main"]["api_key"]}, timeout=30,
        )
        assert response.status_code == 401

    def test_combined_key_is_not_forwarded(self, sonarr) -> None:
        """Instances only ever see their own key, so they must reject ours."""
        response = direct(
            "sonarr-main", "/api/v3/series",
            headers={"X-Api-Key": SONARR_KEY, "Content-Type": "application/json"},
        )
        assert response.status_code == 401


class TestRealBrowserDeepLinks:
    """SeerrFin's "Open in Sonarr/Radarr" buttons point at {base}/series/{slug}.

    The slug is generated by the *arr itself, so this reads it back from the
    live API rather than assuming a slugification rule.
    """

    def test_series_deep_link_reaches_the_owning_instance(self, sonarr) -> None:
        rows = {r["tvdbId"]: r for r in sonarr.get("/api/v3/series").json()}
        anime_slug = rows[76885]["titleSlug"]      # Cowboy Bebop, anime instance
        main_slug = rows[81189]["titleSlug"]       # Breaking Bad, main instance
        assert anime_slug and main_slug and anime_slug != main_slug

        anime = httpx.get(f"{SONARR}/series/{anime_slug}",
                          follow_redirects=False, timeout=30)
        assert anime.status_code == 302
        assert served_by(anime) == ["sonarr-anime"]
        assert anime.headers["location"].endswith(f"/series/{anime_slug}")

        main = httpx.get(f"{SONARR}/series/{main_slug}",
                         follow_redirects=False, timeout=30)
        assert main.status_code == 302
        assert served_by(main) == ["sonarr-main"]

    def test_the_redirect_target_actually_serves_that_page(self, sonarr) -> None:
        """Follow the redirect for real: the instance must answer it."""
        rows = {r["tvdbId"]: r for r in sonarr.get("/api/v3/series").json()}
        slug = rows[76885]["titleSlug"]
        hop = httpx.get(f"{SONARR}/series/{slug}", follow_redirects=False, timeout=30)
        landed = httpx.get(hop.headers["location"], timeout=30)
        assert landed.status_code == 200
        assert "html" in landed.headers.get("content-type", "").lower()

    def test_movie_deep_link(self, radarr) -> None:
        rows = {r["tmdbId"]: r for r in radarr.get("/api/v3/movie").json()}
        slug = rows[372058]["titleSlug"]           # Your Name, anime instance
        response = httpx.get(f"{RADARR}/movie/{slug}",
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302
        assert served_by(response) == ["radarr-anime"]

    def test_add_new_link_lands_on_a_real_instance(self) -> None:
        hop = httpx.get(f"{SONARR}/add/new", params={"term": "tmdb:1234"},
                        follow_redirects=False, timeout=30)
        assert hop.status_code == 302
        landed = httpx.get(hop.headers["location"], timeout=30)
        assert landed.status_code == 200

    def test_deep_links_do_not_need_a_key(self) -> None:
        response = httpx.get(f"{SONARR}/series/anything",
                             follow_redirects=False, timeout=30)
        assert response.status_code == 302

    def test_the_api_is_still_protected(self) -> None:
        assert httpx.get(f"{SONARR}/api/v3/series", timeout=30).status_code == 401
