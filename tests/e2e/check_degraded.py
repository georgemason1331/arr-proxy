"""Failure-mode checks that need to stop and start containers.

Run from the host against the mock stack.  Stdlib only, no virtualenv needed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

SONARR = "http://127.0.0.1:18989"
RADARR = "http://127.0.0.1:17878"
UNIFIED = "http://127.0.0.1:18787"
SONARR_KEY = "e2e-sonarr-combined-key"
RADARR_KEY = "e2e-radarr-combined-key"
# Must match server.cache_ttl in config.cached.yaml.
CACHE_TTL = 5.0

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name} {detail}")


def get(url: str, key: str) -> tuple[int, object, dict[str, str]]:
    """Header keys are lower-cased: HTTP header names are case-insensitive but
    a plain dict lookup is not, and uvicorn sends them lower-case."""
    request = urllib.request.Request(url, headers={"X-Api-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return (
                response.status,
                json.loads(response.read() or b"null"),
                {k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body or b"null")
        except ValueError:
            parsed = body.decode("utf-8", "replace")
        return exc.code, parsed, {k.lower(): v for k, v in exc.headers.items()}


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


def wait_for_full_recovery(url: str, key: str, attempts: int = 40) -> None:
    """Wait until *every* instance is back, not merely until one answers.

    Fail-open means a 200 arrives as soon as the first instance is up, so
    polling for 200 alone would sample a still-degraded merge.
    """
    for _ in range(attempts):
        try:
            status, _, headers = get(url, key)
            if status == 200 and "x-arrproxy-degraded" not in headers:
                return
        except Exception:  # noqa: BLE001 - the container is coming back up
            pass
        time.sleep(2)


def main() -> int:
    print("baseline")
    status, rows, _ = get(f"{SONARR}/api/v3/series", SONARR_KEY)
    check("all instances serve the full library", status == 200 and len(rows) >= 5,
          f"status={status} count={len(rows) if isinstance(rows, list) else '?'}")
    full = len(rows) if isinstance(rows, list) else 0

    print("\none instance stopped")
    docker("stop", "e2e-sonarr-anime")
    try:
        time.sleep(1)
        status, rows, headers = get(f"{SONARR}/api/v3/series", SONARR_KEY)
        check("still answers from the surviving instance", status == 200 and rows,
              f"status={status}")
        check("returns fewer rows than when whole",
              isinstance(rows, list) and 0 < len(rows) < full)
        check("names the missing instance in a header",
              headers.get("x-arrproxy-degraded") == "sonarr-anime",
              f"header={headers.get('x-arrproxy-degraded')!r}")
        check("no row from the stopped instance leaks through",
              all(r["id"] < 10_000_000 for r in rows) if isinstance(rows, list) else False)

        status, body, _ = get(f"{UNIFIED}/-/health", "")
        sonarr_health = body["apps"]["sonarr"] if isinstance(body, dict) else {}
        check("health reports the app as degraded but up",
              sonarr_health.get("healthy") is True and sonarr_health.get("degraded") is True,
              str(sonarr_health.get("healthy")) + "/" + str(sonarr_health.get("degraded")))

        status, rows, _ = get(f"{RADARR}/api/v3/movie", RADARR_KEY)
        check("the other app is unaffected", status == 200 and len(rows) >= 5)

        print("\nboth instances stopped")
        docker("stop", "e2e-sonarr-main")
        time.sleep(1)
        status, body, _ = get(f"{SONARR}/api/v3/series", SONARR_KEY)
        check("refuses rather than returning an empty library", status == 502,
              f"status={status}")
        check("explains which instances failed",
              isinstance(body, dict) and "sonarr-main" in str(body.get("detail", "")))

        status, _, _ = get(f"{UNIFIED}/-/health", "")
        check("health turns unhealthy", status == 503, f"status={status}")

        status, rows, _ = get(f"{RADARR}/api/v3/movie", RADARR_KEY)
        check("the other app still works with Sonarr fully down", status == 200)
    finally:
        print("\nrestoring")
        subprocess.run(["docker", "start", "e2e-sonarr-main", "e2e-sonarr-anime"],
                       check=False, capture_output=True)
        wait_for_full_recovery(f"{SONARR}/api/v3/series", SONARR_KEY)

    # The mocks keep their library in memory, so a restart resets them to the
    # seed file.  The property worth asserting is that both instances are back
    # in the merge, not that a pre-restart row count is reproduced.
    status, rows, headers = get(f"{SONARR}/api/v3/series", SONARR_KEY)
    ids = [r["id"] for r in rows] if isinstance(rows, list) else []
    check("recovers automatically once the instances return",
          status == 200 and "x-arrproxy-degraded" not in headers,
          f"status={status} header={headers.get('x-arrproxy-degraded')!r}")
    check("both instances are serving again after recovery",
          any(i < 10_000_000 for i in ids) and any(i > 10_000_000 for i in ids),
          f"ids={ids}")

    print("\ncache must not hold a partial answer")
    cached = "http://127.0.0.1:28989"
    _, full_rows, _ = get(f"{cached}/api/v3/series", SONARR_KEY)
    baseline = len(full_rows) if isinstance(full_rows, list) else 0

    docker("stop", "e2e-sonarr-anime")
    try:
        # Let the entry cached from the healthy read above lapse first, so the
        # next read is a genuinely fresh degraded one. (Serving the stale *full*
        # answer for the rest of the TTL is the cache working as intended, and
        # is better than serving a partial library.)
        time.sleep(CACHE_TTL + 1.5)

        _, first_rows, first = get(f"{cached}/api/v3/series", SONARR_KEY)
        check("a fresh degraded read is served and marked",
              isinstance(first_rows, list) and 0 < len(first_rows) < baseline
              and first.get("x-arrproxy-degraded") == "sonarr-anime",
              f"count={len(first_rows) if isinstance(first_rows, list) else '?'}")

        # The decisive one: a partial answer must not become a cache entry, or
        # half the library stays missing for a full TTL after recovery.
        _, _, second = get(f"{cached}/api/v3/series", SONARR_KEY)
        check("a degraded answer is never stored in the cache",
              first.get("x-arrproxy-cache") != "hit"
              and second.get("x-arrproxy-cache") != "hit",
              f"first={first.get('x-arrproxy-cache')!r} "
              f"second={second.get('x-arrproxy-cache')!r}")
    finally:
        subprocess.run(["docker", "start", "e2e-sonarr-anime"],
                       check=False, capture_output=True)
        wait_for_full_recovery(f"{SONARR}/api/v3/series", SONARR_KEY)

    status, rows, headers = get(f"{cached}/api/v3/series", SONARR_KEY)
    check("the full library is served again after recovery",
          status == 200 and len(rows) == baseline
          and "x-arrproxy-degraded" not in headers,
          f"count={len(rows) if isinstance(rows, list) else '?'} expected={baseline}")

    healthy_headers = get(f"{cached}/api/v3/series", SONARR_KEY)[2]
    check("healthy answers are still cached",
          healthy_headers.get("x-arrproxy-cache") == "hit",
          f"header={healthy_headers.get('x-arrproxy-cache')!r}")

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  failed: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
