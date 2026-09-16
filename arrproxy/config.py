"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

APP_DEFAULT_PORTS = {"sonarr": 8989, "radarr": 7878, "lidarr": 8686, "readarr": 8787}
APP_API_VERSION = {"sonarr": "v3", "radarr": "v3", "lidarr": "v1", "readarr": "v1"}

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Raised for a configuration problem the operator has to fix."""


def _expand(node: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} inside string values."""
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    if isinstance(node, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), node)
    return node


@dataclass
class Routing:
    """Rules that pick an instance for a create that carries no virtual id."""

    series_types: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    root_folders: list[str] = field(default_factory=list)
    title_regex: str | None = None

    def matches(self, payload: dict[str, Any]) -> bool:
        if self.series_types:
            value = str(payload.get("seriesType") or "").lower()
            if value and value in {s.lower() for s in self.series_types}:
                return True
        if self.genres:
            have = {str(g).lower() for g in payload.get("genres") or []}
            if have & {g.lower() for g in self.genres}:
                return True
        if self.root_folders:
            path = str(payload.get("rootFolderPath") or "")
            if path and any(path.rstrip("/") == r.rstrip("/") for r in self.root_folders):
                return True
        if self.title_regex:
            title = str(payload.get("title") or payload.get("sortTitle") or "")
            if title and re.search(self.title_regex, title, re.IGNORECASE):
                return True
        return False


@dataclass
class Instance:
    name: str
    url: str
    api_key: str
    index: int = 0
    is_default: bool = False
    routing: Routing = field(default_factory=Routing)
    enabled: bool = True
    # Where a *browser* can reach this instance's own web UI.  `url` is usually
    # a container name, which a browser cannot resolve, so deep links (SeerrFin's
    # "Open in Sonarr" button) need a separately reachable address.
    public_url: str | None = None

    @property
    def base(self) -> str:
        return self.url.rstrip("/")

    @property
    def browser_base(self) -> str:
        return (self.public_url or self.url).rstrip("/")


@dataclass
class AppConfig:
    app_type: str
    api_key: str
    instances: list[Instance]
    port: int
    api_version: str
    instance_name: str

    @property
    def default_instance(self) -> Instance:
        for inst in self.instances:
            if inst.is_default:
                return inst
        return self.instances[0]


@dataclass
class Settings:
    apps: dict[str, AppConfig]
    host: str = "0.0.0.0"
    unified_port: int | None = 8787
    log_level: str = "info"
    timeout: float = 30.0
    connect_timeout: float = 5.0
    fanout_timeout: float = 10.0
    cache_ttl: float = 5.0
    fail_open: bool = True
    id_block: int = 10_000_000
    id_fallback_probe: bool = True
    max_page_fetch: int = 2000
    config_path: Path | None = None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _build_app(app_type: str, raw: dict[str, Any], generated: dict[str, str]) -> AppConfig:
    if app_type not in APP_API_VERSION:
        raise ConfigError(
            f"unknown app type {app_type!r}; expected one of {sorted(APP_API_VERSION)}"
        )

    raw_instances = raw.get("instances") or []
    if not raw_instances:
        raise ConfigError(f"app {app_type!r} has no instances configured")

    instances: list[Instance] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_instances):
        name = str(item.get("name") or f"{app_type}-{index}")
        if name in seen:
            raise ConfigError(f"duplicate instance name {name!r} in app {app_type!r}")
        seen.add(name)

        url = str(item.get("url") or "").strip()
        if not url:
            raise ConfigError(f"instance {name!r} is missing a url")
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"instance {name!r} url must start with http:// or https://")

        key = str(item.get("api_key") or "").strip()
        if not key:
            raise ConfigError(f"instance {name!r} is missing an api_key")

        public = str(item.get("public_url") or "").strip() or None
        if public and not public.startswith(("http://", "https://")):
            raise ConfigError(
                f"instance {name!r} public_url must start with http:// or https://"
            )

        rt = item.get("routing") or {}
        instances.append(
            Instance(
                name=name,
                url=url,
                api_key=key,
                index=index,
                public_url=public,
                is_default=bool(item.get("default", False)),
                enabled=bool(item.get("enabled", True)),
                routing=Routing(
                    series_types=_as_list(rt.get("series_types") or rt.get("series_type")),
                    genres=_as_list(rt.get("genres")),
                    root_folders=_as_list(rt.get("root_folders")),
                    title_regex=rt.get("title_regex"),
                ),
            )
        )

    if sum(1 for i in instances if i.is_default) > 1:
        raise ConfigError(f"app {app_type!r} marks more than one instance as default")

    api_key = str(raw.get("api_key") or "").strip()
    if not api_key:
        # A generated key still has to be stable across restarts or every client
        # would need reconfiguring, so we persist it back beside the config.
        api_key = secrets.token_hex(16)
        generated[app_type] = api_key
    if len(api_key) < 8:
        raise ConfigError(f"app {app_type!r} api_key is too short to be a credential")

    return AppConfig(
        app_type=app_type,
        api_key=api_key,
        instances=instances,
        port=int(raw.get("port") or APP_DEFAULT_PORTS[app_type]),
        api_version=str(raw.get("api_version") or APP_API_VERSION[app_type]),
        instance_name=str(raw.get("instance_name") or f"{app_type.title()} (combined)"),
    )


