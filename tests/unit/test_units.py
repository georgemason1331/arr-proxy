"""Unit tests for the pure logic: id translation, merging, config, routing."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from arrproxy import config as cfg
from arrproxy import merge
from arrproxy.idmap import IdMapper
from arrproxy.routing import AppRouter
from arrproxy.upstream import Reply

BLOCK = 10_000_000


def reply(instance_index: int, payload, *, status: int = 200, name: str | None = None) -> Reply:
    inst = cfg.Instance(
        name=name or f"inst{instance_index}",
        url=f"http://host{instance_index}",
        api_key="k",
        index=instance_index,
    )
    return Reply(
        instance=inst,
        status=status,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode(),
    )


# ---------------------------------------------------------------------------
class TestIdMapper:
    @pytest.fixture()
    def mapper(self) -> IdMapper:
        return IdMapper("sonarr", BLOCK)

    def test_primary_instance_is_identity_mapped(self, mapper) -> None:
        assert mapper.to_virtual(1, 0) == 1
        assert mapper.to_virtual(999, 0) == 999

    def test_secondary_instances_are_offset(self, mapper) -> None:
        assert mapper.to_virtual(1, 1) == BLOCK + 1
        assert mapper.to_virtual(1, 2) == 2 * BLOCK + 1

    @pytest.mark.parametrize("real,index", [(1, 0), (42, 1), (999999, 2), (5, 7)])
    def test_round_trip(self, mapper, real, index) -> None:
        assert mapper.to_real(mapper.to_virtual(real, index)) == (index, real)

    @pytest.mark.parametrize("sentinel", [0, -1])
    def test_sentinels_are_preserved(self, mapper, sentinel) -> None:
        """0 means "unset" and is never a row; shifting it would invent one."""
        assert mapper.to_virtual(sentinel, 3) == sentinel

    def test_booleans_are_not_treated_as_ids(self, mapper) -> None:
        assert mapper.to_virtual(True, 1) is True

    def test_external_ids_are_untouched(self, mapper) -> None:
        doc = {"id": 4, "tvdbId": 81189, "tmdbId": 1, "imdbId": "tt1", "tvRageId": 7}
        out = mapper.encode(doc, 1)
        assert out["id"] == BLOCK + 4
        assert out["tvdbId"] == 81189 and out["tmdbId"] == 1 and out["tvRageId"] == 7
        assert out["imdbId"] == "tt1"

    def test_global_quality_ids_are_untouched(self, mapper) -> None:
        doc = {"id": 1, "quality": {"quality": {"id": 3}, "revision": {"version": 1}}}
        assert mapper.encode(doc, 1)["quality"]["quality"]["id"] == 3

    def test_language_ids_are_untouched(self, mapper) -> None:
        doc = {"id": 1, "language": {"id": 1, "name": "English"},
               "languages": [{"id": 2, "name": "Japanese"}]}
        out = mapper.encode(doc, 1)
        assert out["language"]["id"] == 1 and out["languages"][0]["id"] == 2

    def test_tag_lists_are_translated(self, mapper) -> None:
        assert mapper.encode({"tags": [1, 2]}, 1)["tags"] == [BLOCK + 1, BLOCK + 2]

    def test_string_tags_survive(self, mapper) -> None:
        """Some endpoints return tag labels rather than ids."""
        assert mapper.encode({"tags": ["anime", "hd"]}, 1)["tags"] == ["anime", "hd"]

    def test_nested_records_are_translated(self, mapper) -> None:
        doc = {"id": 5, "series": {"id": 5, "title": "x"},
               "episode": {"id": 7, "seriesId": 5}}
        out = mapper.encode(doc, 1)
        assert out["series"]["id"] == BLOCK + 5
        assert out["episode"]["seriesId"] == BLOCK + 5

    def test_media_cover_urls_are_rewritten(self, mapper) -> None:
        doc = {"images": [{"url": "/MediaCover/5/poster.jpg?lastWrite=1",
                           "remoteUrl": "https://x/5/poster.jpg"}]}
        out = mapper.encode(doc, 1)
        assert out["images"][0]["url"] == f"/MediaCover/{BLOCK + 5}/poster.jpg?lastWrite=1"
        assert out["images"][0]["remoteUrl"] == "https://x/5/poster.jpg"

    def test_api_prefixed_cover_urls_are_rewritten(self, mapper) -> None:
        got = mapper.rewrite_cover_url("/api/v3/mediacover/5/banner.jpg", 2)
        assert got == f"/api/v3/mediacover/{2 * BLOCK + 5}/banner.jpg"

    def test_decode_reverses_encode(self, mapper) -> None:
        doc = {"id": 3, "seriesId": 3, "tags": [1], "qualityProfileId": 2,
               "tvdbId": 99, "quality": {"quality": {"id": 3}}}
        assert mapper.decode(mapper.encode(doc, 1)) == doc

    def test_ambiguity_is_flagged_only_below_one_block(self, mapper) -> None:
        assert mapper.is_ambiguous(7) is True
        assert mapper.is_ambiguous(BLOCK + 7) is False

    def test_radarr_keys_differ_from_sonarr(self) -> None:
        radarr = IdMapper("radarr", BLOCK)
        out = radarr.encode({"movieId": 2, "movieFileId": 3, "collectionId": 4}, 1)
        assert out == {"movieId": BLOCK + 2, "movieFileId": BLOCK + 3,
                       "collectionId": BLOCK + 4}


# ---------------------------------------------------------------------------
class TestMerge:
    @pytest.fixture()
    def mapper(self) -> IdMapper:
        return IdMapper("sonarr", BLOCK)

    def test_lists_concatenate_with_translation(self, mapper) -> None:
        out = merge.merge_list(
            [reply(0, [{"id": 1}]), reply(1, [{"id": 1}])], mapper
        )
        assert [r["id"] for r in out] == [1, BLOCK + 1]

    def test_failed_instances_are_skipped(self, mapper) -> None:
        bad = reply(1, None, status=500)
        bad.error = "boom"
        out = merge.merge_list([reply(0, [{"id": 1}]), bad], mapper)
        assert [r["id"] for r in out] == [1]

    def test_sorting_interleaves_instances(self, mapper) -> None:
        out = merge.merge_list(
            [reply(0, [{"id": 1, "d": "2026-01-01"}, {"id": 2, "d": "2026-01-03"}]),
             reply(1, [{"id": 1, "d": "2026-01-02"}])],
            mapper, sort_key="d",
        )
        assert [r["d"] for r in out] == ["2026-01-01", "2026-01-02", "2026-01-03"]

    def test_sorting_tolerates_missing_and_mixed_values(self, mapper) -> None:
        out = merge.merge_list(
            [reply(0, [{"id": 1}, {"id": 2, "d": 5}, {"id": 3, "d": "abc"}])],
            mapper, sort_key="d",
        )
        assert [r["id"] for r in out] == [2, 3, 1], "missing values sort last"

    def test_tuple_sort_key_takes_the_first_present(self, mapper) -> None:
        rows = [{"id": 1, "b": "2026-05-05"}, {"id": 2, "a": "2026-01-01"}]
        out = merge.merge_list([reply(0, rows)], mapper, sort_key=("a", "b"))
        assert [r["id"] for r in out] == [2, 1]

    def test_dotted_sort_key(self, mapper) -> None:
        rows = [{"id": 1, "s": {"t": "b"}}, {"id": 2, "s": {"t": "a"}}]
        out = merge.merge_list([reply(0, rows)], mapper, sort_key="s.t")
        assert [r["id"] for r in out] == [2, 1]

    def test_paged_total_is_the_sum_of_responders(self, mapper) -> None:
        body = merge.merge_paged(
            [reply(0, {"totalRecords": 3, "records": [{"id": 1}, {"id": 2}, {"id": 3}]}),
             reply(1, {"totalRecords": 2, "records": [{"id": 1}, {"id": 2}]})],
            mapper, page=1, page_size=10, sort_key=None, descending=False,
        )
        assert body["totalRecords"] == 5 and len(body["records"]) == 5

    def test_paged_total_excludes_unreachable_instances(self, mapper) -> None:
        """An inflated total would make a paging client loop forever."""
        dead = reply(1, None, status=0)
        dead.error = "connect"
        body = merge.merge_paged(
            [reply(0, {"totalRecords": 2, "records": [{"id": 1}, {"id": 2}]}), dead],
            mapper, page=1, page_size=10, sort_key=None, descending=False,
        )
        assert body["totalRecords"] == 2 == len(body["records"])

    def test_paged_windows_correctly(self, mapper) -> None:
        left = {"totalRecords": 3, "records": [{"id": 1}, {"id": 2}, {"id": 3}]}
        right = {"totalRecords": 2, "records": [{"id": 1}, {"id": 2}]}
        seen: list[int] = []
        for page in (1, 2, 3):
            body = merge.merge_paged(
                [reply(0, left), reply(1, right)],
                mapper, page=page, page_size=2, sort_key=None, descending=False,
            )
            seen.extend(r["id"] for r in body["records"])
        assert len(seen) == 5 and len(set(seen)) == 5

    def test_paged_past_the_end_is_empty(self, mapper) -> None:
        body = merge.merge_paged(
            [reply(0, {"totalRecords": 1, "records": [{"id": 1}]})],
            mapper, page=9, page_size=10, sort_key=None, descending=False,
        )
        assert body["records"] == []

    def test_lookup_keeps_the_instance_that_has_the_title(self, mapper) -> None:
        out = merge.merge_lookup(
            [reply(0, [{"tvdbId": 42, "id": 0, "title": "X"}]),
             reply(1, [{"tvdbId": 42, "id": 3, "title": "X"}])],
            mapper,
        )
        assert len(out) == 1 and out[0]["id"] == BLOCK + 3

    def test_lookup_keeps_distinct_titles(self, mapper) -> None:
        out = merge.merge_lookup(
            [reply(0, [{"tvdbId": 1, "id": 0}]), reply(1, [{"tvdbId": 2, "id": 0}])],
            mapper,
        )
        assert len(out) == 2

    def test_health_is_attributed(self, mapper) -> None:
        out = merge.merge_health(
            [reply(0, [{"source": "S", "message": "m"}], name="main")], mapper
        )
        assert out[0]["message"] == "[main] m" and out[0]["source"] == "main:S"

    def test_shared_mounts_collapse(self, mapper) -> None:
        out = merge.merge_diskspace(
            [reply(0, [{"path": "/data", "freeSpace": 1}]),
             reply(1, [{"path": "/data", "freeSpace": 1}])],
            mapper,
        )
        assert len(out) == 1

    def test_counters_sum(self, mapper) -> None:
        out = merge.merge_counters(
            [reply(0, {"totalCount": 2, "errors": False}),
             reply(1, {"totalCount": 3, "errors": True})],
            mapper,
        )
        assert out["totalCount"] == 5 and out["errors"] is True

    def test_auto_detects_lists(self, mapper) -> None:
        out = merge.merge_auto(
            [reply(0, [{"id": 1}]), reply(1, [{"id": 1}])],
            mapper, page=1, page_size=10, sort_key=None, descending=False,
        )
        assert [r["id"] for r in out] == [1, BLOCK + 1]

    def test_auto_detects_paged_envelopes(self, mapper) -> None:
        out = merge.merge_auto(
            [reply(0, {"totalRecords": 1, "records": [{"id": 1}]}),
             reply(1, {"totalRecords": 1, "records": [{"id": 1}]})],
            mapper, page=1, page_size=10, sort_key=None, descending=False,
        )
        assert out["totalRecords"] == 2

    def test_auto_falls_back_to_the_first_object(self, mapper) -> None:
        out = merge.merge_auto(
            [reply(0, {"urlBase": "a"}), reply(1, {"urlBase": "b"})],
            mapper, page=1, page_size=10, sort_key=None, descending=False,
        )
        assert out == {"urlBase": "a"}


# ---------------------------------------------------------------------------
BASE_CONFIG = """
server:
  unified_port: 8787
