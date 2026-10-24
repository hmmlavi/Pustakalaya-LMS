"""Tests for Pustakalaya — The Living Library Experience (Day/Night, Ambience, Dashboard & A11y)."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
import shutil
import subprocess

from app.web.routes.dashboard import READING_ROOM_QUOTES, daily_quote_for_date


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    assert match is not None, "Expected CSRF token in HTML response"
    return match.group(1)


def _login(client, username: str, password: str) -> None:
    page = client.get("/auth/login").get_data(as_text=True)
    token = _extract_csrf(page)
    resp = client.post(
        "/auth/login",
        data={"csrf_token": token, "username": username, "password": password},
        follow_redirects=True,
    )
    assert resp.status_code == 200


def test_theme_bootstrap_fouc_guard_and_public_screen_persistence(web_app):
    """Verify early pre-paint theme script prevents FOUC and persists across public/error screens."""
    client = web_app.test_client()

    # 1. Default unauthenticated login page
    res = client.get("/auth/login")
    assert res.status_code == 200
    html = res.get_data(as_text=True)

    assert 'data-theme="day"' in html
    assert 'data-theme-pref="system"' in html
    assert 'data-ambience="auto"' in html
    assert 'id="public-theme-toggle"' in html
    assert "Sunlit Reading Room" in html

    # Inline bootstrap script must execute BEFORE main.css link to prevent FOUC
    script_pos = html.index('localStorage.getItem("pustakalaya.theme")')
    css_pos = html.index("css/main.css")
    assert script_pos < css_pos

    # 2. Simulate persisted Night + Minimal ambience cookies on public login & 404 pages
    client.set_cookie("pustakalaya_theme", "night")
    client.set_cookie("pustakalaya_ambience", "minimal")

    night_login = client.get("/auth/login").get_data(as_text=True)
    assert 'data-theme="night"' in night_login
    assert 'data-theme-pref="night"' in night_login
    assert 'data-ambience="minimal"' in night_login
    assert "Lantern-lit Library" in night_login
    assert 'aria-pressed="true"' in night_login

    err_resp = client.get("/non-existent-sanctuary-shelf")
    assert err_resp.status_code == 404
    err_html = err_resp.get_data(as_text=True)
    assert 'data-theme="night"' in err_html
    assert 'id="public-theme-toggle"' in err_html


def test_appearance_and_library_ambience_endpoint_across_roles(web_app):
    """Verify all staff roles can persist independent Theme and Library Ambience preferences."""
    credentials = [
        ("admin", "AdminTestPassword!99"),
        ("librarian", "LibrarianPassword!99"),
        ("readonly", "ReadonlyPassword!99"),
    ]

    for username, password in credentials:
        client = web_app.test_client()
        _login(client, username, password)

        settings_html = client.get("/settings").get_data(as_text=True)
        assert "Appearance &amp; Library Ambience" in settings_html
        csrf = _extract_csrf(settings_html)

        # Save Night Mode with Ambience Off (proves Night Mode works without lanterns/fireflies)
        post_resp = client.post(
            "/settings/appearance",
            data={
                "csrf_token": csrf,
                "theme_preference": "night",
                "ambience_mode": "off",
            },
            follow_redirects=True,
        )
        assert post_resp.status_code == 200
        updated_html = post_resp.get_data(as_text=True)
        assert "Saved appearance preferences (Theme: Night, Library Ambience: Off)." in updated_html
        assert 'data-theme="night"' in updated_html
        assert 'data-theme-pref="night"' in updated_html
        assert 'data-ambience="off"' in updated_html
        assert "Quiet Night Study" in updated_html
        assert "Ambience: Off" in updated_html

        # Verify CSRF enforcement on /settings/appearance
        no_csrf = client.post(
            "/settings/appearance",
            data={"theme_preference": "day", "ambience_mode": "auto"},
        )
        assert no_csrf.status_code == 400

        # Invalid option must be rejected cleanly without corrupting saved cookies
        bad_resp = client.post(
            "/settings/appearance",
            data={
                "csrf_token": _extract_csrf(updated_html),
                "theme_preference": "neon-cyberpunk",
                "ambience_mode": "off",
            },
            follow_redirects=True,
        )
        assert bad_resp.status_code == 200
        bad_html = bad_resp.get_data(as_text=True)
        assert "Not a valid choice" in bad_html
        assert 'data-theme="night"' in bad_html


def test_living_dashboard_real_data_quotes_discovery_and_role_actions(db_env, web_app):
    """Verify Dashboard renders real catalog discovery, audit activity, daily quote & RBAC actions."""
    book = db_env["books"].create_book(
        isbn="9788126701940",
        title="Godaan",
        subtitle="The Gift of a Cow",
        publisher="Rajkamal Prakashan",
        publication_year=1936,
        edition="Centenary Hardcover",
        language="Hindi",
        category="Classic Literature",
        description="Landmark novel of rural Awadh.",
        author_names=["Munshi Premchand"],
        actor_user_id=db_env["admin_user"].id,
    )
    copy_avail = db_env["books"].add_copy(
        book_id=book.id,
        owning_branch_id=db_env["branches"][0].id,
        accession_number="LKO-HZR-GOD-01",
        condition_notes="GOOD · Shelf LIT-A1",
        actor_user_id=db_env["admin_user"].id,
    )
    assert copy_avail.status == "AVAILABLE"

    # Verify deterministic quote rotation & verified non-empty attribution/source
    assert len(READING_ROOM_QUOTES) >= 5
    for q in READING_ROOM_QUOTES:
        assert q.text.strip()
        assert q.attribution.strip()
        assert q.source.strip()
    q_day_1 = daily_quote_for_date(date(2026, 10, 24))
    q_day_2 = daily_quote_for_date(date(2026, 10, 25))
    assert q_day_1 != q_day_2
    assert daily_quote_for_date(date(2026, 10, 24)) == q_day_1

    # 1. ADMIN sees + Add a Book AND circulation quick actions + real Book Discovery & Activity
    admin_client = web_app.test_client()
    _login(admin_client, "admin", "AdminTestPassword!99")
    admin_dash = admin_client.get("/").get_data(as_text=True)

    assert "Network Operations Dashboard" in admin_dash
    assert "From the Reading Room" in admin_dash
    assert "Catalog Discovery — From the Shelves" in admin_dash
    assert "Godaan" in admin_dash
    assert "Munshi Premchand" in admin_dash
    assert "1 Available" in admin_dash
    assert "book-cover-plate" in admin_dash
    assert "Recent Library Activity" in admin_dash
    assert "BOOK_CREATED" in admin_dash
    assert "+ Add a Book" in admin_dash
    assert "+ Register a Member" in admin_dash
    assert "Issue a Book" in admin_dash

    # 2. LIBRARIAN sees circulation actions, NOT + Add a Book
    lib_client = web_app.test_client()
    _login(lib_client, "librarian", "LibrarianPassword!99")
    lib_dash = lib_client.get("/").get_data(as_text=True)
    assert "+ Add a Book" not in lib_dash
    assert "+ Register a Member" in lib_dash
    assert "Issue a Book" in lib_dash

    # 3. Read-only STAFF sees catalog/directory actions, NOT mutation quick actions
    staff_client = web_app.test_client()
    _login(staff_client, "readonly", "ReadonlyPassword!99")
    staff_dash = staff_client.get("/").get_data(as_text=True)
    assert "+ Add a Book" not in staff_dash
    assert "+ Register a Member" not in staff_dash
    assert "Issue a Book" not in staff_dash
    assert "Browse Member Directory" in staff_dash


def test_decorative_layers_accessibility_reduced_motion_and_offline_assets(web_app):
    """Verify decorative sanctuary layers, reduced-motion CSS, silent operation, and zero CDN URLs."""
    repo_root = Path(__file__).resolve().parents[2]
    css_text = (repo_root / "app" / "web" / "static" / "css" / "main.css").read_text(encoding="utf-8")
    js_text = (repo_root / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    # Zero external CDN / Google Fonts / third-party network calls in static assets
    assert "http://" not in css_text and "https://" not in css_text
    assert "fonts.googleapis.com" not in css_text
    assert "https://cdn" not in js_text and "unpkg" not in js_text

    # Zero audio / sound effects (experience must be silent by default)
    assert "AudioContext" not in js_text
    assert "new Audio" not in js_text

    # Decorative layers never intercept pointer events and respect prefers-reduced-motion
    assert ".forest-sanctuary-backdrop" in css_text
    assert "pointer-events: none;" in css_text
    assert "@media (prefers-reduced-motion: reduce)" in css_text
    assert 'html[data-ambience="off"] .forest-lantern-layer' in css_text

    client = web_app.test_client()
    _login(client, "admin", "AdminTestPassword!99")
    html = client.get("/").get_data(as_text=True)

    assert "http://" not in html and "https://" not in html
    assert '<div class="forest-sanctuary-backdrop" aria-hidden="true">' in html
    assert html.count('class="forest-lantern forest-lantern--') == 5
    assert 'id="forest-firefly-layer"' in html
    assert 'class="badge-glyph" aria-hidden="true"' in html
    assert 'id="pustakalaya-confirm-modal"' in html
    assert 'role="alertdialog"' in html
    assert 'class="stat-icon-well"' in html


def test_skeuomorphic_botanical_tokens_cool_lanterns_and_cover_variants(db_env, web_app):
    """Verify calm botanical Light & Moonlit Forest Dark tokens, 5 depth tiers, and cool lanterns."""
    repo_root = Path(__file__).resolve().parents[2]
    css_upper = (repo_root / "app" / "web" / "static" / "css" / "main.css").read_text(encoding="utf-8").upper()
    js_upper = (repo_root / "app" / "web" / "static" / "js" / "app.js").read_text(encoding="utf-8").upper()
    base_upper = (repo_root / "app" / "web" / "templates" / "base.html").read_text(encoding="utf-8").upper()

    # Light mode botanical tokens
    for light_hex in ("#F2F6F1", "#FFFFFF", "#E9F0E8", "#E3EBE2", "#26372F", "#66766B", "#668875", "#8CA99A", "#91B5B5", "#D5E1D6"):
        assert light_hex in css_upper, f"Missing Light botanical token {light_hex}"

    # Dark mode moonlit forest tokens
    for dark_hex in ("#101A19", "#172422", "#1E302C", "#0C1514", "#E4EEE8", "#A4B8AD", "#8AB6A0", "#7FAEAD", "#A7C5D5", "#2B4039"):
        assert dark_hex in css_upper, f"Missing Dark moonlit token {dark_hex}"

    # 5 distinct skeuomorphic depth tiers
    for depth_var in ("--DEPTH-INSET:", "--DEPTH-CARD:", "--DEPTH-ELEVATED:", "--DEPTH-CONTROL:", "--DEPTH-DIALOG:"):
        assert depth_var in css_upper, f"Missing skeuomorphic depth token {depth_var}"

    # No warm amber/gold lantern or firefly colors
    for banned_warm_hex in ("#D9A94E", "#B9872F", "#FBE3A2", "#FADF8E", "#E4B54C"):
        assert banned_warm_hex not in css_upper
        assert banned_warm_hex not in js_upper
        assert banned_warm_hex not in base_upper

    # Verify hardcover bookplate variant classes & embossed catalog chips render in Catalog
    b1 = db_env["books"].create_book(
        isbn="9780143418795",
        title="Twilight in Delhi",
        subtitle=None,
        publisher="Penguin India",
        publication_year=1940,
        edition="Reprint",
        language="English",
        category="Historical Fiction",
        description="Classic literary portrait.",
        author_names=["Ahmed Ali"],
        actor_user_id=db_env["admin_user"].id,
    )
    b2 = db_env["books"].create_book(
        isbn="9788129118929",
        title="Raag Darbari",
        subtitle=None,
        publisher="Rajpal & Sons",
        publication_year=1968,
        edition="Classic",
        language="Hindi",
        category="Satire",
        description="Sahitya Akademi winning satire.",
        author_names=["Shrilal Shukla"],
        actor_user_id=db_env["admin_user"].id,
    )

    client = web_app.test_client()
    _login(client, "admin", "AdminTestPassword!99")
    catalog_html = client.get("/books").get_data(as_text=True)
    assert f"book-cover-plate--v{b1.id % 5}" in catalog_html
    assert f"book-cover-plate--v{b2.id % 5}" in catalog_html
    assert 'class="catalog-chip"' in catalog_html


def test_client_js_theme_ambience_and_firefly_lifecycle():
    """Execute app.js in a lightweight Node VM harness to verify theme, ambience & fireflies."""
    node_bin = shutil.which("node")
    repo_root = Path(__file__).resolve().parents[2]
    js_path = repo_root / "app" / "web" / "static" / "js" / "app.js"
    assert js_path.is_file()

    if not node_bin:
        js_src = js_path.read_text(encoding="utf-8")
        assert "syncFireflies" in js_src and "pustakalaya.theme" in js_src
        return

    harness = r"""
