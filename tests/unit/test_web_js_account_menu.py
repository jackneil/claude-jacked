"""Account-card kebab (three-dot) overflow menu.

The rare actions (copy launch command, rename, re-auth, enable/disable,
delete) and the occasionally-useful metadata (account id, organization) moved
off the card face into an overflow menu. Two properties matter and are pinned
here:

* every menu row keeps the LEGACY button class and data-* attributes, so the
  handlers already bound in account-actions.js keep working untouched;
* the menu cannot get stuck open — a re-render drops the open reference, and
  the close listener runs in the CAPTURE phase so an item handler that calls
  stopPropagation() (.btn-edit-label does) cannot strand it.

The web UI is plain browser JS with no bundler or JS test harness, so these
tests drive ``node`` from pytest: each case evals the component source inside a
minimal DOM stub and asserts on rendered output and handler behavior. Skipped
when node is not on PATH.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web"
ACCOUNTS_JS = WEB / "js" / "components" / "accounts.js"
ACCOUNT_ACTIONS_JS = WEB / "js" / "components" / "account-actions.js"
APP_JS = WEB / "js" / "app.js"
STYLE_CSS = WEB / "css" / "style.css"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


# ---------------------------------------------------------------------------
# Render harness (accounts.js)
# ---------------------------------------------------------------------------

# Sloppy-mode eval of the component source so its function declarations leak
# into module scope where the appended snippet can call them. eval is safe
# here: it only executes first-party component source checked into this repo,
# never external or user-supplied input, and it is the only way to load
# non-module browser scripts for unit testing.
_RENDER_HARNESS = r"""
global.escapeHtml = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
global.TOKEN_EXPIRY_WARN_SECS = 3600;
global.window = { jackedState: { activeCredentialAccountId: null, swapSettings: {}, accounts: [] } };
// Sibling components the card calls into — not under test here.
global.renderTokenPills = () => '<span data-stub="pills"></span>';
global.providerBadge = (p) => '<span data-stub="badge">' + p + '</span>';
global.renderUsageBar = () => '<div data-stub="bar"></div>';
global.renderActiveSessions = () => '<div data-stub="sessions"></div>';
global.computeElapsedFraction5h = () => 0;
global.computeElapsedFraction7d = () => 0;
global.timeAgoFromUnix = () => '1m ago';
global.usageTextClass = () => 'text-green-400';
const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');
eval(require('fs').readFileSync(__TARGET__, 'utf8'));
"""


def _run_render(tmp_path, snippet):
    program = _RENDER_HARNESS.replace("__TARGET__", json.dumps(str(ACCOUNTS_JS))) + snippet
    script = tmp_path / "render.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def _actions(tmp_path, **acct):
    base = {"id": 1, "email": "a@x.com", "provider": "claude", "is_active": True,
            "validation_status": "valid"}
    base.update(acct)
    return _run_render(tmp_path, f"out({{ html: renderActionButtons({json.dumps(base)}) }});")["html"]


def _card(tmp_path, **acct):
    base = {"id": 1, "email": "a@x.com", "provider": "claude", "is_active": True,
            "validation_status": "valid", "priority": 0}
    base.update(acct)
    return _run_render(tmp_path, f"out({{ html: renderAccountCard({json.dumps(base)}, 0, 1) }});")["html"]


def _item(html, cls):
    """Extract the <button> carrying `cls`, or None."""
    if cls not in html:
        return None
    start = html.rindex("<button", 0, html.index(cls))
    return html[start:html.index("</button>", start)]


def _menu_panel(html):
    marker = '<div class="account-menu '
    assert marker in html, "every card renders an overflow menu panel"
    return html[html.index(marker):]


# ---------------------------------------------------------------------------
# Syntax
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("js_file", [ACCOUNTS_JS, ACCOUNT_ACTIONS_JS], ids=lambda p: p.name)
def test_node_syntax_check(js_file):
    proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class TestKebabButton:
    def test_renders_on_every_card_state(self, tmp_path):
        states = [
            {"id": 1, "provider": "claude", "is_active": True, "validation_status": "valid"},
            {"id": 2, "provider": "claude", "is_active": False, "validation_status": "invalid"},
            {"id": 3, "provider": "codex", "is_active": True, "validation_status": "valid"},
        ]
        for acct in states:
            html = _actions(tmp_path, **acct)
            btn = _item(html, "btn-account-menu")
            assert btn is not None, f"no kebab for {acct}"
            assert f'data-id="{acct["id"]}"' in btn
            assert 'aria-haspopup="menu"' in btn
            assert 'aria-expanded="false"' in btn
            assert 'aria-label="Account actions"' in btn
            assert 'title="More actions"' in btn
            # Stroke-based inline SVG in the existing card idiom, no emoji
            assert 'class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"' in btn

    def test_wrapper_is_positioned_and_panel_clears_sibling_cards(self, tmp_path):
        html = _actions(tmp_path)
        assert '<div class="account-menu-wrap relative">' in html
        panel = _menu_panel(html)
        assert 'role="menu"' in panel
        assert "hidden" in panel.split(">")[0], "panel starts hidden"
        assert "z-30" in panel.split(">")[0], "panel must clear sibling grid cards"
        assert "min-w-[13rem]" in panel

    def test_attention_dot_only_for_invalid_or_expired(self, tmp_path):
        dot = '<span class="account-menu-dot"'
        # Healthy / checking / disabled-but-valid: no dot
        assert dot not in _actions(tmp_path, validation_status="valid")
        assert dot not in _actions(tmp_path, validation_status="checking")
        assert dot not in _actions(tmp_path, is_active=False, validation_status="valid")
        # invalid
        assert dot in _actions(tmp_path, validation_status="invalid")
        # expired, non-refreshable token
        assert dot in _actions(
            tmp_path, validation_status="valid", is_expired=True, has_refresh_token=False,
        )

    def test_use_account_stays_on_the_card_face(self, tmp_path):
        html = _actions(tmp_path)
        before_menu = html[:html.index('<div class="account-menu-wrap')]
        assert "btn-use-account" in before_menu
        # Everything rare moved into the menu
        for cls in ("btn-toggle", "btn-delete", "btn-reauth", "btn-copy-cmd", "btn-edit-label"):
            assert cls not in before_menu, f"{cls} must not sit on the card face"


class TestMetadataHeader:
    def test_shows_account_id_and_org_name(self, tmp_path):
        html = _actions(tmp_path, id=7, organization_name="Acme Inc")
        panel = _menu_panel(html)
        assert 'role="presentation"' in panel
        assert "Account #7" in panel
        assert "Acme Inc" in panel
        # Header sits above the items, separated by a border
        assert panel.index("Account #7") < panel.index("btn-copy-cmd")
        assert "border-b border-slate-700" in panel

    def test_falls_back_to_truncated_org_uuid(self, tmp_path):
        html = _actions(tmp_path, organization_uuid="abcdef1234567890")
        panel = _menu_panel(html)
        assert "abcdef12…" in panel
        assert "abcdef1234567890" not in panel

    def test_org_line_omitted_when_unknown(self, tmp_path):
        panel = _menu_panel(_actions(tmp_path, id=4))
        assert "Account #4" in panel
        assert "text-[11px] text-slate-500" not in panel

    def test_org_name_is_escaped(self, tmp_path):
        panel = _menu_panel(_actions(tmp_path, organization_name='<img src=x>"'))
        assert "<img src=x>" not in panel
        assert "&lt;img src=x&gt;" in panel


class TestMenuItems:
    def test_claude_enabled_labeled_account(self, tmp_path):
        html = _actions(tmp_path, id=9, email="a@x.com", display_name="Work Max")
        panel = _menu_panel(html)
        order = [panel.index(c) for c in
                 ("btn-copy-cmd", "btn-edit-label", "btn-reauth", "btn-toggle", "btn-delete")]
        assert order == sorted(order), "menu order: copy, rename, re-auth, toggle, delete"
        assert "Copy launch command" in panel
        assert "Rename" in panel and "Add label" not in panel
        assert "Re-auth" in panel
        assert "Disable" in panel and ">Enable<" not in panel
        assert "Delete account" in panel

    def test_unlabeled_account_says_add_label(self, tmp_path):
        panel = _menu_panel(_actions(tmp_path, email="a@x.com", display_name="a@x.com"))
        assert "Add label" in panel
        assert ">Rename<" not in panel

    def test_disabled_account_offers_enable(self, tmp_path):
        panel = _menu_panel(_actions(tmp_path, is_active=False))
        assert "Enable" in panel and "Disable" not in panel
        toggle = _item(panel, "btn-toggle")
        assert 'data-active="false"' in toggle

    def test_codex_account_has_no_reauth_row(self, tmp_path):
        panel = _menu_panel(_actions(tmp_path, provider="codex"))
        assert "btn-reauth" not in panel
        # everything else is still there
        for cls in ("btn-copy-cmd", "btn-edit-label", "btn-toggle", "btn-delete"):
            assert cls in panel

    def test_items_carry_legacy_handler_classes_and_data(self, tmp_path):
        html = _actions(tmp_path, id=12, email="z@x.com", display_name="Personal")
        copy_item = _item(html, "btn-copy-cmd")
        assert 'data-cmd="jacked claude 12"' in copy_item
        rename = _item(html, "btn-edit-label")
        assert 'data-id="12"' in rename and 'data-label="Personal"' in rename
        reauth = _item(html, "btn-reauth")
        assert 'data-id="12"' in reauth and 'data-email="z@x.com"' in reauth
        toggle = _item(html, "btn-toggle")
        assert 'data-id="12"' in toggle and 'data-active="true"' in toggle
        delete = _item(html, "btn-delete")
        assert 'data-id="12"' in delete
        for item in (copy_item, rename, reauth, toggle, delete):
            assert 'role="menuitem"' in item
            assert "px-3 py-2 text-xs" in item
            assert 'stroke="currentColor" viewBox="0 0 24 24"' in item, "stroke icon, no emoji"

    def test_delete_row_is_red_and_last(self, tmp_path):
        panel = _menu_panel(_actions(tmp_path))
        delete = _item(panel, "btn-delete")
        assert "text-red-400 hover:text-red-300 hover:bg-red-900/30" in delete
        assert 'role="separator"' in panel
        assert panel.index('role="separator"') < panel.index("btn-delete")

    def test_no_ai_slop_tells(self, tmp_path):
        panel = _menu_panel(_actions(tmp_path, validation_status="invalid"))
        for banned in ("backdrop-blur", "border-l-4", "border-l-2", "gradient",
                       "purple", "violet", "shadow-purple", "uppercase"):
            assert banned not in panel, f"banned design tell in menu: {banned}"


class TestCardHeader:
    def test_pencil_removed_from_header_but_card_keeps_the_rest(self, tmp_path):
        html = _card(tmp_path, display_name="Work Max", organization_name="Acme")
        header = html[:html.index('<div class="account-menu-wrap')]
        assert "btn-edit-label" not in header, "Rename lives in the menu now"
        # Everything else in the header survives untouched
        assert "btn-refresh-single" in header
        assert 'data-stub="pills"' in header, "token pills"
        assert 'data-stub="badge">claude' in header, "provider badge"
        assert '<span class="status-dot' in header
        assert "Work Max" in header, "custom label is still the card title"
        assert "(Acme)" in header, "org still shown on the card body line"
    def test_card_renders_exactly_one_menu(self, tmp_path):
        html = _card(tmp_path)
        assert html.count("btn-account-menu") == 1
        assert html.count('class="account-menu ') == 1
        assert "delete-confirm-container" in html, "inline delete confirm target survives"


# ---------------------------------------------------------------------------
# Behavior harness (account-actions.js)
# ---------------------------------------------------------------------------

# A DOM stub with just enough of the real API for the menu code: class lists,
# closest()/querySelector(All) with tag/class/attribute selectors, focus
# tracking, and recorded listeners so the test can dispatch synthetic events
# through capture -> target -> bubble -> document, exactly as a browser would.
_BEHAVIOR_HARNESS = r"""
const fs = require('fs');
const TARGET = __TARGET__;

