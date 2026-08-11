"""Color theme must survive the browser <-> tray webview split.

The dashboard runs in the user's browser; the menu-bar dropdown and side panel
are WKWebViews created inside the jacked process. Those two have SEPARATE
localStorage stores, so a theme kept only in localStorage never reaches the
tray. The server setting 'color_theme' is therefore the source of truth and
localStorage is only a per-webview pre-paint cache.

These tests cover both halves: the settings API round-trip (plus a guard that
nobody protects the key out from under the generic PUT) and the browser JS,
driven through ``node`` the same way tests/unit/test_web_js_swap_ui.py does.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import jacked
from jacked.api.main import app
from jacked.api.routes.system import _PROTECTED_SETTING_KEYS
from jacked.web.database import Database

WEB = Path(jacked.__file__).resolve().parent / "data" / "web"
SETTINGS_JS = WEB / "js" / "components" / "settings.js"
PANEL_JS = WEB / "js" / "components" / "panel.js"


# ---------------------------------------------------------------------------
# API: the server is the source of truth
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """Throwaway file-backed DB wired onto app.state (see test_settings_remote).

    File-backed, not ``:memory:``: TestClient serves handlers on a worker thread
    and Database keeps a thread-local connection.
    """
    database = Database(str(tmp_path / "jacked.db"))
    prev = getattr(app.state, "db", None)
    app.state.db = database
    yield database
    if prev is not None:
        app.state.db = prev
    else:
        try:
            del app.state.db
        except AttributeError:
            pass


@pytest.fixture
def client(db):
    return TestClient(app)


def _settings_map(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    return {row["key"]: row["value"] for row in resp.json()}


def test_color_theme_absent_on_fresh_db(client):
    """No stored value means 'the server has no opinion' — that null is what
    triggers the one-time migration of an existing localStorage choice."""
    assert "color_theme" not in _settings_map(client)


@pytest.mark.parametrize("theme", ["classic", "america250"])
def test_color_theme_round_trips_through_settings_api(client, theme):
    resp = client.put("/api/settings/color_theme", json={"value": theme})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"key": "color_theme", "value": theme, "updated": True}
    assert _settings_map(client)["color_theme"] == theme


def test_color_theme_overwrite_wins(client):
    client.put("/api/settings/color_theme", json={"value": "america250"})
    client.put("/api/settings/color_theme", json={"value": "classic"})
    assert _settings_map(client)["color_theme"] == "classic"


def test_color_theme_is_not_a_protected_key():
    """Regression guard: protecting the key would make the generic PUT 422 and
    silently desync the tray again."""
    assert "color_theme" not in _PROTECTED_SETTING_KEYS


# ---------------------------------------------------------------------------
# Browser JS (node harness)
# ---------------------------------------------------------------------------

pytest_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)

# Minimal DOM/window/api stubs, then a sloppy-mode eval of the target file so
# its function declarations leak into module scope for the appended snippet to
# call. eval is safe here: it only runs first-party source checked into this
# repo. classList.toggle honors the two-arg force form, which the theme code
# relies on.
_HARNESS = r"""
const fs = require('fs');
const TARGET = __TARGET__;

const _localStore = __LOCALSTORE__;
const _classes = new Set(__CLASSES__);
const _toasts = [];
const _calls = [];

const documentElement = {
    classList: {
        add: (c) => _classes.add(c),
        remove: (c) => _classes.delete(c),
        contains: (c) => _classes.has(c),
        toggle: (c, force) => {
            if (force === undefined) {
                if (_classes.has(c)) { _classes.delete(c); return false; }
                _classes.add(c); return true;
            }
            if (force) { _classes.add(c); } else { _classes.delete(c); }
            return !!force;
        },
    },
};

global.window = { jackedState: {} };
global.localStorage = {
    getItem: (k) => Object.prototype.hasOwnProperty.call(_localStore, k) ? _localStore[k] : null,
    setItem: (k, v) => { _localStore[k] = String(v); },
    removeItem: (k) => { delete _localStore[k]; },
};
global.document = {
    documentElement,
    addEventListener: () => {},
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({ style: {}, classList: { add: () => {} } }),
};
global.escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.showToast = (message, type) => _toasts.push({ message, type });

