"""Generate the four mock-instance datasets used by the end-to-end suite.

Ids deliberately collide across instances (both Sonarr mocks own a series ``1``)
because that collision is exactly what the proxy exists to resolve.  Calendar
dates interleave between instances so a correctly merged, correctly sorted
calendar has to alternate between them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

OUT = Path(__file__).parent / "seeds"


def slugify(title: str) -> str:
    """Mirrors the titleSlug the *arrs generate; SeerrFin deep-links on it."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def cover(entity_id: int) -> list[dict]:
    return [
        {
            "coverType": "poster",
            "url": f"/MediaCover/{entity_id}/poster.jpg?lastWrite=638",
            "remoteUrl": f"https://images.example/{entity_id}/poster.jpg",
        }
    ]


def quality(qid: int, name: str) -> dict:
    # Quality *definition* ids are global constants shared by every instance;
    # the proxy must leave them alone even while remapping everything around.
    return {"quality": {"id": qid, "name": name, "source": "web"},
            "revision": {"version": 1, "real": 0}}


def sonarr_seed(name: str, series: list[dict], anime: bool) -> dict:
    episodes, calendar, queue, history, wanted = [], [], [], [], []
    for offset, show in enumerate(series):
        sid = show["id"]
        for n in (1, 2):
            eid = sid * 10 + n
            episodes.append({
                "id": eid, "seriesId": sid, "seasonNumber": 1, "episodeNumber": n,
                "absoluteEpisodeNumber": n, "title": f"{show['title']} E{n}",
                "airDateUtc": f"2026-09-{16 + offset * 2 + (1 if anime else 0):02d}T01:00:00Z",
                "hasFile": n == 1, "monitored": True, "episodeFileId": 0,
            })
        day = 16 + offset * 2 + (1 if anime else 0)
        calendar.append({
            "id": sid * 10 + 2, "seriesId": sid, "seasonNumber": 1, "episodeNumber": 2,
            "title": f"{show['title']} E2", "airDateUtc": f"2026-09-{day:02d}T01:00:00Z",
            "hasFile": False, "monitored": True,
            "series": {"id": sid, "title": show["title"], "path": show["path"],
                       "images": cover(sid)},
        })
        queue.append({
            "id": 500 + sid, "seriesId": sid, "episodeId": sid * 10 + 2,
            "title": f"{show['title']}.S01E02.1080p", "size": 1000.0 * sid,
            "sizeleft": 100.0 * sid, "status": "downloading",
            "timeleft": f"00:0{sid}:00", "trackedDownloadState": "downloading",
            "quality": quality(3, "WEBDL-1080p"), "downloadId": f"HASH{sid}",
            "series": {"id": sid, "title": show["title"], "path": show["path"]},
            "episode": {"id": sid * 10 + 2, "seriesId": sid, "seasonNumber": 1,
                        "episodeNumber": 2, "title": f"{show['title']} E2"},
        })
        history.append({
            "id": 700 + sid, "seriesId": sid, "episodeId": sid * 10 + 1,
            "eventType": "grabbed", "sourceTitle": f"{show['title']}.S01E01",
            "date": f"2026-09-{10 + offset:02d}T0{sid}:00:00Z",
            "quality": quality(3, "WEBDL-1080p"),
        })
        wanted.append({
            "id": sid * 10 + 2, "seriesId": sid, "seasonNumber": 1, "episodeNumber": 2,
            "title": f"{show['title']} E2", "airDateUtc": f"2026-09-{day:02d}T01:00:00Z",
            "monitored": True, "hasFile": False,
        })

    root = "/data/media/anime" if anime else "/data/media/tv"
    return {
        "series": series,
        "episode": episodes,
        "calendar": calendar,
        "queue": queue,
        "history": history,
        "wanted": wanted,
        "qualityprofile": (
            [{"id": 1, "name": "[Anime] Remux-1080p", "upgradeAllowed": True,
              "cutoff": 3, "items": [{"id": 1000, "quality": {"id": 3, "name": "WEBDL-1080p"},
                                      "allowed": True}]}]
            if anime else
            [{"id": 1, "name": "WEB-1080p", "upgradeAllowed": True, "cutoff": 3,
              "items": [{"id": 1000, "quality": {"id": 3, "name": "WEBDL-1080p"},
                         "allowed": True}]},
             {"id": 2, "name": "WEB-2160p", "upgradeAllowed": True, "cutoff": 5,
              "items": [{"id": 1001, "quality": {"id": 5, "name": "WEBDL-2160p"},
                         "allowed": True}]}]
        ),
        "rootfolder": [{"id": 1, "path": root, "accessible": True, "freeSpace": 900}],
        "tag": [{"id": 1, "label": "anime" if anime else "hd"}],
        "health": [{"source": "IndexerStatusCheck", "type": "warning",
                    "message": f"{name} indexer unavailable", "wikiUrl": ""}],
        # Both instances live on the same host, so they report the same mounts --
        # the proxy has to collapse them instead of double-counting the disk.
        "diskspace": [{"path": "/data", "label": "data", "freeSpace": 500,
                       "totalSpace": 4000}],
        "command": [{"id": 1, "name": "RefreshSeries", "status": "completed"}],
        "lookup": [],
    }