def load(path: str | os.PathLike[str] | None = None) -> Settings:
    path = Path(path or os.environ.get("ARRPROXY_CONFIG", "/config/config.yaml"))
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    if path.is_dir():
        # Docker silently creates a directory when a bind-mount source is
        # missing, so this is the usual first-run mistake.
        raise ConfigError(
            f"{path} is a directory, not a file. If this is a Docker bind mount, "
            "the source file does not exist on the host -- create it before "
            "starting the container."
        )

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    raw = _expand(parsed or {})
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    server = raw.get("server") or {}
    apps_raw = raw.get("apps") or {}
    if not apps_raw:
        raise ConfigError("config has no apps: section")

    generated: dict[str, str] = {}
    apps = {name: _build_app(name, cfg or {}, generated) for name, cfg in apps_raw.items()}

    ports = [a.port for a in apps.values()]
    unified = server.get("unified_port", 8787)
    unified = int(unified) if unified else None
    if unified is not None:
        ports.append(unified)
    if len(ports) != len(set(ports)):
        raise ConfigError(f"listener ports collide: {sorted(ports)}")

    settings = Settings(
        apps=apps,
        host=str(server.get("host") or "0.0.0.0"),
        unified_port=unified,
        log_level=str(server.get("log_level") or "info").lower(),
        timeout=float(server.get("timeout", 30.0)),
        connect_timeout=float(server.get("connect_timeout", 5.0)),
        fanout_timeout=float(server.get("fanout_timeout", 10.0)),
        cache_ttl=float(server.get("cache_ttl", 5.0)),
        fail_open=bool(server.get("fail_open", True)),
        id_block=int(server.get("id_block", 10_000_000)),
        id_fallback_probe=bool(server.get("id_fallback_probe", True)),
        max_page_fetch=int(server.get("max_page_fetch", 2000)),
        config_path=path,
    )

    if generated:
        _persist_generated_keys(path, generated)
    return settings


def _persist_generated_keys(path: Path, generated: dict[str, str]) -> None:
    """Write auto-generated api keys back into the YAML so they survive restarts."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for app_type, key in generated.items():
            data.setdefault("apps", {}).setdefault(app_type, {})["api_key"] = key
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    except OSError as exc:  # read-only mount: refuse rather than rotate keys silently
        raise ConfigError(
            "no api_key was set for "
            + ", ".join(generated)
            + f" and the generated one could not be saved to {path}: {exc}. "
            "Set apps.<name>.api_key explicitly."
        ) from exc