// GET/PUT responses are driven per test through __responses.
global.__responses = { get: null, put: null };
global.api = {
    get: async (path) => {
        _calls.push({ method: 'GET', path });
        const r = global.__responses.get;
        if (r && r.throws) throw new Error(r.throws);
        return r ? r.body : null;
    },
    put: async (path, body) => {
        _calls.push({ method: 'PUT', path, body });
        const r = global.__responses.put;
        if (r && r.throws) throw new Error(r.throws);
        return { ok: true };
    },
};
global.fetch = async (path) => {
    _calls.push({ method: 'FETCH', path });
    const r = (global.__fetchResponses || {})[path];
    if (r === undefined) return { ok: false, status: 404, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => r };
};

global.__calls = _calls;
global.__toasts = _toasts;
global.__classes = _classes;
global.__store = _localStore;
const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');

eval(fs.readFileSync(TARGET, 'utf8'));
"""


def _run_js(tmp_path, snippet, js_file=SETTINGS_JS, store=None, classes=None):
    program = (
        _HARNESS.replace("__TARGET__", json.dumps(str(js_file)))
        .replace("__LOCALSTORE__", json.dumps(store or {}))
        .replace("__CLASSES__", json.dumps(classes or []))
        + "\n"
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


def _rows(**kwargs):
    """A GET /api/settings payload (list of {key, value} rows)."""
    return [{"key": k, "value": v} for k, v in kwargs.items()]


@pytest_node
@pytest.mark.parametrize("js_file", [SETTINGS_JS, PANEL_JS], ids=lambda p: p.name)
def test_node_syntax_check(js_file):
    proc = subprocess.run(
        ["node", "--check", str(js_file)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


# --- resolver ---


@pytest_node
def test_resolver_prefers_server_over_localstorage(tmp_path):
    """The whole point of the fix: a stale cache must lose to the server."""
    got = _run_js(
        tmp_path,
        "out({"
        "  serverClassic: resolveColorTheme(%s, 'america250'),"
        "  serverA250: resolveColorTheme(%s, 'classic'),"
        "  noServer: resolveColorTheme([], 'classic'),"
        "  noServerNoCache: resolveColorTheme([], null),"
        "  junkServer: resolveColorTheme(%s, 'classic'),"
        "});"
        % (
            json.dumps(_rows(color_theme="classic")),
            json.dumps(_rows(color_theme="america250")),
            json.dumps(_rows(color_theme="chartreuse")),
        ),
    )
    assert got == {
        "serverClassic": "classic",
        "serverA250": "america250",
        "noServer": "classic",
        "noServerNoCache": "america250",
        "junkServer": "classic",
    }


@pytest_node
def test_color_theme_from_settings_reads_rows_maps_and_quoted_values(tmp_path):
    got = _run_js(
        tmp_path,
        "out({"
        "  rows: colorThemeFromSettings(%s),"
        "  map: colorThemeFromSettings({color_theme: 'classic'}),"
        "  quoted: colorThemeFromSettings(%s),"
        "  missing: colorThemeFromSettings(%s),"
        "  empty: colorThemeFromSettings(null),"
        "});"
        % (
            json.dumps(_rows(color_theme="classic")),
            json.dumps([{"key": "color_theme", "value": '"classic"'}]),
            json.dumps(_rows(other="x")),
        ),
    )
    assert got == {
        "rows": "classic",
        "map": "classic",
        "quoted": "classic",
        "missing": None,
        "empty": None,
    }


# --- load-time reconcile + one-time migration ---


@pytest_node
def test_sync_applies_server_value_over_wrong_prepaint_class(tmp_path):
    """Pre-paint said America 250 (from a stale cache); the server says classic."""
    got = _run_js(
        tmp_path,
        "global.__responses.get = { body: %s };\n"
        "syncColorThemeFromServer().then((t) => out({"
        "  applied: t,"
        "  isA250: __classes.has('theme-america250'),"
        "  cache: __store['jacked_color_theme'],"
        "  puts: __calls.filter(c => c.method === 'PUT'),"
        "}));" % json.dumps(_rows(color_theme="classic")),
        store={"jacked_color_theme": "america250"},
        classes=["theme-america250"],
    )
    assert got["applied"] == "classic"
    assert got["isA250"] is False
    assert got["cache"] == "classic"
    assert got["puts"] == []  # server already knew — nothing to migrate


@pytest_node
def test_sync_migrates_localstorage_to_server_when_server_is_empty(tmp_path):
    got = _run_js(
        tmp_path,
        "global.__responses.get = { body: [] };\n"
        "syncColorThemeFromServer().then((t) => out({"
        "  applied: t,"
        "  puts: __calls.filter(c => c.method === 'PUT'),"
        "}));",
        store={"jacked_color_theme": "classic"},
    )
    assert got["applied"] == "classic"
    assert got["puts"] == [
        {"method": "PUT", "path": "/api/settings/color_theme", "body": {"value": "classic"}}
    ]


@pytest_node
def test_sync_migration_runs_once_and_cannot_loop(tmp_path):
    """A second call (poll, re-render, route change) must be a no-op."""
    got = _run_js(
        tmp_path,
        "global.__responses.get = { body: [] };\n"
        "(async () => {"
        "  const first = await syncColorThemeFromServer();"
        "  const second = await syncColorThemeFromServer();"
        "  const third = await syncColorThemeFromServer();"
        "  out({ first, second, third, calls: __calls.length,"
        "        puts: __calls.filter(c => c.method === 'PUT').length });"
        "})();",
        store={"jacked_color_theme": "classic"},
    )
    assert got["first"] == "classic"
    assert got["second"] is None and got["third"] is None
    assert got["puts"] == 1
    assert got["calls"] == 2  # exactly one GET + one PUT, ever


@pytest_node
def test_sync_pushes_nothing_when_neither_side_has_a_value(tmp_path):
    got = _run_js(
        tmp_path,
        "global.__responses.get = { body: [] };\n"
        "syncColorThemeFromServer().then((t) => out({"
        "  applied: t, puts: __calls.filter(c => c.method === 'PUT').length,"
        "}));",
    )
    assert got == {"applied": None, "puts": 0}


@pytest_node
def test_sync_keeps_cached_theme_when_server_is_unreachable(tmp_path):
    got = _run_js(
        tmp_path,
        "global.__responses.get = { throws: 'Network error' };\n"
        "syncColorThemeFromServer().then((t) => out({"
        "  applied: t, isA250: __classes.has('theme-america250'),"
        "  puts: __calls.filter(c => c.method === 'PUT').length,"
        "}));",
        store={"jacked_color_theme": "america250"},
        classes=["theme-america250"],
    )
    assert got == {"applied": None, "isA250": True, "puts": 0}


# --- picking a theme in the dashboard ---


_CLICK_HARNESS = """
const buttons = [
    { dataset: { theme: 'america250' }, addEventListener: (_e, cb) => { buttons[0]._cb = cb; } },
    { dataset: { theme: 'classic' }, addEventListener: (_e, cb) => { buttons[1]._cb = cb; } },
];
const container = {
    innerHTML: '',
    querySelectorAll: () => buttons,
};
renderAppearanceTab(container);
"""


@pytest_node
def test_picking_a_theme_paints_caches_and_persists_to_server(tmp_path):
    got = _run_js(
        tmp_path,
        _CLICK_HARNESS
        + "buttons[1]._cb().then(() => out({"
        "  isA250: __classes.has('theme-america250'),"
        "  cache: __store['jacked_color_theme'],"
        "  puts: __calls.filter(c => c.method === 'PUT'),"
        "  toasts: __toasts,"
        "  activeBadge: /Classic[\\s\\S]*?Active/.test(container.innerHTML),"
        "}));",
        classes=["theme-america250"],
    )
    assert got["isA250"] is False
    assert got["cache"] == "classic"
    assert got["puts"] == [
        {"method": "PUT", "path": "/api/settings/color_theme", "body": {"value": "classic"}}
    ]
    assert got["toasts"] == [{"message": "Classic theme applied", "type": "success"}]
    # The re-render must show the freshly picked option as active, not the stale one.
    assert got["activeBadge"] is True


@pytest_node
def test_user_pick_beats_a_slow_reconcile_still_in_flight(tmp_path):
    """The reconcile GET must never revert a choice made while it was running.

    The exact interleaving: the server holds 'america250' and the GET is slow
    (busy DB; the api client allows up to 60s). Mid-flight the user clicks
    Classic — the class flips, localStorage caches 'classic', the picker marks
    Classic Active and a PUT starts. When the stale GET finally lands it must
    NOT re-apply 'america250', or the browser paints america250 while the server
    (which the PUT wrote) holds 'classic', the picker still says Classic, and
    the three disagree until a reload.
    """
    got = _run_js(
        tmp_path,
        # A GET that hangs until the test releases it.
        (
            "let releaseGet;\n"
            "const gate = new Promise((r) => { releaseGet = r; });\n"
            "global.api.get = async (path) => {\n"
            "  __calls.push({ method: 'GET', path });\n"
            "  await gate;\n"
            "  return %s;\n"
            "};\n"
        ) % json.dumps(_rows(color_theme="america250"))
        + _CLICK_HARNESS
        + "(async () => {\n"
        "  const reconcile = syncColorThemeFromServer();   // t=0: GET issued, hangs\n"
        "  await buttons[1]._cb();                         // t=1s: user picks Classic\n"
        "  const midPick = { isA250: __classes.has('theme-america250'),"
        "                    cache: __store['jacked_color_theme'] };\n"
        "  releaseGet();                                   // t=2s: the stale GET lands\n"
        "  const applied = await reconcile;\n"
        "  out({ midPick, applied,"
        "        isA250: __classes.has('theme-america250'),"
        "        cache: __store['jacked_color_theme'],"
        '        classicPressed: /data-theme="classic" aria-pressed="true"/.test(container.innerHTML),'
        '        a250Pressed: /data-theme="america250" aria-pressed="true"/.test(container.innerHTML),'
        "        activeBadges: (container.innerHTML.match(/badge-primary/g) || []).length,"
        "        puts: __calls.filter(c => c.method === 'PUT') });\n"
        "})();",
        store={"jacked_color_theme": "america250"},
        classes=["theme-america250"],
    )
    # The click applied Classic...
    assert got["midPick"] == {"isA250": False, "cache": "classic"}
    # ...and the stale reconcile left it alone.
    assert got["applied"] is None, "the reconcile must skip once the user has picked"
    assert got["isA250"] is False, "painted class must still be Classic"
    assert got["cache"] == "classic", "localStorage cache must still be Classic"
    assert got["classicPressed"] is True, "the picker must still mark Classic as Active"
    assert got["a250Pressed"] is False
    assert got["activeBadges"] == 1, "exactly one option carries the Active badge"
    # The server ends up holding exactly what the user picked.
    assert got["puts"] == [
        {"method": "PUT", "path": "/api/settings/color_theme", "body": {"value": "classic"}}
    ]


# --- reconcile re-renders the surfaces that bake theme classes into HTML ---


# usageTextClass() picks its Tailwind class at RENDER time from the html class,
# so flipping that class restyles the bars via CSS but leaves the percent labels
# in the old palette until something re-renders. These stubs record whether the
# reconcile forced that rebuild.
_REPAINT_HARNESS = """
let rerenders = 0;
global.rerenderAccountsView = () => { rerenders++; };
let appearanceRenders = 0;
let _html = '';
const container = {
    set innerHTML(v) { appearanceRenders++; _html = v; },
    get innerHTML() { return _html; },
    querySelectorAll: () => [],
};
// The accounts view is mounted (a repaint only fires when it really is on
// screen) and Appearance is the tab showing.
const accountsList = { id: 'accounts-list' };
global.document.getElementById = (id) => {
    if (id === 'settings-tab-content') return container;
    if (id === 'accounts-list') return accountsList;
    return null;
};
"""


# Same stubs, but with the accounts view NOT mounted.
_REPAINT_UNMOUNTED_HARNESS = _REPAINT_HARNESS.replace(
    "    if (id === 'accounts-list') return accountsList;\n", ""
)


@pytest_node
def test_reconcile_that_changes_the_theme_rerenders_baked_in_classes(tmp_path):
    got = _run_js(
        tmp_path,
        "global.__responses.get = { body: %s };\n" % json.dumps(_rows(color_theme="classic"))
        + _REPAINT_HARNESS
        + "syncColorThemeFromServer().then((applied) => out({"
        "  applied, rerenders, appearanceRenders,"
        '  classicPressed: /data-theme="classic" aria-pressed="true"/.test(container.innerHTML),'
        "}));",
        store={"jacked_color_theme": "america250", "jacked_settings_tab": "appearance"},
        classes=["theme-america250"],
    )
    assert got["applied"] == "classic"
    assert got["rerenders"] == 1, "the accounts view must rebuild its percent-label classes"
    assert got["appearanceRenders"] == 1, "the Appearance picker must move its ring + badge"
    assert got["classicPressed"] is True


@pytest_node
def test_reconcile_that_changes_nothing_does_not_rerender(tmp_path):
    """No pointless churn: re-rendering the accounts view on every page load
    would fight the user's scroll and expansion state for no visible gain."""
    got = _run_js(
        tmp_path,
        "global.__responses.get = { body: %s };\n" % json.dumps(_rows(color_theme="america250"))
        + _REPAINT_HARNESS
        + "syncColorThemeFromServer().then((applied) => out({"
        "  applied, rerenders, appearanceRenders,"
        "  isA250: __classes.has('theme-america250'),"
        "}));",
        store={"jacked_color_theme": "america250", "jacked_settings_tab": "appearance"},
        classes=["theme-america250"],
    )
    assert got["applied"] == "america250"
    assert got["isA250"] is True
    assert got["rerenders"] == 0
    assert got["appearanceRenders"] == 0


