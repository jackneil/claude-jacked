"""Open one URL in a jacked browser profile.

A provider CLI that honors ``$BROWSER`` runs this as:

    python -m jacked.web.profile_opener <profile-dir> <url>

The URL is a sign-in or device-login address. It is not logged. When no
browser can open the profile, nothing opens: the system default browser
would keep the sign-in in the wrong profile.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def open_in_profile(profile_dir: Path, url: str) -> int:
    """Open ``url`` with ``profile_dir`` pinned. Returns a process exit status.

    2: the URL is not https. 1: no browser that can open a profile.
    """
    if not url.startswith("https://"):
        return 2
    from jacked.web.browser_launch import find_browser, launch_argv

    spec = find_browser()
    if spec is None:
        return 1
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile_dir.chmod(0o700)
    except OSError:
        pass
    command = launch_argv(spec, url, "profile", profile_dir)
    subprocess.Popen(  # noqa: S603 - argv is a detected browser plus fixed flags
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        return 2
    return open_in_profile(Path(args[0]), args[-1])


if __name__ == "__main__":
    raise SystemExit(main())
