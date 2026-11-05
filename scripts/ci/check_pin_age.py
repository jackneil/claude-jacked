#!/usr/bin/env python3
"""Fail when a locked package file is younger than the cooldown, or yanked.

The 7-day Dependabot cooldown covers only the package that Dependabot bumps.
A bump can pull in a new transitive pin that is hours old, because the uv
updater re-runs ``uv pip compile`` without ``--exclude-newer`` (#312i). This
check reads every pin of every lock and proves, from PyPI, that each locked
file was uploaded at least ``--min-days`` days before the run and is not
yanked.

Two lock formats are read. Both are checked against PyPI, never against
times that the lock records, because a lock is text in a pull request:

* ``uv.lock``: every registry package, with the ``sha256`` of each file.
  Only the PyPI registry is accepted.
* ``uv pip compile --generate-hashes`` output (``.github/requirements/*.txt``).

For each ``name==version`` the check reads ``https://pypi.org/pypi/<name>/
<version>/json`` and matches every pinned ``sha256`` to a PyPI file.

The check fails closed. A pin it cannot parse, a hash token that pip would
accept but this check cannot prove, a hash PyPI does not list, a PyPI answer
with a missing field, or a PyPI request that still fails after the retries is
an error, never a skip. One bad lock or pin does not hide the others: every
lock is checked and every result is printed.

Exit status: 0 when every file is old enough and not yanked, 1 when a file is
too young or yanked, 2 when the check could not prove every file (bad
arguments, a parse error, a PyPI error), also when it found problems too.

Usage::

    python scripts/ci/check_pin_age.py --min-days 7 uv.lock .github/requirements/*.txt

Only the standard library is used, so the bare runner Python can run it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import http.client
import json
import math
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
PYPI_REGISTRY = "https://pypi.org/simple"
# PyPI documents no rate limit for the JSON API; 16 parallel requests finish
# the ~160 pins in a few seconds without a burst large enough to throttle.
MAX_WORKERS = 16
ATTEMPTS = 4
TIMEOUT_SECONDS = 20

# The whole logical line must be what uv writes, nothing else:
#   name==version[ ; marker] --hash=sha256:<hex>[ --hash=sha256:<hex>...]
# pip ends the requirement at the first space-separated token that starts
# with "-", runs shlex on the rest, expands ${VAR}, strips "#" comments, and
# accepts any --hash spelling (any case, sha384/sha512, "--hash <v>"). A
# marker here may hold only identifiers, operators, parentheses and quoted
# strings without "-", "$", "#", quotes or whitespace, so no token before the
# hashes can start with "-" and pip sees exactly the hashes this check sees.
# Spaces only: a tab is a separator to pip and is refused here. The
# quantifiers are possessive (Python 3.11+), so a crafted marker cannot make
# the match backtrack exponentially.
_MARKER_TOKEN = r"""(?:[A-Za-z_][A-Za-z0-9_.]*+|===|==|!=|<=|>=|~=|<|>|\(|\)|'[A-Za-z0-9._+*]*'|"[A-Za-z0-9._+*]*")"""
_LINE = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9._+!]+)"
    rf"(?P<marker> +;(?: *{_MARKER_TOKEN})++)?"
    r"(?P<hashes>(?: +--hash=sha256:[0-9a-f]{64})+) *"
)
_HASH_TOKEN = re.compile(r"--hash=sha256:(?P<digest>[0-9a-f]{64})")
_SHA256 = re.compile(r"sha256:(?P<digest>[0-9a-f]{64})")


class CheckError(Exception):
    """The check cannot reach a verdict for one lock or pin (exit 2)."""


@dataclasses.dataclass(frozen=True)
class Pin:
    """One ``name==version`` of a lock and the sha256 of each locked file."""

    source: str
    name: str
    version: str
    hashes: frozenset[str]


@dataclasses.dataclass(frozen=True)
class LockedFile:
    """One locked distribution file, as PyPI describes it."""

    source: str
    name: str
    version: str
    filename: str
    uploaded: dt.datetime
    yanked: bool = False


def normalize(name: str) -> str:
    """PEP 503 name normalization.

    >>> normalize("Jaraco.Classes")
    'jaraco-classes'
    >>> normalize("backports_tarfile")
    'backports-tarfile'
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_time(value: str) -> dt.datetime:
    """Parse an ISO-8601 UTC time as PyPI writes it.

    >>> parse_time("2026-10-11T01:05:12.408Z").isoformat()
    '2026-10-11T01:05:12.408000+00:00'
    >>> parse_time("2025-09-04T15:43:15.994841Z").isoformat()
    '2025-09-04T15:43:15.994841+00:00'
    """
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"upload time {value!r} has no time zone")
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


