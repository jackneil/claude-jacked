"""update.html polling lifecycle, run under node with a tiny fake DOM (#178i).

The skill packs refresh runs after the new service answers with the target
version. The page must keep polling until that phase closes, or a packs
warning never reaches the banner. Skipped when node is not on PATH.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import jacked

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

UPDATE_HTML = Path(jacked.__file__).resolve().parent / "data" / "web" / "update.html"

HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const [scriptPath, stepsPath] = process.argv.slice(2);
const steps = JSON.parse(fs.readFileSync(stepsPath, "utf8"));

function makeNode(tag) {
  const node = {
    tag, children: [], className: "", style: {}, _text: "",
    classList: { add(c) { node.className = (node.className + " " + c).trim(); } },
    appendChild(child) { node.children.push(child); return child; },
    removeChild(child) { node.children.splice(node.children.indexOf(child), 1); },
    get firstChild() { return node.children[0] || null; },
    set textContent(v) { node._text = String(v); node.children = []; },
    get textContent() { return node._text + node.children.map(c => c.textContent).join(""); },
  };
  return node;
}
const ids = {};
for (const id of ["heading", "meta", "phases", "banner"]) ids[id] = makeNode("div");
let step = 0;
const cleared = [];
const context = {
  document: {
    getElementById: id => ids[id] || (ids[id] = makeNode("div")),
    createElement: tag => makeNode(tag),
    createTextNode: text => { const n = makeNode("#text"); n._text = String(text); return n; },
  },
  fetch: async url => {
    const s = steps[Math.min(step, steps.length - 1)];
    const body = url.includes("/api/update/status")
      ? {status: s.status, mtime_iso: s.mtime_iso || new Date().toISOString(),
         updater_alive: s.updater_alive === undefined ? null : s.updater_alive}
      : {current: s.version};
    return {ok: true, json: async () => body};
  },
  setInterval: () => 1,
  clearInterval: h => cleared.push(h),
  window: { parent: { postMessage() {} } },
  console, Date, Promise, Number, JSON, Set, Boolean, String,
};
context.window.parent = { postMessage() {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context);
(async () => {
  const out = [];
  for (step = 0; step < steps.length; step++) {
    await context.tick();
    out.push({
      heading: ids.heading.textContent,
      banner: ids.banner.style.display === "block" ? ids.banner.textContent : "",
      polling: cleared.length === 0,
    });
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _phases(*pairs):
    return [{"name": n, "status": s} for n, s in pairs]


def _run(tmp_path, steps):
    html = UPDATE_HTML.read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)
    # The page starts polling on load; the harness drives tick() itself.
    script = script.replace("tick();\n    pollHandle = setInterval(tick, 1000);",
                            "pollHandle = setInterval(tick, 1000);")
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    (tmp_path / "steps.json").write_text(json.dumps(steps), encoding="utf-8")
    (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(tmp_path / "harness.js"), str(tmp_path / "page.js"),
         str(tmp_path / "steps.json")],
        # node writes UTF-8; Windows' default code page would mangle the
        # heading's ellipsis.
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


BASE = {"from_version": "0.108.1", "to_version": "0.109.0", "method": "uv"}
UPDATING = "Jacked is updating\u2026"


def test_the_page_waits_through_the_restart_and_packs_then_shows_the_warning(tmp_path):
    """The new service answers with the target version while the record is
    still verifying it, then refreshing packs; only the record's own end
    finishes the page."""
    verifying = dict(BASE, overall="in_progress", current_phase="verifying_service",
                     phases=_phases(("verifying_service", "in_progress")))
    running = dict(BASE, overall="in_progress", current_phase="refreshing_packs",
                   phases=_phases(("verifying_service", "ok"), ("refreshing_packs", "in_progress")))
    warned = dict(BASE, overall="succeeded", current_phase=None,
                  phases=_phases(("verifying_service", "ok"), ("refreshing_packs", "warning")))
    out = _run(tmp_path, [
        {"status": verifying, "version": "0.109.0"},  # new service answers early
        {"status": running, "version": "0.109.0"},
        {"status": warned, "version": "0.109.0"},
    ])
    for step in out[:2]:
        assert step == {"heading": UPDATING, "banner": "", "polling": True}
    assert out[2]["heading"] == "Jacked is updated"
    assert "The skill packs were not refreshed." in out[2]["banner"]
    assert "jacked packs update" in out[2]["banner"]
    assert out[2]["polling"] is False


def test_a_stale_record_with_the_new_version_still_finishes(tmp_path):
    """An updater that died after the restart never closes its record; once
    the record stops changing, the version match finishes the page."""
    stale = dict(BASE, overall="in_progress", current_phase="verifying_service",
                 phases=_phases(("verifying_service", "in_progress")))
    out = _run(tmp_path, [
        {"status": stale, "version": "0.109.0", "mtime_iso": "2020-01-01T00:00:00Z"},
    ])
    assert out[0]["heading"] == "Jacked is updated"
    assert out[0]["polling"] is False


def test_a_long_pack_refresh_by_a_live_updater_keeps_the_page_polling(tmp_path):
    """The pack refresh may run 15 minutes without touching its record. While
    the server reports the updater alive, an old record is not abandoned:
    the page must not report the update finished and stop polling."""
    running = dict(BASE, overall="in_progress", current_phase="refreshing_packs",
                   phases=_phases(("verifying_service", "ok"), ("refreshing_packs", "in_progress")))
    long_ago = "2020-01-01T00:00:00Z"
    out = _run(tmp_path, [
        {"status": running, "version": "0.109.0", "mtime_iso": long_ago, "updater_alive": True},
    ])
    assert out[0] == {"heading": UPDATING, "banner": "", "polling": True}

    gone = _run(tmp_path, [
        {"status": running, "version": "0.109.0", "mtime_iso": long_ago, "updater_alive": False},
    ])
    assert gone[0]["heading"] == "Jacked is updated" and gone[0]["polling"] is False


def test_liveness_is_cleared_when_the_record_disappears(tmp_path):
    """The record was live, then the updater died and the API hid the
    abandoned record (status null). The page must drop the old liveness and
    finish, not stay suppressed forever."""
    running = dict(BASE, overall="in_progress", current_phase="refreshing_packs",
                   phases=_phases(("verifying_service", "ok"), ("refreshing_packs", "in_progress")))
    long_ago = "2020-01-01T00:00:00Z"
    out = _run(tmp_path, [
        {"status": running, "version": "0.109.0", "mtime_iso": long_ago, "updater_alive": True},
        {"status": None, "version": "0.109.0", "updater_alive": None},
    ])
    assert out[0]["polling"] is True
    assert out[1]["heading"] == "Jacked is updated" and out[1]["polling"] is False


def test_a_failed_update_says_so_in_the_heading(tmp_path):
    failed = {"from_version": "0.108.1", "to_version": "0.109.0", "method": "uv",
              "overall": "failed", "current_phase": None, "error": "uv exited 2",
              "phases": _phases(("installing_package", "failed"))}
    out = _run(tmp_path, [{"status": failed, "version": "0.108.1"}])
    assert out[0]["heading"] == "Jacked update failed"
    assert "Error: uv exited 2" in out[0]["banner"]
    assert out[0]["polling"] is False
