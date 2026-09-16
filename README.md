# arr-proxy

One URL and one API key in front of several Sonarr or Radarr instances.

Some Jellyfin plugins — **Home Screen Sections** and **SeerrFin** among them —
store exactly one Sonarr and one Radarr connection. If you run a regular *and*
an anime instance of each, you have to pick one and the other is invisible.
This proxy presents all of them as a single instance: it fans each request out,
merges the answers, and rewrites the entity ids so nothing collides.

Requests that name a specific entity are routed back to the instance that owns
it, so `GET /series/10000001` reaches your anime Sonarr and `GET /series/1`
reaches the regular one.

Anything that already accepts multiple instances — Seerr, Prowlarr, Recyclarr,
Bazarr, Unpackerr — should keep talking to each instance directly. This exists
only for the clients that cannot.

---

## How the ids work

Every instance numbers its rows from 1 independently. Both your Sonarr
instances have a series `1`, a tag `1`, and quality profiles `1–6`. Merging
them naively produces duplicates that no client can disambiguate.

arr-proxy applies a stateless block offset:

```
virtual id = real id + (instance index × id_block)      # id_block defaults to 10,000,000
```

| Instance | Real id | Virtual id |
|---|---|---|
| index 0 (first in the config) | 1 | **1** |
| index 1 | 1 | **10000001** |
| index 2 | 1 | **20000001** |

Two properties matter:

- **Instance 0 is identity mapped.** Its ids pass through untouched, so anything
  that already holds a real id from your primary instance keeps working.
- **It is stateless.** Decoding is `divmod`, so there is no database to lose and
  ids survive restarts and reconfiguration.

### What gets rewritten, and what does not

Rewritten (ids local to one instance): `id`, `seriesId`, `episodeId`,
`episodeFileId`, `movieId`, `movieFileId`, `collectionId`, `qualityProfileId`,
`languageProfileId`, `metadataProfileId`, `rootFolderId`, `tags`, `indexerId`,
`downloadClientId`, `importListId`, and the entity id inside `/MediaCover/…`
URLs.

**Never** rewritten:

- External ids — `tvdbId`, `tmdbId`, `imdbId`, `tvRageId`, `tvMazeId`. These
  identify the title itself; shifting one would break every metadata lookup.
- Global constants — quality definition ids (`quality.quality.id`), language
  ids. They are the same on every instance, so they are already unambiguous.
- `remoteUrl` on images, and `path` on series and movies. Home Screen Sections
  matches library items by `path`, so it has to arrive verbatim.

---

## Deploying

Copy this directory to your Docker host, e.g. `<stack-dir>/arrproxy`.

**1. Configuration.**

```bash
mkdir -p <stack-dir>/appdata/arrproxy
cp <stack-dir>/arrproxy/config.example.yaml <stack-dir>/appdata/arrproxy/config.yaml
```

Edit it: the four instance URLs already match your container names
(`sonarr`, `sonarr-anime`, `radarr`, `radarr-anime`). The instance API keys are
read from the environment, so the four keys already in your `.env` are reused.
Then add two more to `<stack-dir>/.env` — these are the keys you will hand
to the Jellyfin plugins:

```bash
ARRPROXY_SONARR_KEY=<a long random string>
ARRPROXY_RADARR_KEY=<a different long random string>
```

`openssl rand -hex 24` produces a suitable one. If you leave them out, the proxy
generates keys on first start and writes them into `config.yaml` — read them
from there; the log only shows the last four characters.

**2. Add the service** to `<stack-dir>/docker-compose.yml` so it shares the
network with the *arr containers and can reach them by name:

```yaml
  arrproxy:
    build: ./arrproxy
    container_name: arrproxy
    environment:
      - TZ=${TZ}
      - ARRPROXY_CONFIG=/config/config.yaml
      - ARRPROXY_HEALTH_URL=http://127.0.0.1:8787/-/health
      - SONARR_API_KEY=${SONARR_API_KEY}
      - SONARR_ANIME_API_KEY=${SONARR_ANIME_API_KEY}
      - RADARR_MOVIES_API_KEY=${RADARR_MOVIES_API_KEY}
      - RADARR_ANIME_API_KEY=${RADARR_ANIME_API_KEY}
      - ARRPROXY_SONARR_KEY=${ARRPROXY_SONARR_KEY}
      - ARRPROXY_RADARR_KEY=${ARRPROXY_RADARR_KEY}
    volumes:
      - ./appdata/arrproxy:/config
    ports:
      - "18989:8989"   # combined Sonarr
      - "17878:7878"   # combined Radarr
      - "18787:8787"   # unified: /sonarr/... and /radarr/...
    restart: unless-stopped
```

The host ports are shifted into the 1xxxx range because `8989`, `7878`, `8990`
and `7879` are already taken by the real *arrs on that host.