function makeEl(tag) {
    const el = {
        tagName: (tag || '').toUpperCase(),
        children: [],
        dataset: {},
        attributes: {},
        id: '',
        className: '',
        textContent: '',
        innerHTML: '',
        parentNode: null,
        style: {},
        disabled: false,
        _classes: new Set(),
        _listeners: {},
    };
    el.classList = {
        add: (...cs) => cs.forEach(c => el._classes.add(c)),
        remove: (...cs) => cs.forEach(c => el._classes.delete(c)),
        contains: (c) => el._classes.has(c),
        toggle: (c) => el._classes.has(c) ? el._classes.delete(c) : el._classes.add(c),
    };
    el.setAttribute = (k, v) => {
        el.attributes[k] = String(v);
        if (k === 'class') String(v).split(/\s+/).filter(Boolean).forEach(c => el._classes.add(c));
        if (k.indexOf('data-') === 0) {
            const key = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
            el.dataset[key] = String(v);
        }
    };
    el.getAttribute = (k) => (k in el.attributes ? el.attributes[k] : null);
    el.appendChild = (c) => { c.parentNode = el; el.children.push(c); return c; };
    el.prepend = (c) => { c.parentNode = el; el.children.unshift(c); return c; };
    el.remove = () => {
        if (el.parentNode) {
            const i = el.parentNode.children.indexOf(el);
            if (i >= 0) el.parentNode.children.splice(i, 1);
        }
        el.parentNode = null;
    };
    el.addEventListener = (type, cb, capture) => {
        const key = type + (capture ? ':capture' : ':bubble');
        (el._listeners[key] = el._listeners[key] || []).push(cb);
    };
    el.insertAdjacentHTML = () => {};
    el.focus = () => { global.document.activeElement = el; };
    el.matches = (sel) => matchesSelector(el, sel);
    el.closest = (sel) => {
        let node = el;
        while (node) {
            if (node.matches && node.matches(sel)) return node;
            node = node.parentNode;
        }
        return null;
    };
    el.contains = (other) => {
        let node = other;
        while (node) { if (node === el) return true; node = node.parentNode; }
        return false;
    };
    el.querySelectorAll = (sel) => collect(el, sel);
    el.querySelector = (sel) => collect(el, sel)[0] || null;
    return el;
}