def radarr_seed(name: str, movies: list[dict], anime: bool) -> dict:
    calendar, queue, history, wanted = [], [], [], []
    for offset, film in enumerate(movies):
        mid = film["id"]
        day = 16 + offset * 2 + (1 if anime else 0)
        calendar.append({**film, "inCinemas": f"2026-09-{day:02d}T00:00:00Z",
                         "digitalRelease": f"2026-10-{day:02d}T00:00:00Z",
                         "hasFile": False})
        queue.append({
            "id": 500 + mid, "movieId": mid, "title": f"{film['title']}.2026.1080p",
            "size": 2000.0 * mid, "sizeleft": 200.0 * mid, "status": "downloading",
            "timeleft": f"00:0{mid}:00", "quality": quality(3, "WEBDL-1080p"),
            "downloadId": f"MHASH{mid}",
            "movie": {"id": mid, "title": film["title"], "tmdbId": film["tmdbId"],
                      "path": film["path"]},
        })
        history.append({
            "id": 700 + mid, "movieId": mid, "eventType": "grabbed",
            "sourceTitle": film["title"],
            "date": f"2026-09-{10 + offset:02d}T0{mid}:00:00Z",
            "quality": quality(3, "WEBDL-1080p"),
        })
        wanted.append({**film, "hasFile": False, "monitored": True})

    root = "/data/media/anime-movies" if anime else "/data/media/movies"
    return {
        "movie": movies,
        "calendar": calendar,
        "queue": queue,
        "history": history,
        "wanted": wanted,
        "qualityprofile": (
            [{"id": 1, "name": "[Anime] Remux-1080p", "upgradeAllowed": True, "cutoff": 3,
              "items": [{"id": 1000, "quality": {"id": 3, "name": "WEBDL-1080p"},
                         "allowed": True}]}]
            if anime else
            [{"id": 1, "name": "HD Bluray + WEB", "upgradeAllowed": True, "cutoff": 3,
              "items": [{"id": 1000, "quality": {"id": 3, "name": "WEBDL-1080p"},
                         "allowed": True}]},
             {"id": 2, "name": "UHD Bluray + WEB", "upgradeAllowed": True, "cutoff": 5,
              "items": [{"id": 1001, "quality": {"id": 5, "name": "WEBDL-2160p"},
                         "allowed": True}]}]
        ),
        "rootfolder": [{"id": 1, "path": root, "accessible": True, "freeSpace": 900}],
        "tag": [{"id": 1, "label": "anime" if anime else "default"}],
        "health": [{"source": "DownloadClientCheck", "type": "warning",
                    "message": f"{name} client unavailable", "wikiUrl": ""}],
        "diskspace": [{"path": "/data", "label": "data", "freeSpace": 500,
                       "totalSpace": 4000}],
        "command": [{"id": 1, "name": "RefreshMovie", "status": "completed"}],
        "collection": [{"id": 1, "title": f"{name} collection", "tmdbId": 9000 + anime}],
        "lookup": [],
    }