@pytest_node
def test_reconcile_skips_the_accounts_rebuild_when_that_view_is_not_mounted(tmp_path):
    """The reconcile resolves on DOMContentLoaded, possibly before app.js has
    rendered anything. Rebuilding an unmounted accounts view would only paint an
    empty list; that first render already uses the theme just applied."""
    got = _run_js(
        tmp_path,
        "global.__responses.get = { body: %s };\n" % json.dumps(_rows(color_theme="classic"))
        + _REPAINT_UNMOUNTED_HARNESS
        + "syncColorThemeFromServer().then((applied) => out({"
        "  applied, rerenders, appearanceRenders,"
        "}));",
        store={"jacked_color_theme": "america250", "jacked_settings_tab": "appearance"},
        classes=["theme-america250"],
    )
    assert got["applied"] == "classic"
    assert got["rerenders"] == 0, "nothing to rebuild when the accounts view is absent"
    assert got["appearanceRenders"] == 1, "the visible Appearance tab still repaints"


@pytest_node
def test_reconcile_repaint_survives_a_page_without_those_surfaces(tmp_path):
    """settings.js is also loaded standalone (and the accounts view may not be
    mounted); a missing rerenderAccountsView or container must not throw."""
    got = _run_js(
        tmp_path,
        "global.__responses.get = { body: %s };\n" % json.dumps(_rows(color_theme="classic"))
        + "syncColorThemeFromServer().then((applied) => out({"
        "  applied, isA250: __classes.has('theme-america250'),"
        "}), (e) => out({ threw: String(e && e.message || e) }));",
        store={"jacked_color_theme": "america250", "jacked_settings_tab": "appearance"},
        classes=["theme-america250"],
    )
    assert got == {"applied": "classic", "isA250": False}


