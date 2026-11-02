"""Text for the menu-bar pill and the tray icon.

:func:`menubar_title` gives the menu-bar pill text on macOS.
:func:`usage_line` gives the usage line of the Windows and Linux tray menu,
and :func:`tray_tooltip` gives the hover text of the Windows tray icon. All
three read a summary from :func:`jacked.service.menubar_summary.summarize_account`
and show only the limits that the account has.

A summary can come from a response on the network, so every field is
untrusted here: a number shows only when it is a finite number, and a name
shows only after :func:`jacked.usage_plans.clean_label`. Pure Python: no
rumps, no pystray, safe to import from any layer.
"""

from __future__ import annotations

import math
import re
from typing import Optional

# Windows keeps a tray tooltip in NOTIFYICONDATAW.szTip: 128 UTF-16 code
# units with the terminating null. pystray raises on a longer title.
TOOLTIP_MAX_UNITS = 127
TRAY_TOOLTIP_UNAVAILABLE = "Jacked · usage unavailable"
USAGE_LINE_UNAVAILABLE = "Usage unavailable"
USAGE_LINE_EMPTY = "No usage data yet"
_PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex"}


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


def finite_number(value) -> Optional[float]:
    """*value* as a float when it is a finite int or float, else None.

    A bool, a string, NaN, an infinity and an int too large for a float are
    not readings.

    >>> finite_number(12), finite_number(12.5), finite_number(0)
    (12.0, 12.5, 0.0)
    >>> [finite_number(v) for v in (float("nan"), float("inf"), 10**400, "12", True, None)]
    [None, None, None, None, None, None]
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _pct(value: float) -> str:
    """A percent label, rounded half up like ``Math.round`` in usage.js.

    Python's ``round`` rounds half to even, so 40.5 would show 40% in the
    pill and 41% on the dashboard.

    >>> _pct(40.5), _pct(40.4), _pct(0), _pct(100.0)
    ('41%', '40%', '0%', '100%')
    """
    return f"{math.floor(value + 0.5)}%"


def _label(value) -> Optional[str]:
    """A provider-supplied name made safe to show, or None."""
    from jacked.usage_plans import clean_label

    return clean_label(value)


def _limits(summary) -> tuple:
    """(5h, 7d, (model name, percent)) of a summary. A part that cannot be
    read is None, so a malformed summary shows fewer limits, never junk."""
    if not isinstance(summary, dict):
        return None, None, None
    five = finite_number(summary.get("five_hour"))
    week = finite_number(summary.get("seven_day"))
    cap = summary.get("model_cap")
    model = None
    if isinstance(cap, dict):
        percent = finite_number(cap.get("utilization"))
        if percent is not None:
            model = (_label(cap.get("label")) or "Model", percent)
    return five, week, model


def menubar_title(summary: Optional[dict]) -> str:
    """Render the menu-bar pill text. The arm icon carries the color.

    The text shows only the limits that the account has, in this order: the
    5-hour window, the 7-day window, then the model limit (its one-letter
    tag and its percent, for example ``F18%`` for Fable). Two numbers
    without a tag are always the 5-hour and the 7-day windows. When one of
    the two windows has no value, the other window gets its tag (``7d`` or
    ``5h``), so the first number never changes its meaning when the active
    account changes. The tag comes from the model name, never from the
    summary. No data gives a hyphen. The agent shows the server-down state
    itself.

    >>> menubar_title(None)
    '-'
    >>> menubar_title({"five_hour": 37.0, "seven_day": 87.0, "color": "yellow"})
    '37%·87%'
    >>> menubar_title({"five_hour": 40.4, "seven_day": 30.6, "color": "green"})
    '40%·31%'
    >>> cap = {"label": "Fable", "utilization": 18.0}
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
    five, week, model = _limits(summary)
    if five is not None and week is not None:
        slots = [_pct(five), _pct(week)]
    elif week is not None:
        slots = [f"7d {_pct(week)}"]
    elif five is not None:
        slots = [f"5h {_pct(five)}"]
    else:
        slots = []
    if model is not None:
        slots.append(f"{model_cap_tag(model[0])}{_pct(model[1])}")
    return "·".join(slots) or "-"


def _provider_name(value) -> str:
    """The display name of a provider; an unknown one shows its cleaned name."""
    if isinstance(value, str) and value in _PROVIDER_NAMES:
        return _PROVIDER_NAMES[value]
    label = _label(value)
    return label.title() if label else "Unknown provider"


def usage_line(summary: Optional[dict]) -> Optional[str]:
    """The provider and every limit of the account, with full names.

    None when the summary has no limit to show. A provider-blocked account
    ends with "usage blocked".

    >>> usage_line({"provider": "claude", "five_hour": None, "seven_day": 74.0,
    ...             "model_cap": {"label": "Fable", "utilization": 100.0}})
    'Claude · 7d 74% · Fable 100%'
    >>> usage_line({"provider": "codex", "five_hour": 5.0, "seven_day": 40.0,
    ...             "blocked": True})
    'Codex · 5h 5% · 7d 40% · usage blocked'
    >>> usage_line(None) is None
    True
    """
    five, week, model = _limits(summary)
    parts = []
    if five is not None:
        parts.append(f"5h {_pct(five)}")
    if week is not None:
        parts.append(f"7d {_pct(week)}")
    if model is not None:
        parts.append(f"{model[0]} {_pct(model[1])}")
    if not parts:
        return None
    if summary.get("blocked") is True:
        parts.append("usage blocked")
    return " · ".join([_provider_name(summary.get("provider")), *parts])


def fit_tooltip(text: str) -> str:
    """*text* cut to :data:`TOOLTIP_MAX_UNITS` UTF-16 units.

    A cut text ends with an ellipsis. The cut never splits a character that
    takes two units, and it takes one pass over the text.

    >>> fit_tooltip("Jacked")
    'Jacked'
    >>> t = fit_tooltip("x" * 500)
    >>> len(t), t[-1]
    (127, '…')
    """
    units = text.encode("utf-16-le")
    if len(units) // 2 <= TOOLTIP_MAX_UNITS:
        return text
    head = units[: (TOOLTIP_MAX_UNITS - 1) * 2].decode("utf-16-le", errors="ignore")
    return head + "…"


def tray_tooltip(summary: Optional[dict]) -> str:
    """The hover text of the tray icon: "Jacked" and the :func:`usage_line`.

    >>> tray_tooltip(None)
    'Jacked'
    >>> tray_tooltip({"provider": "claude", "five_hour": None, "seven_day": 74.0,
    ...               "model_cap": {"label": "Fable", "utilization": 100.0}})
    'Jacked · Claude · 7d 74% · Fable 100%'
    """
    line = usage_line(summary)
    return fit_tooltip("Jacked" if line is None else f"Jacked · {line}")
