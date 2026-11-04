"""Account provider names, and the one test for "is this a Claude account".

An account row has a ``provider`` column: ``claude``, ``codex`` or ``grok``.
Only a Claude row holds Anthropic tokens. Codex and Grok rows hold a marker
in ``access_token``, and their CLI rotates its own refresh token. Code that
calls an Anthropic endpoint, or writes the Claude credential store, must ask
``is_claude`` and must not compare against one other provider name. A test
for ``== "codex"`` lets every later provider through.

>>> provider_of({"provider": "Grok"}), is_claude({"provider": "codex"})
('grok', False)
"""

from __future__ import annotations

from typing import Mapping, Optional

CLAUDE = "claude"
CODEX = "codex"
GROK = "grok"


def provider_of(account: Optional[Mapping]) -> str:
    """The provider name of ``account``, lower case. An empty value is Claude.

    >>> provider_of(None), provider_of({}), provider_of({"provider": " CODEX "})
    ('claude', 'claude', 'codex')
    """
    raw = (account or {}).get("provider") or CLAUDE
    return str(raw).strip().lower() or CLAUDE


def is_claude(account: Optional[Mapping]) -> bool:
    """True when ``account`` holds Anthropic tokens.

    >>> is_claude({}), is_claude({"provider": "claude"}), is_claude({"provider": "grok"})
    (True, True, False)
    """
    return provider_of(account) == CLAUDE


def launch_refusal(account: dict, with_email: bool = False) -> str:
    """The message that refuses to launch a non-Claude account as Claude Code.

    >>> launch_refusal({"id": 4, "provider": "codex"})
    'Account 4 is a Codex account. Launch it with: jacked codex launch 4'
    >>> launch_refusal({"id": 5, "provider": "grok", "email": "g@x.com"}, True)
    'Account 5 (g@x.com) is a grok account. jacked cannot launch it as Claude Code.'
    """
    provider = provider_of(account)
    who = f"Account {account.get('id')}"
    if with_email:
        who += f" ({account.get('email')})"
    if provider == "codex":
        return f"{who} is a Codex account. Launch it with: jacked codex launch {account.get('id')}"
    return f"{who} is a {provider} account. jacked cannot launch it as Claude Code."