// Supports "tag", ".cls", "tag.a.b", "[attr]", "[attr=\"val\"]" and comma lists.
function matchesSelector(el, sel) {
    return String(sel).split(',').some(raw => {
        const part = raw.trim();
        if (!part) return false;
        const tagMatch = part.match(/^[a-zA-Z][\w-]*/);
        const tag = tagMatch ? tagMatch[0] : null;
        if (tag && el.tagName !== tag.toUpperCase()) return false;
        const rest = tag ? part.slice(tag.length) : part;
        const tokens = rest.match(/\.[-\w]+|\[[^\]]+\]|#[-\w]+/g) || [];
        if (!tag && tokens.length === 0) return false;
        return tokens.every(tok => {
            if (tok[0] === '.') return el._classes.has(tok.slice(1));
            if (tok[0] === '#') return el.id === tok.slice(1);
            const m = tok.slice(1, -1).match(/^([-\w]+)(?:=["']?([^"'\]]*)["']?)?$/);
            if (!m) return false;
            const val = el.attributes[m[1]];
            if (val === undefined) return false;
            return m[2] === undefined || val === m[2];
        });
    });
}

function collect(root, sel) {
    const acc = [];
    (function walk(node) {
        for (const child of node.children || []) {
            if (child.matches && child.matches(sel)) acc.push(child);
            walk(child);
        }
    })(root);
    return acc;
}

