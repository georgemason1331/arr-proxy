"""Virtual <-> real entity ID translation.

Every backing instance numbers its entities from 1 independently, so a naive
merge produces colliding ids and the client can no longer say which instance a
follow-up request belongs to.  We solve that with a stateless block offset:

    virtual = real + (instance_index * block)
    real    = virtual % block
    index   = virtual // block

Instance 0 (the primary) is therefore **identity mapped** -- its ids are handed
to clients untouched.  That is deliberate: anything that already holds a real id
from the primary instance (Seerr's externalServiceId, a bookmark, a log line)
keeps working through the proxy unchanged.

The scheme is stateless, survives restarts, and is trivially reversible, which
matters because the proxy must decode ids on paths it has never seen before.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

# 10 million ids per instance per entity type.  Sonarr/Radarr ids are int32 and
# each entity type (series, episode, history, ...) has its own sequence, so this
# leaves room for ~200 instances while being far beyond any real library.
DEFAULT_BLOCK = 10_000_000

# Keys whose integer values are ids *local to one instance* and so must be
# translated.  Anything not listed here is passed through untouched -- an
# allowlist, because guessing wrong on an external id (tmdbId, tvdbId) silently
# corrupts metadata lookups.
COMMON_ID_KEYS = frozenset(
    {
        "id",
        "qualityProfileId",
        "metadataProfileId",
        "languageProfileId",
        "rootFolderId",
        "indexerId",
        "downloadClientId",
        "importListId",
        "tags",
        "tagIds",
        "notificationId",
    }
)

APP_ID_KEYS: dict[str, frozenset[str]] = {
    "sonarr": COMMON_ID_KEYS
    | {"seriesId", "seriesIds", "episodeId", "episodeIds", "episodeFileId"},
    "radarr": COMMON_ID_KEYS
    | {"movieId", "movieIds", "movieFileId", "collectionId", "movieMetadataId"},
    "lidarr": COMMON_ID_KEYS
    | {"artistId", "artistIds", "albumId", "albumIds", "trackFileId", "trackId"},
    "readarr": COMMON_ID_KEYS
    | {"authorId", "authorIds", "bookId", "bookIds", "bookFileId"},
}

# Sub-objects we descend into but never rewrite.  These hold ids that are either
# global constants shared by every instance (quality definitions, languages) or
# purely decorative -- rewriting them would corrupt what the user sees, and
# leaving them raw is safe because nothing routes on them.
BLOCKED_SUBTREES = frozenset(
    {
        "quality",
        "revision",
        "language",
        "languages",
        "originalLanguage",
        "customFormats",
        "ratings",
        "statistics",
        "seasons",
        "images",
        "alternateTitles",
        "addOptions",
        "items",
        "formatItems",
        "fields",
        "specifications",
        "mediaInfo",
        "credits",
        "cast",
        "crew",
        "originalLanguages",
    }
)

# /MediaCover/15/poster.jpg and /api/v3/mediacover/15/poster.jpg embed the
# entity id in the path, so the id inside served URLs has to move too.
MEDIACOVER_RE = re.compile(r"(?i)(/(?:api/v\d+/)?mediacover/)(\d+)(/|$)")

log = logging.getLogger("arrproxy.idmap")


class IdMapper:
    """Translates ids between the proxy's virtual space and one instance."""

    def __init__(self, app_type: str, block: int = DEFAULT_BLOCK) -> None:
        self.app_type = app_type
        self.block = block
        self.id_keys = APP_ID_KEYS.get(app_type, COMMON_ID_KEYS)
        self._overflow_reported = False

    # -- scalar -----------------------------------------------------------
    def to_virtual(self, real_id: int, index: int) -> int:
        # 0 and negatives are sentinels ("not set", "not in library"), never
        # real rows -- translating them would invent an entity that isn't there.
        if not isinstance(real_id, int) or isinstance(real_id, bool) or real_id <= 0:
            return real_id
        if real_id >= self.block:
            self._report_overflow(real_id, index)
        return real_id + index * self.block

    def _report_overflow(self, real_id: int, index: int) -> None:
        """An id at or above the block size breaks the scheme silently.

        ``real_id`` would land inside the next instance's range, so a later
        request for it would be routed to the wrong instance.  Nothing here can
        repair that, so the one useful thing is to say so loudly and exactly
        once, with the fix.
        """
        if self._overflow_reported:
            return
        self._overflow_reported = True
        log.error(
            "%s instance %d returned id %d, which is >= id_block (%d). Ids this "
            "large collide with the next instance's range and will be routed "
            "incorrectly. Raise server.id_block above %d and restart.",
            self.app_type, index, real_id, self.block, real_id,
        )

    def to_real(self, virtual_id: int) -> tuple[int, int]:
        """Return (instance_index, real_id) for a virtual id."""
        if not isinstance(virtual_id, int) or isinstance(virtual_id, bool) or virtual_id <= 0:
            return 0, virtual_id
        return virtual_id // self.block, virtual_id % self.block

    def index_of(self, virtual_id: int) -> int:
        return self.to_real(virtual_id)[0]

    def is_ambiguous(self, virtual_id: int) -> bool:
        """True if this id could also be a raw id belonging to another instance.

        Ids below one block decode to the identity-mapped primary, but a client
        that learned the id from somewhere other than this proxy (Seerr stores
        the id the real instance handed it) may mean a different instance.  The
        caller uses this to decide whether a 404 is worth re-probing.
        """
        return isinstance(virtual_id, int) and 0 < virtual_id < self.block

    # -- documents --------------------------------------------------------
    def encode(self, node: Any, index: int) -> Any:
        """Rewrite an upstream document's local ids into virtual ids."""
        return self._walk(node, index, self.to_virtual)

    def decode(self, node: Any) -> Any:
        """Rewrite a client document's virtual ids back to one instance's ids."""
        return self._walk(node, None, lambda v, _i: self.to_real(v)[1])

    def _walk(self, node: Any, index: int | None, fn) -> Any:
        if isinstance(node, list):
            return [self._walk(item, index, fn) for item in node]
        if not isinstance(node, dict):
            return node

        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in BLOCKED_SUBTREES:
                out[key] = self._rewrite_urls_only(value, index)
            elif key in self.id_keys:
                out[key] = self._map_value(value, index, fn)
            else:
                out[key] = self._walk(value, index, fn)
        return out

    def _map_value(self, value: Any, index: int | None, fn) -> Any:
        if isinstance(value, list):
            return [self._map_value(v, index, fn) for v in value]
        if isinstance(value, bool) or not isinstance(value, int):
            # tags is sometimes a list of label strings; leave those alone.
            return self._walk(value, index, fn)
        return fn(value, index)

    def _rewrite_urls_only(self, node: Any, index: int | None) -> Any:
        """Inside a blocked subtree only MediaCover URLs are touched."""
        if isinstance(node, list):
            return [self._rewrite_urls_only(n, index) for n in node]
        if isinstance(node, dict):
            return {k: self._rewrite_urls_only(v, index) for k, v in node.items()}
        if isinstance(node, str) and index is not None:
            return self.rewrite_cover_url(node, index)
        return node

    # -- media covers -----------------------------------------------------
    def rewrite_cover_url(self, url: str, index: int) -> str:
        def sub(match: re.Match[str]) -> str:
            return (
                f"{match.group(1)}{self.to_virtual(int(match.group(2)), index)}"
                f"{match.group(3)}"
            )

        return MEDIACOVER_RE.sub(sub, url)


def collect_indexes(mapper: IdMapper, values: Iterable[Any]) -> set[int]:
    """Instance indexes referenced by a set of virtual ids."""
    found: set[int] = set()
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            found.add(mapper.index_of(value))
    return found