```bash
cd <stack-dir> && docker compose up -d arrproxy
docker compose logs arrproxy | head -20
curl -s http://<docker-host>:18787/-/health | jq
```

`/-/health` actively probes every instance and returns 503 if an app has none
reachable — it is what the container health check uses.

**3. Point the plugins at it** (Jellyfin):

| Plugin | Field | Value |
|---|---|---|
| Home Screen Sections | Sonarr URL | `http://<docker-host>:18989` |
| | Sonarr API key | your `ARRPROXY_SONARR_KEY` |
| | Radarr URL | `http://<docker-host>:17878` |
| | Radarr API key | your `ARRPROXY_RADARR_KEY` |
| SeerrFin | Sonarr / Radarr URL + key | the same two pairs |

Use the direct `IP:port`, not a Caddy hostname — it keeps the proxy out of the reverse-proxy path.

**4. Optional Caddy entries**, if you want to reach it from a browser. Inside
your existing wildcard site block, before the final `handle { abort }`
(substitute your own domain):

```caddy
    @arrproxy host arrproxy.example.com
    handle @arrproxy { reverse_proxy <docker-host>:18787 }
```

Then `https://arrproxy.example.com/sonarr/api/v3/series?apikey=…`.

---

## Listeners

Three listeners, all serving the same thing:

| Port | Serves |
|---|---|
| `8989` | Combined Sonarr at the root — `http://host:18989/api/v3/series` |
| `7878` | Combined Radarr at the root — `http://host:17878/api/v3/movie` |
| `8787` | Both, prefixed — `/sonarr/api/v3/series`, `/radarr/api/v3/movie` |

Per-app ports exist because most clients take a bare `http://host:port` and
append `/api/v3` themselves. The unified port is there if you prefer one.

Authentication accepts `X-Api-Key` (any casing), `?apikey=`, or
`Authorization: Bearer`. The combined key is validated and then **replaced**
with that instance's own key before the request goes upstream — a backing
instance never sees the combined key, and the combined key is never a valid
credential on an instance.

`/ping`, `/-/health` and `/-/version` need no key.

---

## What each endpoint does

| Endpoint | Behaviour |
|---|---|
| `GET /series`, `/movie`, `/qualityprofile`, `/rootfolder`, `/tag`, `/customformat`, `/collection`, … | Fan out, concatenate, translate ids |
| `GET /calendar` | Fan out and **re-sort** — by `airDateUtc` for Sonarr, by the first present of `inCinemas` / `digitalRelease` / `physicalRelease` for Radarr |
| `GET /queue`, `/history`, `/wanted/missing`, `/wanted/cutoff`, `/blocklist` | Paged merge: sorted globally, windowed to the requested page, `totalRecords` summed over the instances that answered |
| `GET /series/lookup`, `/movie/lookup` | Merged and de-duplicated by external id, keeping the row from whichever instance already has the title added |
| `GET /health` | Merged, each item prefixed with the instance it came from |
| `GET /diskspace` | Merged, collapsing mounts the instances share |
| `GET /queue/status` | Counters summed |
| `GET /system/status` | Primary's status with `instanceName` replaced and an `arrProxy` block listing instances. `appName` is left exactly as-is — clients branch on it |
| `GET/PUT/DELETE /…/{id}` | Routed to the owning instance |
| `GET /episode?seriesId=…` | Routed by the id in the query; only that instance is contacted |
| `GET /MediaCover/{id}/…` | Routed to the owning instance, bytes streamed back |
| `GET /series/{slug}`, `/movie/{slug}`, `/add/new` | **Not API paths.** SeerrFin's "Open in Sonarr" buttons; 302-redirected to the owning instance's own web UI (see below) |
| `POST /series`, `/movie` | Routed (see below) |
| `POST /command` | Routed if it names an entity, broadcast to every instance if it is global (`RssSync`, `RefreshMonitoredDownloads`) |
| `PUT/DELETE /…/editor`, `/queue/bulk` | Ids split per instance, each instance gets only its own |
| anything else | Shape-driven fallback: a list aggregates, a paged envelope pages, anything else comes from the primary |

Two response headers make behaviour visible: `X-ArrProxy-Instances` names who
served the request, and `X-ArrProxy-Degraded` appears when an instance could not
be reached.

### "Open in Sonarr" buttons

SeerrFin renders buttons that link into the *arr web UI: `{base}/series/{titleSlug}`
when it has matched the title to your library, and `{base}/add/new?term=tmdb:N`
when it hasn't. Point it at the proxy and those would 404, because the proxy
serves no web UI.

So the proxy answers those three non-API paths with a **302** to whichever
instance actually owns the title. Set `public_url` on each instance for it to
work: `url` is a container name the browser cannot resolve.

