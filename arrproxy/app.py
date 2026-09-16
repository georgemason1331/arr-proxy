"""ASGI surface: authentication, app selection, and the local endpoints."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import Settings
from .routing import AppRouter
from .upstream import Upstream

log = logging.getLogger("arrproxy.app")

__version__ = "1.0.0"

# Served without a key so Docker health checks and "is it up?" probes work.
PUBLIC_PATHS = {"/ping", "/-/health", "/-/version", "/"}


class ProxyApp:
    """One ASGI listener.

    ``bound_app`` pins a listener to a single combined app so the plugin can be
    pointed at a bare ``http://host:port``.  The unified listener leaves it
    unset and selects on a ``/sonarr`` or ``/radarr`` path prefix instead.
    """

    def __init__(
        self,
        settings: Settings,
        routers: dict[str, AppRouter],
        upstream: Upstream,
        bound_app: str | None = None,
    ) -> None:
        self.settings = settings
        self.routers = routers
        self.upstream = upstream
        self.bound_app = bound_app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        try:
            response = await self.handle(request)
        except Exception:  # never leak a stack trace to a media client
            log.exception("unhandled error for %s %s", request.method, request.url.path)
            response = JSONResponse({"error": "internal proxy error"}, status_code=500)
        await response(scope, receive, send)

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ------------------------------------------------------------------
    def _select(self, path: str) -> tuple[AppRouter | None, str]:
        """Resolve the target router and the path as the upstream will see it."""
        for name, router in self.routers.items():
            prefix = f"/{name}"
            if path == prefix or path.startswith(prefix + "/"):
                return router, path[len(prefix) :] or "/"
        if self.bound_app:
            return self.routers.get(self.bound_app), path
        return None, path

    async def handle(self, request: Request) -> Response:
        path = request.url.path.rstrip("/") or "/"
        router, sub_path = self._select(path)

        if sub_path in PUBLIC_PATHS or path in PUBLIC_PATHS:
            return await self._public(request, sub_path if router else path, router)

        if router is None:
            return JSONResponse(
                {
                    "error": "no app selected",
                    "detail": "prefix the request with /"
                    + " or /".join(self.routers)
                    + ", or use that app's dedicated port",
                },
                status_code=404,
            )

        if router.is_browser_link(sub_path, request.method):
            # Deep links are opened by a browser that has no API key, so they
            # are unauthenticated by necessity. They only issue a redirect to an
            # instance the viewer can already reach.
            return await router.dispatch(request, sub_path)

        supplied = self._client_key(request)
        if not supplied:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        # Compare as bytes: compare_digest refuses non-ASCII str, which would
        # turn a key with an accented character into a 500 instead of a 401.
        if not hmac.compare_digest(
            supplied.encode("utf-8"), router.app.api_key.encode("utf-8")
        ):
            log.warning(
                "rejected %s %s: bad api key from %s",
                request.method,
                path,
                request.client.host if request.client else "?",
            )
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await router.dispatch(request, sub_path)

    @staticmethod
    def _client_key(request: Request) -> str | None:
        header = request.headers.get("x-api-key")
        if header:
            return header.strip()
        for name in ("apikey", "apiKey"):
            value = request.query_params.get(name)
            if value:
                return value.strip()
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    # ------------------------------------------------------------------
    async def _public(
        self, request: Request, path: str, router: AppRouter | None
    ) -> Response:
        if path == "/ping":
            return JSONResponse({"status": "OK"})
        if path == "/-/version":
            return JSONResponse({"version": __version__})
        if path == "/-/health":
            return await self._health(router)
        return JSONResponse(
            {
                "service": "arr-proxy",
                "version": __version__,
                "apps": {
                    name: {
                        "port": r.app.port,
                        "prefix": f"/{name}",
                        "instances": [i.name for i in r.app.instances],
                    }
                    for name, r in self.routers.items()
                },
            }
        )

    async def _health(self, only: AppRouter | None) -> Response:
        """Actively probe every instance so an outage is visible, not inferred."""
        routers = [only] if only else list(self.routers.values())
        report: dict[str, Any] = {}
        healthy = True

        async def probe(router: AppRouter, inst) -> dict[str, Any]:
            started = time.monotonic()
            reply = await self.upstream.call(
                router.app, inst, "GET", f"{router.api_prefix}/system/status"
            )
            body = reply.json() if reply.ok else None
            return {
                "name": inst.name,
                "url": inst.url,
                "enabled": inst.enabled,
                "reachable": reply.ok,
                "status": reply.status or None,
                "version": (body or {}).get("version") if isinstance(body, dict) else None,
                "error": reply.error,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            }

        for router in routers:
            if router is None:
                continue
            results = await asyncio.gather(
                *(probe(router, inst) for inst in router.app.instances)
            )
            reachable = [r for r in results if r["reachable"]]
            if not reachable:
                healthy = False
            report[router.app.app_type] = {
                "healthy": bool(reachable),
                "degraded": len(reachable) != len(results),
                "instances": list(results),
            }

        return JSONResponse(
            {"status": "ok" if healthy else "unhealthy", "apps": report},
            status_code=200 if healthy else 503,
        )


def build(settings: Settings) -> tuple[Upstream, dict[str, AppRouter], list[tuple[int, ProxyApp]]]:
    """Wire the routers and return the listeners to run."""
    upstream = Upstream(settings)
    routers = {
        name: AppRouter(settings, app, upstream) for name, app in settings.apps.items()
    }

    listeners: list[tuple[int, ProxyApp]] = [
        (router.app.port, ProxyApp(settings, routers, upstream, bound_app=name))
        for name, router in routers.items()
    ]
    if settings.unified_port:
        listeners.append(
            (settings.unified_port, ProxyApp(settings, routers, upstream, bound_app=None))
        )
    return upstream, routers, listeners