@pytest_node
def test_failed_persist_keeps_local_choice_and_warns_about_the_tray(tmp_path):
    got = _run_js(
        tmp_path,
        "global.__responses.put = { throws: 'Database unavailable' };\n"
        + _CLICK_HARNESS
        + "buttons[1]._cb().then(() => out({"
        "  isA250: __classes.has('theme-america250'),"
        "  cache: __store['jacked_color_theme'],"
        "  toasts: __toasts,"
        "}));",
        classes=["theme-america250"],
    )
    assert got["isA250"] is False           # local choice still applies
    assert got["cache"] == "classic"
    assert got["toasts"][0]["type"] == "success"
    warning = got["toasts"][1]
    assert warning["type"] == "warning"
    assert "Database unavailable" in warning["message"]
    assert "menu-bar panel" in warning["message"]


# --- the tray panel ---


@pytest_node
def test_panel_reads_theme_from_settings_payload(tmp_path):
    got = _run_js(
        tmp_path,
        "out({"
        "  classic: panelColorThemeFromSettings(%s),"
        "  a250: panelColorThemeFromSettings(%s),"
        "  missing: panelColorThemeFromSettings(%s),"
        "  notAList: panelColorThemeFromSettings(null),"
        "});"
        % (
            json.dumps(_rows(color_theme="classic")),
            json.dumps(_rows(color_theme="america250")),
            json.dumps(_rows(other="x")),
        ),
        js_file=PANEL_JS,
    )
    assert got == {
        "classic": "classic",
        "a250": "america250",
        "missing": None,
        "notAList": None,
    }