apps:
  sonarr:
    api_key: {key}
    instances:
      - name: main
        url: http://sonarr:8989
        api_key: a
        default: true
      - name: anime
        url: http://sonarr-anime:8989
        api_key: b
        routing:
          series_types: [anime]
          root_folders: ["/data/media/anime"]
"""


def write(tmp_path: Path, text: str) -> Path:
    target = tmp_path / "config.yaml"
    target.write_text(text, encoding="utf-8")
    return target


class TestConfig:
    def test_loads_a_valid_file(self, tmp_path) -> None:
        settings = cfg.load(write(tmp_path, BASE_CONFIG.format(key="k" * 12)))
        app = settings.apps["sonarr"]
        assert app.api_key == "k" * 12
        assert [i.name for i in app.instances] == ["main", "anime"]
        assert [i.index for i in app.instances] == [0, 1]
        assert app.default_instance.name == "main"
        assert app.port == 8989

    def test_missing_key_is_generated_and_persisted(self, tmp_path) -> None:
        path = write(tmp_path, BASE_CONFIG.replace("    api_key: {key}\n", ""))
        first = cfg.load(path)
        generated = first.apps["sonarr"].api_key
        assert len(generated) >= 16
        # A key that changed on restart would break every configured client.
        assert cfg.load(path).apps["sonarr"].api_key == generated

    def test_env_vars_expand(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("MY_KEY", "from-env-key")
        text = BASE_CONFIG.format(key="${MY_KEY}")
        assert cfg.load(write(tmp_path, text)).apps["sonarr"].api_key == "from-env-key"

    def test_env_var_default_is_used(self, tmp_path) -> None:
        text = BASE_CONFIG.format(key="${ABSENT_VAR:-fallback-key}")
        assert cfg.load(write(tmp_path, text)).apps["sonarr"].api_key == "fallback-key"

    @pytest.mark.parametrize(
        "key",
        [
            "change-me-to-a-long-random-string",
            "CHANGE-ME-please-1234",
            # What the old example config fell back to when the variable was unset.
            "${ABSENT_VAR:-change-me-to-a-long-random-string}",
        ],
    )
    def test_the_published_placeholder_is_refused(self, tmp_path, key) -> None:
        """Earlier example configs shipped these strings, so they are not secrets."""
        with pytest.raises(cfg.ConfigError, match="placeholder"):
            cfg.load(write(tmp_path, BASE_CONFIG.format(key=key)))

    @pytest.mark.parametrize("empty", [False, True], ids=["unset", "empty"])
    def test_an_unset_key_variable_generates_a_key(self, tmp_path, monkeypatch, empty) -> None:
        """Compose passes an empty string for a variable missing from .env."""
        if empty:
            monkeypatch.setenv("COMBINED_KEY", "")
        else:
            monkeypatch.delenv("COMBINED_KEY", raising=False)
        path = write(tmp_path, BASE_CONFIG.format(key="${COMBINED_KEY}"))
        generated = cfg.load(path).apps["sonarr"].api_key
        assert re.fullmatch(r"[0-9a-f]{32}", generated)
        assert cfg.load(path).apps["sonarr"].api_key == generated

    def test_saving_a_generated_key_writes_no_other_secret(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("INSTANCE_KEY", "instance-secret-value")
        text = BASE_CONFIG.replace("    api_key: {key}\n", "").replace(
            "api_key: a", "api_key: ${INSTANCE_KEY}"
        )
        path = write(tmp_path, text)
        cfg.load(path)
        saved = path.read_text(encoding="utf-8")
        assert "${INSTANCE_KEY}" in saved, "references stay references"
        assert "instance-secret-value" not in saved

    @pytest.mark.parametrize(
        "mutation,message",
        [
            ("      - name: main\n        url: http://a\n        api_key: a\n"
             "      - name: main\n        url: http://b\n        api_key: b\n", "duplicate"),
            ("      - name: x\n        url: sonarr:8989\n        api_key: a\n", "http://"),
            ("      - name: x\n        url: http://a\n", "api_key"),
            ("      - name: x\n        api_key: a\n", "url"),
        ],
    )
    def test_invalid_instances_are_rejected(self, tmp_path, mutation, message) -> None:
        text = BASE_CONFIG.format(key="k" * 12).split("    instances:")[0] \
            + "    instances:\n" + mutation
        with pytest.raises(cfg.ConfigError, match=message):
            cfg.load(write(tmp_path, text))

    def test_two_defaults_are_rejected(self, tmp_path) -> None:
        text = BASE_CONFIG.format(key="k" * 12).replace(
            "        api_key: b\n", "        api_key: b\n        default: true\n"
        )
        with pytest.raises(cfg.ConfigError, match="default"):
            cfg.load(write(tmp_path, text))

    def test_port_collision_is_rejected(self, tmp_path) -> None:
        text = BASE_CONFIG.format(key="k" * 12).replace(
            "  unified_port: 8787", "  unified_port: 8989"
        )
        with pytest.raises(cfg.ConfigError, match="collide"):
            cfg.load(write(tmp_path, text))

    def test_unknown_app_type_is_rejected(self, tmp_path) -> None:
        text = BASE_CONFIG.format(key="k" * 12).replace("  sonarr:", "  sonaar:")
        with pytest.raises(cfg.ConfigError, match="unknown app type"):
            cfg.load(write(tmp_path, text))

    def test_empty_instance_list_is_rejected(self, tmp_path) -> None:
        with pytest.raises(cfg.ConfigError, match="no instances"):
            cfg.load(write(tmp_path, "apps:\n  sonarr:\n    api_key: kkkkkkkkkkkk\n"))

    def test_missing_file_is_reported_clearly(self, tmp_path) -> None:
        with pytest.raises(cfg.ConfigError, match="not found"):
            cfg.load(tmp_path / "nope.yaml")

    def test_a_directory_is_reported_clearly(self, tmp_path) -> None:
        """Docker creates a directory when a bind-mount source is missing, so
        this is the usual first-run mistake and deserves a real message."""
        (tmp_path / "config.yaml").mkdir()
        with pytest.raises(cfg.ConfigError, match="is a directory"):
            cfg.load(tmp_path / "config.yaml")

    def test_malformed_yaml_is_reported_clearly(self, tmp_path) -> None:
        with pytest.raises(cfg.ConfigError, match="not valid YAML"):
            cfg.load(write(tmp_path, "apps:\n  sonarr:\n   - [unclosed\n"))


EXAMPLE_CONFIG = Path(
    os.environ.get("ARRPROXY_EXAMPLE_CONFIG")
    or Path(__file__).resolve().parents[2] / "config.example.yaml"
)


class TestShippedExample:
    """config.example.yaml is what people copy, so it has to work as documented."""

    INSTANCE_KEYS = ("SONARR_API_KEY", "SONARR_ANIME_API_KEY", "RADARR_API_KEY", "RADARR_ANIME_API_KEY")

    @pytest.fixture
    def example(self, tmp_path, monkeypatch) -> Path:
        for var in self.INSTANCE_KEYS:
            monkeypatch.setenv(var, f"{var.lower()}-value")
        for var in ("ARRPROXY_SONARR_KEY", "ARRPROXY_RADARR_KEY"):
            monkeypatch.delenv(var, raising=False)
        return write(tmp_path, EXAMPLE_CONFIG.read_text(encoding="utf-8"))

    def test_it_loads(self, example) -> None:
        settings = cfg.load(example)
        assert set(settings.apps) == {"sonarr", "radarr"}
        for app in settings.apps.values():
            assert len(app.instances) == 2
            assert app.default_instance is app.instances[0]

    def test_unset_combined_keys_are_generated_never_defaulted(self, example) -> None:
        keys = [app.api_key for app in cfg.load(example).apps.values()]
        assert all(re.fullmatch(r"[0-9a-f]{32}", key) for key in keys), keys
        assert len(set(keys)) == len(keys), "each app gets its own key"
        saved = example.read_text(encoding="utf-8")
        for var in self.INSTANCE_KEYS:
            assert "${%s}" % var in saved
            assert f"{var.lower()}-value" not in saved


class TestRoutingRules:
    def test_series_type_matches(self) -> None:
        assert cfg.Routing(series_types=["anime"]).matches({"seriesType": "Anime"})

    def test_genre_matches(self) -> None:
        assert cfg.Routing(genres=["Animation"]).matches({"genres": ["Drama", "animation"]})

    def test_root_folder_matches_ignoring_trailing_slash(self) -> None:
        rule = cfg.Routing(root_folders=["/data/media/anime"])
        assert rule.matches({"rootFolderPath": "/data/media/anime/"})

    def test_title_regex_matches(self) -> None:
        assert cfg.Routing(title_regex=r"^\[anime\]").matches({"title": "[Anime] Bebop"})

    def test_empty_rule_never_matches(self) -> None:
        assert not cfg.Routing().matches({"title": "anything"})


# ---------------------------------------------------------------------------
def build_router(tmp_path: Path) -> AppRouter:
    settings = cfg.load(write(tmp_path, BASE_CONFIG.format(key="k" * 12)))
    return AppRouter(settings, settings.apps["sonarr"], upstream=None)  # type: ignore[arg-type]


class TestRouter:
    @pytest.fixture()
    def router(self, tmp_path) -> AppRouter:
        return build_router(tmp_path)

    @pytest.mark.parametrize(
        "method,path,kind",
        [
            ("GET", "/api/v3/series", "agg"),
            ("POST", "/api/v3/series", "create"),
            ("GET", "/api/v3/series/5", "byid"),
            ("GET", "/api/v3/series/lookup", "lookup"),
            ("GET", "/api/v3/calendar", "agg"),
            ("GET", "/api/v3/queue", "paged"),
            ("GET", "/api/v3/history", "paged"),
            ("GET", "/api/v3/wanted/missing", "paged"),
            ("GET", "/api/v3/health", "health"),
            ("GET", "/api/v3/diskspace", "diskspace"),
            ("GET", "/api/v3/system/status", "status"),
            ("GET", "/api/v3/queue/status", "counters"),
            ("POST", "/api/v3/command", "command"),
            ("PUT", "/api/v3/series/editor", "split"),
            ("GET", "/MediaCover/5/poster.jpg", "mediacover"),
            ("GET", "/api/v3/config/ui", "primary"),
            ("GET", "/api/v3/something-new", "auto"),
        ],
    )
    def test_route_table(self, router, method, path, kind) -> None:
        assert router._match(path, method)[1] == kind

    def test_lookup_wins_over_the_numeric_id_rule(self, router) -> None:
        """Ordering matters: /series/lookup must not be read as /series/{id}."""
        assert router._match("/api/v3/series/lookup", "GET")[1] == "lookup"

    def test_api_key_is_stripped_from_the_query(self, router) -> None:
        plain, ids = router.split_query("apikey=secret&term=x")
        assert plain == [("term", "x")] and ids == {}

    def test_id_query_parameters_are_separated(self, router) -> None:
        plain, ids = router.split_query(f"seriesId={BLOCK + 3}&includeSeries=true")
        assert plain == [("includeSeries", "true")]
        assert ids == {"seriesId": [BLOCK + 3]}

    def test_query_is_rebuilt_per_instance(self, router) -> None:
        plain, ids = router.split_query(f"seriesId={BLOCK + 3}&seriesId=4")
        assert router.query_for(plain, ids, 1) == [("seriesId", "3")]
        assert router.query_for(plain, ids, 0) == [("seriesId", "4")]

    def test_path_id_is_replaced_with_the_real_one(self, router) -> None:
        assert router._replace_id("/api/v3/series/10000003", 10000003, 3) == "/api/v3/series/3"

    def test_targets_narrow_to_the_referenced_instance(self, router) -> None:
        _, ids = router.split_query(f"seriesId={BLOCK + 1}")
        assert [i.name for i in router._targets_from_ids(ids)] == ["anime"]

    def test_no_ids_means_fan_out(self, router) -> None:
        assert router._targets_from_ids({}) is None

    def test_create_routes_on_a_virtual_profile_id(self, router) -> None:
        picked = router.pick_for_payload({"qualityProfileId": BLOCK + 2})
        assert picked.name == "anime"

    def test_create_routes_on_a_rule_when_no_id_is_present(self, router) -> None:
        assert router.pick_for_payload({"seriesType": "anime"}).name == "anime"

    def test_create_routes_on_root_folder(self, router) -> None:
        picked = router.pick_for_payload({"rootFolderPath": "/data/media/anime"})
        assert picked.name == "anime"

    def test_create_falls_back_to_the_default_instance(self, router) -> None:
        assert router.pick_for_payload({"title": "Andor"}).name == "main"

    def test_a_primary_profile_id_beats_an_unrelated_rule(self, router) -> None:
        """An explicit choice from the primary's own list must be honoured."""
        assert router.pick_for_payload({"qualityProfileId": 2}).name == "main"

    def test_body_ids_are_decoded_for_the_target(self, router) -> None:
        body = json.dumps({"id": BLOCK + 5, "tags": [BLOCK + 1], "tvdbId": 7}).encode()
        out = json.loads(router._body_for(body))
        assert out == {"id": 5, "tags": [1], "tvdbId": 7}

    def test_non_json_bodies_pass_through(self, router) -> None:
        assert router._body_for(b"not json") == b"not json"