function findById(root, id) {
    if (!root) return null;
    if (root.id === id) return root;
    for (const c of root.children || []) {
        const hit = findById(c, id);
        if (hit) return hit;
    }
    return null;
}

const body = makeEl('body');
const _toasts = [];
const _docListeners = {};
const _localStore = {};

global.window = { jackedState: {}, addEventListener: () => {} };
global.localStorage = {
    getItem: (k) => Object.prototype.hasOwnProperty.call(_localStore, k) ? _localStore[k] : null,
    setItem: (k, v) => { _localStore[k] = String(v); },
    removeItem: (k) => { delete _localStore[k]; },
};
global.setInterval = () => 1;
global.clearInterval = () => {};
global.document = {
    body,
    activeElement: null,
    createElement: makeEl,
    createTextNode: (t) => ({ nodeType: 3, textContent: String(t), parentNode: null }),
    getElementById: (id) => findById(body, id),
    querySelector: (sel) => collect(body, sel)[0] || null,
    querySelectorAll: (sel) => collect(body, sel),
    // The capture flag is recorded, not ignored: __click below dispatches
    // capture-phase document listeners BEFORE the target's own handlers, which
    // is the whole point of the outside-click fix.
    addEventListener: (type, cb, capture) => {
        (_docListeners[type] = _docListeners[type] || []).push({ cb, capture: !!capture });
    },
};
global.api = { get: async () => ({}), post: async () => ({}), patch: async () => ({}),
               delete: async () => ({}) };
global.escapeHtml = (s) => String(s == null ? '' : s);
global.showToast = (message, type) => _toasts.push({ message, type });
global.refreshAndRender = async () => {};
global.Swal = { fire: async () => ({ isConfirmed: false }) };
global.__toasts = _toasts;
global.__docListeners = _docListeners;
global.__makeEl = makeEl;
global.__body = body;

// Build a card: #accounts-list > card > [refresh-single, menu-wrap > kebab + menu]
global.__buildCard = function(id) {
    const card = makeEl('div');
    card.setAttribute('class', 'provider-card provider-claude card-hover');
    card.setAttribute('data-account-id', String(id));
    // The per-card refresh-usage icon sits on the card FACE, outside the menu,
    // and its handler calls e.stopPropagation(). It is the real-world outside
    // click that a bubble-phase close listener would never see.
    const refresh = makeEl('button');
    refresh.setAttribute('class', 'btn-refresh-single');
    refresh.setAttribute('data-id', String(id));
    card.appendChild(refresh);
    const wrap = makeEl('div');
    wrap.setAttribute('class', 'account-menu-wrap relative');
    const kebab = makeEl('button');
    kebab.setAttribute('class', 'btn-account-menu');
    kebab.setAttribute('data-id', String(id));
    kebab.setAttribute('aria-expanded', 'false');
    const menu = makeEl('div');
    menu.setAttribute('class', 'account-menu hidden');
    menu.setAttribute('role', 'menu');
    menu._classes.add('hidden');
    const header = makeEl('div');
    header.setAttribute('class', 'menu-header');
    header.setAttribute('role', 'presentation');
    menu.appendChild(header);
    const items = {};
    for (const cls of ['btn-copy-cmd', 'btn-edit-label', 'btn-reauth', 'btn-toggle', 'btn-delete']) {
        const item = makeEl('button');
        item.setAttribute('class', cls + ' account-menu-item');
        item.setAttribute('role', 'menuitem');
        item.setAttribute('data-id', String(id));
        item.setAttribute('data-active', 'true');
        item.setAttribute('data-cmd', 'jacked claude ' + id);
        menu.appendChild(item);
        items[cls] = item;
    }
    wrap.appendChild(kebab);
    wrap.appendChild(menu);
    card.appendChild(wrap);
    return { card, wrap, kebab, menu, header, items, refresh };
};

