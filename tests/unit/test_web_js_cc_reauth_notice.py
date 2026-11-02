"""The dashboard tells the user what a CC_REAUTH_REQUIRED refusal means (#149i).

The switch route answers the refusal with HTTP 422 and the body now carries
`code`. The dashboard must act on the code (a notice that names the fix:
authorize the Claude Code token again), not only echo the server sentence.
After a sign-in, an activation the engine refused must not hide behind a
success banner. The copy under test is the copy that ships.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

COMPONENTS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js" / "components"
ACTIONS_JS = COMPONENTS / "account-actions.js"
OAUTH_JS = COMPONENTS / "oauth-flows.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("path", [ACTIONS_JS, OAUTH_JS], ids=lambda p: p.name)
def test_node_syntax_check(path):
    proc = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_switch_refusal_branch_keys_on_the_code():
    src = ACTIONS_JS.read_text(encoding="utf-8")
    assert "outcome.code === 'CC_REAUTH_REQUIRED'" in src
    notice = re.search(r"const CC_REAUTH_REQUIRED_NOTICE = '([^']+)';", src)
    assert notice, "the notice constant is missing"
    text = notice.group(1)
    assert "Authorize the Claude Code token for this account again" in text
    assert "—" not in text
    # The branch comes before the generic fallback, so the fallback never wins.
    assert src.index("outcome.code === 'CC_REAUTH_REQUIRED'") < src.index(
        "'The switch could not be completed.'"
    )


def test_a_refused_activation_after_a_sign_in_is_noticed():
    src = OAUTH_JS.read_text(encoding="utf-8")
    assert "function noticeRefusedActivation(poll)" in src
    assert "noticeRefusedActivation(poll);" in src
    ok = re.search(r"const ACTIVATION_OK = \[([^\]]+)\];", src)
    assert ok, "the accepted-outcome list is missing"
    accepted = {item.strip().strip("'") for item in ok.group(1).split(",")}
    assert accepted == {"committed", "committed_degraded", "observed_target_unfenced", "local_only"}
    assert "The account was saved, but it was not made active" in src
