"""Entry point: run every listener in one event loop."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

import uvicorn

from .app import __version__, build
from .config import ConfigError, load

log = logging.getLogger("arrproxy")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _serve(settings) -> int:
    upstream, routers, listeners = build(settings)
    await upstream.start()

    servers = [
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.host,
                port=port,
                log_level=settings.log_level,
                access_log=settings.log_level in ("debug", "trace"),
                lifespan="on",
                # One process owns several servers, so signals are handled once
                # here rather than by each server fighting over the handler.
                timeout_graceful_shutdown=10,
            )
        )
        for port, app in listeners
    ]
    for server in servers:
        server.config.install_signal_handlers = False

    for name, router in routers.items():
        log.info(
            "%s -> port %s, instances: %s",
            name,
            router.app.port,
            ", ".join(f"{i.name}({i.url})" for i in router.app.instances),
        )
        log.info("%s combined api key: %s", name, router.app.api_key)
    if settings.unified_port:
        log.info(
            "unified listener on port %s (%s)",
            settings.unified_port,
            ", ".join(f"/{n}" for n in routers),
        )

    stopping = asyncio.Event()

    def _stop(*_args: object) -> None:
        if not stopping.is_set():
            log.info("shutting down")
            stopping.set()
            for server in servers:
                server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # Windows
            signal.signal(sig, _stop)

    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        await upstream.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arrproxy",
        description="Combine several Sonarr/Radarr instances behind one URL and API key.",
    )
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("ARRPROXY_CONFIG", "/config/config.yaml"),
        help="path to config.yaml (env: ARRPROXY_CONFIG)",
    )
    parser.add_argument("--check", action="store_true",
                        help="validate the config and exit")
    parser.add_argument("--version", action="version", version=f"arr-proxy {__version__}")
    args = parser.parse_args(argv)

    _setup_logging(os.environ.get("ARRPROXY_LOG_LEVEL", "info"))
    try:
        settings = load(args.config)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    _setup_logging(settings.log_level)

    if args.check:
        for name, app in settings.apps.items():
            print(f"{name}: port {app.port}, {len(app.instances)} instance(s)")
            for inst in app.instances:
                flag = " (default)" if inst.is_default else ""
                print(f"  [{inst.index}] {inst.name} -> {inst.url}{flag}")
        print("config OK")
        return 0

    try:
        return asyncio.run(_serve(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