// Full browser order: capture walks DOWN from document to the list, the target's
// own listeners run, and only then does the event bubble back up. stopPropagation()
// in a target handler kills the bubble half — capture listeners have already run,
// which is exactly why the outside-click close is bound with capture=true.
global.__click = function(list, target) {
    const ev = { type: 'click', target, _stopped: false,
                 preventDefault() {}, stopPropagation() { ev._stopped = true; } };
    const docs = _docListeners['click'] || [];
    docs.filter(l => l.capture).forEach(l => l.cb(ev));
    (list._listeners['click:capture'] || []).forEach(cb => cb(ev));
    (target._listeners['click:bubble'] || []).forEach(cb => cb(ev));
    if (ev._stopped) return ev;
    (list._listeners['click:bubble'] || []).forEach(cb => cb(ev));
    docs.filter(l => !l.capture).forEach(l => l.cb(ev));
    return ev;
};

global.__keydown = function(list, target, key) {
    const ev = { type: 'keydown', key, target, defaultPrevented: false,
                 preventDefault() { ev.defaultPrevented = true; }, stopPropagation() {} };
    const docs = _docListeners['keydown'] || [];
    docs.filter(l => l.capture).forEach(l => l.cb(ev));
    (list._listeners['keydown:bubble'] || []).forEach(cb => cb(ev));
    docs.filter(l => !l.capture).forEach(l => l.cb(ev));
    return ev;
};

const out = (o) => process.stdout.write('\n' + JSON.stringify(o) + '\n');
eval(fs.readFileSync(TARGET, 'utf8'));
"""


def _run_behavior(tmp_path, snippet):
    program = (
        _BEHAVIOR_HARNESS.replace("__TARGET__", json.dumps(str(ACCOUNT_ACTIONS_JS)))
        + "\n" + snippet
    )
    script = tmp_path / "behavior.js"
    script.write_text(program, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert proc.returncode == 0, f"node failed:\nstderr={proc.stderr}\nstdout={proc.stdout}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1])


_TWO_CARDS = r"""
const list = __makeEl('div');
list.id = 'accounts-list';
__body.appendChild(list);
const a = __buildCard(1);
const b = __buildCard(2);
list.appendChild(a.card);
list.appendChild(b.card);
bindAccountEvents();
const isOpen = (c) => !c.menu.classList.contains('hidden');
"""

# Loads the REAL rerenderAccountsView from app.js on top of the behavior harness,
# so the open-menu save/restore is exercised end to end rather than re-implemented
# in the test. #content's innerHTML setter stands in for the browser's HTML parse:
# it throws the old cards away and mounts fresh ones for whatever accounts are in
# state, which is precisely what makes the open menu's node detached.
_RERENDER_HARNESS = r"""
eval(require('fs').readFileSync(__APP_JS__, 'utf8'));

const content = __makeEl('div');
content.id = 'content';
__body.appendChild(content);

let cards = {};
let list = null;
let renders = 0;

function mountCards() {
    renders++;
    content.children.length = 0;
    cards = {};
    list = __makeEl('div');
    list.id = 'accounts-list';
    content.appendChild(list);
    for (const acct of window.jackedState.accounts) {
        const c = __buildCard(acct.id);
        cards[acct.id] = c;
        list.appendChild(c.card);
    }
}
Object.defineProperty(content, 'innerHTML', {
    get: () => '',
    set: () => { mountCards(); },
});

global.renderAccounts = () => '<accounts/>';
window.jackedState.activeRoute = 'accounts';
window.jackedState.expandedRepoGroups = new Set();
window.jackedState.accounts = [{ id: 1 }, { id: 2 }];

mountCards();
bindAccountEvents();
const isOpen = (c) => !!c && !c.menu.classList.contains('hidden');
const anyOpen = () => Object.keys(cards).some(k => isOpen(cards[k]));
const anyRaised = () => Object.keys(cards).some(k => cards[k].card.classList.contains('menu-open'));
"""


def _run_rerender(tmp_path, snippet):
    return _run_behavior(
        tmp_path, _RERENDER_HARNESS.replace("__APP_JS__", json.dumps(str(APP_JS))) + snippet
    )


class TestMenuBehavior:
    def test_toggle_open_and_close(self, tmp_path):
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
__click(list, a.kebab);
const afterOpen = { open: isOpen(a), expanded: a.kebab.getAttribute('aria-expanded'),
                    cardRaised: a.card.classList.contains('menu-open') };
__click(list, a.kebab);
const afterClose = { open: isOpen(a), expanded: a.kebab.getAttribute('aria-expanded'),
                     cardRaised: a.card.classList.contains('menu-open') };
out({ afterOpen, afterClose });
""")
        assert result["afterOpen"] == {"open": True, "expanded": "true", "cardRaised": True}
        assert result["afterClose"] == {"open": False, "expanded": "false", "cardRaised": False}

    def test_opening_one_closes_the_other(self, tmp_path):
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
__click(list, a.kebab);
__click(list, b.kebab);
out({ aOpen: isOpen(a), bOpen: isOpen(b),
      aExpanded: a.kebab.getAttribute('aria-expanded'),
      bExpanded: b.kebab.getAttribute('aria-expanded'),
      aRaised: a.card.classList.contains('menu-open') });
