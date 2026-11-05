#!/usr/bin/env python3
"""Fail when a locked package file is younger than the cooldown.

The 7-day Dependabot cooldown covers only the package that Dependabot bumps.
A bump can pull in a new transitive pin that is hours old, because the uv
updater re-runs ``uv pip compile`` without ``--exclude-newer`` (#312i). This
check reads every pin of every lock and proves that each locked file was
uploaded at least ``--min-days`` days before the run.

Two lock formats are read:

* ``uv.lock``: each registry file carries its own ``upload-time``, so the
  check is offline.
* ``uv pip compile --generate-hashes`` output (``.github/requirements/*.txt``):
  the file names no upload time, so the check asks PyPI for each
  ``name==version`` and matches every pinned ``sha256`` to a PyPI file.

The check fails closed. A pin it cannot parse, a hash PyPI does not list, a
file with no upload time, a yanked file, or a PyPI request that still fails
after the retries is an error, never a skip.

Exit status: 0 when every file is old enough, 1 when a file is too young or
yanked, 2 when the check cannot run (bad arguments, a parse error, a network
error).

Usage::

    python scripts/ci/check_pin_age.py --min-days 7 uv.lock .github/requirements/*.txt

Only the standard library is used, so the bare runner Python can run it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
# PyPI documents no rate limit for the JSON API; 16 parallel requests finish
# the ~100 CI pins in about a second without a burst large enough to throttle.
MAX_WORKERS = 16
ATTEMPTS = 4
TIMEOUT_SECONDS = 20

_PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;\\]+)(?P<rest>.*)$")
_HASH = re.compile(r"--hash=sha256:(?P<digest>[0-9a-f]{64})\b")


class CheckError(Exception):
    """The check cannot reach a verdict (exit 2)."""


@dataclasses.dataclass(frozen=True)
class LockedFile:
    """One locked distribution file and the time PyPI received it."""

    source: str
    name: str
    version: str
    filename: str
    uploaded: dt.datetime
    yanked: bool = False


@dataclasses.dataclass(frozen=True)
class Pin:
    """One ``name==version`` line of a hash-locked requirements file."""

    source: str
    name: str
    version: str
    hashes: frozenset[str]


def normalize(name: str) -> str:
    """PEP 503 name normalization.

    >>> normalize("Jaraco.Classes")
    'jaraco-classes'
    >>> normalize("backports_tarfile")
    'backports-tarfile'
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_time(value: str) -> dt.datetime:
    """Parse an ISO-8601 time that PyPI or uv wrote (always UTC).

    >>> parse_time("2026-10-11T01:05:12.408Z").isoformat()
    '2026-10-11T01:05:12.408000+00:00'
    """
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise CheckError(f"upload time {value!r} has no time zone")
    return parsed


def logical_lines(text: str) -> Iterable[tuple[int, str]]:
    r"""Join backslash continuations; drop comments and blank lines.

    Yields the 1-based number of the first physical line with the joined text.

    >>> list(logical_lines("# head\na==1 \\\n    --hash=sha256:x\n    # via b\n"))
    [(2, 'a==1 --hash=sha256:x')]
    """
    start, parts = 0, []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not parts:
            if not line or line.startswith("#"):
                continue
            start = number
        continued = line.endswith("\\")
        parts.append(line[:-1].strip() if continued else line)
        if not continued:
            yield start, " ".join(part for part in parts if part)
            parts = []
    if parts:
        yield start, " ".join(part for part in parts if part)


def parse_requirements(path: Path) -> list[Pin]:
    """Read a ``--generate-hashes`` lock. Every non-comment line must be a
    hashed exact pin; anything else (an editable, a URL, a range, an option)
    is refused, because its age cannot be proven."""
    pins = []
    for number, line in logical_lines(path.read_text(encoding="utf-8")):
        match = _PIN.match(line)
        hashes = frozenset(_HASH.findall(line))
        if not match or not hashes:
            raise CheckError(f"{path}:{number}: not a hashed name==version pin: {line[:80]!r}")
        pins.append(Pin(f"{path}:{number}", normalize(match["name"]), match["version"], hashes))
    if not pins:
        raise CheckError(f"{path}: no pins found")
    return pins