class TestCache:
    def test_entries_expire(self) -> None:
        from arrproxy.routing import TTLCache

        cache = TTLCache(ttl=0.01)
        cache.put("k", "v")
        assert cache.get("k") == "v"
        import time

        time.sleep(0.05)
        assert cache.get("k") is None

    def test_zero_ttl_disables_caching(self) -> None:
        from arrproxy.routing import TTLCache

        cache = TTLCache(ttl=0)
        cache.put("k", "v")
        assert cache.get("k") is None

    def test_capacity_is_bounded(self) -> None:
        from arrproxy.routing import TTLCache

        cache = TTLCache(ttl=60, capacity=3)
        for n in range(10):
            cache.put(f"k{n}", n)
        assert len(cache._data) <= 3


# ---------------------------------------------------------------------------
class TestInstanceShapes:
    """Config shapes that differ from the two-instance default."""

    def test_a_single_instance_still_works(self, tmp_path) -> None:
        text = """
apps:
  sonarr:
    api_key: kkkkkkkkkkkk
    instances:
      - {name: only, url: 'http://sonarr:8989', api_key: a}
"""
        settings = cfg.load(write(tmp_path, text))
        app = settings.apps["sonarr"]
        assert app.default_instance.name == "only"
        mapper = IdMapper("sonarr", settings.id_block)
        assert mapper.to_virtual(7, 0) == 7, "a lone instance is identity mapped"

    def test_three_instances_get_distinct_ranges(self, tmp_path) -> None:
        text = """
apps:
  sonarr:
    api_key: kkkkkkkkkkkk
    instances:
      - {name: a, url: 'http://a:8989', api_key: a, default: true}
      - {name: b, url: 'http://b:8989', api_key: b}
      - {name: c, url: 'http://c:8989', api_key: c}
"""
        settings = cfg.load(write(tmp_path, text))
        router = AppRouter(settings, settings.apps["sonarr"], upstream=None)  # type: ignore[arg-type]
        assert [i.index for i in router.app.instances] == [0, 1, 2]
        assert router.mapper.to_virtual(1, 2) == 2 * BLOCK + 1
        assert router.mapper.to_real(2 * BLOCK + 1) == (2, 1)
        _, ids = router.split_query(f"seriesId={2 * BLOCK + 5}")
        assert [i.name for i in router._targets_from_ids(ids)] == ["c"]

    def test_disabled_instances_are_left_out(self, tmp_path) -> None:
        text = """
apps:
  sonarr:
    api_key: kkkkkkkkkkkk
    instances:
      - {name: a, url: 'http://a:8989', api_key: a, default: true}
      - {name: b, url: 'http://b:8989', api_key: b, enabled: false}
"""
        settings = cfg.load(write(tmp_path, text))
        router = AppRouter(settings, settings.apps["sonarr"], upstream=None)  # type: ignore[arg-type]
        assert [i.name for i in router.live] == ["a"]
        # A create pointed at the disabled instance must not be sent to it.
        assert router.pick_for_payload({"qualityProfileId": BLOCK + 1}).name == "a"
        # Nor may a query id resolve to it.
        _, ids = router.split_query(f"seriesId={BLOCK + 1}")
        assert router._targets_from_ids(ids) == []

    def test_public_url_is_used_for_browser_links(self, tmp_path) -> None:
        text = """
apps:
  sonarr:
    api_key: kkkkkkkkkkkk
    instances:
      - {name: a, url: 'http://sonarr:8989', api_key: a, default: true,
         public_url: 'https://sonarr.example.test'}
      - {name: b, url: 'http://sonarr-anime:8989/', api_key: b}
"""
        settings = cfg.load(write(tmp_path, text))
        a, b = settings.apps["sonarr"].instances
        assert a.browser_base == "https://sonarr.example.test"
        assert b.browser_base == "http://sonarr-anime:8989", "falls back to url"

    def test_public_url_must_be_absolute(self, tmp_path) -> None:
        text = """
apps:
  sonarr:
    api_key: kkkkkkkkkkkk
    instances:
      - {name: a, url: 'http://a:8989', api_key: a, public_url: 'a:8989'}
"""
        with pytest.raises(cfg.ConfigError, match="public_url"):
            cfg.load(write(tmp_path, text))