""")
        assert result == {"aOpen": False, "bOpen": True, "aExpanded": "false",
                          "bExpanded": "true", "aRaised": False}

    def test_clicking_an_item_closes_but_still_runs_the_legacy_handler(self, tmp_path):
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
(async () => {
    __click(list, a.kebab);
    __click(list, a.items['btn-toggle']);   // real .btn-toggle handler is bound here
    await new Promise(r => setTimeout(r, 10));
    out({ open: isOpen(a), raised: a.card.classList.contains('menu-open'),
          toasts: __toasts.map(t => t.message) });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["open"] is False
        assert result["raised"] is False
        # The account-actions .btn-toggle handler ran untouched
        assert result["toasts"] == ["Account disabled"]

    def test_reauth_row_starts_the_targeted_reauth_flow(self, tmp_path):
        """The kebab Re-auth row must re-auth THIS account, like the token pill does.

        Regression: the row called startAddAccountFlow(), the untargeted
        add-account flow. With no account identity the browser launcher falls
        back to an incognito window (no login hint, no per-account profile)
        and the OAuth callback can create a duplicate row instead of updating
        the one the user clicked. The pill handler already routed through
        startReauthFlow(id, email); the menu row must do the same.
        """
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
(async () => {
    const calls = { reauth: [], add: [] };
    global.startReauthFlow = (id, email) => { calls.reauth.push([id, email]); };
    global.startAddAccountFlow = () => { calls.add.push(true); };
    global.Swal = { fire: async () => ({ isConfirmed: true }) };
    a.items['btn-reauth'].setAttribute('data-email', 'one@x.com');
    __click(list, a.kebab);
    __click(list, a.items['btn-reauth']);
    await new Promise(r => setTimeout(r, 10));
    out({ calls, open: isOpen(a) });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["calls"]["reauth"] == [["1", "one@x.com"]]
        assert result["calls"]["add"] == []
        assert result["open"] is False

    def test_reauth_row_cancel_starts_nothing(self, tmp_path):
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
(async () => {
    const calls = { reauth: [], add: [] };
    global.startReauthFlow = (id, email) => { calls.reauth.push([id, email]); };
    global.startAddAccountFlow = () => { calls.add.push(true); };
    global.Swal = { fire: async () => ({ isConfirmed: false }) };
    __click(list, a.kebab);
    __click(list, a.items['btn-reauth']);
    await new Promise(r => setTimeout(r, 10));
    out({ calls });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["calls"] == {"reauth": [], "add": []}

    def test_item_handler_stop_propagation_cannot_strand_the_menu(self, tmp_path):
        # .btn-edit-label calls e.stopPropagation(); the close listener runs in
        # the capture phase precisely so that cannot leave the menu open.
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
(async () => {
    __click(list, a.kebab);
    const ev = __click(list, a.items['btn-edit-label']);
    await new Promise(r => setTimeout(r, 10));
    out({ stopped: ev._stopped, open: isOpen(a) });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["stopped"] is True, "the legacy handler still stops propagation"
        assert result["open"] is False

    def test_clicking_inside_the_metadata_header_closes_it(self, tmp_path):
        """Any click inside the panel dismisses the menu, header included.

        The metadata header is non-interactive text, not a menu item, so this is
        worth stating out loud: dismiss-on-any-in-panel-click is the INTENDED
        contract, because that is what a native menu does. The list-level capture
        handler closes on `target.closest('.account-menu')` without checking
        whether the click landed on a `[role="menuitem"]`, so this pins that
        deliberate choice rather than an accident of the selector.
        """
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
__click(list, a.kebab);
__click(list, a.header);
out({ open: isOpen(a) });
""")
        assert result["open"] is False

    def test_outside_click_that_stops_propagation_still_closes(self, tmp_path):
        """The real-world outside click, not a bare <div>.

        test_outside_click_closes below clicks an element with no listeners of
        its own, so it passes with the close bound on either phase. The per-card
        ``.btn-refresh-single`` handler calls e.stopPropagation(), which kills the
        bubble half of the event: with a bubble-phase close listener, opening card
        A's menu and then clicking card B's refresh icon left the menu open AND
        card A carrying .menu-open (z-index 40) over its neighbour. The close
        listener is bound in the CAPTURE phase precisely so this cannot happen.
        """
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
(async () => {
    __click(list, a.kebab);
    const openBefore = isOpen(a);
    const ev = __click(list, b.refresh);      // other card's refresh-usage icon
    await new Promise(r => setTimeout(r, 10));
    out({ openBefore, stopped: ev._stopped, open: isOpen(a),
          raised: a.card.classList.contains('menu-open'),
          expanded: a.kebab.getAttribute('aria-expanded') });
    process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
""")
        assert result["openBefore"] is True
        assert result["stopped"] is True, "the refresh handler still stops propagation"
        assert result["open"] is False, "menu must close on an outside click that stops propagation"
        assert result["raised"] is False, "the stale card must not keep z-index 40"
        assert result["expanded"] == "false"

    def test_outside_click_closes(self, tmp_path):
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
__click(list, a.kebab);
const other = __makeEl('div');
__body.appendChild(other);
__click(list, other);
out({ open: isOpen(a), expanded: a.kebab.getAttribute('aria-expanded'),
      raised: a.card.classList.contains('menu-open') });
""")
        assert result == {"open": False, "expanded": "false", "raised": False}

    def test_escape_closes_and_returns_focus(self, tmp_path):
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
__click(list, a.kebab);
__keydown(list, a.items['btn-reauth'], 'Escape');
out({ open: isOpen(a), focused: document.activeElement === a.kebab,
      expanded: a.kebab.getAttribute('aria-expanded') });
""")
        assert result == {"open": False, "focused": True, "expanded": "false"}

    def test_arrow_keys_move_focus_and_wrap(self, tmp_path):
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
const order = ['btn-copy-cmd', 'btn-edit-label', 'btn-reauth', 'btn-toggle', 'btn-delete'];
const where = () => {
    const hit = order.find(c => a.items[c] === document.activeElement);
    return hit || null;
};
__click(list, a.kebab);
a.kebab.focus();
__keydown(list, a.kebab, 'ArrowDown');
const first = where();
__keydown(list, document.activeElement, 'ArrowDown');
const second = where();
__keydown(list, document.activeElement, 'ArrowUp');
const back = where();
__keydown(list, document.activeElement, 'ArrowUp');
const wrappedUp = where();
__keydown(list, document.activeElement, 'ArrowDown');
const wrappedDown = where();
const ev = __keydown(list, document.activeElement, 'ArrowDown');
out({ first, second, back, wrappedUp, wrappedDown, prevented: ev.defaultPrevented });
""")
        assert result["first"] == "btn-copy-cmd"
        assert result["second"] == "btn-edit-label"
        assert result["back"] == "btn-copy-cmd"
        assert result["wrappedUp"] == "btn-delete", "ArrowUp from the first item wraps to the last"
        assert result["wrappedDown"] == "btn-copy-cmd"
        assert result["prevented"] is True, "arrow keys must not scroll the page"

    def test_arrow_keys_ignored_when_no_menu_is_open(self, tmp_path):
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
__keydown(list, a.items['btn-copy-cmd'], 'ArrowDown');
out({ focused: document.activeElement === null });
""")
        assert result["focused"] is True

    def test_open_menu_survives_a_background_rerender(self, tmp_path):
        """A websocket sessions_changed event must not close the menu.

        rerenderAccountsView() already saves and restores every other piece of
        transient UI state across the innerHTML swap (expanded details, repo
        groups, the lookup input, the live OAuth banner). The open menu joins
        that list: a Claude Code session starting or stopping fires this render,
        and the menu used to vanish under the user's pointer with no action of
        their own.
        """
        result = _run_rerender(tmp_path, r"""
