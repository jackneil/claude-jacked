"""Browser install locations that ``browser_launch`` probes on Windows and macOS.

Moved out of ``browser_launch`` (module size ratchet). Linux resolves its
browsers through PATH and fixed locations, which stay in ``browser_launch``.
"""

from __future__ import annotations


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