const fs = require("fs");
const vm = require("vm");

const store = {};
const listeners = {};
let prefersDark = true;
let reducedMotion = false;

function makeEl(tag, id = "") {
  const attrs = {};
  const classes = new Set();
  const el = {
    tagName: tag.toUpperCase(),
    id,
    hidden: true,
    value: "",
    textContent: "",
    dataset: {},
    children: [],
    style: { setProperty(k, v) { this[k] = v; } },
    get firstChild() { return this.children[0] || null; },
    appendChild(child) {
      if (child && child.__isFragment) {
        this.children.push(...child.children);
      } else {
        this.children.push(child);
      }
      return child;
    },
    removeChild(child) {
      const idx = this.children.indexOf(child);
      if (idx >= 0) this.children.splice(idx, 1);
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      toggle(c, force) {
        const next = force === undefined ? !classes.has(c) : Boolean(force);
        if (next) classes.add(c); else classes.delete(c);
        return next;
      },
      contains(c) { return classes.has(c); }
    },
    addEventListener(ev, fn) {
      el["_on_" + ev] = el["_on_" + ev] || [];
      el["_on_" + ev].push(fn);
    },
    click() {
      (el._on_click || []).forEach(fn => fn({ preventDefault() {}, target: el }));
    },
    querySelector(sel) {
      if (sel === "[data-horizon-label]") return horizonLabel;
      return null;
    },
    querySelectorAll() { return []; },
    focus() {}
  };
  return el;
}

