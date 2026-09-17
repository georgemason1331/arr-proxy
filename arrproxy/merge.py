"""Strategies for folding several instances' answers into one."""

from __future__ import annotations

from typing import Any, Callable

from . import paths
from .idmap import IdMapper
from .upstream import Reply


def decoded(reply: Reply, mapper: IdMapper) -> Any:
    """Parse one reply and lift its local ids into the virtual id space."""
    payload = reply.json()
    if payload is None:
        return None
    # Paths are rewritten here too, so every read -- merged or single -- serves
    # the same view of where a title lives.  Usually a no-op: see paths.py.
    return paths.rewrite(mapper.encode(payload, reply.instance.index), reply.instance.path_map)


def _path_value(record: Any, dotted: str) -> Any:
    current = record
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _rank(value: Any) -> tuple[int, float, str]:
    """Total order across mixed JSON types so sorting can never raise.

    Numbers first, then strings, then missing values -- which keeps rows the
    upstream could not supply a sort field for at the end of the list.
    """
    if value is None:
        return (2, 0.0, "")
    if isinstance(value, bool):
        return (0, float(value), "")
    if isinstance(value, (int, float)):
        return (0, float(value), "")
    return (1, 0.0, str(value).lower())


SortKey = "str | tuple[str, ...] | None"


def _resolve(record: Any, sort_key: Any) -> Any:
    """Value to sort a record by.

    A tuple means "first of these that is present" -- Radarr's calendar dates
    the same row by inCinemas, digitalRelease or physicalRelease depending on
    the title, so keying on any single one would strand half the rows.
    """
    if isinstance(sort_key, tuple):
        for candidate in sort_key:
            value = _path_value(record, candidate)
            if value is not None:
                return value
        return None
    return _path_value(record, sort_key)


def sorter(sort_key: Any, descending: bool = False) -> Callable[[list[Any]], list[Any]]:
    def apply(records: list[Any]) -> list[Any]:
        if not sort_key:
            return records
        return sorted(
            records, key=lambda r: _rank(_resolve(r, sort_key)), reverse=descending
        )

    return apply


def merge_list(
    replies: list[Reply],
    mapper: IdMapper,
    *,
    sort_key: Any = None,
    descending: bool = False,
) -> list[Any]:
    """Concatenate every instance's array, id-translated, optionally re-sorted."""
    merged: list[Any] = []
    for reply in replies:
        if not reply.ok:
            continue
        payload = decoded(reply, mapper)
        if isinstance(payload, list):
            merged.extend(payload)
        elif payload is not None:
            merged.append(payload)
    return sorter(sort_key, descending)(merged) if sort_key else merged


def merge_paged(
    replies: list[Reply],
    mapper: IdMapper,
    *,
    page: int,
    page_size: int,
    sort_key: Any,
    descending: bool,
) -> dict[str, Any]:
    """Merge Sonarr/Radarr paged envelopes into one consistent page.

    ``totalRecords`` is summed over the instances that actually answered, never
    over the ones we failed to reach.  Clients such as SeerrFin page until
    ``collected >= totalRecords``, so an inflated total would make them spin.
    """
    records: list[Any] = []
    total = 0
    for reply in replies:
        if not reply.ok:
            continue
        payload = decoded(reply, mapper)
        if not isinstance(payload, dict):
            continue
        rows = payload.get("records")
        if isinstance(rows, list):
            records.extend(rows)
        total += int(payload.get("totalRecords") or 0)

    records = sorter(sort_key, descending)(records) if sort_key else records

    start = max(page - 1, 0) * page_size
    window = records[start : start + page_size] if page_size > 0 else records

    return {
        "page": page,
        "pageSize": page_size,
        "sortKey": sort_key or "",
        "sortDirection": "descending" if descending else "ascending",
        "totalRecords": total,
        "records": window,
    }


EXTERNAL_KEYS = ("tvdbId", "tmdbId", "imdbId", "foreignId", "foreignArtistId", "titleSlug")