`/add/new` links need care. SeerrFin produces them for titles that **are** in a
library too — it drops a monitored title's progress entry, link included, while
nothing is downloaded yet. So rather than send every add link to the default
instance, the proxy works out who owns the title, cheapest check first:

1. **Your libraries.** The TMDB/TVDB/IMDb id is matched against every
   instance's own library listing. That's a local read, around 10ms, so a
   click on a title you already have is instant.
2. **Each instance's metadata lookup**, only when no library lists that id.
   This is a trip to the internet and can take a few seconds the first time a
   title is looked up. It still finds titles whose stored id is out of date
   (Sonarr marks a lookup result with its library id when it holds the show),
   and supplies the genres your routing rules match on.

Either way the browser lands on the title's page on the instance that has it.
A title nobody holds keeps its add form, on whichever instance your routing
rules claim it for, or the default.

```yaml
      - name: sonarr-anime
        url: http://sonarr-anime:8989          # how the proxy reaches it
        public_url: http://<docker-host>:8990 # how your browser reaches it
```

These paths are **unauthenticated** — a browser following a button has no API
key to present. They only issue a redirect to an instance the viewer can already
reach, and the API itself stays behind the key.

Every redirect records why it went where it did, in an
`X-ArrProxy-Resolution` header and in the log: `library` (an instance holds the
title), `rule` (a routing rule claimed it) or `default` (nothing did).

### Where a newly added title goes

In priority order:

1. **A virtual id in the payload.** If the client picked `qualityProfileId:
   10000004`, that profile belongs to instance 1, so the series goes to instance
   1. This needs no configuration and is exact, because the client got that id
   from this proxy in the first place.
2. **A configured rule** — `series_types`, `genres`, `root_folders`,
   `title_regex`.
3. **The instance marked `default: true`.**

---

## Configuration reference

See [`config.example.yaml`](config.example.yaml) for the annotated version.

| Key | Default | Meaning |
|---|---|---|
| `server.unified_port` | `8787` | Prefixed listener; `null` disables it |
| `server.cache_ttl` | `5` | Seconds to hold a fanned-out GET. `0` disables |
| `server.fail_open` | `true` | Serve partial results when an instance is down, instead of failing |
| `server.id_block` | `10000000` | Ids per instance per entity type |
| `server.id_fallback_probe` | `true` | Retry a 404 for an ambiguous id against the other instances (reads only) |
| `server.timeout` / `connect_timeout` | `30` / `5` | Upstream timeouts, seconds |
| `server.fanout_timeout` | `10` | Seconds one instance may hold up a fanned-out **read** before being dropped from it. Writes are never cut short |
| `apps.<app>.api_key` | generated | The key clients present |
| `apps.<app>.port` | 8989 / 7878 | That app's dedicated listener |
| `apps.<app>.instances[].default` | first | Where creates land with no other signal |
| `apps.<app>.instances[].enabled` | `true` | Set `false` to take one out of rotation |
| `apps.<app>.instances[].public_url` | `url` | Browser-reachable address of that instance's own web UI, for deep links |

`${VAR}` and `${VAR:-default}` are expanded from the environment.

Instance **order is significant**: the first is index 0 and identity mapped.
Put your largest instance first, and do not reorder the list afterwards — that
would renumber every virtual id.

---

## Caveats worth knowing

- **`cache_ttl` means up to 5 seconds of staleness** on aggregate reads. A write
  through the proxy clears the cache; a change made directly in a *arr's own UI
  does not. Set it to `0` if that bothers you. Partial (degraded) answers are
  never cached, so an instance blipping cannot leave half your library missing
  for a whole TTL afterwards.
- **The same title in both instances shows up twice.** Nothing de-duplicates
  library rows or calendar entries, on purpose: two entries means two instances
  are genuinely tracking it, which is usually a mistake you want to see rather
  than one the proxy quietly hides. (`/lookup` *is* de-duplicated — those rows
  are metadata from a shared upstream, not library state.)
- **An id at or above `id_block` breaks routing**, because it lands inside the
  next instance's range. That needs ten million rows of one entity type in one
  instance, so it should never happen; if it does, the proxy logs an error
  naming the id and telling you to raise `id_block`.
- **A unanimous upstream error is relayed as-is.** If every instance answers
  404, so does the proxy — a missing endpoint is an answer, not an outage. Only
  a genuine failure to reach any instance, or instances disagreeing about the
  error, produces a 502.
- **An instance under a `urlBase`** works: give the full base in `url`, e.g.
  `http://sonarr:8989/sonarr`. The proxy always reports `urlBase: ""` in
  `system/status` because the proxy itself serves at the root.
- **Seerr's `externalServiceId`.** Seerr talks to your instances directly, so it
  stores the id the *real* instance assigned. When SeerrFin uses that id against
  the proxy, an id belonging to a non-primary instance looks like a primary id.
  Its main code path matches by `tmdbId` over the full list, which merges
  correctly; the id path is only a fallback, and `id_fallback_probe` recovers it
  by retrying other instances on a 404. Probing is **reads only** — a write is
  never retried against a guessed instance.