const root = makeEl("html");
root.setAttribute("data-theme", "day");
root.setAttribute("data-theme-pref", "system");
root.setAttribute("data-ambience", "auto");

const horizonLabel = makeEl("span");
const themeBtn = makeEl("button", "topbar-theme-toggle");
const ambBtn = makeEl("button", "ambience-quick-toggle");
const atmText = makeEl("span");
const fireflyLayer = makeEl("div", "forest-firefly-layer");

const byId = {
  "forest-firefly-layer": fireflyLayer
};

const document = {
  cookie: "",
  documentElement: root,
  activeElement: null,
  getElementById(id) { return byId[id] || null; },
  createElement(tag) { return makeEl(tag); },
  createDocumentFragment() {
    return { __isFragment: true, children: [], appendChild(c) { this.children.push(c); } };
  },
  querySelectorAll(sel) {
    if (sel === "[data-theme-toggle]") return [themeBtn];
    if (sel === "[data-ambience-cycle]") return [ambBtn];
    if (sel === "[data-reading-atmosphere-text]") return [atmText];
    return [];
  },
  addEventListener(ev, fn) {
    listeners[ev] = listeners[ev] || [];
    listeners[ev].push(fn);
  }
};

const windowObj = {
  innerWidth: 1280,
  location: { protocol: "http:" },
  localStorage: {
    getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); }
  },
  matchMedia(q) {
    return {
      matches: q.includes("dark") ? prefersDark : reducedMotion,
      addEventListener() {}
    };
  },
  addEventListener(ev, fn) {
    listeners["win_" + ev] = listeners["win_" + ev] || [];
    listeners["win_" + ev].push(fn);
  },
  setTimeout(fn) { return 1; },
  clearTimeout() {}
};

