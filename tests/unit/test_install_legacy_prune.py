"""`jacked install` must prune hooks from features retired in 0.70.0.

The security gatekeeper (PreToolUse/PermissionRequest) and the session-
indexing Stop hook are gone; a stale settings.json entry would fire a
retired command on every tool call / session stop. The prune must operate
on the shared in-memory dict so later hook installers (session tracker,
QA suggest) can't write a stale copy back — that exact clobber shipped
once in a pre-release build of 0.70.0.
"""

import json

from click.testing import CliRunner


def _legacy_settings() -> dict:
    return {
        "hooks": {
            "PreToolUse": [
                {"matcher": "", "hooks": [{
                    "type": "command",
                    "command": '"/x/.local/bin/jacked" _hook security_gatekeeper',
                    "timeout": 30,
                }]},
                {"matcher": "Bash", "hooks": [{
                    "type": "command", "command": "/x/my-own-hook.sh",
                }]},
            ],
            "PermissionRequest": [
                {"matcher": "", "hooks": [{
                    "type": "command",
                    "command": '"/x/.local/bin/jacked" _hook security_gatekeeper',
                    "timeout": 10,
                }]},
            ],
            "Stop": [
                {"matcher": "", "hooks": [{
                    "type": "command",
                    "command": 'jacked index --repo "$CLAUDE_PROJECT_DIR"',
                    "async": True,
                }]},
            ],
        }
    }


def test_install_prunes_retired_hooks_but_keeps_user_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(_legacy_settings(), indent=2))
    (claude_dir / "gatekeeper-prompt.txt").write_text("custom prompt")

    from jacked.cli import main

    result = CliRunner().invoke(
        main, ["install", "--force", "--no-tray", "--no-codex", "--no-rules"]
    )
    assert result.exit_code == 0, result.output

    final = json.dumps(json.loads(settings_path.read_text()))
    assert "security_gatekeeper" not in final  # both events pruned, survives later writers
    assert "jacked index" not in final          # legacy Stop hook pruned
    assert "my-own-hook.sh" in final            # user hook untouched
    assert not (claude_dir / "gatekeeper-prompt.txt").exists()  # dead config removed
