"""Request routing: decide which instances serve a call, then fold the answers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from . import merge
from .config import AppConfig, Instance, Settings
from .idmap import MEDIACOVER_RE, IdMapper
from .upstream import Reply, Upstream, sanitize_request_headers

log = logging.getLogger("arrproxy.routing")

# Keys a client may use to pass the API key, in header or query form.
API_KEY_QUERY = {"apikey", "apiKey"}

# A lookup term naming one exact title, e.g. "tmdb:207468" or "tvdb:423075".
# Only these can be resolved to an owner; free text matches many titles.
ID_TERM = re.compile(r"^(?:tmdb|tvdb|imdb)(?:id)?:\S+$", re.IGNORECASE)

# Extra query parameters that carry entity ids but are not object field names.
EXTRA_ID_QUERY_KEYS = frozenset({"ids", "id"})

PAGED_DEFAULT_SORT = {
    "sonarr": {"history": "date", "wanted": "airDateUtc", "queue": "timeleft"},
    "radarr": {"history": "date", "wanted": "movieMetadata.sortTitle", "queue": "timeleft"},
}

CALENDAR_SORT = {
    "sonarr": "airDateUtc",
    "radarr": ("inCinemas", "digitalRelease", "physicalRelease"),
    "lidarr": "releaseDate",
    "readarr": "releaseDate",
}

PRIMARY_ENTITY = {
    "sonarr": "series",
    "radarr": "movie",
    "lidarr": "artist",
    "readarr": "author",
}


class Degraded(Exception):
    """No instance could be reached at all, so there is nothing to return."""

    def __init__(self, replies: list[Reply]) -> None:
        self.replies = replies
        super().__init__("no instance answered")


class UpstreamRefused(Exception):
    """Every instance answered, and all of them with the same error status.

    Distinct from Degraded: the instances are up and talking, they just all said
    no.  A missing endpoint or a bad id is an answer, and relaying the real 404
    is far more useful to a client than a 502 implying the backends are down.
    """

    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        super().__init__(f"all instances returned HTTP {reply.status}")


class TTLCache:
    """Tiny time-boxed cache for fanned-out GETs.

    A Jellyfin home screen asks for the same calendar on every page load and for
    every user; without this each of those becomes N upstream calls.
    """

    def __init__(self, ttl: float, capacity: int = 512) -> None:
        self.ttl = ttl
        self.capacity = capacity
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        if self.ttl <= 0:
            return None
        hit = self._data.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if time.monotonic() - stored_at > self.ttl:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        if self.ttl <= 0:
            return
        if len(self._data) >= self.capacity:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._data.clear()


def _as_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed


class AppRouter:
    """Serves one combined app (all Sonarr instances, or all Radarr instances)."""

    def __init__(self, settings: Settings, app: AppConfig, upstream: Upstream) -> None:
        self.settings = settings
        self.app = app
        self.upstream = upstream
        self.mapper = IdMapper(app.app_type, settings.id_block)
        self.cache = TTLCache(settings.cache_ttl)
        self.entity = PRIMARY_ENTITY.get(app.app_type, "series")
        self.api_prefix = f"/api/{app.api_version}"
        self.id_query_keys = self.mapper.id_keys | EXTRA_ID_QUERY_KEYS
        self._routes = self._build_routes()

    # ------------------------------------------------------------------
    # route table
    # ------------------------------------------------------------------
    def _build_routes(self) -> list[tuple[re.Pattern[str], set[str], str, dict[str, Any]]]:
        e = re.escape(self.entity)
        p = re.escape(self.api_prefix)
        any_m = {"GET", "POST", "PUT", "DELETE", "PATCH"}

        def rx(suffix: str) -> re.Pattern[str]:
            return re.compile(rf"^{p}/{suffix}/?$", re.IGNORECASE)

        table: list[tuple[re.Pattern[str], set[str], str, dict[str, Any]]] = [
            # --- web-UI deep links (no /api prefix) ---------------------------
            # SeerrFin renders "Open in Sonarr/Radarr" buttons pointing at
            # {base}/series/{titleSlug} and {base}/add/new?term=tmdb:N.  Pointed
            # at the proxy those would 404, so resolve the owning instance and
            # bounce the browser to its real UI.
            (re.compile(rf"^/{e}/(?P<slug>[^/]+)/?$", re.I), {"GET"}, "uilink", {}),
            (re.compile(r"^/add/new/?$", re.I), {"GET"}, "uilink", {"add": True}),
            # --- media covers -------------------------------------------------
            (re.compile(r"^/mediacover/(?P<id>\d+)/.+$", re.I), {"GET"}, "mediacover", {}),
            (re.compile(rf"^{p}/mediacover/(?P<id>\d+)/.+$", re.I), {"GET"}, "mediacover", {}),
            # --- lookups (must precede the /{id} rule) ------------------------
            (rx(rf"{e}/lookup"), {"GET"}, "lookup", {}),
            (rx(r"(?:search|importlist/movie|importlist/series)"), {"GET"}, "lookup", {}),
            # --- bulk editors -------------------------------------------------
            (rx(rf"{e}/editor"), {"PUT", "DELETE"}, "split", {}),
            (rx(rf"{e}/import"), {"POST"}, "create", {}),
            (rx(r"queue/bulk"), {"DELETE"}, "split", {}),
            (rx(r"queue/grab/bulk"), {"POST"}, "split", {}),
            # --- primary entity -----------------------------------------------
            (rx(e), {"GET"}, "agg", {}),
            (rx(e), {"POST"}, "create", {}),
            (rx(rf"{e}/(?P<id>\d+)"), any_m, "byid", {}),
            # --- calendar ------------------------------------------------------
            (rx(r"calendar"), {"GET"}, "agg",
             {"sort_key": CALENDAR_SORT.get(self.app.app_type)}),
            (rx(r"calendar/(?P<id>\d+)"), {"GET"}, "byid", {}),
            # --- queue ---------------------------------------------------------
            (rx(r"queue"), {"GET"}, "paged", {"kind": "queue"}),
            (rx(r"queue/details"), {"GET"}, "byquery", {}),
            (rx(r"queue/status"), {"GET"}, "counters", {}),
            (rx(r"queue/(?P<id>\d+)"), {"DELETE"}, "byid", {}),
            (rx(r"queue/grab/(?P<id>\d+)"), {"POST"}, "byid", {}),
            # --- history / wanted ----------------------------------------------
            (rx(r"history"), {"GET"}, "paged", {"kind": "history"}),
            (rx(r"history/since"), {"GET"}, "agg", {"sort_key": "date", "desc": True}),
            (rx(rf"history/{e}"), {"GET"}, "byquery", {}),
            (rx(r"history/failed/(?P<id>\d+)"), {"POST"}, "byid", {}),
            (rx(r"wanted/missing"), {"GET"}, "paged", {"kind": "wanted"}),
            (rx(r"wanted/cutoff"), {"GET"}, "paged", {"kind": "wanted"}),
            (rx(r"wanted/missing/(?P<id>\d+)"), {"GET"}, "byid", {}),
            (rx(r"wanted/cutoff/(?P<id>\d+)"), {"GET"}, "byid", {}),
            # --- blocklist ------------------------------------------------------
            (rx(r"blocklist"), {"GET"}, "paged", {"kind": "history"}),
            (rx(r"blocklist/(?P<id>\d+)"), {"DELETE"}, "byid", {}),
            (rx(r"blocklist/bulk"), {"DELETE"}, "split", {}),
            # --- system ----------------------------------------------------------
            (rx(r"health"), {"GET"}, "health", {}),
            (rx(r"diskspace"), {"GET"}, "diskspace", {}),
            (rx(r"system/status"), {"GET"}, "status", {}),
            (rx(r"command"), {"GET"}, "agg", {}),
            (rx(r"command"), {"POST"}, "command", {}),
            (rx(r"command/(?P<id>\d+)"), {"GET", "DELETE"}, "byid", {}),
            # --- release / manual grab -------------------------------------------
            (rx(r"release"), {"GET", "POST"}, "byquery", {}),
            (rx(r"release/push"), {"POST"}, "primary", {}),
            # --- settings-ish collections -----------------------------------------
            (rx(r"(?:qualityprofile|profile|languageprofile|metadataprofile|delayprofile"
                r"|releaseprofile|customformat|tag|tag/detail|rootfolder|indexer"
                r"|downloadclient|importlist|notification|metadata|remotepathmapping"
                r"|autotagging|customfilter|qualitydefinition|collection)"),
             {"GET"}, "agg", {}),
            (rx(r"(?:qualityprofile|profile|languageprofile|metadataprofile|delayprofile"
                r"|releaseprofile|customformat|tag|tag/detail|rootfolder|indexer"
                r"|downloadclient|importlist|notification|metadata|remotepathmapping"
                r"|autotagging|customfilter|qualitydefinition|collection)/(?P<id>\d+)"),
             any_m, "byid", {}),
            # --- config / localization are per-instance settings: serve the primary
            (re.compile(rf"^{p}/(?:config|localization|system|update|filesystem|parse"
                        r"|log|languages?)(?:/.*)?$", re.I), any_m, "primary", {}),
        ]
        return table

    def _match(self, path: str, method: str):
        for pattern, methods, kind, options in self._routes:
            if method.upper() not in methods:
                continue
            found = pattern.match(path)
            if found:
                return found, kind, options
        return None, "auto", {}

    # ------------------------------------------------------------------
    # instance selection
    # ------------------------------------------------------------------
    @property
    def live(self) -> list[Instance]:
        return [i for i in self.app.instances if i.enabled]

    def instance_at(self, index: int) -> Instance | None:
        for inst in self.app.instances:
            if inst.index == index:
                return inst
        return None

    def pick_for_payload(self, payload: dict[str, Any]) -> Instance:
        """Choose the instance that should own a newly created entity.

        Preference order matters.  A virtual id the client got *from us* is the
        strongest signal available -- the quality profile or root folder it
        picked came out of one specific instance's list -- so it beats any
        heuristic rule we could write.
        """
        if isinstance(payload, dict):
            candidates: list[int] = []
            for key in ("qualityProfileId", "metadataProfileId", "languageProfileId"):
                value = _as_int(payload.get(key))
                if value:
                    candidates.append(self.mapper.index_of(value))
            for value in payload.get("tags") or []:
                parsed = _as_int(value)
                if parsed:
                    candidates.append(self.mapper.index_of(parsed))
            for index in candidates:
                if index != 0:
                    found = self.instance_at(index)
                    if found and found.enabled:
                        return found

            for inst in self.live:
                if inst.routing.matches(payload):
                    return inst

            # A profile id that decoded to the primary is still a real signal,
            # just a weaker one than a rule -- fall back to it before the default.
            if candidates and all(c == 0 for c in candidates):
                primary = self.instance_at(0)
                if primary and primary.enabled:
                    return primary

        fallback = self.app.default_instance
        if fallback.enabled:
            return fallback
        # The configured default was disabled; anything live beats writing to it.
        return self.live[0] if self.live else fallback

    # ------------------------------------------------------------------
    # query handling
    # ------------------------------------------------------------------
    def split_query(self, raw: str) -> tuple[list[tuple[str, str]], dict[str, list[int]]]:
        """Return (non-id pairs, {key: [virtual ids]}) with the api key removed."""
        plain: list[tuple[str, str]] = []
        ids: dict[str, list[int]] = {}
        for key, value in parse_qsl(raw, keep_blank_values=True):
            if key in API_KEY_QUERY:
                continue  # never leak the combined key upstream
            if key in self.id_query_keys:
                parsed = _as_int(value)
                if parsed is not None and parsed > 0:
                    ids.setdefault(key, []).append(parsed)
                    continue
            plain.append((key, value))
        return plain, ids

    def query_for(self, plain: list[tuple[str, str]], ids: dict[str, list[int]],
                  index: int) -> list[tuple[str, str]]:
        """Rebuild the upstream query, keeping only ids owned by ``index``."""
        out = list(plain)
        for key, values in ids.items():
            for virtual in values:
                owner, real = self.mapper.to_real(virtual)
                if owner == index:
                    out.append((key, str(real)))
        return out

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------
    async def dispatch(self, request: Request, path: str) -> Response:
        method = request.method.upper()
        found, kind, options = self._match(path, method)
        raw_query = request.url.query
        plain, ids = self.split_query(raw_query)
        path_id = _as_int(found.groupdict().get("id")) if found and "id" in found.groupdict() else None

        body = await request.body()
        headers = sanitize_request_headers(request.headers.items())

        cacheable = method == "GET" and kind in {
            "agg", "paged", "lookup", "health", "diskspace", "counters", "status", "auto"
        }
        cache_key = f"{method} {path}?{raw_query}"
        if cacheable:
            hit = self.cache.get(cache_key)
            if hit is not None:
                response = self._json(hit[0], headers_extra=hit[1])
                response.headers["X-ArrProxy-Cache"] = "hit"
                return response

        handler = getattr(self, f"_h_{kind}", self._h_auto)
        try:
            result = await handler(
                request=request, path=path, method=method, plain=plain, ids=ids,
                path_id=path_id, body=body, headers=headers, options=options,
            )
        except UpstreamRefused as exc:
            return self._single(exc.reply, exc.reply.instance.index)
        except Degraded as exc:
            detail = "; ".join(
                f"{r.instance.name}: {r.error or f'HTTP {r.status}'}" for r in exc.replies
            )
            log.error("%s %s -- every instance failed: %s", method, path, detail)
            return JSONResponse(
                {"error": "no backing instance answered", "detail": detail}, status_code=502
            )

        if isinstance(result, Response):
            return result

        payload, extra = result
        if cacheable and "X-ArrProxy-Degraded" not in extra:
            # Never cache a partial answer: an instance that was briefly
            # unreachable would keep half the library missing for the whole TTL
            # after it came back.
            self.cache.put(cache_key, (payload, extra))
        elif method != "GET":
            # A write invalidates anything we might be holding for this app.
            self.cache.clear()
        return self._json(payload, headers_extra=extra)

    def _json(self, payload: Any, headers_extra: dict[str, str] | None = None) -> Response:
        response = JSONResponse(payload)
        for key, value in (headers_extra or {}).items():
            response.headers[key] = value
        return response

    @staticmethod
    def _annotate(replies: list[Reply]) -> dict[str, str]:
        served = [r.instance.name for r in replies if r.ok]
        failed = [r.instance.name for r in replies if not r.ok]
        headers = {"X-ArrProxy-Instances": ",".join(served)}
        if failed:
            headers["X-ArrProxy-Degraded"] = ",".join(failed)
        return headers

    def _require_any(self, replies: list[Reply]) -> dict[str, str]:
        if not any(r.ok for r in replies):
            answered = [r for r in replies if r.error is None and r.status]
            statuses = {r.status for r in answered}
            if len(statuses) == 1:
                # Unanimous refusal -- relay it verbatim rather than masking a
                # genuine 404 or 400 behind a gateway error.
                raise UpstreamRefused(answered[0])
            raise Degraded(replies)
        annotations = self._annotate(replies)
        if "X-ArrProxy-Degraded" in annotations and not self.settings.fail_open:
            raise Degraded(replies)
        return annotations

    async def _fan_parallel(self, method: str, path: str, plain, ids, body, headers,
                            targets: Iterable[Instance] | None = None,
                            extra_params: list[tuple[str, str]] | None = None) -> list[Reply]:
        chosen = list(targets) if targets is not None else self.live
        # Only reads get the short deadline: aborting a slow write could leave
        # the instance half-changed with no way to tell what landed.
        deadline = self.settings.fanout_timeout if method == "GET" else None
        calls = [
            self.upstream.call(
                self.app, inst, method, path,
                params=self.query_for(plain, ids, inst.index) + list(extra_params or []),
                content=self._body_for(body), headers=headers, deadline=deadline,
            )
            for inst in chosen
        ]
        return list(await asyncio.gather(*calls))

    def _body_for(self, body: bytes | None) -> bytes | None:
        """Translate virtual ids inside a request body back to instance ids."""
        if not body:
            return None
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body
        return json.dumps(self.mapper.decode(parsed)).encode("utf-8")

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------
    @staticmethod
    def _no_owner(targets: list[Instance] | None) -> bool:
        """The request named entities that no live instance owns."""
        return targets is not None and not targets

    async def _h_agg(self, *, method, path, plain, ids, body, headers, options, **_):
        targets = self._targets_from_ids(ids)
        if self._no_owner(targets):
            return [], {"X-ArrProxy-Instances": ""}
        replies = await self._fan_parallel(method, path, plain, ids, body, headers, targets)
        extra = self._require_any(replies)
        payload = merge.merge_list(
            replies, self.mapper,
            sort_key=options.get("sort_key"), descending=bool(options.get("desc")),
        )
        return payload, extra

    async def _h_byquery(self, **kwargs):
        """Route on an id in the query string; aggregate when there is none."""
        return await self._h_agg(**kwargs)

    async def _h_lookup(self, *, method, path, plain, ids, body, headers, **_):
        replies = await self._fan_parallel(method, path, plain, ids, body, headers)
        extra = self._require_any(replies)
        return merge.merge_lookup(replies, self.mapper), extra

    async def _h_health(self, *, method, path, plain, ids, body, headers, **_):
        replies = await self._fan_parallel(method, path, plain, ids, body, headers)
        extra = self._require_any(replies)
        return merge.merge_health(replies, self.mapper), extra

    async def _h_diskspace(self, *, method, path, plain, ids, body, headers, **_):
        replies = await self._fan_parallel(method, path, plain, ids, body, headers)
        extra = self._require_any(replies)
        return merge.merge_diskspace(replies, self.mapper), extra

    async def _h_counters(self, *, method, path, plain, ids, body, headers, **_):
        replies = await self._fan_parallel(method, path, plain, ids, body, headers)
        extra = self._require_any(replies)
        return merge.merge_counters(replies, self.mapper), extra

    async def _h_status(self, *, method, path, plain, ids, body, headers, **_):
        replies = await self._fan_parallel(method, path, plain, ids, body, headers)
        extra = self._require_any(replies)
        first = next((merge.decoded(r, self.mapper) for r in replies if r.ok), None)
        if not isinstance(first, dict):
            raise Degraded(replies)
        status = dict(first)
        # appName must stay exactly "Sonarr"/"Radarr": clients branch on it.
        status["instanceName"] = self.app.instance_name
        # The proxy serves at the root even when the instance behind it sits
        # under a urlBase; relaying the instance's value would have clients
        # prefixing a path we do not answer on.
        status["urlBase"] = ""
        status["arrProxy"] = {
            "instances": [
                {"name": r.instance.name, "reachable": r.ok, "url": r.instance.url}
                for r in replies
            ]
        }
        return status, extra

    async def _h_paged(self, *, method, path, plain, ids, body, headers, options, **_):
        query = dict(plain)
        page = max(_as_int(query.get("page")) or 1, 1)
        page_size = max(_as_int(query.get("pageSize")) or 20, 1)
        sort_key = query.get("sortKey") or PAGED_DEFAULT_SORT.get(
            self.app.app_type, {}
        ).get(options.get("kind", ""), None)
        descending = str(query.get("sortDirection", "")).lower().startswith("desc")

        targets = self._targets_from_ids(ids)
        if self._no_owner(targets):
            return (
                {"page": page, "pageSize": page_size, "sortKey": sort_key or "",
                 "sortDirection": "descending" if descending else "ascending",
                 "totalRecords": 0, "records": []},
                {"X-ArrProxy-Instances": ""},
            )

        # To serve page N of the merged order we need the first N pages from
        # every instance -- a single instance could own the whole window.
        want = page * page_size
        need = min(want, self.settings.max_page_fetch)
        if need < want:
            log.warning(
                "page %d x pageSize %d needs %d rows per instance but "
                "max_page_fetch is %d; deep pages may be incomplete. Raise "
                "server.max_page_fetch if this endpoint is paged that deeply.",
                page, page_size, want, self.settings.max_page_fetch,
            )
        upstream_plain = [
            (k, v) for k, v in plain if k not in {"page", "pageSize"}
        ] + [("page", "1"), ("pageSize", str(need))]

        replies = await self._fan_parallel(
            method, path, upstream_plain, ids, body, headers, targets
        )
        extra = self._require_any(replies)
        payload = merge.merge_paged(
            replies, self.mapper, page=page, page_size=page_size,
            sort_key=sort_key, descending=descending,
        )
        return payload, extra

    async def _h_byid(self, *, method, path, plain, ids, body, headers, path_id, **_):
        if path_id is None:
            return await self._h_auto(
                method=method, path=path, plain=plain, ids=ids, body=body,
                headers=headers, options={}, path_id=None,
            )
        index, real = self.mapper.to_real(path_id)
        inst = self.instance_at(index)
        if inst is None or not inst.enabled:
            return JSONResponse({"error": f"unknown instance for id {path_id}"}, status_code=404)

        upstream_path = self._replace_id(path, path_id, real)
        reply = await self.upstream.call(
            self.app, inst, method, upstream_path,
            params=self.query_for(plain, ids, index),
            content=self._body_for(body), headers=headers,
        )

        # Only a *read* may be retried elsewhere.  Guessing an instance for a
        # PUT or DELETE could mutate the wrong library, so ambiguity there is
        # surfaced as the upstream 404 instead.
        if (
            method == "GET"
            and not reply.ok
            and reply.status in (404, 400)
            and self.settings.id_fallback_probe
            and self.mapper.is_ambiguous(path_id)
        ):
            probe = await self._probe_other_instances(
                path, path_id, plain, ids, headers, skip=index
            )
            if probe is not None:
                reply, index = probe

        return self._single(reply, index)

    async def _probe_other_instances(self, path, path_id, plain, ids, headers, skip):
        """Re-try a raw id against the other instances.

        Seerr hands plugins the id that the *real* instance assigned, so an id
        that looks like it belongs to the identity-mapped primary may in fact be
        the anime instance's.  Rather than return a confusing 404 we ask the
        others -- read-only, so a wrong guess costs nothing.
        """
        for inst in self.live:
            if inst.index == skip:
                continue
            candidate = await self.upstream.call(
                self.app, inst, "GET", path,  # the raw id as the client sent it
                params=self.query_for(plain, ids, inst.index), headers=headers,
            )
            if candidate.ok:
                log.info(
                    "id %s was ambiguous; resolved it on %s", path_id, inst.name
                )
                return candidate, inst.index
        return None

    @staticmethod
    def _replace_id(path: str, virtual: int, real: int) -> str:
        head, _, _ = path.rpartition(f"/{virtual}")
        return f"{head}/{real}" if head else path.replace(str(virtual), str(real), 1)

    async def _h_primary(self, *, method, path, plain, ids, body, headers, **_):
        inst = self.app.default_instance
        reply = await self.upstream.call(
            self.app, inst, method, path,
            params=self.query_for(plain, ids, inst.index),
            content=self._body_for(body), headers=headers,
        )
        if not reply.ok and reply.error:
            raise Degraded([reply])
        return self._single(reply, inst.index)

    async def _h_create(self, *, method, path, plain, ids, body, headers, **_):
        try:
            payload = json.loads(body) if body else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        inst = self.pick_for_payload(payload if isinstance(payload, dict) else {})
        log.info("routing %s %s to %s", method, path, inst.name)
        reply = await self.upstream.call(
            self.app, inst, method, path,
            params=self.query_for(plain, ids, inst.index),
            content=self._body_for(body), headers=headers,
        )
        if not reply.ok and reply.error:
            raise Degraded([reply])
        response = self._single(reply, inst.index)
        response.headers["X-ArrProxy-Instances"] = inst.name
        return response

    async def _h_command(self, *, method, path, plain, ids, body, headers, **_):
        """POST /command: route when it names an entity, broadcast when global."""
        try:
            payload = json.loads(body) if body else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}

        indexes: set[int] = set()
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key not in self.mapper.id_keys:
                    continue
                for item in value if isinstance(value, list) else [value]:
                    parsed = _as_int(item)
                    if parsed and parsed > 0:
                        indexes.add(self.mapper.index_of(parsed))

        if indexes:
            targets = [i for i in self.live if i.index in indexes]
            if not targets:
                # Falling back to a broadcast here would run a command named for
                # one entity against every instance.
                return JSONResponse(
                    {"error": "command references an unknown instance"},
                    status_code=404,
                )
        else:
            targets = self.live

        replies = await self._fan_parallel(
            method, path, plain, ids, body, headers, targets
        )
        extra = self._require_any(replies)
        first = next(r for r in replies if r.ok)
        response = self._single(first, first.instance.index)
        for key, value in extra.items():
            response.headers[key] = value
        return response

    async def _h_split(self, *, method, path, plain, ids, body, headers, **_):
        """Bulk operation whose id list can straddle instances."""
        try:
            payload = json.loads(body) if body else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}

        groups: dict[int, dict[str, Any]] = {}
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in self.mapper.id_keys and isinstance(value, list):
                    for item in value:
                        parsed = _as_int(item)
                        if parsed and parsed > 0:
                            index, real = self.mapper.to_real(parsed)
                            bucket = groups.setdefault(index, {})
                            bucket.setdefault(key, []).append(real)

        if not groups:
            return await self._h_primary(
                method=method, path=path, plain=plain, ids=ids, body=body, headers=headers,
            )

        replies: list[Reply] = []
        for index, id_fields in groups.items():
            inst = self.instance_at(index)
            if inst is None or not inst.enabled:
                continue
            scoped = dict(payload)
            scoped.update(id_fields)
            # Non-id fields may still carry virtual references (a tag to apply).
            for key, value in list(scoped.items()):
                if key in self.mapper.id_keys and key not in id_fields:
                    scoped[key] = self.mapper.decode({key: value})[key]
            replies.append(
                await self.upstream.call(
                    self.app, inst, method, path,
                    params=self.query_for(plain, ids, index),
                    content=json.dumps(scoped).encode("utf-8"), headers=headers,
                )
            )

        extra = self._require_any(replies)
        merged = merge.merge_list(replies, self.mapper)
        return merged, extra

    async def _h_auto(self, *, method, path, plain, ids, body, headers, options, **_):
        """Shape-driven fallback so unlisted endpoints still behave sensibly."""
        targets = self._targets_from_ids(ids)
        if self._no_owner(targets):
            return [], {"X-ArrProxy-Instances": ""}
        if method != "GET" and targets is None:
            return await self._h_primary(
                method=method, path=path, plain=plain, ids=ids, body=body, headers=headers,
            )

        replies = await self._fan_parallel(method, path, plain, ids, body, headers, targets)
        extra = self._require_any(replies)

        if targets is not None and len(list(targets)) == 1:
            only = next(r for r in replies if r.ok)
            return self._single(only, only.instance.index)

        query = dict(plain)
        payload = merge.merge_auto(
            replies, self.mapper,
            page=max(_as_int(query.get("page")) or 1, 1),
            page_size=max(_as_int(query.get("pageSize")) or 20, 1),
            sort_key=query.get("sortKey"),
            descending=str(query.get("sortDirection", "")).lower().startswith("desc"),
        )
        if payload is None:
            first = next(r for r in replies if r.ok)
            return self._single(first, first.instance.index)
        return payload, extra

    # ------------------------------------------------------------------
    def _targets_from_ids(self, ids: dict[str, list[int]]) -> list[Instance] | None:
        """Narrow the fan-out when the query names entities of one instance.

        Three outcomes, and the difference between the last two matters:
          ``None`` -- no ids in the query, so every instance is asked.
          ``[...]`` -- the instances that own the named ids.
          ``[]``   -- ids were named but no live instance owns them.  Returning
                      ``None`` here (as an "or None" would) means the caller
                      fans out with the id filter silently dropped, answering
                      "give me series 99999999's episodes" with *every* episode
                      in *every* instance.
        """
        indexes: set[int] = set()
        for key, values in ids.items():
            if key in ("page", "pageSize"):
                continue
            for value in values:
                indexes.add(self.mapper.index_of(value))
        if not indexes:
            return None
        return [i for i in self.live if i.index in indexes]

    def _single(self, reply: Reply, index: int) -> Response:
        """Return one instance's reply, id-translated, status and type preserved."""
        headers = {
            k: v for k, v in reply.headers.items()
            if k not in {"content-type", "content-length"}
        }
        headers["X-ArrProxy-Instances"] = reply.instance.name

        if reply.error:
            raise Degraded([reply])

        if reply.status in (204, 304) or not reply.body:
            # These statuses must not carry a body; serialising "null" into a
            # 204 is a protocol violation some HTTP clients reject outright.
            response = Response(status_code=reply.status)
            for key, value in headers.items():
                response.headers[key] = value
            return response

        if reply.is_json:
            payload = merge.decoded(reply, self.mapper)
            response = JSONResponse(payload if payload is not None else None,
                                    status_code=reply.status)
        else:
            body = reply.body
            if reply.content_type.startswith("text/") and body:
                body = MEDIACOVER_RE.sub(
                    lambda m: f"{m.group(1)}{self.mapper.to_virtual(int(m.group(2)), index)}{m.group(3)}",
                    body.decode("utf-8", "replace"),
                ).encode("utf-8")
            response = Response(
                content=body, status_code=reply.status,
                media_type=reply.content_type or None,
            )
        for key, value in headers.items():
            response.headers[key] = value
        return response

    def is_browser_link(self, path: str, method: str) -> bool:
        """True for the unauthenticated web-UI redirect paths."""
        return self._match(path, method)[1] == "uilink"

    async def _h_uilink(self, *, path, plain, options, **_) -> Response:
        """Bounce a web-UI deep link to whichever instance owns the title.

        ``resolution`` records *why* a target was chosen -- ``library`` (an
        instance holds the title), ``rule`` (a routing rule claimed it) or
        ``default`` (nothing did).  Without it a title found on the default
        instance and a title found nowhere redirect identically, which made a
        misrouted link impossible to diagnose from the outside.
        """
        if options.get("add"):
            target, destination_path, query, resolution = await self._resolve_add_link(plain)
        else:
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            owner = await self._instance_owning_slug(slug)
            target = owner or self.app.default_instance
            destination_path, query = path, plain
            resolution = "library" if owner else "default"

        destination = f"{target.browser_base}{destination_path}"
        if query:
            destination += "?" + urlencode(query)
        log.info(
            "deep link %s%s -> %s (%s)",
            path, f"?{urlencode(plain)}" if plain else "", destination, resolution,
        )
        response = RedirectResponse(destination, status_code=302)
        response.headers["X-ArrProxy-Instances"] = target.name
        response.headers["X-ArrProxy-Resolution"] = resolution
        return response

    async def _resolve_add_link(
        self, plain: list[tuple[str, str]]
    ) -> tuple[Instance, str, list[tuple[str, str]], str]:
        """Work out where ``/add/new?term=tmdb:N`` should really go.

        SeerrFin emits this link for titles that ARE in a library -- whenever a
        monitored title has nothing downloaded yet it drops its progress entry,
        link included, and falls back to "add new".  A lone Sonarr would just
        show the title as already added; behind the proxy it would land on the
        default instance, which may not have it at all.

        So every instance's own lookup is asked.  Sonarr and Radarr both mark a
        lookup result with its library id when they already hold the title, so
        whichever instance does is the owner, and the browser is sent straight
        to that title's page instead of an add form.
        """
        add_path = "/add/new"
        term = next((v.strip() for k, v in plain if k == "term"), "")
        if not ID_TERM.match(term):
            # A free-text search has no single owner to find.
            return self.app.default_instance, add_path, plain, "default"

        lookup = f"{self.api_prefix}/{self.entity}/lookup"
        replies = await asyncio.gather(
            *(
                self.upstream.call(
                    self.app, inst, "GET", lookup,
                    params=[("term", term)], deadline=self.settings.fanout_timeout,
                )
                for inst in self.live
            )
        )

        metadata: dict[str, Any] | None = None
        for reply in replies:  # configuration order, so the first owner wins
            rows = reply.json() if reply.ok else None
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                metadata = metadata or row
                library_id = _as_int(row.get("id"))
                if library_id and library_id > 0:
                    slug = await self._library_slug(reply.instance, library_id, row)
                    if slug:
                        return reply.instance, f"/{self.entity}/{slug}", [], "library"

        if metadata is not None:
            chosen = self.pick_for_payload(metadata)
            resolution = "rule" if chosen is not self.app.default_instance else "default"
            return chosen, add_path, plain, resolution
        return self.app.default_instance, add_path, plain, "default"

    async def _library_slug(self, inst: Instance, library_id: int, row: dict[str, Any]) -> str | None:
        """The slug the owning instance's own UI routes on.

        A lookup row carries the metadata server's slug; the library copy can
        differ (the *arrs de-duplicate colliding slugs), so prefer the stored
        record and fall back to the lookup row only if that read fails.
        """
        reply = await self.upstream.call(
            self.app, inst, "GET", f"{self.api_prefix}/{self.entity}/{library_id}",
            deadline=self.settings.fanout_timeout,
        )
        stored = reply.json() if reply.ok else None
        if isinstance(stored, dict) and stored.get("titleSlug"):
            return str(stored["titleSlug"])
        return str(row["titleSlug"]) if row.get("titleSlug") else None

    async def _instance_owning_slug(self, slug: str) -> Instance | None:
        if not slug:
            return None
        listing = f"{self.api_prefix}/{self.entity}"
        for inst in self.live:
            reply = await self.upstream.call(self.app, inst, "GET", listing)
            if not reply.ok:
                continue
            rows = reply.json()
            if isinstance(rows, list) and any(
                isinstance(r, dict) and r.get("titleSlug") == slug for r in rows
            ):
                return inst
        return None

    async def _h_mediacover(self, *, method, path, plain, ids, headers, path_id, **_):
        """Serve a poster/banner from whichever instance owns the entity."""
        if path_id is None:
            return JSONResponse({"error": "media cover id missing"}, status_code=404)
        index, real = self.mapper.to_real(path_id)
        inst = self.instance_at(index)
        if inst is None or not inst.enabled:
            return JSONResponse({"error": "unknown instance"}, status_code=404)

        upstream_path = MEDIACOVER_RE.sub(
            lambda m: f"{m.group(1)}{real}{m.group(3)}", path, count=1
        )
        reply = await self.upstream.call(
            self.app, inst, "GET", upstream_path,
            params=self.query_for(plain, ids, index), headers=headers, stream_hint=True,
        )
        if reply.error:
            raise Degraded([reply])
        response = Response(
            content=reply.body, status_code=reply.status,
            media_type=reply.content_type or "application/octet-stream",
        )
        response.headers["X-ArrProxy-Instances"] = inst.name
        return response