@pytest_node
def test_panel_apply_repaints_and_refreshes_its_prepaint_cache(tmp_path):
    got = _run_js(
        tmp_path,
        "const changed = applyPanelColorTheme('classic');\n"
        "const again = applyPanelColorTheme('classic');\n"
        "out({ changed, again, isA250: __classes.has('theme-america250'),"
        "      cache: __store['jacked_color_theme'] });",
        js_file=PANEL_JS,
        classes=["theme-america250"],
    )
    assert got == {
        "changed": True,
        "again": False,
        "isA250": False,
        "cache": "classic",
    }


@pytest_node
def test_panel_refresh_applies_server_theme_before_rendering(tmp_path):
    """The theme must land BEFORE the render, or usage.js emits percent-label
    classes for the old theme (it reads the html class at render time)."""
    got = _run_js(
        tmp_path,
        "global.groupAccountsByLogin = () => [];\n"
        "global.__fetchResponses = {"
        "  '/api/auth/accounts': [],"
        "  '/api/menubar-summary': {},"
        "  '/api/settings': %s,"
        "};\n"
        "let themeAtRender = null;\n"
        "const container = { set innerHTML(v) { themeAtRender = __classes.has('theme-america250'); },"
        "                    get innerHTML() { return ''; } };\n"
        "loadPanel(container).then(() => out({"
        "  themeAtRender,"
        "  isA250: __classes.has('theme-america250'),"
        "  cache: __store['jacked_color_theme'],"
        "  fetched: __calls.filter(c => c.method === 'FETCH').map(c => c.path).sort(),"
        "}));" % json.dumps(_rows(color_theme="classic")),
        js_file=PANEL_JS,
        classes=["theme-america250"],
        store={"jacked_color_theme": "america250"},
    )
    assert got["themeAtRender"] is False   # repainted before the HTML was built
    assert got["isA250"] is False
    assert got["cache"] == "classic"
    assert got["fetched"] == [
        "/api/auth/accounts", "/api/menubar-summary", "/api/settings",
    ]


# ---------------------------------------------------------------------------
# Source guards
# ---------------------------------------------------------------------------


def test_panel_html_keeps_the_prepaint_snippet():
    """The cache read must stay in <head>: it is what prevents a flash of the
    wrong scheme before panel.js's server read lands."""
    html = (WEB / "panel.html").read_text(encoding="utf-8")
    head = html.split("</head>")[0]
    assert "jacked_color_theme" in head
    assert "theme-america250" in head
    assert "classList.add('theme-america250')" in head


def test_index_html_keeps_the_prepaint_snippet():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    head = html.split("</head>")[0]
    assert "jacked_color_theme" in head
    assert "classList.add('theme-america250')" in head


def test_settings_js_uses_the_server_key():
    js = SETTINGS_JS.read_text(encoding="utf-8")
    assert "'color_theme'" in js
    assert "/api/settings/" in js


def test_panel_js_reads_settings_on_refresh():
    js = PANEL_JS.read_text(encoding="utf-8")
    assert "'/api/settings'" in js
    assert "color_theme" in js