const ctx = vm.createContext({
  window: windowObj,
  document,
  localStorage: windowObj.localStorage
});

vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
(listeners.DOMContentLoaded || []).forEach(fn => fn());

// 1. System dark preference resolves to night with 14 desktop fireflies
const step1 = {
  theme: root.getAttribute("data-theme"),
  pref: root.getAttribute("data-theme-pref"),
  fireflies: fireflyLayer.children.length,
  atmosphere: atmText.textContent
};

// 2. Click horizon toggle -> switches to day & removes all fireflies
themeBtn.click();
const step2 = {
  theme: root.getAttribute("data-theme"),
  storedTheme: store["pustakalaya.theme"],
  fireflies: fireflyLayer.children.length,
  atmosphere: atmText.textContent,
  transitioning: root.classList.contains("theme-transitioning")
};

// 3. Click horizon toggle back to night, then cycle ambience: auto -> minimal -> off
themeBtn.click();
ambBtn.click(); // minimal
const step3Minimal = {
  theme: root.getAttribute("data-theme"),
  ambience: root.getAttribute("data-ambience"),
  fireflies: fireflyLayer.children.length
};

ambBtn.click(); // off
const step4Off = {
  theme: root.getAttribute("data-theme"),
  ambience: root.getAttribute("data-ambience"),
  fireflies: fireflyLayer.children.length,
  atmosphere: atmText.textContent,
  cookie: document.cookie
};

console.log(JSON.stringify({ step1, step2, step3Minimal, step4Off }));
"""
    proc = subprocess.run(
        [node_bin, "-e", harness, str(js_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    out = json.loads(proc.stdout.strip())

    assert out["step1"] == {
        "theme": "night",
        "pref": "system",
        "fireflies": 14,
        "atmosphere": "Lantern-lit Library",
    }
    assert out["step2"] == {
        "theme": "day",
        "storedTheme": "day",
        "fireflies": 0,
        "atmosphere": "Sunlit Reading Room",
        "transitioning": True,
    }
    assert out["step3Minimal"] == {
        "theme": "night",
        "ambience": "minimal",
        "fireflies": 4,
    }
    assert out["step4Off"]["theme"] == "night"
    assert out["step4Off"]["ambience"] == "off"
    assert out["step4Off"]["fireflies"] == 0
    assert out["step4Off"]["atmosphere"] == "Quiet Night Study"
    assert "pustakalaya_ambience=off" in out["step4Off"]["cookie"]
