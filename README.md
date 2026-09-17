# arr-proxy

**Put several Sonarr or Radarr instances behind one URL and one API key.**

Some tools only let you connect *one* Sonarr and *one* Radarr — the Jellyfin
plugins **Home Screen Sections** and **SeerrFin** are two examples. If you run
more than one of each (a regular instance and an anime instance, say),
everything in the second one is invisible to them.

arr-proxy looks like a single Sonarr (or Radarr) to those tools. Behind the
scenes it sends each request to all of your real instances, merges the
answers, and remembers which instance owns what, so follow-up requests reach
the right one.

**What you get**

- One combined calendar, library, queue and history across all your instances.
- Ids that never collide, with requests for a specific series or movie routed
  to the instance that owns it.
- Posters and "Open in Sonarr/Radarr" links that land on the right instance.
- Graceful degradation: if one instance is down or hung, you still get the
  others, plus a response header naming the one that's missing.
- Your real instance API keys stay on the server. Clients only ever see the
  combined key.

**When not to use it:** anything that already supports multiple instances —
Seerr/Jellyseerr/Overseerr, Prowlarr, Bazarr, Recyclarr, Unpackerr — should keep
talking to each instance directly. arr-proxy is for the tools that can't.

---

## Quick start

You need Docker with Docker Compose, and Sonarr/Radarr instances the proxy can
reach over the network.

The steps below use placeholders. Replace each one with your own value:

| Placeholder | What to put there |
|---|---|
| `<stack-dir>` | The directory of the Docker Compose project your Sonarr/Radarr containers run in |
| `<docker-host>` | IP address or hostname of the machine running Docker, as your other devices reach it |
| `<sonarr-container>`, `<anime-sonarr-container>` | Container names of your Sonarr instances, as other containers on the same Compose network reach them |
| `<radarr-container>`, `<anime-radarr-container>` | Container names of your Radarr instances |
| `<sonarr-ui-port>`, `<anime-sonarr-ui-port>`, … | Host ports each instance's own web UI is published on |

The examples assume one regular and one anime instance of each app. Any
number works; just list as many instances as you have.

### 1. Get the code

Clone this repository into your Compose project (copy the URL from the
**Code** button at the top of the repository page):

```bash
git clone <this-repository-url> <stack-dir>/arrproxy
```

Prefer not to keep a copy? You can build straight from GitHub instead — see
step 4.

### 2. Create your config

```bash
mkdir -p <stack-dir>/appdata/arrproxy
cp <stack-dir>/arrproxy/config.example.yaml <stack-dir>/appdata/arrproxy/config.yaml
```

Open the new `config.yaml` and, for every instance, fill in:

- `url` — how the **proxy** reaches the instance, e.g. `http://<sonarr-container>:8989`
- `public_url` — how **your browser** reaches that instance's web UI, e.g.
  `http://<docker-host>:<sonarr-ui-port>`

