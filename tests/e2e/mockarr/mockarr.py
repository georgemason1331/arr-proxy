"""A deterministic stand-in for one Sonarr or Radarr instance.

Faithful to the parts of the v3 API the proxy touches: api-key auth, the paged
envelope with a correct ``totalRecords``, ``/lookup`` returning ``id: 0`` for
titles not in this library, real MediaCover bytes, and 404s for unknown ids.

It also records every request it receives at ``/__mock/requests`` so the tests
can assert *which* instance served a call, not merely that the merged body
looked right.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

APP = os.environ.get("MOCK_APP", "sonarr")
NAME = os.environ.get("MOCK_NAME", "mock")
API_KEY = os.environ.get("MOCK_API_KEY", "mockkey")
SEED_PATH = os.environ.get("MOCK_SEED", "/seed/seed.json")
PORT = int(os.environ.get("MOCK_PORT", "8989"))

ENTITY = "series" if APP == "sonarr" else "movie"

# 1x1 transparent PNG -- enough to prove bytes and content-type survive the hop.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

with open(SEED_PATH, "r", encoding="utf-8") as handle:
    SEED: dict[str, Any] = json.load(handle)

REQUESTS: list[dict[str, Any]] = []


def _authorized(request: Request) -> bool:
    supplied = request.headers.get("x-api-key") or request.query_params.get("apikey")
    return supplied == API_KEY


def _record(request: Request) -> None:
    REQUESTS.append(
        {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "instance": NAME,
        }
    )


def _rows(name: str) -> list[dict[str, Any]]:
    return list(SEED.get(name, []))


def _path_value(record: Any, dotted: str) -> Any:
    current = record
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _sorted(rows: list[dict], key: str | None, direction: str) -> list[dict]:
    if not key:
        return rows

    def rank(row: dict) -> tuple:
        value = _path_value(row, key)
        if value is None:
            return (2, 0.0, "")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (0, float(value), "")
        return (1, 0.0, str(value).lower())

    return sorted(rows, key=rank, reverse=direction.lower().startswith("desc"))


def paged(request: Request, collection: str, default_sort: str | None = None) -> Response:
    rows = _rows(collection)
    page = max(int(request.query_params.get("page", 1) or 1), 1)
    size = max(int(request.query_params.get("pageSize", 20) or 20), 1)
    rows = _sorted(
        rows,
        request.query_params.get("sortKey") or default_sort,
        request.query_params.get("sortDirection", "ascending"),
    )
    start = (page - 1) * size
    return JSONResponse(
        {
            "page": page,
            "pageSize": size,
            "sortKey": request.query_params.get("sortKey") or default_sort or "",
            "sortDirection": request.query_params.get("sortDirection", "ascending"),
            "totalRecords": len(rows),
            "records": rows[start : start + size],
        }
    )


async def body_json(request: Request) -> Any:
    """Parse a request body the way the real apps do: 400 on garbage."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise BadBody()


class BadBody(Exception):
    pass


async def dispatch(request: Request) -> Response:
    try:
        return await _dispatch(request)
    except BadBody:
        return JSONResponse(
            [{"errorMessage": "Invalid request body"}], status_code=400
        )