class TestUrlBaseSupport:
    """An instance may sit under a urlBase, e.g. http://host:8989/sonarr.

    The proxy relies on httpx joining that base path with the API path rather
    than replacing it, so pin the behaviour instead of assuming it.
    """

    def test_httpx_joins_a_base_path(self) -> None:
        import httpx

        client = httpx.Client(base_url="http://host:8989/sonarr")
        request = client.build_request("GET", "/api/v3/series")
        assert str(request.url) == "http://host:8989/sonarr/api/v3/series"

    def test_httpx_join_without_a_base_path(self) -> None:
        import httpx

        client = httpx.Client(base_url="http://host:8989")
        request = client.build_request("GET", "/api/v3/series")
        assert str(request.url) == "http://host:8989/api/v3/series"

    def test_trailing_slash_is_normalised(self, tmp_path) -> None:
        text = """
apps:
  sonarr:
    api_key: kkkkkkkkkkkk
    instances:
      - {name: a, url: 'http://a:8989/sonarr/', api_key: a}
"""
        settings = cfg.load(write(tmp_path, text))
        assert settings.apps["sonarr"].instances[0].base == "http://a:8989/sonarr"


class TestIdOverflow:
    def test_an_id_at_the_block_size_is_reported(self, caplog) -> None:
        """Such an id lands in the next instance's range and routes wrongly."""
        mapper = IdMapper("sonarr", BLOCK)
        with caplog.at_level("ERROR"):
            mapper.encode({"id": BLOCK + 5}, 0)
        assert any("id_block" in r.getMessage() for r in caplog.records)

    def test_it_is_reported_only_once(self, caplog) -> None:
        mapper = IdMapper("sonarr", BLOCK)
        with caplog.at_level("ERROR"):
            for _ in range(5):
                mapper.encode({"id": BLOCK + 5}, 0)
        assert len([r for r in caplog.records if "id_block" in r.getMessage()]) == 1

    def test_normal_ids_log_nothing(self, caplog) -> None:
        mapper = IdMapper("sonarr", BLOCK)
        with caplog.at_level("ERROR"):
            mapper.encode({"id": 42}, 1)
        assert caplog.records == []


