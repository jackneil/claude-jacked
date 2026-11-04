"""The names of the GitHub issue queue labels (#96i): the one list.

The SessionStart hook ranks them (``jacked.data.hooks.issue_queue``), the
service fetches and creates them (``jacked.service.issue_queue_refresh``), and
the CLI accepts them (``jacked.issues_cli``). The list is in its own module
because the CLI must stay light to import and the hook starts no network code.
"""
from __future__ import annotations

#: In display order: the hook shows ``in-flight`` first and ``punted`` last.
QUEUE_LABEL_NAMES = ("in-flight", "next", "punted")
