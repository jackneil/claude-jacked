"""Tests for the shared account-grouping util (groupAccountsByLogin).

account-grouping.js is a pure, dependency-free CommonJS-exporting module, so
these tests `require()` it directly under node and assert on the grouped output.
Skipped when node is not on PATH. Covers the two cases the feature hinges on —
a single-org login and a shared-email/multi-org login — plus sorting, the
active-org marker, the personal-sentinel label, and defensive inputs.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

GROUPING_JS = (
    Path(__file__).resolve().parents[2]
    / "jacked" / "data" / "web" / "js" / "util" / "account-grouping.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _run(tmp_path, snippet):
    program = (
        f"const {{ groupAccountsByLogin, orgLabel }} = require({json.dumps(str(GROUPING_JS))});\n"
        "const out = (o) => process.stdout.write('\\n' + JSON.stringify(o) + '\\n');\n"
        + snippet
    )
    script = tmp_path / "harness.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_node_syntax_check():
    proc = subprocess.run(
        ["node", "--check", str(GROUPING_JS)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_single_org_login(tmp_path):
    """One account → one group with a single org sub-row."""
    result = _run(tmp_path, """
const groups = groupAccountsByLogin([
    { id: 1, email: 'solo@x.com', organization_uuid: '', priority: 0 },
], null);
out(groups.map(g => ({ email: g.email, orgCount: g.orgCount, hasActive: g.hasActive,
                       orgs: g.orgs.map(o => ({ id: o.id, label: o.orgLabel, active: o.isActive })) })));
""")
    assert len(result) == 1
    g = result[0]
    assert g["email"] == "solo@x.com"
    assert g["orgCount"] == 1
    assert g["hasActive"] is False
    assert g["orgs"][0]["label"] == "Personal"


def test_shared_email_multi_org(tmp_path):
    """Same email, different orgs → ONE group with per-org sub-rows; active marked."""
    result = _run(tmp_path, """
const groups = groupAccountsByLogin([
    { id: 1, email: 'jack@x.com', organization_uuid: '', priority: 1 },
    { id: 2, email: 'jack@x.com', organization_uuid: 'org-123', organization_name: 'Acme', priority: 0 },
], 2);
out(groups.map(g => ({ email: g.email, orgCount: g.orgCount, hasActive: g.hasActive,
                       orgs: g.orgs.map(o => ({ id: o.id, label: o.orgLabel, active: o.isActive, priority: o.priority })) })));
""")
    assert len(result) == 1, "same email must collapse into one login group"
    g = result[0]
    assert g["email"] == "jack@x.com"
    assert g["orgCount"] == 2
    assert g["hasActive"] is True
    # Sorted by priority: org-123 (priority 0) first, Personal (priority 1) second
    assert g["orgs"][0]["id"] == 2
    assert g["orgs"][0]["label"] == "Acme"
    assert g["orgs"][0]["active"] is True
    assert g["orgs"][1]["label"] == "Personal"
    assert g["orgs"][1]["active"] is False


def test_distinct_emails_make_distinct_groups(tmp_path):
    result = _run(tmp_path, """
out(groupAccountsByLogin([
    { id: 1, email: 'a@x.com', organization_uuid: '', priority: 1 },
    { id: 2, email: 'b@x.com', organization_uuid: '', priority: 0 },
], null).map(g => g.email));
""")
    assert result == ["b@x.com", "a@x.com"], "groups sort by best (lowest) priority, then email"


def test_active_account_group_pinned_to_top(tmp_path):
    """The active account's login is ALWAYS first in the dropdown, even when
    another login has a lower (better) priority."""
    result = _run(tmp_path, """
out(groupAccountsByLogin([
    { id: 1, email: 'low@x.com', organization_uuid: '', priority: 0 },
    { id: 2, email: 'active@x.com', organization_uuid: '', priority: 5 },
], 2).map(g => g.email));
""")
    assert result[0] == "active@x.com", "active login pinned to top regardless of priority"
    assert result[1] == "low@x.com"


def test_email_case_insensitive_grouping(tmp_path):
    """Same email differing only in case is still one login."""
    result = _run(tmp_path, """
out(groupAccountsByLogin([
    { id: 1, email: 'Jack@X.com', organization_uuid: 'o1', organization_name: 'One', priority: 0 },
    { id: 2, email: 'jack@x.com', organization_uuid: 'o2', organization_name: 'Two', priority: 1 },
], null).length);
""")
    assert result == 1


def test_deleted_excluded_and_empty_inputs(tmp_path):
    result = _run(tmp_path, """
out({
    deleted: groupAccountsByLogin([{ id: 1, email: 'a@x.com', is_deleted: true }], null).length,
    nullInput: groupAccountsByLogin(null, null).length,
    emptyInput: groupAccountsByLogin([], null).length,
});
""")
    assert result == {"deleted": 0, "nullInput": 0, "emptyInput": 0}


def test_org_label_fallbacks(tmp_path):
    """orgLabel: name > truncated uuid > 'Personal'."""
    result = _run(tmp_path, """
out({
    named: orgLabel({ organization_name: 'Acme', organization_uuid: 'org-123456789' }),
    uuidOnly: orgLabel({ organization_uuid: 'org-123456789' }),
    personal: orgLabel({ organization_uuid: '' }),
});
""")
    assert result["named"] == "Acme"
    assert result["uuidOnly"].startswith("org-1234")
    assert result["personal"] == "Personal"


def test_org_label_collapses_anthropic_personal_org(tmp_path):
    """Anthropic's auto "<email>'s Organization" name is noise → 'Personal';
    a real org name is shown verbatim."""
    result = _run(tmp_path, """
out({
    auto: orgLabel({ organization_name: "jack@x.com's Organization", organization_uuid: 'o1' }),
    real: orgLabel({ organization_name: 'Hank.ai', organization_uuid: 'o2' }),
});
""")
    assert result["auto"] == "Personal"
    assert result["real"] == "Hank.ai"
