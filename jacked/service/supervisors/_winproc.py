"""``system_program`` for the supervisors package, safe in an interpreter mid-upgrade.

``system_program`` is new in 0.111.0. A process that runs an upgrade from an
older release holds that release's ``jacked.winproc`` without it, and
imports this package for the first time after the install replaced the
package on disk (the 0.110.2 CLI upgrade died on exactly that import). The
package and its submodules take the name from here, so the held module is
reloaded once, before any of them needs it (see jacked.mixed_generation).
"""

from __future__ import annotations

from jacked.mixed_generation import names_from

(system_program,) = names_from("jacked.winproc", "system_program")

__all__ = ["system_program"]