class TestUnknownOwnerHelper:
    @pytest.fixture()
    def router(self, tmp_path) -> AppRouter:
        return build_router(tmp_path)

    def test_no_ids_means_fan_out(self, router) -> None:
        assert AppRouter._no_owner(None) is False

    def test_ids_naming_a_missing_instance_is_flagged(self, router) -> None:
        assert AppRouter._no_owner([]) is True

    def test_resolved_ids_are_not_flagged(self, router) -> None:
        assert AppRouter._no_owner(router.live[:1]) is False

    def test_an_unknown_instance_index_yields_no_targets(self, router) -> None:
        _, ids = router.split_query(f"seriesId={9 * BLOCK + 1}")
        assert router._targets_from_ids(ids) == []


class TestSecretsStayOutOfLogs:
    def test_startup_log_never_contains_the_full_key(self, tmp_path, caplog) -> None:
        from arrproxy.__main__ import log_startup

        key = "example-combined-key-for-tests-0000"
        settings = cfg.load(write(tmp_path, BASE_CONFIG.format(key=key)))
        routers = {"sonarr": AppRouter(settings, settings.apps["sonarr"], upstream=None)}  # type: ignore[arg-type]
        with caplog.at_level("INFO"):
            log_startup(routers)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert key not in text
        assert "...0000" in text, "the tail is still shown so you can tell which key is live"

    def test_short_keys_are_not_partially_revealed(self) -> None:
        from arrproxy.__main__ import mask

        assert mask("abcdefgh") == "(set)"