List your main instance first — see [Instance order matters](#instance-order-matters).

### 3. Add your API keys

API keys are read from environment variables. Add these to `<stack-dir>/.env`:

```bash
# Each instance's own key: in that instance, Settings -> General -> Security
SONARR_API_KEY=<your regular Sonarr's API key>
SONARR_ANIME_API_KEY=<your anime Sonarr's API key>
RADARR_API_KEY=<your regular Radarr's API key>
RADARR_ANIME_API_KEY=<your anime Radarr's API key>

# The combined keys you'll give your apps. Make up long random values:
ARRPROXY_SONARR_KEY=<output of: openssl rand -hex 24>
ARRPROXY_RADARR_KEY=<a second, different random value>
```

If you leave a combined key empty, arr-proxy generates a random one on first
start and saves it into `config.yaml` (so the config directory must be
writable). Read it from there — the log only ever shows its last four
characters. Saving rewrites the file without its comments, so set both keys
yourself if you want to keep them.

### 4. Add the service to Compose

Add this to `<stack-dir>/docker-compose.yml`, so arr-proxy shares a network
with your *arr containers and can reach them by name:

```yaml
  arrproxy:
    build: ./arrproxy
    # or, without cloning:
    # build: https://github.com/<owner>/arr-proxy.git#main
    container_name: arrproxy
    environment:
      - TZ=${TZ:-UTC}
      - ARRPROXY_CONFIG=/config/config.yaml
      - ARRPROXY_HEALTH_URL=http://127.0.0.1:8787/-/health
      - SONARR_API_KEY=${SONARR_API_KEY}
      - SONARR_ANIME_API_KEY=${SONARR_ANIME_API_KEY}
      - RADARR_API_KEY=${RADARR_API_KEY}
      - RADARR_ANIME_API_KEY=${RADARR_ANIME_API_KEY}
      - ARRPROXY_SONARR_KEY=${ARRPROXY_SONARR_KEY}
      - ARRPROXY_RADARR_KEY=${ARRPROXY_RADARR_KEY}
    volumes:
      - ./appdata/arrproxy:/config
    ports:
      - "18989:8989"   # combined Sonarr
      - "17878:7878"   # combined Radarr
      - "18787:8787"   # both, under /sonarr/... and /radarr/...
    restart: unless-stopped
```

The host ports sit in the 1xxxx range so they don't clash with your real Sonarr
and Radarr, which usually publish `8989` and `7878`. Any free ports work.
Building from a GitHub URL is supported by Docker Compose on Linux.

### 5. Start it and check it

```bash
cd <stack-dir>
docker compose run --rm arrproxy --check   # validate the config without starting
docker compose up -d arrproxy
curl -s http://<docker-host>:18787/-/health
```

`/-/health` contacts every instance and reports whether each is reachable, with
its version and latency. It returns HTTP 503 if an app has no reachable
instances, which is also what the container's health check uses.

### 6. Point your apps at it

| App | Field | Value |
|---|---|---|
| Home Screen Sections | Sonarr URL | `http://<docker-host>:18989` |
| | Sonarr API key | your `ARRPROXY_SONARR_KEY` |
| | Radarr URL | `http://<docker-host>:17878` |
| | Radarr API key | your `ARRPROXY_RADARR_KEY` |
| SeerrFin | Sonarr / Radarr URL and key | the same two pairs |

Use the direct `host:port` rather than a reverse-proxy hostname. These plugins
call it from the Jellyfin server, so an extra hop only adds latency.

If a Jellyfin plugin doesn't pick up new settings, restart Jellyfin.

### Updating

```bash
cd <stack-dir>/arrproxy && git pull          # skip if you build from the GitHub URL
cd <stack-dir> && docker compose up -d --build arrproxy
```

---

## Configuration

[`config.example.yaml`](config.example.yaml) is fully commented. A trimmed
version:

```yaml
apps:
  sonarr:
    api_key: ${ARRPROXY_SONARR_KEY}     # the combined key your apps use
    port: 8989
    instances:
      - name: sonarr                    # listed first: see "Instance order matters"
        url: http://<sonarr-container>:8989
        api_key: ${SONARR_API_KEY}
        public_url: http://<docker-host>:<sonarr-ui-port>
        default: true                   # new series go here unless a rule matches
      - name: sonarr-anime
        url: http://<anime-sonarr-container>:8989
        api_key: ${SONARR_ANIME_API_KEY}
        public_url: http://<docker-host>:<anime-sonarr-ui-port>
        routing:
          series_types: [anime]
```

`${VAR}` and `${VAR:-default}` are filled in from the environment.

### All settings

| Key | Default | Meaning |
|---|---|---|
| `server.host` | `0.0.0.0` | Address to listen on |
| `server.unified_port` | `8787` | The listener serving both apps under `/sonarr` and `/radarr`; `null` turns it off |
| `server.log_level` | `info` | `debug` also logs every request |
| `server.cache_ttl` | `5` | Seconds to reuse a merged read; `0` turns caching off |
| `server.fail_open` | `true` | Serve partial results when an instance is down, instead of failing the request |
| `server.fanout_timeout` | `10` | Seconds one instance may hold up a merged **read** before it is left out. Writes are never cut short |
| `server.timeout` / `server.connect_timeout` | `30` / `5` | Upstream request timeouts, in seconds |
| `server.id_block` | `10000000` | Ids reserved per instance (see [How ids work](#how-ids-work)) |
| `server.id_fallback_probe` | `true` | If a read for an id 404s, try the id on the other instances (reads only) |
| `server.max_page_fetch` | `2000` | Most rows fetched per instance to build one merged page |
| `apps.<app>.api_key` | generated | The combined key your apps present |
| `apps.<app>.port` | `8989` / `7878` | That app's dedicated listener |
| `apps.<app>.instance_name` | `Sonarr (combined)` | The name reported in `system/status` |
| `apps.<app>.instances[].name` | — | A label used in logs and response headers |
| `apps.<app>.instances[].url` | — | How the proxy reaches the instance (include any URL base, e.g. `http://<sonarr-container>:8989/sonarr`) |
| `apps.<app>.instances[].api_key` | — | That instance's own API key |
| `apps.<app>.instances[].public_url` | `url` | How a browser reaches the instance's web UI, for "Open in" links |
| `apps.<app>.instances[].default` | first | Where new titles go when nothing else decides |
| `apps.<app>.instances[].enabled` | `true` | Set to `false` to take an instance out of rotation |
| `apps.<app>.instances[].routing` | — | Rules for new titles (below) |

Supported apps are `sonarr` and `radarr`. `lidarr` and `readarr` configurations
are accepted but untested.

### Where a newly added title goes

When something adds a series or movie through the proxy, it goes to the first
match of:

1. **The instance the client's choices came from.** A quality profile or tag
   the client picked from the proxy's lists belongs to exactly one instance, so
   the title goes there. This needs no configuration.
2. **A routing rule** on an instance: `series_types`, `genres`, `root_folders`
   or `title_regex`.
3. **The instance marked `default: true`.**

### Instance order matters

The first instance in each list keeps its own ids; later instances are shifted
(see below). **Put your main instance first, and don't reorder the list
later** — that would change every id your apps have seen.

---

## How it works

### How ids work

Every Sonarr numbers its series, episodes, tags and profiles from 1, so two
instances will both have a "series 1". arr-proxy gives each instance its own
id range:

```
proxy id = instance id + (position in list × 10,000,000)
```

| Instance | Its id | Id your apps see |
|---|---|---|
| 1st in the list | 1 | **1** |
| 2nd | 1 | **10000001** |
| 3rd | 1 | **20000001** |

Nothing is stored, so ids survive restarts. Ids local to one instance are
translated (`id`, `seriesId`, `episodeId`, `movieId`, `qualityProfileId`,
`tags`, the id inside `/MediaCover/…` URLs, and so on). Ids that identify a
title everywhere — `tvdbId`, `tmdbId`, `imdbId` — are never touched, and
neither are quality and language ids, which are the same on every instance.

<details>
<summary><b>What each endpoint does</b></summary>

| Endpoint | Behaviour |
|---|---|
| `GET /series`, `/movie`, `/qualityprofile`, `/rootfolder`, `/tag`, … | Asked of every instance, combined, ids translated |
| `GET /calendar` | Combined and re-sorted by air or release date |
| `GET /queue`, `/history`, `/wanted/missing`, `/wanted/cutoff`, `/blocklist` | Paged across all instances, with a correct combined total |
| `GET /series/lookup`, `/movie/lookup` | Combined, with duplicates collapsed to the copy an instance already has |
| `GET /health`, `/diskspace`, `/queue/status` | Combined (health items name their instance; shared disks counted once; counters summed) |
| `GET /system/status` | The first instance's status, renamed, plus a list of instances |
| `GET/PUT/DELETE /…/{id}` | Sent to the instance that owns the id |
| `GET /episode?seriesId=…` | Sent only to the instance that owns the series |
| `GET /MediaCover/{id}/…` | Image fetched from the owning instance |
| `POST /series`, `/movie` | Sent to one instance (see [Where a newly added title goes](#where-a-newly-added-title-goes)) |
| `POST /command` | Sent to the owning instance, or to every instance for global commands like `RssSync` |
| Anything else | Lists are combined, paged results are paged, everything else comes from the first instance |

</details>

Response headers show what happened:

- `X-ArrProxy-Instances` — which instances answered
- `X-ArrProxy-Degraded` — which instances couldn't be reached (results are partial)
- `X-ArrProxy-Cache: hit` — served from the short-lived cache
- `X-ArrProxy-Resolution` — for "Open in" links: `library`, `rule` or `default`

If one instance is down, you get the others' results with `X-ArrProxy-Degraded`
set, and partial results are never cached. If every instance is down, you get a
502 explaining why. If every instance answers with the same error (say, a 404),
that error is passed through unchanged.

### "Open in Sonarr/Radarr" links

SeerrFin's buttons link into the Sonarr/Radarr web interface, which the proxy
doesn't serve. So the proxy answers those links — `/series/{name}`,
`/movie/{name}` and `/add/new?term=tmdb:…` — with a redirect to the right
instance's own web UI, using that instance's `public_url`.

To find the right instance for an "add new" link, it first checks your
libraries (a fast local read), and only if no library has the title does it
ask each instance to look it up online (which can take a few seconds the first
time). A title you already have opens on the instance that has it; a title
nobody has opens the add form on the instance your routing rules pick.

---

## Security

- **Keep arr-proxy on your private network.** Never expose its ports to the
  internet. It is a convenience layer, not a security boundary.
- **A combined key is as powerful as your instance keys.** Anyone holding it
  can do anything through the proxy that your instances' API allows, on every
  instance behind it. Use long random values, and treat them like passwords.
- **Never commit** your `.env` or `config.yaml`.
- **Your instance keys stay server-side.** The proxy strips the key a client
  sends and uses each instance's own key upstream. Keys are compared in
  constant time, and the startup log shows only a key's last four characters.
- **Send the key in the `X-Api-Key` header, not `?apikey=`.** At
  `log_level: debug` every request line is logged, query string included.
- **Some endpoints need no key**, by design:
  - `/ping`, `/-/version` and `/` report that the proxy is running, its version,
    and the names of your configured instances.
  - `/-/health` reports each instance's internal address, reachability and
    version.
  - "Open in" links (`/series/…`, `/movie/…`, `/add/new`) redirect a browser —
    which has no key to send — to the instance holding that title. Anyone who
    can reach the proxy can use them to check whether a title is in one of
    your libraries.

---

## Troubleshooting

```bash
# Is everything reachable?
curl -s http://<docker-host>:18787/-/health

# Which instances answered this request?
curl -sI -H "X-Api-Key: $KEY" http://<docker-host>:18989/api/v3/series | grep -i arrproxy

# Where did "Open in" links go, and why?
docker compose logs arrproxy | grep "deep link"
```

| Symptom | Likely cause |
|---|---|
| `401 Unauthorized` | You're sending an instance's own key instead of the combined key, or the Radarr key to the Sonarr port |
| Only one instance's titles appear | Another instance is unreachable — check `X-ArrProxy-Degraded` and `/-/health` |
| `502 no backing instance answered` | Every instance for that app is down; the response says why for each |
| An app shows nothing | It points at an instance's own port instead of the proxy's (`18989` / `17878` in the example) |
| A 404 through the proxy | Every instance returned 404 — the thing really isn't there |
| "Open in" opens a page that won't load | Set `public_url` on each instance to an address your browser can reach |
| "Open in" opens the wrong instance | `docker compose logs arrproxy \| grep "deep link"` shows each click; `default` means no instance reported having that title |
| One instance's data is sometimes missing | It's slower than `fanout_timeout` — check that instance |
| Ids changed after editing the config | The instance list was reordered — put it back |
| Log says an id is `>= id_block` | Raise `server.id_block` above that id and restart |

---

## Limitations

- **Merged reads can be up to `cache_ttl` seconds stale.** Changes made through
  the proxy clear the cache; changes made in an instance's own UI don't. Set
  `cache_ttl: 0` if that matters to you.
- **A title in two instances appears twice.** That's deliberate — it usually
  means something is being tracked twice, which you'd want to see.
- **Tags with the same name on two instances appear twice.** They really are
  different tags.
- **`system/status` shows the first instance's version.** `/-/health` shows
  every instance's.
- **Adding titles through the proxy relies on the rules above.** Tools that can
  talk to each instance directly should.
- **Ids from outside the proxy can be ambiguous.** A tool that stores an
  instance's own id elsewhere (Seerr does) and later asks the proxy for it may
  reach the first instance's item with that number. For reads, the proxy
  retries other instances when that returns 404; writes are never guessed.

---

## Development

Tests run in containers, so nothing needs installing besides Docker.

```bash
./run-tests.sh          # everything
./run-tests.sh unit     # unit tests only
./run-tests.sh mock     # end-to-end against scripted instances
./run-tests.sh real     # end-to-end against real Sonarr and Radarr containers
KEEP=1 ./run-tests.sh   # leave the test containers running afterwards
```

On Windows, use `.\run-tests.ps1` with `-Suite unit|mock|real` and `-Keep`.

| Suite | What it covers |
|---|---|
| unit | Id translation, merging, config validation, routing decisions |
| mock end-to-end | The proxy over real HTTP against four scripted instances with colliding ids — each records the requests it receives, so tests check which instance was actually contacted |
| failure modes | Instances stopped and restarted mid-test: partial results, total outages, recovery, and that partial results are never cached |
| real end-to-end | Four real `linuxserver/sonarr` and `linuxserver/radarr` containers with real titles from the live metadata servers |

## Requirements

Python 3.11+ (the Docker image uses 3.12) with `starlette`, `uvicorn`, `httpx`
and `PyYAML`. The container runs as an unprivileged user; only `/config` needs
to be writable, and only to save a generated key.
