"""Rewrite on-disk paths between an instance's view and the client's.

Some clients decide what a title *is* from where it lives.  Jellyfin's Home
Screen Sections works out which library an upcoming episode belongs to by
comparing Sonarr's ``path`` against its own library folders, as a plain string
prefix, and shows the item to everyone when nothing matches.  So when Jellyfin
sees the same media somewhere else -- a different mount point, or a virtual file
system such as Shoko's -- that library filter silently does nothing.

Mapping the prefix in what we serve makes the comparison work.  Nothing here
touches a disk: only these two keys' string values change, and every rewrite is
undone on the way back upstream, so a client that echoes a path back at us can
never make an *arr move files somewhere that does not exist.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# Sonarr and Radarr use these two keys for a folder belonging to one instance.
# `path` covers a series, movie, root folder and file; `rootFolderPath` is what
# a create carries.  Nothing else is touched.
PATH_KEYS = frozenset({"path", "rootFolderPath"})

Pairs = Sequence[tuple[str, str]]


def order(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Longest source first, so /data/media/anime-movies beats /data/media/anime."""
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def invert(pairs: Pairs) -> list[tuple[str, str]]:
    """The same mapping in reverse: what a client sends -> what the instance uses."""
    return order([(dst, src) for src, dst in pairs])


def rewrite(node: Any, pairs: Pairs) -> Any:
    """Return ``node`` with PATH_KEYS remapped.  No pairs means no work at all."""
    if not pairs:
        return node
    return _walk(node, pairs)


def _walk(node: Any, pairs: Pairs) -> Any:
    if isinstance(node, list):
        return [_walk(item, pairs) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        key: _swap(value, pairs) if key in PATH_KEYS and isinstance(value, str)
        else _walk(value, pairs)
        for key, value in node.items()
    }


def _swap(value: str, pairs: Pairs) -> str:
    for src, dst in pairs:
        if value == src:
            return dst
        # Whole segments only: without this, /data/media/anime would claim
        # /data/media/anime-movies and rewrite it to the wrong library.
        if value.startswith(src + "/") or value.startswith(src + "\\"):
            return dst + value[len(src):]
    return value