class StubUpstream:
    """Serves library listings and metadata lookups from fixed tables.

    Every call is recorded, so a test can assert which requests were *not*
    made -- the fast path's whole point is skipping the metadata lookup.
    """

    def __init__(self, lookups: dict[str, list] | None = None,
                 libraries: dict[str, list] | None = None):
        self.lookups = lookups or {}
        self.libraries = libraries or {}
        self.calls: list[tuple[str, str]] = []

    async def call(self, app, inst, method, path, params=None, **_):
        self.calls.append((inst.name, path))
        if path.endswith("/lookup"):
            payload = self.lookups.get(inst.name, [])
        elif path == "/api/v3/series":
            payload = self.libraries.get(inst.name, [])
        else:
            return Reply(instance=inst, status=404, headers={"content-type": "application/json"},
                         body=b'{"message":"NotFound"}')
        return Reply(instance=inst, status=200, headers={"content-type": "application/json"},
                     body=json.dumps(payload).encode())

    def looked_up(self) -> bool:
        return any(path.endswith("/lookup") for _, path in self.calls)


class TestAddLinkResolution:
    """/add/new?term=... from SeerrFin, for titles that may already be owned."""

    def resolve(self, tmp_path, upstream, term):
        import asyncio

        settings = cfg.load(write(tmp_path, BASE_CONFIG.format(key="k" * 12)))
        router = AppRouter(settings, settings.apps["sonarr"], upstream)
        return asyncio.run(router._resolve_add_link([("term", term)]))

    @pytest.mark.parametrize("term", ["tmdb:207468", "TVDB:423075", "imdb:tt123", "tmdbid:5"])
    def test_exact_id_terms_are_recognised(self, term) -> None:
        from arrproxy.routing import ID_TERM

        assert ID_TERM.match(term)

    @pytest.mark.parametrize("term", ["kaiju no 8", "tmdb:", "", "tmdb 207468"])
    def test_free_text_is_not_an_id_term(self, term) -> None:
        from arrproxy.routing import ID_TERM

        assert not ID_TERM.match(term)

    def test_owner_is_the_instance_whose_lookup_carries_a_library_id(self, tmp_path) -> None:
        upstream = StubUpstream(
            lookups={"main": [{"title": "Kaiju No. 8", "id": 0, "titleSlug": "kaiju-no-8"}],
                     "anime": [{"title": "Kaiju No. 8", "id": 37, "titleSlug": "kaiju-no-8"}]},
        )
        inst, path, query, how = self.resolve(tmp_path, upstream, "tmdb:207468")
        assert (inst.name, path, how) == ("anime", "/series/kaiju-no-8", "library")
        assert query == []

    def test_lookup_slug_is_used_if_the_library_row_is_missing(self, tmp_path) -> None:
        upstream = StubUpstream(lookups={"anime": [{"id": 37, "titleSlug": "kaiju-no-8"}]})
        inst, path, _, how = self.resolve(tmp_path, upstream, "tmdb:207468")
        assert (inst.name, path, how) == ("anime", "/series/kaiju-no-8", "library")

    def test_owned_title_is_found_without_a_metadata_lookup(self, tmp_path) -> None:
        """The fast path: ~10ms of LAN reads instead of a ~4s metadata round trip."""
        upstream = StubUpstream(libraries={
            "main": [{"id": 5, "tmdbId": 1396, "titleSlug": "breaking-bad"}],
            "anime": [{"id": 37, "tmdbId": 207468, "titleSlug": "kaiju-no-8"}],
        })
        inst, path, query, how = self.resolve(tmp_path, upstream, "tmdb:207468")
        assert (inst.name, path, how) == ("anime", "/series/kaiju-no-8", "library")
        assert query == []
        assert not upstream.looked_up(), "an owned title must not wait on the metadata server"

    def test_imdb_ids_match_on_the_fast_path(self, tmp_path) -> None:
        upstream = StubUpstream(libraries={"anime": [{"id": 1, "imdbId": "tt0213338", "titleSlug": "cowboy-bebop"}]})
        inst, _, _, how = self.resolve(tmp_path, upstream, "imdb:tt0213338")
        assert (inst.name, how) == ("anime", "library") and not upstream.looked_up()

    def test_stale_stored_id_still_resolves_through_the_lookup(self, tmp_path) -> None:
        """Library row has an outdated TMDB id; the lookup bridges it (Sonarr matches on TVDB)."""
        upstream = StubUpstream(
            libraries={"anime": [{"id": 37, "tmdbId": 999, "titleSlug": "kaiju-no-8-2024"}]},
            lookups={"anime": [{"id": 37, "tmdbId": 207468, "titleSlug": "kaiju-no-8"}]},
        )
        inst, path, _, how = self.resolve(tmp_path, upstream, "tmdb:207468")
        assert (inst.name, how) == ("anime", "library")
        assert path == "/series/kaiju-no-8-2024", "the owner's stored slug wins over the lookup's"

    def test_a_zero_id_matches_nothing(self, tmp_path) -> None:
        """Titles missing a TMDB id store 0; "tmdb:0" must not claim them."""
        upstream = StubUpstream(libraries={"anime": [{"id": 1, "tmdbId": 0, "titleSlug": "x"}]})
        inst, _, _, how = self.resolve(tmp_path, upstream, "tmdb:0")
        assert (inst.name, how) == ("main", "default")
        assert upstream.calls == []

    @pytest.mark.parametrize(
        "term", ["imdb:None", "imdb:null", "imdbid:tt", "imdb:0", "tvdb:-3", "tmdb:abc"]
    )
    def test_malformed_ids_match_nothing(self, tmp_path, term) -> None:
        """A client with no id may send "None"; titles lacking that id must not claim it."""
        upstream = StubUpstream(libraries={"anime": [
            {"id": 1, "imdbId": None, "tvdbId": None, "tmdbId": None, "titleSlug": "no-ids"},
            {"id": 2, "titleSlug": "no-id-fields"},
        ]})
        inst, path, _, how = self.resolve(tmp_path, upstream, term)
        assert (inst.name, path, how) == ("main", "/add/new", "default")
        assert upstream.calls == [], "a malformed id is not worth a round trip"

    def test_unowned_title_follows_a_routing_rule(self, tmp_path) -> None:
        upstream = StubUpstream(lookups={"main": [{"id": 0, "seriesType": "anime"}],
                                         "anime": [{"id": 0, "seriesType": "anime"}]})
        inst, path, query, how = self.resolve(tmp_path, upstream, "tmdb:1")
        assert (inst.name, path, how) == ("anime", "/add/new", "rule")
        assert query == [("term", "tmdb:1")], "the add form keeps its search term"

    def test_unowned_unclaimed_title_goes_to_the_default(self, tmp_path) -> None:
        upstream = StubUpstream(lookups={"main": [{"id": 0}], "anime": [{"id": 0}]})
        inst, _, _, how = self.resolve(tmp_path, upstream, "tmdb:1")
        assert (inst.name, how) == ("main", "default")

    def test_free_text_never_queries_the_instances(self, tmp_path) -> None:
        upstream = StubUpstream(lookups={})
        inst, _, _, how = self.resolve(tmp_path, upstream, "kaiju no 8")
        assert (inst.name, how) == ("main", "default")
        assert upstream.calls == []