__click(list, cards[1].kebab);
const openBefore = isOpen(cards[1]);
const rendersBefore = renders;
const oldCard = cards[1];

rerenderAccountsView();          // the websocket-driven background re-render

out({ openBefore, rerendered: renders === rendersBefore + 1,
      freshNode: cards[1] !== oldCard,
      stillOpen: isOpen(cards[1]),
      raised: cards[1].card.classList.contains('menu-open'),
      expanded: cards[1].kebab.getAttribute('aria-expanded'),
      otherClosed: !isOpen(cards[2]) });
""")
        assert result["openBefore"] is True
        assert result["rerendered"] is True, "the harness must actually rebuild the cards"
        assert result["freshNode"] is True, "the restore must target the NEW node, not the detached one"
        assert result["stillOpen"] is True, "the open menu must survive a background re-render"
        assert result["raised"] is True, "the restored card keeps its raised stacking context"
        assert result["expanded"] == "true"
        assert result["otherClosed"] is True, "only the saved account's menu re-opens"

    def test_rerender_after_the_account_is_gone_opens_nothing_and_does_not_throw(self, tmp_path):
        result = _run_rerender(tmp_path, r"""
__click(list, cards[1].kebab);
const openBefore = isOpen(cards[1]);

// The account is deleted (or filtered out) between renders.
window.jackedState.accounts = [{ id: 2 }];
let threw = null;
try { rerenderAccountsView(); } catch (e) { threw = String(e && e.message || e); }

