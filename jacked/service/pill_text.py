"""Text for the menu-bar pill and the tray tooltip.

:func:`menubar_title` gives the menu-bar pill text on macOS and
:func:`tray_tooltip` gives the hover text of the Windows and Linux tray icon.
Both read a summary from :func:`jacked.service.menubar_summary.summarize_account`
and show only the limits that the account has. Pure Python: no rumps, no
pystray, safe to import from any layer.
"""

from __future__ import annotations

import math
import re
from typing import Optional


def model_cap_tag(label) -> str:
    """The one-letter tag of a model limit in the menu-bar pill.

    The tag is the first letter of the last word of the label, in upper
    case. The last word names the model for both providers ("Fable",
    "GPT-5.3-Codex-Spark"). A label without letters gets "M" (model).

    >>> model_cap_tag("Fable"), model_cap_tag("Claude Opus 5")
    ('F', 'O')
    >>> model_cap_tag("GPT-5.3-Codex-Spark")
    'S'
    >>> model_cap_tag("42"), model_cap_tag(None)
    ('M', 'M')
    """
    words = re.findall(r"[^\W\d_]+", str(label or ""))
    return words[-1][0].upper() if words else "M"


def _pct(value) -> str:
    """A percent label, rounded half up like ``Math.round`` in usage.js.

    Python's ``round`` rounds half to even, so 40.5 would show 40% in the
    pill and 41% on the dashboard.

    >>> _pct(40.5), _pct(40.4), _pct(0), _pct(100.0)
    ('41%', '40%', '0%', '100%')
    """
    return f"{math.floor(float(value) + 0.5)}%"


def menubar_title(summary: Optional[dict]) -> str:
    """Render the menu-bar pill text. The arm icon carries the color.

    The text shows only the limits that the account has, in this order: the
    5-hour window, the 7-day window, then the model limit (its one-letter
    tag and its percent, for example ``F18%`` for Fable). Two numbers
    without a tag are always the 5-hour and the 7-day windows. When one of
    the two windows has no value, the other window gets its tag (``7d`` or
    ``5h``), so the first number never changes its meaning when the active
    account changes. No data gives a hyphen. The agent shows the
    server-down state itself; this function sees only live data.

    >>> menubar_title(None)
    '-'
    >>> menubar_title({"five_hour": 37.0, "seven_day": 87.0, "color": "yellow"})
    '37%·87%'
    >>> menubar_title({"five_hour": 40.4, "seven_day": 30.6, "color": "green"})
    '40%·31%'
    >>> cap = {"label": "Fable", "tag": "F", "utilization": 18.0}
    >>> menubar_title({"five_hour": 5.0, "seven_day": 11.0, "model_cap": cap})
    '5%·11%·F18%'
    >>> menubar_title({"five_hour": None, "seven_day": 74.0,
    ...                "model_cap": dict(cap, utilization=100.0)})
    '7d 74%·F100%'
    >>> menubar_title({"five_hour": None, "seven_day": 100.0, "color": "red"})
    '7d 100%'
    >>> menubar_title({"five_hour": 40.0, "seven_day": None})
    '5h 40%'
    """
    if not summary:
        return "-"
    five, week = summary.get("five_hour"), summary.get("seven_day")
    if five is not None and week is not None:
        slots = [_pct(five), _pct(week)]
    elif week is not None:
        slots = [f"7d {_pct(week)}"]
    elif five is not None:
        slots = [f"5h {_pct(five)}"]
    else:
        slots = []
    cap = summary.get("model_cap")
    if cap:
        tag = cap.get("tag") or model_cap_tag(cap.get("label"))
        slots.append(f"{tag}{_pct(cap['utilization'])}")
    return "·".join(slots) or "-"


# Windows keeps a tray tooltip in NOTIFYICONDATAW.szTip: 128 UTF-16 code
# units with the terminating null. pystray raises on a longer title.
TOOLTIP_MAX_UNITS = 127
TRAY_TOOLTIP_UNAVAILABLE = "Jacked · usage unavailable"
_PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex"}


def tray_tooltip(summary: Optional[dict]) -> str:
    """The hover text of the Windows and Linux tray icon.

    The tray icon color shows the state of the service, so the usage is in
    the tooltip. The tooltip names the provider and every limit the account
    has, with full names: ``5h``, ``7d`` and the model name. No data gives
    "Jacked". A tooltip longer than :data:`TOOLTIP_MAX_UNITS` is cut and
    ends with an ellipsis.

    >>> tray_tooltip(None)
    'Jacked'
    >>> tray_tooltip({"provider": "claude", "five_hour": None, "seven_day": 74.0,
    ...               "model_cap": {"label": "Fable", "utilization": 100.0}})
    'Jacked · Claude · 7d 74% · Fable 100%'
    >>> tray_tooltip({"provider": "codex", "five_hour": 5.0, "seven_day": 40.0,
    ...               "blocked": True})
    'Jacked · Codex · 5h 5% · 7d 40% · usage blocked'
    """
    if not summary:
        return "Jacked"
    provider = str(summary.get("provider") or "claude")
    parts = ["Jacked", _PROVIDER_NAMES.get(provider, provider.title())]
    for name, key in (("5h", "five_hour"), ("7d", "seven_day")):
        if summary.get(key) is not None:
            parts.append(f"{name} {_pct(summary[key])}")
    cap = summary.get("model_cap")
    if cap:
        parts.append(f"{cap.get('label') or 'Model'} {_pct(cap['utilization'])}")
    if summary.get("blocked"):
        parts.append("usage blocked")
    text = " · ".join(parts)
    if len(text.encode("utf-16-le")) // 2 <= TOOLTIP_MAX_UNITS:
        return text
    while len((text + "…").encode("utf-16-le")) // 2 > TOOLTIP_MAX_UNITS:
        text = text[:-1]
    return text + "…"