async def _dispatch(request: Request) -> Response:
    path = request.url.path.rstrip("/") or "/"

    if path == "/__mock/requests":
        return JSONResponse(REQUESTS)
    if path == "/__mock/reset":
        REQUESTS.clear()
        return JSONResponse({"reset": True})
    if path == "/ping":
        return JSONResponse({"status": "OK"})

    if not _authorized(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    _record(request)

    method = request.method.upper()
    api = "/api/v3"

    # ---- media cover -----------------------------------------------------
    if path.lower().startswith("/mediacover/"):
        parts = path.split("/")
        entity_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        if not any(row.get("id") == entity_id for row in _rows(ENTITY)):
            return JSONResponse({"error": "NotFound"}, status_code=404)
        return Response(PNG, media_type="image/png",
                        headers={"X-Mock-Instance": NAME})

    if not path.startswith(api):
        return JSONResponse({"error": "NotFound"}, status_code=404)

    tail = path[len(api) :] or "/"
    segments = [s for s in tail.split("/") if s]

    # ---- system ----------------------------------------------------------
    if tail == "/system/status":
        return JSONResponse(
            {
                "appName": "Sonarr" if APP == "sonarr" else "Radarr",
                "instanceName": NAME,
                "version": "4.0.0.0",
                "isProduction": True,
                "urlBase": "",
            }
        )
    if tail == "/health":
        return JSONResponse(_rows("health"))
    if tail == "/diskspace":
        return JSONResponse(_rows("diskspace"))
    if tail == "/queue/status":
        rows = _rows("queue")
        return JSONResponse(
            {"totalCount": len(rows), "count": len(rows), "unknownCount": 0,
             "errors": False, "warnings": False}
        )
    # Test hook: only the anime instance hangs, so the suite can check that
    # one slow instance does not stall the whole merged response.
    if tail == "/__slow":
        if "anime" in NAME:
            await asyncio.sleep(30)
        return JSONResponse([{"id": 1, "instance": NAME}])

    # Test hook: each instance answers this path with a DIFFERENT status, so
    # the suite can check what the proxy does when instances disagree.
    if tail == "/__mixed":
        return JSONResponse(
            {"instance": NAME}, status_code=404 if "main" in NAME else 500
        )

    if tail.startswith("/config") or tail == "/localization":
        return JSONResponse({"id": 1, "instance": NAME, "urlBase": ""})

    # ---- lookup ----------------------------------------------------------
    if tail == f"/{ENTITY}/lookup":
        raw_term = (request.query_params.get("term") or "").strip()
        kind, sep, value = raw_term.partition(":")
        field = {"tmdb": "tmdbId", "tvdb": "tvdbId", "imdb": "imdbId"}.get(
            kind.lower().removesuffix("id")
        )
        if sep and field and value:
            # Like the real apps: an exact id lookup returns the library record
            # (carrying its id) when this instance holds the title, and bare
            # metadata with id 0 when it does not.
            owned = [r for r in _rows(ENTITY) if str(r.get(field)) == value]
            if owned:
                return JSONResponse(owned)
            return JSONResponse(
                [dict(r, id=0) for r in _rows("lookup") if str(r.get(field)) == value]
            )
        term = raw_term.lower()
        hits = [r for r in _rows("lookup") if term in str(r.get("title", "")).lower()]
        return JSONResponse(hits)

    # ---- collections -----------------------------------------------------
    simple = {
        "/qualityprofile": "qualityprofile",
        "/rootfolder": "rootfolder",
        "/tag": "tag",
        "/calendar": "calendar",
        "/command": "command",
        "/collection": "collection",
        "/queue/details": "queue",
    }
    if tail in simple and method == "GET":
        return JSONResponse(_rows(simple[tail]))

    if tail == "/queue" and method == "GET":
        return paged(request, "queue", "timeleft")
    if tail == "/history" and method == "GET":
        return paged(request, "history", "date")
    if tail in ("/wanted/missing", "/wanted/cutoff") and method == "GET":
        return paged(request, "wanted", "airDateUtc" if APP == "sonarr" else "title")

    if tail == "/episode" and method == "GET":
        series_id = request.query_params.get("seriesId")
        rows = _rows("episode")
        if series_id:
            rows = [r for r in rows if str(r.get("seriesId")) == str(series_id)]
        return JSONResponse(rows)

    # ---- primary entity --------------------------------------------------
    if tail == f"/{ENTITY}":
        if method == "GET":
            return JSONResponse(_rows(ENTITY))
        if method == "POST":
            body = await body_json(request)
            created = dict(body)
            created["id"] = max([r["id"] for r in _rows(ENTITY)] + [0]) + 1
            created.setdefault("title", "New")
            SEED.setdefault(ENTITY, []).append(created)
            return JSONResponse(created, status_code=201)

    if len(segments) == 2 and segments[0] == ENTITY and segments[1].isdigit():
        entity_id = int(segments[1])
        rows = SEED.setdefault(ENTITY, [])
        found = next((r for r in rows if r.get("id") == entity_id), None)
        if found is None:
            return JSONResponse({"error": "NotFound"}, status_code=404)
        if method == "GET":
            return JSONResponse(found)
        if method == "PUT":
            body = await body_json(request)
            found.update(body)
            return JSONResponse(found)
        if method == "DELETE":
            rows.remove(found)
            return JSONResponse({}, status_code=200)

    if tail == "/command" and method == "POST":
        body = await body_json(request)
        return JSONResponse(
            {"id": 900 + len(REQUESTS), "name": body.get("name", "Unknown"),
             "status": "queued", "body": body, "instance": NAME},
            status_code=201,
        )

    return JSONResponse({"error": "NotFound", "path": path}, status_code=404)


app = Starlette(
    routes=[
        Route(
            "/{path:path}",
            dispatch,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        )
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