out({ openBefore, threw, gone: cards[1] === undefined,
      anyOpen: anyOpen(), anyRaised: anyRaised(),
      tracked: openAccountMenuId() });
""")
        assert result["openBefore"] is True
        assert result["threw"] is None, "a vanished account must not throw during restore"
        assert result["gone"] is True
        assert result["anyOpen"] is False, "no menu may be left open"
        assert result["anyRaised"] is False, "no card may keep the raised z-index"
        assert result["tracked"] is None

    def test_rerender_with_no_menu_open_leaves_every_menu_closed(self, tmp_path):
        result = _run_rerender(tmp_path, r"""
rerenderAccountsView();
out({ anyOpen: anyOpen(), tracked: openAccountMenuId() });
""")
        assert result == {"anyOpen": False, "tracked": None}

    def test_rerender_cannot_leave_a_menu_stuck_open(self, tmp_path):
        # The accounts list re-renders on polling: bindAccountEvents() runs
        # again against a brand-new #accounts-list. A menu open on the OLD
        # (detached) list must not linger as open state, and the document
        # listeners must not stack up render after render.
        result = _run_behavior(tmp_path, _TWO_CARDS + r"""
__click(list, a.kebab);
const openBefore = isOpen(a);
const docClicksBefore = (__docListeners['click'] || []).length;

// Re-render: the old list is replaced wholesale
list.remove();
const list2 = __makeEl('div');
list2.id = 'accounts-list';
__body.appendChild(list2);
const c = __buildCard(3);
list2.appendChild(c.card);
bindAccountEvents();
const docClicksAfter = (__docListeners['click'] || []).length;

// The fresh list's kebab still works, and the stale menu is not tracked
__click(list2, c.kebab);
const freshOpen = !c.menu.classList.contains('hidden');
__click(list2, c.kebab);
out({ openBefore, docClicksBefore, docClicksAfter, freshOpen,
      freshClosed: c.menu.classList.contains('hidden') });
""")
        assert result["openBefore"] is True
        assert result["docClicksBefore"] == result["docClicksAfter"] == 1, (
            "document-level listeners must be bound exactly once, not per render"
        )
        assert result["freshOpen"] is True
        assert result["freshClosed"] is True


# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

class TestStyles:
    def test_attention_dot_and_raised_card_rules_exist(self):
        css = STYLE_CSS.read_text(encoding="utf-8")
        assert ".account-menu-dot {" in css
        dot = css[css.index(".account-menu-dot {"):]
        dot = dot[:dot.index("}")]
        assert "position: absolute" in dot
        assert "width: 6px" in dot and "height: 6px" in dot
        assert "box-shadow" not in dot, "a solid dot, never a colored glow"
        # A disabled card's opacity opens a stacking context; the open card is
        # raised so the panel still paints over the next card in the grid.
        assert ".provider-card.menu-open {" in css
        raised = css[css.index(".provider-card.menu-open {"):]
        raised = raised[:raised.index("}")]
        assert "z-index" in raised and "position: relative" in raised

    def test_menu_css_has_no_slop_tells(self):
        css = STYLE_CSS.read_text(encoding="utf-8")
        block = css[css.index("/* Account overflow (kebab) menu"):css.index("/* Delete confirmation inline */")]
        for banned in ("backdrop-filter", "blur(", "gradient", "border-left"):
            assert banned not in block, f"banned design tell in menu CSS: {banned}"

    def test_account_cards_carry_no_colored_edge_accent(self):
        """No colored left/top border on the account cards, ever.

        A colored edge stripe on a card is the most recognizable AI-design tell,
        and the lavender it used (#a78bfa) is a second one. Provider identity is
        carried by the labeled chip providerBadge() renders in the card header
        (a glyph plus the word "Claude" or "Codex"), so the stripe was redundant
        decoration on top of an explicit label. The card keeps the uniform 1px
        `border border-slate-700` from its Tailwind classes on all four sides.

        Scoped to .provider-card rules so the load-bearing
        .provider-card.menu-open stacking rule is still free to exist (it is
        asserted separately above).
        """
        css = STYLE_CSS.read_text(encoding="utf-8")
        banned_props = (
            "border-left", "border-top",
            "border-inline-start", "border-block-start",
        )
        for selector, body in re.findall(r"([^{}]*)\{([^{}]*)\}", css):
            if ".provider-card" not in selector:
                continue
            for prop in banned_props:
                assert prop not in body, (
                    f"colored edge accent is back on `{selector.strip()}`: {prop}"
                )
            assert "border-color" not in body, (
                f"provider brand color must not paint a card border: {selector.strip()}"
            )
