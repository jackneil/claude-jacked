"""The errors of the DCR engine config (``jacked.dcr_settings``).

They live in their own module so that ``dcr_settings`` stays inside its line
budget. ``jacked.dcr_settings`` imports and re-exports all three, so callers
keep ``from jacked.dcr_settings import DcrSettingsAccessError``.

``DcrSettingsAccessError`` and ``DcrSettingsValueError`` are ``PublicError``
subclasses: their text is written for the user, and the dashboard shows it.
"""

from __future__ import annotations

from jacked.public_errors import PublicError


class DcrSettingsUnreadableError(Exception):
    """jacked-dcr.json exists but cannot be safely read, or was written corrupt.

    Raised instead of silently falling back to ``DEFAULTS`` so a read-modify-write
    never overwrites a config file we failed to parse.
    """


class DcrSettingsAccessError(PublicError):
    """The config could not be WRITTEN or DELETED (a filesystem refusal).

    Distinct from ``DcrSettingsUnreadableError``, which means "the bytes are
    there but unusable". This one means the operating system said no --
    permissions, a read-only volume, a full disk. Raised instead of leaking a
    bare ``PermissionError`` so both surfaces can turn it into a friendly
    failure (CLI: ``[FAIL]`` + exit 1; API: 503) instead of a traceback.

    The message names the config path and the errno text. It does not contain
    the ``OSError`` text, so the dashboard can show it as it is (#289i).
    """


class DcrSettingsValueError(PublicError, ValueError):
    """A DCR config field has a value that jacked cannot run with.

    The message names the field and the value that the user gave, so the
    dashboard shows it as it is. It is a ``ValueError`` for older callers.
    """