def build() -> dict[str, dict]:
    sonarr_main = sonarr_seed(
        "sonarr-main",
        [
            {"id": 1, "title": "Breaking Bad", "tvdbId": 81189, "year": 2008,
             "path": "/data/media/tv/Breaking Bad", "monitored": True,
             "qualityProfileId": 1, "tags": [1], "seriesType": "standard",
             "images": cover(1), "seasons": [{"seasonNumber": 1, "monitored": True}],
             "statistics": {"episodeCount": 2, "sizeOnDisk": 123}},
            {"id": 2, "title": "The Office", "tvdbId": 73244, "year": 2005,
             "path": "/data/media/tv/The Office", "monitored": True,
             "qualityProfileId": 1, "tags": [], "seriesType": "standard",
             "images": cover(2), "seasons": [{"seasonNumber": 1, "monitored": True}],
             "statistics": {"episodeCount": 2, "sizeOnDisk": 456}},
            {"id": 3, "title": "Severance", "tvdbId": 371980, "year": 2022,
             "path": "/data/media/tv/Severance", "monitored": True,
             "qualityProfileId": 2, "tags": [], "seriesType": "standard",
             "images": cover(3), "seasons": [{"seasonNumber": 1, "monitored": True}],
             "statistics": {"episodeCount": 2, "sizeOnDisk": 789}},
        ],
        anime=False,
    )
    sonarr_anime = sonarr_seed(
        "sonarr-anime",
        [
            {"id": 1, "title": "Cowboy Bebop", "tvdbId": 76885, "year": 1998,
             "path": "/data/media/anime/Cowboy Bebop", "monitored": True,
             "qualityProfileId": 1, "tags": [1], "seriesType": "anime",
             "images": cover(1), "seasons": [{"seasonNumber": 1, "monitored": True}],
             "statistics": {"episodeCount": 2, "sizeOnDisk": 321}},
            {"id": 2, "title": "Frieren", "tvdbId": 424536, "year": 2023,
             "path": "/data/media/anime/Frieren", "monitored": True,
             "qualityProfileId": 1, "tags": [1], "seriesType": "anime",
             "images": cover(2), "seasons": [{"seasonNumber": 1, "monitored": True}],
             "statistics": {"episodeCount": 2, "sizeOnDisk": 654}},
        ],
        anime=True,
    )

    # A low id that exists ONLY on the anime instance.  Seerr hands SeerrFin the
    # id the real instance assigned, so a bare "7" can legitimately mean the
    # anime library even though it decodes to the identity-mapped primary --
    # this row is what the read-only fallback probe has to find.
    sonarr_anime["series"].append({
        "id": 7, "title": "Ghost in the Shell SAC", "tvdbId": 72233, "year": 2002,
        "path": "/data/media/anime/Ghost in the Shell SAC", "monitored": True,
        "qualityProfileId": 1, "tags": [1], "seriesType": "anime",
        "images": cover(7), "seasons": [{"seasonNumber": 1, "monitored": True}],
        "statistics": {"episodeCount": 0, "sizeOnDisk": 0},
    })

    # The same title known to both instances, already added only on the anime
    # one: the merged lookup must keep the row that carries the real id.
    shared = {"title": "Trigun Stampede", "tvdbId": 424097, "year": 2023,
              "images": cover(0), "seasons": []}
    sonarr_main["lookup"] = [
        {**shared, "id": 0},
        {"title": "Breaking Bad", "tvdbId": 81189, "year": 2008, "id": 1,
         "images": cover(1), "seasons": []},
    ]
    sonarr_anime["lookup"] = [{**shared, "id": 2, "seriesType": "anime"}]

    radarr_main = radarr_seed(
        "radarr-main",
        [
            {"id": 1, "title": "Dune", "tmdbId": 438631, "year": 2021,
             "path": "/data/media/movies/Dune", "monitored": True,
             "qualityProfileId": 1, "tags": [], "images": cover(1),
             "hasFile": True, "sizeOnDisk": 1000},
            {"id": 2, "title": "Arrival", "tmdbId": 329865, "year": 2016,
             "path": "/data/media/movies/Arrival", "monitored": True,
             "qualityProfileId": 1, "tags": [], "images": cover(2),
             "hasFile": True, "sizeOnDisk": 2000},
            {"id": 3, "title": "Blade Runner 2049", "tmdbId": 335984, "year": 2017,
             "path": "/data/media/movies/Blade Runner 2049", "monitored": True,
             "qualityProfileId": 2, "tags": [], "images": cover(3),
             "hasFile": False, "sizeOnDisk": 0},
        ],
        anime=False,
    )
    radarr_anime = radarr_seed(
        "radarr-anime",
        [
            {"id": 1, "title": "Your Name", "tmdbId": 372058, "year": 2016,
             "path": "/data/media/anime-movies/Your Name", "monitored": True,
             "qualityProfileId": 1, "tags": [1], "images": cover(1),
             "hasFile": True, "sizeOnDisk": 3000},
            {"id": 2, "title": "Suzume", "tmdbId": 916224, "year": 2022,
             "path": "/data/media/anime-movies/Suzume", "monitored": True,
             "qualityProfileId": 1, "tags": [1], "images": cover(2),
             "hasFile": False, "sizeOnDisk": 0},
        ],
        anime=True,
    )
    shared_movie = {"title": "Akira", "tmdbId": 149, "year": 1988,
                    "images": cover(0)}
    radarr_main["lookup"] = [{**shared_movie, "id": 0}]
    radarr_anime["lookup"] = [{**shared_movie, "id": 2}]

    return {
        "sonarr-main": sonarr_main,
        "sonarr-anime": sonarr_anime,
        "radarr-main": radarr_main,
        "radarr-anime": radarr_anime,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        for key in ("series", "movie", "lookup"):
            for row in data.get(key, []):
                row.setdefault("titleSlug", slugify(row.get("title", "")))
        target = OUT / f"{name}.json"
        # An explicit LF newline: Windows would otherwise write CRLF, leaving the
        # committed seeds perpetually "modified" after a local test run.
        target.write_text(json.dumps(data, indent=2), encoding="utf-8", newline="\n")
        print(f"wrote {target} ({len(json.dumps(data))} bytes)")


if __name__ == "__main__":
    main()