def parse_pin_line(line: str) -> tuple[str, str, frozenset[str]] | None:
    """Split one logical line into name, version and hashes. Return None
    unless the line is exactly ``name==version [; marker] --hash=sha256:...``.

    >>> parse_pin_line("Twine==6.2.0 ; os_name == 'nt' --hash=sha256:" + "a" * 64)[:2]
    ('twine', '6.2.0')
    >>> parse_pin_line("twine==6.2.0 --hash=sha256:" + "A" * 64) is None
    True
    >>> parse_pin_line("twine==6.2.0 --hash=sha256:" + "a" * 64 + " --hash=sha512:" + "b" * 128) is None
    True
    >>> parse_pin_line("twine==6.2.0 ; os_name == 'nt' - '--hash=sha512:" + "b" * 128 + "' --hash=sha256:" + "a" * 64) is None
    True
    """
    match = _LINE.fullmatch(line)
    if not match:
        return None
    tokens = match["hashes"].split(" ")
    tokens = [token for token in tokens if token]
    hashes = frozenset(_HASH_TOKEN.fullmatch(token)["digest"] for token in tokens)
    return normalize(match["name"]), match["version"], hashes


def pins_from_requirements(path: Path) -> list[Pin]:
    """Read a ``--generate-hashes`` lock. Every non-comment line must be a
    hashed exact pin. Anything else (an editable, a URL, a range, an option,
    a hash spelling that uv does not write) is refused, because its age
    cannot be proven."""
    pins = []
    for number, line in logical_lines(path.read_text(encoding="utf-8")):
        parsed = parse_pin_line(line)
        if parsed is None:
            raise CheckError(
                f"{path}:{number}: not a name==version pin with only --hash=sha256 hashes: {line[:100]!r}"
            )
        pins.append(Pin(f"{path}:{number}", *parsed))
    if not pins:
        raise CheckError(f"{path}: no pins found")
    return pins