def merge_lookup(replies: list[Reply], mapper: IdMapper) -> list[Any]:
    """Merge ``/lookup`` results, collapsing the same title across instances.

    Every instance queries the same metadata server, so the rows are duplicates
    of each other.  The one worth keeping is whichever instance already has the
    title in its library (``id`` > 0), because that is what tells the client it
    is already added.
    """
    ordered: list[Any] = []
    position: dict[tuple[str, Any], int] = {}

    for reply in replies:
        if not reply.ok:
            continue
        payload = decoded(reply, mapper)
        if not isinstance(payload, list):
            continue
        for item in payload:
            key = None
            if isinstance(item, dict):
                for name in EXTERNAL_KEYS:
                    value = item.get(name)
                    if value:
                        key = (name, value)
                        break
            if key is None:
                ordered.append(item)
                continue
            if key not in position:
                position[key] = len(ordered)
                ordered.append(item)
            else:
                kept = ordered[position[key]]
                existing_id = kept.get("id") if isinstance(kept, dict) else None
                incoming_id = item.get("id") if isinstance(item, dict) else None
                if not existing_id and incoming_id:
                    ordered[position[key]] = item
    return ordered


def merge_health(replies: list[Reply], mapper: IdMapper) -> list[Any]:
    """Aggregate health checks, naming the instance each one came from."""
    merged: list[Any] = []
    for reply in replies:
        if not reply.ok:
            continue
        payload = decoded(reply, mapper)
        if not isinstance(payload, list):
            continue
        for item in payload:
            if isinstance(item, dict):
                item = dict(item)
                item["source"] = f"{reply.instance.name}:{item.get('source', '')}".rstrip(":")
                item["message"] = f"[{reply.instance.name}] {item.get('message', '')}".strip()
            merged.append(item)
    return merged


def merge_diskspace(replies: list[Reply], mapper: IdMapper) -> list[Any]:
    """Aggregate disk space, collapsing mounts the instances share."""
    merged: list[Any] = []
    seen: set[str] = set()
    for reply in replies:
        if not reply.ok:
            continue
        payload = decoded(reply, mapper)
        if not isinstance(payload, list):
            continue
        for item in payload:
            path = item.get("path") if isinstance(item, dict) else None
            if path is not None:
                if path in seen:
                    continue
                seen.add(path)
            merged.append(item)
    return merged


_SUM_KEYS = ("totalCount", "count", "unknownCount", "errors", "warnings",
             "unknownErrors", "unknownWarnings")


def merge_counters(replies: list[Reply], mapper: IdMapper) -> dict[str, Any]:
    """Sum the numeric counters of an endpoint like ``/queue/status``."""
    out: dict[str, Any] = {}
    for reply in replies:
        if not reply.ok:
            continue
        payload = decoded(reply, mapper)
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if isinstance(value, bool):
                out[key] = bool(out.get(key, False)) or value
            elif isinstance(value, (int, float)) and key in _SUM_KEYS:
                out[key] = out.get(key, 0) + value
            elif key not in out:
                out[key] = value
    return out


def merge_auto(
    replies: list[Reply],
    mapper: IdMapper,
    *,
    page: int,
    page_size: int,
    sort_key: Any,
    descending: bool,
) -> Any:
    """Shape-driven merge for endpoints with no explicit rule.

    Lets the proxy cover the whole API surface -- an unrecognised list endpoint
    still aggregates, an unrecognised paged endpoint still pages -- instead of
    failing on anything not enumerated.
    """
    usable = [r for r in replies if r.ok and r.is_json]
    if not usable:
        return None

    payloads = [(r, decoded(r, mapper)) for r in usable]

    if all(isinstance(p, list) for _, p in payloads):
        merged: list[Any] = []
        for _, p in payloads:
            merged.extend(p)
        return sorter(sort_key, descending)(merged) if sort_key else merged

    if all(
        isinstance(p, dict) and "records" in p and "totalRecords" in p for _, p in payloads
    ):
        return merge_paged(
            replies,
            mapper,
            page=page,
            page_size=page_size,
            sort_key=sort_key,
            descending=descending,
        )

    return payloads[0][1]
