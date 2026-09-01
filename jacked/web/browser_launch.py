"""Open the Claude authorize URL in a browser jacked controls.

Re-auth used to hand the URL to ``webbrowser.open``, which lands in whatever
profile the user's default browser has open. If that profile is signed in to a
different Claude account, the user has to log out, remember which account the
card was for, and click the link again.

So each account gets its own browser profile directory under
``~/.claude/jacked-browser-profiles``. Chrome, Edge, Brave, Chromium and
Firefox all accept a profile directory on the command line, so the cookies for
one Claude account stay in one place and a later re-auth is a single Authorize
click. A flow with no known identity yet (Add Account) has no profile to reuse,
so it falls back to a private window. The system default browser stays
available as the opt-out.

The authorize URL carries PKCE state, so it is never logged.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jacked.web.window_focus import bring_to_front_windows as _bring_to_front_windows

logger = logging.getLogger("jacked.oauth.browser")

SETTING_KEY = "oauth_browser_mode"
MODES = ("profile", "incognito", "default")
DEFAULT_MODE = "profile"
PROFILE_ROOT = Path.home() / ".claude" / "jacked-browser-profiles"

# Characters kept verbatim in a profile directory name. Everything else becomes
# "_" so an email can never escape PROFILE_ROOT or confuse a shell.
_SLUG_SAFE = set("abcdefghijklmnopqrstuvwxyz0123456789._@+-")

# Env override for a browser jacked would not otherwise find.
BROWSER_ENV_VAR = "JACKED_OAUTH_BROWSER"


def normalize_mode(raw: Optional[str]) -> str:
    """Coerce a stored setting value to a known mode.

    The generic settings endpoint stores JSON, so the dashboard writes
    ``"incognito"`` with the quotes included; a value written by hand has no
    quotes. Both have to mean the same thing.

    >>> normalize_mode("incognito")
    'incognito'
    >>> normalize_mode('"incognito"')
    'incognito'
    >>> normalize_mode("  DEFAULT  ")
    'default'
    >>> normalize_mode("nonsense")
    'profile'
    >>> normalize_mode(None)
    'profile'
    """
    if not raw:
        return DEFAULT_MODE
    value = raw.strip().strip('"').strip("'").strip().lower()
    return value if value in MODES else DEFAULT_MODE


def read_mode(db) -> str:
    """The configured browser mode, falling back to the default when unset.

    >>> class FakeDb:
    ...     def __init__(self, value):
    ...         self.value = value
    ...     def get_setting(self, key):
    ...         return self.value
    >>> read_mode(FakeDb("incognito"))
    'incognito'
    >>> read_mode(FakeDb('"default"'))
    'default'
    >>> read_mode(FakeDb("nonsense"))
    'profile'
    >>> read_mode(FakeDb(None))
    'profile'
    >>> read_mode(None)
    'profile'
    """
    value = None
    if db is not None:
        try:
            value = db.get_setting(SETTING_KEY)
        except Exception as e:  # a settings read must never break OAuth
            logger.debug("Could not read %s: %s", SETTING_KEY, e)
            value = None
    return normalize_mode(value)


def profile_slug(account: Optional[dict]) -> Optional[str]:
    """Directory name for this account's browser profile.

    Keyed on the email alone: one claude.ai login covers every org that email
    belongs to, so two accounts differing only by org share a profile.

    >>> profile_slug(None) is None
    True
    >>> profile_slug({"email": "Jack@Example.com"})
    'jack@example.com'
    >>> profile_slug({"email": "  jack+alt@example.com  "})
    'jack+alt@example.com'
    >>> profile_slug({"email": "odd/name?@example.com"})
    'odd_name_@example.com'
    >>> profile_slug({"email": "..", "id": 7})
    'account-7'
    >>> profile_slug({"email": "", "id": 7})
    'account-7'
    >>> profile_slug({"email": ""}) is None
    True
    >>> a = profile_slug({"email": "j@x.com", "organization_uuid": "org-1"})
    >>> b = profile_slug({"email": "j@x.com", "organization_uuid": "org-2"})
    >>> a == b
    True
    """
    if account is None:
        return None
    email = (account.get("email") or "").strip().lower()
    slug = "".join(c if c in _SLUG_SAFE else "_" for c in email)
    # A slug of nothing but dots would name the parent directory instead of a
    # profile, so treat it as an unusable email and fall back to the id.
    if slug.strip("."):
        return slug
    account_id = account.get("id")
    if account_id is not None:
        return f"account-{account_id}"
    return None


def profile_dir_for(account: Optional[dict]) -> Optional[Path]:
    """Profile directory for this account, or None when the identity is unknown.

    >>> profile_dir_for(None) is None
    True
    >>> profile_dir_for({"email": "j@x.com"}).name
    'j@x.com'
    >>> profile_dir_for({"email": "j@x.com"}).parent == PROFILE_ROOT
    True
    """
    slug = profile_slug(account)
    if slug is None:
        return None
    return PROFILE_ROOT / slug


@dataclass(frozen=True)
class BrowserSpec:
    """One launchable browser: display name, executable, and CLI dialect."""

    name: str
    path: str
    family: str  # "chromium" | "firefox"


def _win_candidates() -> list[tuple[str, str, str]]:
    """(name, relative path, family) triples under the Windows program roots."""
    return [
        ("Chrome", r"Google\Chrome\Application\chrome.exe", "chromium"),
        ("Edge", r"Microsoft\Edge\Application\msedge.exe", "chromium"),
        ("Brave", r"BraveSoftware\Brave-Browser\Application\brave.exe", "chromium"),
        ("Chromium", r"Chromium\Application\chrome.exe", "chromium"),
        ("Firefox", r"Mozilla Firefox\firefox.exe", "firefox"),
    ]


def _mac_candidates() -> list[tuple[str, str, str]]:
    """(name, app-relative path, family) triples for macOS app bundles."""
    return [
        ("Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome", "chromium"),
        ("Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "chromium"),
        ("Brave", "Brave Browser.app/Contents/MacOS/Brave Browser", "chromium"),
        ("Chromium", "Chromium.app/Contents/MacOS/Chromium", "chromium"),
        ("Firefox", "Firefox.app/Contents/MacOS/firefox", "firefox"),
    ]


def _linux_candidates() -> list[tuple[str, str, str]]:
    """(name, executable, family) triples resolved through PATH."""
    return [
        ("Chrome", "google-chrome", "chromium"),
        ("Chrome", "google-chrome-stable", "chromium"),
        ("Edge", "microsoft-edge", "chromium"),
        ("Brave", "brave-browser", "chromium"),
        ("Chromium", "chromium", "chromium"),
        ("Chromium", "chromium-browser", "chromium"),
        ("Firefox", "firefox", "firefox"),
    ]


def _candidates() -> list[BrowserSpec]:
    """Installed browsers this platform can launch, best first."""
    found: list[BrowserSpec] = []
    if sys.platform == "win32":
        roots = [
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        for name, rel, family in _win_candidates():
            for root in roots:
                if not root:
                    continue
                path = Path(root) / rel
                if path.exists():
                    found.append(BrowserSpec(name, str(path), family))
                    break
    elif sys.platform == "darwin":
        roots = [Path("/Applications"), Path.home() / "Applications"]
        for name, rel, family in _mac_candidates():
            for root in roots:
                path = root / rel
                if path.exists():
                    found.append(BrowserSpec(name, str(path), family))
                    break
    else:
        for name, exe, family in _linux_candidates():
            resolved = shutil.which(exe)
            if resolved:
                found.append(BrowserSpec(name, resolved, family))
    return found


def find_browser() -> Optional[BrowserSpec]:
    """The browser to drive: the env override if set, else the best installed."""
    override = os.environ.get(BROWSER_ENV_VAR)
    if override and Path(override).exists():
        base = Path(override).name
        family = "firefox" if "firefox" in base.lower() else "chromium"
        return BrowserSpec(base, override, family)
    candidates = _candidates()
    return candidates[0] if candidates else None


def build_command(
    spec: BrowserSpec, url: str, mode: str, profile_dir: Optional[Path]
) -> list[str]:
    """Argv that opens ``url`` in ``spec`` under ``mode``.

    >>> chrome = BrowserSpec("Chrome", "/usr/bin/google-chrome", "chromium")
    >>> cmd = build_command(chrome, "u", "profile", Path("/p"))
    >>> cmd[1] == "--user-data-dir=" + str(Path("/p"))
    True
    >>> cmd[2:]
    ['--no-first-run', '--no-default-browser-check', '--new-window', 'u']
    >>> build_command(chrome, "u", "incognito", None)[1:]
    ['--incognito', '--new-window', 'u']
    >>> edge = BrowserSpec("Edge", "/opt/msedge/msedge", "chromium")
    >>> build_command(edge, "u", "incognito", None)[1:]
    ['--inprivate', '--new-window', 'u']
    >>> ff = BrowserSpec("Firefox", "/usr/bin/firefox", "firefox")
    >>> build_command(ff, "u", "profile", Path("/p"))[1:] == [
    ...     "-profile", str(Path("/p")), "-no-remote", "-new-window", "u"]
    True
    >>> build_command(ff, "u", "incognito", None)[1:]
    ['-private-window', 'u']
    >>> build_command(chrome, "u", "default", None)
    Traceback (most recent call last):
    ValueError: build_command does not drive the system default browser
    >>> build_command(chrome, "u", "profile", None)
    Traceback (most recent call last):
    ValueError: profile mode needs a profile directory
    """
    if mode == "default":
        raise ValueError("build_command does not drive the system default browser")
    if mode == "profile" and profile_dir is None:
        raise ValueError("profile mode needs a profile directory")

    if spec.family == "firefox":
        if mode == "profile":
            return [
                spec.path,
                "-profile",
                str(profile_dir),
                "-no-remote",
                "-new-window",
                url,
            ]
        return [spec.path, "-private-window", url]

    if mode == "profile":
        return [
            spec.path,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            url,
        ]
    private_flag = "--inprivate" if "msedge" in Path(spec.path).name.lower() else "--incognito"
    return [spec.path, private_flag, "--new-window", url]


def _app_bundle_for(path: str) -> Optional[str]:
    """The ``.app`` bundle containing this executable, or None.

    >>> _app_bundle_for("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    '/Applications/Google Chrome.app'
    >>> _app_bundle_for("/usr/bin/google-chrome") is None
    True
    >>> _app_bundle_for("/Applications/Firefox.app") is None
    True
    """
    if "/Contents/MacOS/" not in path:
        return None
    index = path.find(".app/")
    if index == -1:
        return None
    return path[: index + len(".app")]


def launch_argv(
    spec: BrowserSpec,
    url: str,
    mode: str,
    profile_dir: Optional[Path],
    platform: str = sys.platform,
) -> list[str]:
    """Argv to spawn, adapted to how the platform activates a new window.

    macOS ignores a window opened by a background process just like Windows
    does, but ``open -n -a`` asks Launch Services to start and *activate* the
    app, so the sign-in window comes to the front on its own. Everywhere else
    the executable is invoked directly.

    >>> mac = BrowserSpec(
    ...     "Chrome",
    ...     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ...     "chromium",
    ... )
    >>> launch_argv(mac, "u", "incognito", None, platform="darwin")
    ['open', '-n', '-a', '/Applications/Google Chrome.app', '--args', '--incognito', '--new-window', 'u']
    >>> nix = BrowserSpec("Chrome", "/usr/bin/google-chrome", "chromium")
    >>> launch_argv(nix, "u", "incognito", None, platform="darwin")
    ['/usr/bin/google-chrome', '--incognito', '--new-window', 'u']
    >>> launch_argv(mac, "u", "incognito", None, platform="win32")[0] == mac.path
    True
    """
    cmd = build_command(spec, url, mode, profile_dir)
    if platform == "darwin":
        bundle = _app_bundle_for(spec.path)
        if bundle is not None:
            return ["open", "-n", "-a", bundle, "--args", *cmd[1:]]
    return cmd


@dataclass(frozen=True)
class LaunchResult:
    """What the launch actually did: the mode used, and the browser driven.

    ``browser`` is None whenever no controlled browser was involved, which is
    every path that fell back to the system default.
    """

    mode: str
    browser: Optional[str] = None


def _detach_kwargs() -> dict:
    """Popen kwargs that let the browser outlive the jacked process."""
    if sys.platform == "win32":
        return {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    return {"start_new_session": True}


def open_auth_url(url: str, account: Optional[dict], db) -> LaunchResult:
    """Open the authorize URL, reporting what actually happened.

    The result is what the dashboard tells the user, so it reports the
    fallback that happened rather than the mode that was asked for, and names
    the browser so the banner can say which window to look for.
    """
    mode = read_mode(db)
    if mode == "default":
        webbrowser.open(url)
        return LaunchResult("default")

    spec = find_browser()
    if spec is None:
        logger.info("OAuth browser: no supported browser found, using system default")
        webbrowser.open(url)
        return LaunchResult("default")

    profile_dir = profile_dir_for(account)
    effective = mode
    if mode == "profile" and profile_dir is None:
        # No known identity yet (Add Account). Reusing a shared profile would
        # silently authorize whoever is already signed in there.
        effective = "incognito"
        profile_dir = None

    try:
        if effective == "profile":
            profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                profile_dir.chmod(0o700)
            except OSError as e:  # Windows/ACL filesystems may refuse
                logger.debug("Could not tighten profile dir permissions: %s", e)
        cmd = launch_argv(spec, url, effective, profile_dir)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **_detach_kwargs(),
        )
    except Exception as e:
        logger.warning("OAuth browser launch failed (%s), using system default: %s", spec.name, e)
        webbrowser.open(url)
        return LaunchResult("default")

    logger.info(
        "OAuth browser: mode=%s browser=%s profile=%s",
        effective,
        spec.name,
        str(profile_dir) if profile_dir else "-",
    )
    if sys.platform == "win32":
        # Windows denies foreground activation to a window opened by a
        # background process (the tray service), so the sign-in window lands
        # behind the dashboard and the user never sees it. Raising it takes
        # seconds of polling, so it must not hold up the API response.
        threading.Thread(
            target=_bring_to_front_windows,
            args=(proc.pid, spec.path),
            daemon=True,
            name="jacked-oauth-focus",
        ).start()
    return LaunchResult(effective, spec.name)