def pins_from_uv_lock(path: Path) -> tuple[list[Pin], int]:
    """Every PyPI package of a ``uv.lock`` and the sha256 of each of its
    files, plus the count of local projects skipped (the editable project
    itself). Any other source (git, URL, path, another registry) is refused,
    because PyPI cannot prove its age."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    pins, skipped = [], 0
    for package in data.get("package", []):
        name, version = package.get("name", "?"), package.get("version", "?")
        source = package.get("source", {})
        where = f"{path}: {name}=={version}"
        if set(source) & {"editable", "virtual"}:
            skipped += 1
            continue
        if source.get("registry") != PYPI_REGISTRY:
            raise CheckError(f"{where} has source {source}; only {PYPI_REGISTRY} can be proven")
        entries = ([package["sdist"]] if "sdist" in package else []) + package.get("wheels", [])
        if not entries:
            raise CheckError(f"{where} lists no files")
        hashes = set()
        for entry in entries:
            match = _SHA256.fullmatch(str(entry.get("hash", "")))
            if not match:
                raise CheckError(f"{where} file {entry.get('url', '?')} has hash {entry.get('hash')!r}, not sha256")
            hashes.add(match["digest"])
        pins.append(Pin(str(path), normalize(name), version, frozenset(hashes)))
    if not pins:
        raise CheckError(f"{path}: no PyPI packages found")
    return pins, skipped


def fetch_release(name: str, version: str) -> dict:
    """GET the PyPI JSON for one release, retrying transient failures."""
    url = PYPI_JSON.format(name=name, version=version)
    headers = {"Accept": "application/json", "User-Agent": "claude-jacked-pin-age-check"}
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise CheckError(f"{name}=={version}: PyPI has no such release (404)") from error
            if error.code != 429 and error.code < 500:
                raise CheckError(f"{name}=={version}: PyPI answered HTTP {error.code}") from error
            reason = f"HTTP {error.code}"
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
            # OSError: a reset or timeout during the body read. HTTPException:
            # IncompleteRead. ValueError: a cut-off or non-UTF-8 JSON body.
            reason = f"{type(error).__name__}: {error}"
        if attempt == ATTEMPTS:
            raise CheckError(f"{name}=={version}: PyPI request failed {ATTEMPTS} times, last: {reason}")
        time.sleep(2 ** (attempt - 1))
    if not isinstance(body, dict) or not isinstance(body.get("urls"), list):
        raise CheckError(f"{name}=={version}: PyPI JSON has no 'urls' list (API change?)")
    return body


def files_for_pin(pin: Pin, release: dict) -> list[LockedFile]:
    """Match each pinned hash to the PyPI file that carries it. Every field
    that the verdict reads must be present, with the right type."""
    by_hash = {}
    for entry in release["urls"]:
        digests = entry.get("digests") if isinstance(entry, dict) else None
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if isinstance(digest, str):
            by_hash[digest.lower()] = entry
    where = f"{pin.source}: {pin.name}=={pin.version}"
    files = []
    for digest in sorted(pin.hashes):
        entry = by_hash.get(digest)
        if entry is None:
            raise CheckError(f"{where}: PyPI lists no file with sha256 {digest}")
        filename, uploaded, yanked = entry.get("filename"), entry.get("upload_time_iso_8601"), entry.get("yanked")
        if not isinstance(filename, str) or not isinstance(uploaded, str) or not isinstance(yanked, bool):
            raise CheckError(
                f"{where} sha256 {digest}: PyPI file entry lacks a filename, an upload_time_iso_8601 "
                f"or a boolean yanked (got {filename!r}, {uploaded!r}, {yanked!r}; API change?)"
            )
        try:
            when = parse_time(uploaded)
        except ValueError as error:
            raise CheckError(f"{where} {filename}: bad upload time {uploaded!r}: {error}") from error
        files.append(LockedFile(pin.source, pin.name, pin.version, filename, when, yanked))
    return files


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


def read_lock(path: Path) -> tuple[list[Pin], int]:
    """Pins of one lock file and its count of skipped local projects."""
    if path.name == "uv.lock":
        return pins_from_uv_lock(path)
    if path.suffix == ".txt":
        return pins_from_requirements(path), 0
    raise CheckError(f"{path}: not a uv.lock or a .txt requirements lock")


def fetch_all(
    keys: set[tuple[str, str]], fetch: Callable[[str, str], dict], workers: int
) -> tuple[dict, dict[tuple[str, str], float]]:
    """Fetch every release once, in parallel. Return the results and the
    seconds each fetch took. A failure is kept as the CheckError for its key,
    so one bad release never hides the others."""
    results: dict[tuple[str, str], dict | CheckError] = {}
    seconds: dict[tuple[str, str], float] = {}
    if not keys:
        return results, seconds

    def timed(key):
        started = time.monotonic()
        try:
            return fetch(*key)
        finally:
            seconds[key] = time.monotonic() - started

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(keys)))) as pool:
        futures = {pool.submit(timed, key): key for key in sorted(keys)}
        for future in concurrent.futures.as_completed(futures):
            name, version = futures[future]
            try:
                results[(name, version)] = future.result()
            except CheckError as error:
                results[(name, version)] = error
            except Exception as error:  # noqa: BLE001 - a fetch defect is an unproven pin, never a crash
                results[(name, version)] = CheckError(f"{name}=={version}: {type(error).__name__}: {error}")
    return results, seconds


def check(
    paths: Iterable[Path],
    min_days: float,
    now: dt.datetime,
    fetch: Callable[[str, str], dict] | None = None,
    out=None,
    workers: int = MAX_WORKERS,
) -> int:
    """Check every lock. For each lock, print a summary line, then each of
    its problems and errors. Return the exit status (see the module
    docstring)."""
    fetch = fetch or fetch_release  # resolved per call, so a test seam or patch applies
    out = out or sys.stdout
    started = time.monotonic()
    locks: list[tuple[Path, list[Pin], int, str | None]] = []
    for path in paths:
        try:
            pins, skipped = read_lock(path)
            locks.append((path, pins, skipped, None))
        except (CheckError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            # tomllib.TOMLDecodeError and UnicodeDecodeError are ValueErrors.
            locks.append((path, [], 0, f"{path}: cannot read lock: {type(error).__name__}: {error}"))
    fetch_started = time.monotonic()
    releases, seconds = fetch_all({(pin.name, pin.version) for _, pins, _, _ in locks for pin in pins}, fetch, workers)
    if seconds:
        slowest = max(seconds, key=seconds.get)
        print(
            f"PyPI: {len(seconds)} releases in {time.monotonic() - fetch_started:.1f}s, {workers} at a time; "
            f"slowest {slowest[0]}=={slowest[1]} {seconds[slowest]:.1f}s",
            file=out,
        )

    problem_count = error_count = 0
    for path, pins, skipped, read_error in locks:
        files, errors = [], [read_error] if read_error else []
        for pin in pins:
            release = releases[(pin.name, pin.version)]
            try:
                if isinstance(release, CheckError):
                    raise CheckError(f"{pin.source}: {release}")
                files += files_for_pin(pin, release)
            except CheckError as error:
                errors.append(str(error))
        problems = violations(files, now, min_days)
        problem_count += len(problems)
        error_count += len(errors)
        youngest = max(files, key=lambda file: file.uploaded, default=None)
        newest = f"{youngest.name}=={youngest.version} uploaded {youngest.uploaded:%Y-%m-%dT%H:%MZ}" if youngest else "none"
        note = f", {skipped} local project skipped" if skipped else ""
        print(
            f"{path}: {len(pins)} pins, {len(files)} files proven, {len(problems)} problems, "
            f"{len(errors)} errors{note}; youngest {newest}",
            file=out,
        )
        for problem in problems:
            print(f"FAIL {problem}", file=out)
        for error in errors:
            print(f"ERROR {error}", file=out)

    verdict = "ERROR" if error_count else "FAIL" if problem_count else "PASS"
    print(
        f"pin age: {verdict}, {problem_count} problems, {error_count} errors; minimum {min_days:g} days "
        f"before {now:%Y-%m-%dT%H:%MZ} ({time.monotonic() - started:.1f}s)",
        file=out,
        flush=True,
    )
    return 2 if error_count else 1 if problem_count else 0


def main(argv: list[str] | None = None, fetch: Callable[[str, str], dict] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--min-days", type=float, required=True, help="minimum age of every locked file, in days")
    parser.add_argument("paths", nargs="+", type=Path, help="uv.lock and/or hash-locked .txt files")
    args = parser.parse_args(argv)
    if not math.isfinite(args.min_days) or args.min_days <= 0:
        parser.error("--min-days must be a positive finite number")
    try:
        return check(args.paths, args.min_days, dt.datetime.now(dt.timezone.utc), fetch=fetch)
    except Exception as error:  # noqa: BLE001 - an unexpected defect must exit 2, never 1 ("too young")
        print(f"pin age: ERROR {type(error).__name__}: {error}", flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