def files_from_uv_lock(path: Path) -> list[LockedFile]:
    """Every registry file of a ``uv.lock``, with the upload time the lock
    records. The editable project itself is skipped; every other source
    (git, URL, path) is refused, because it has no upload time to prove."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    files = []
    for package in data.get("package", []):
        name, version = package["name"], package.get("version", "?")
        source = package.get("source", {})
        if "registry" not in source:
            if "editable" in source or "virtual" in source:
                continue
            raise CheckError(f"{path}: {name}=={version} has a non-registry source {source}")
        entries = ([package["sdist"]] if "sdist" in package else []) + package.get("wheels", [])
        if not entries:
            raise CheckError(f"{path}: {name}=={version} lists no files")
        for entry in entries:
            filename = entry.get("url", "?").rsplit("/", 1)[-1]
            if "upload-time" not in entry:
                raise CheckError(f"{path}: {name}=={version} {filename} has no upload-time")
            files.append(LockedFile(str(path), name, version, filename, parse_time(entry["upload-time"])))
    if not files:
        raise CheckError(f"{path}: no registry files found")
    return files


def fetch_release(name: str, version: str) -> dict:
    """GET the PyPI JSON for one release, retrying transient failures."""
    url = PYPI_JSON.format(name=name, version=version)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "claude-jacked-pin-age-check"})
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise CheckError(f"{name}=={version}: PyPI has no such release (404)") from error
            if error.code != 429 and error.code < 500:
                raise CheckError(f"{name}=={version}: PyPI answered HTTP {error.code}") from error
            reason = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            reason = f"{type(error).__name__}: {error}"
        if attempt == ATTEMPTS:
            raise CheckError(f"{name}=={version}: PyPI request failed {ATTEMPTS} times, last: {reason}")
        time.sleep(2 ** (attempt - 1))
    raise AssertionError("unreachable")


def files_for_pin(pin: Pin, release: dict) -> list[LockedFile]:
    """Match each pinned hash to the PyPI file that carries it."""
    by_hash = {entry["digests"]["sha256"]: entry for entry in release.get("urls", [])}
    files = []
    for digest in sorted(pin.hashes):
        entry = by_hash.get(digest)
        if entry is None:
            raise CheckError(f"{pin.source}: {pin.name}=={pin.version}: PyPI lists no file with sha256 {digest}")
        uploaded = entry.get("upload_time_iso_8601")
        if not uploaded:
            raise CheckError(f"{pin.source}: {pin.name}=={pin.version} {entry['filename']} has no upload time")
        files.append(
            LockedFile(pin.source, pin.name, pin.version, entry["filename"], parse_time(uploaded), bool(entry.get("yanked")))
        )
    return files


def files_from_requirements(
    path: Path, fetch: Callable[[str, str], dict] = fetch_release, workers: int = MAX_WORKERS
) -> list[LockedFile]:
    """Resolve every pin of a hash-locked file to its PyPI files, in parallel."""
    pins = parse_requirements(path)
    releases: dict[tuple[str, str], dict] = {}
    keys = sorted({(pin.name, pin.version) for pin in pins})
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(keys)))) as pool:
        futures = {pool.submit(fetch, *key): key for key in keys}
        for future in concurrent.futures.as_completed(futures):
            releases[futures[future]] = future.result()
    return [file for pin in pins for file in files_for_pin(pin, releases[(pin.name, pin.version)])]


def violations(files: Iterable[LockedFile], now: dt.datetime, min_days: float) -> list[str]:
    """One line per pin that has a yanked file or a file younger than
    ``min_days``; the line names the youngest file and how many are young.

    >>> utc = dt.timezone.utc
    >>> now = dt.datetime(2026, 11, 5, tzinfo=utc)
    >>> young = LockedFile("ci.txt:3", "uv", "1.0", "uv-1.0.tar.gz", dt.datetime(2026, 11, 3, tzinfo=utc))
    >>> old = dataclasses.replace(young, filename="uv-1.0-py3-none-any.whl", uploaded=dt.datetime(2026, 10, 1, tzinfo=utc))
    >>> violations([old, young], now, 7)
    ['ci.txt:3: uv==1.0 has 1 of 2 files younger than 7 days; youngest uv-1.0.tar.gz uploaded 2026-11-03T00:00Z, 2.0 days old (passes after 2026-11-10T00:00Z)']
    >>> violations([old], now, 7)
    []
    """
    cutoff = now - dt.timedelta(days=min_days)
    by_pin: dict[tuple[str, str, str], list[LockedFile]] = {}
    for file in files:
        by_pin.setdefault((file.source, file.name, file.version), []).append(file)
    problems = []
    for (source, name, version), pin_files in by_pin.items():
        for file in pin_files:
            if file.yanked:
                problems.append(f"{source}: {name}=={version} {file.filename} is yanked on PyPI")
        young = [file for file in pin_files if file.uploaded > cutoff]
        if young:
            newest = max(young, key=lambda file: file.uploaded)
            age = (now - newest.uploaded).total_seconds() / 86400
            ready = newest.uploaded + dt.timedelta(days=min_days)
            problems.append(
                f"{source}: {name}=={version} has {len(young)} of {len(pin_files)} files younger than "
                f"{min_days:g} days; youngest {newest.filename} uploaded {newest.uploaded:%Y-%m-%dT%H:%MZ}, "
                f"{age:.1f} days old (passes after {ready:%Y-%m-%dT%H:%MZ})"
            )
    return problems


def check(
    paths: Iterable[Path],
    min_days: float,
    now: dt.datetime,
    fetch: Callable[[str, str], dict] = fetch_release,
    out=sys.stdout,
) -> int:
    """Run the check over every path; print one summary line per lock."""
    problems = []
    for path in paths:
        started = time.monotonic()
        if path.name == "uv.lock":
            files = files_from_uv_lock(path)
        elif path.suffix == ".txt":
            files = files_from_requirements(path, fetch)
        else:
            raise CheckError(f"{path}: not a uv.lock or a .txt requirements lock")
        found = violations(files, now, min_days)
        problems += found
        pins = len({(file.name, file.version) for file in files})
        youngest = max(files, key=lambda file: file.uploaded)
        print(
            f"{path}: {pins} pins, {len(files)} files, {len(found)} problems; youngest "
            f"{youngest.name}=={youngest.version} uploaded {youngest.uploaded:%Y-%m-%dT%H:%MZ} "
            f"({time.monotonic() - started:.1f}s)",
            file=out,
        )
    for problem in problems:
        print(f"FAIL {problem}", file=out)
    print(f"pin age: {'FAIL' if problems else 'PASS'}, minimum {min_days:g} days before {now:%Y-%m-%dT%H:%MZ}", file=out)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--min-days", type=float, required=True, help="minimum age of every locked file, in days")
    parser.add_argument("paths", nargs="+", type=Path, help="uv.lock and/or hash-locked .txt files")
    args = parser.parse_args(argv)
    if args.min_days <= 0:
        parser.error("--min-days must be positive")
    try:
        return check(args.paths, args.min_days, dt.datetime.now(dt.timezone.utc))
    except (CheckError, OSError, tomllib.TOMLDecodeError, KeyError) as error:
        print(f"pin age: ERROR {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