- **Tag labels can appear twice** in the merged list. They are genuinely
  different tags on different instances; only the ids are made unique.
- **Custom format and quality definition ids are not namespaced** — they are
  displayed, never routed on, and namespacing them would corrupt what you see.
- **`system/status` reports the primary's version.** If your instances run
  different versions, that is the one clients see; `/-/health` shows all of them.
- **This is not a security boundary.** It holds your instance API keys and runs
  in the same trust domain. Keep it LAN-only, as with everything else in
  the stack.
- **Writes are best served directly.** Routing a create is a heuristic, however
  good. Seerr already talks to all four instances, so leave it that way.

---

## Testing

```powershell
.\run-tests.ps1              # everything
.\run-tests.ps1 -Suite mock  # just the scripted stack
.\run-tests.ps1 -Suite real -Keep
```

```bash
./run-tests.sh               # everything
./run-tests.sh real          # just the genuine-instance stack
KEEP=1 ./run-tests.sh mock
```

Three layers, all run in containers so nothing needs installing on the host:

| Suite | What it proves |
|---|---|
| **unit** (126 tests) | Id translation, merge strategies, config validation, the route table, and instance selection, in isolation |
| **mock end-to-end** (101 tests + 17 failure-mode checks) | The proxy over real HTTP against four scripted instances with deliberately colliding ids. Each mock records the requests it receives, so routing is asserted by *which instance was contacted*, not inferred from the body |
| **real end-to-end** (41 tests) | The same proxy against four genuine `linuxserver/sonarr` and `linuxserver/radarr` containers, with real titles fetched from the live metadata servers |

The real suite is the one that matters most. Four fresh instances each number
their series, tags and quality profiles from 1, so every id collides — the exact
condition this proxy exists to resolve. It checks, among other things, that
twelve quality profiles merge without collision, that a real
`/MediaCover/…/poster.jpg` URL is rewritten and still serves the right
instance's bytes, that `/series/lookup` collapses a title both instances know
while keeping the one that actually has it, and that a write reaches only the
owning instance — verified by querying the instances directly, never through the
proxy.

The failure-mode checks stop and restart containers to confirm that one
instance going down degrades to partial results with a header rather than a
hard error, that a total outage returns 502 rather than an empty library, that
a partial answer never enters the cache, and that recovery is automatic.

A dedicated edge-case suite covers where these pieces surprise each other: an
id naming an instance nobody configured (which must narrow the request to
nothing, never silently widen it to every instance), a status code that must
not carry a body, an API key with characters that cannot travel in an HTTP
header, Japanese titles round-tripping through the merge, page numbers that are
zero or negative or absurd, one instance hanging while the others answer, and
the browser deep links above.

---

## Operating it

```bash
python -m arrproxy --config /config/config.yaml --check   # validate and exit
docker compose logs -f arrproxy
curl -s http://<docker-host>:18787/-/health | jq
```

Useful when something looks wrong:

```bash
# Who served this request?
curl -sI -H "X-Api-Key: $KEY" http://<docker-host>:18989/api/v3/series | grep -i arrproxy

# Does a virtual id decode where you expect? 10000001 -> instance 1, real id 1
curl -s -H "X-Api-Key: $KEY" http://<docker-host>:18989/api/v3/series/10000001 | jq .title
```

| Symptom | Likely cause |
|---|---|
| 401 from the proxy | Presenting an *instance's* key instead of the combined one, or the Radarr key on the Sonarr port |
| Only one instance's titles appear | Check `X-ArrProxy-Degraded` and `/-/health`; the other instance is unreachable |
| 502 `no backing instance answered` | Every instance for that app is down; the detail names each failure |
| Plugin shows nothing | Confirm it points at the shifted host port (`18989`/`17878`), not `8989`/`7878` |
| Ids look wrong after a config change | Instances were reordered; index 0 must stay index 0 |
| An endpoint returns 404 through the proxy | Every instance returned 404 — the path really is absent, not a proxy fault |
| "Open in Sonarr" lands on a dead page | Set `public_url` on each instance to a browser-reachable address |
| "Open in Sonarr" lands on the wrong instance | `docker compose logs arrproxy \| grep "deep link"` shows each click and why it resolved as it did; `default` means no instance reported owning that title |
| One instance's data is intermittently missing | It is exceeding `fanout_timeout`; check that instance's own responsiveness |
| Log says an id is `>= id_block` | Raise `server.id_block` above the id it names and restart |

## Requirements

Python 3.11+ (the image uses 3.12); `starlette`, `uvicorn`, `httpx`, `PyYAML`.
Runs unprivileged in the container. Only `/config` needs to be writable, and
only to persist a generated API key.
