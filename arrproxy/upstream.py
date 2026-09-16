"""HTTP access to the backing Sonarr/Radarr instances."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from .config import AppConfig, Instance, Settings

log = logging.getLogger("arrproxy.upstream")

# Hop-by-hop headers plus the ones we must own ourselves.  Content-Length and
# Content-Encoding are dropped because httpx already decoded the body and we may
# re-serialise it at a different length.
DROP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "server",
    "date",
}
DROP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "accept-encoding",
    "x-api-key",
    "authorization",
    "cookie",
}


@dataclass
class Reply:
    """One instance's answer to a fanned-out request."""

    instance: Instance
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            return None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type.lower()


class Upstream:
    """Owns one keep-alive pool per instance and does the fan-out."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def start(self) -> None:
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=30)
        timeout = httpx.Timeout(
            self.settings.timeout, connect=self.settings.connect_timeout
        )
        for app in self.settings.apps.values():
            for inst in app.instances:
                self._clients[self._key(app, inst)] = httpx.AsyncClient(
                    base_url=inst.base,
                    timeout=timeout,
                    limits=limits,
                    follow_redirects=True,
                    headers={"X-Api-Key": inst.api_key, "Accept": "application/json"},
                )

    async def close(self) -> None:
        await asyncio.gather(
            *(c.aclose() for c in self._clients.values()), return_exceptions=True
        )
        self._clients.clear()

    @staticmethod
    def _key(app: AppConfig, inst: Instance) -> str:
        return f"{app.app_type}/{inst.name}"

    def client(self, app: AppConfig, inst: Instance) -> httpx.AsyncClient:
        return self._clients[self._key(app, inst)]

    async def call(
        self,
        app: AppConfig,
        inst: Instance,
        method: str,
        path: str,
        *,
        params: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        stream_hint: bool = False,
        deadline: float | None = None,
    ) -> Reply:
        """Perform one upstream request, converting transport errors into a Reply.

        ``deadline`` caps how long a *single* instance may hold up a fan-out. An
        instance that is reachable but hung would otherwise stall the whole
        merged response for the full request timeout, which on a Jellyfin home
        screen reads as the page being broken.
        """
        started = time.monotonic()
        client = self.client(app, inst)
        send_headers = dict(headers or {})
        if stream_hint:
            send_headers.pop("accept", None)
        try:
            request = client.request(
                method,
                path if path.startswith("/") else "/" + path,
                params=params,
                content=content,
                headers=send_headers or None,
            )
            if deadline and deadline > 0:
                response = await asyncio.wait_for(request, timeout=deadline)
            else:
                response = await request
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            log.warning(
                "%s did not answer %s %s within %.1fs; serving without it",
                inst.name, method, path, deadline or 0,
            )
            return Reply(
                instance=inst,
                error=f"timed out after {deadline}s",
                elapsed=elapsed,
            )
        except httpx.HTTPError as exc:
            elapsed = time.monotonic() - started
            log.warning(
                "%s %s %s failed after %.2fs: %s",
                inst.name,
                method,
                path,
                elapsed,
                exc,
            )
            return Reply(instance=inst, error=f"{type(exc).__name__}: {exc}", elapsed=elapsed)

        if response.status_code in (401, 403):
            # Almost always a wrong api_key in the config rather than a genuine
            # outage, and it would otherwise hide behind a generic "degraded".
            log.warning(
                "%s rejected our API key (HTTP %d) for %s %s -- check "
                "apps.*.instances[%s].api_key",
                inst.name, response.status_code, method, path, inst.name,
            )
        elif response.status_code >= 500:
            log.warning(
                "%s returned HTTP %d for %s %s", inst.name, response.status_code,
                method, path,
            )

        return Reply(
            instance=inst,
            status=response.status_code,
            headers={
                k.lower(): v
                for k, v in response.headers.items()
                if k.lower() not in DROP_RESPONSE_HEADERS
            },
            body=response.content,
            elapsed=time.monotonic() - started,
        )

    async def fanout(
        self,
        app: AppConfig,
        instances: Iterable[Instance],
        method: str,
        path: str,
        **kwargs: Any,
    ) -> list[Reply]:
        """Call every instance concurrently, preserving configuration order."""
        targets = [i for i in instances if i.enabled]
        results = await asyncio.gather(
            *(self.call(app, inst, method, path, **kwargs) for inst in targets)
        )
        return list(results)


def sanitize_request_headers(raw: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Forward the client's headers minus anything we must not relay upstream."""
    out: dict[str, str] = {}
    for key, value in raw:
        lowered = key.lower()
        if lowered in DROP_REQUEST_HEADERS:
            continue
        out[key] = value
    return out
