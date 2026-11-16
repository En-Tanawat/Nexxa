"""Task 7: public dashboard (/) and trends page (/trends): no login, vendored Chart.js, safe DOM code."""
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main, stats
from app.db import get_session, init_db
from app.models import Analysis, AppSetting, Batch, Capture, Crop, Station
from app.web import routes_public_api

APP_WEB = Path(__file__).resolve().parents[1] / "app" / "web"
STATIC = APP_WEB / "static"
JS = STATIC / "js"
SERVER = Path(__file__).resolve().parents[1]
PAGES = ["/", "/trends"]
APP_JS = ["dashboard.js", "trends.js", "public-lib.js"]
EVIL = "<script>alert(1)</script>"
SECRETS = ["RFID-SECRET-UID-9", "RAW-NOTE-SECRET", "/data/images/secret.jpg"]


@pytest.fixture
def eng():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(e)
    return e


@pytest.fixture
def client(eng, monkeypatch):
    def override():
        with Session(eng) as s:
            yield s

    main.app.dependency_overrides[get_session] = override
    monkeypatch.setattr(routes_public_api, "_hits", {})
    yield TestClient(main.app, follow_redirects=False)  # no login at all
    main.app.dependency_overrides.clear()


def seed(eng, name="สถานี 1", demo=False):
    today = stats.today()
    with Session(eng) as s:
        crop = Crop(name="กรีนคอส", labels=["ปกติ", "ใบเหลือง"], prompt="p", notes="RAW-NOTE-SECRET")
        s.add(crop)
        s.flush()
        st = Station(rfid_uid="RFID-SECRET-UID-9", name=name, crop_id=crop.id, route_order=1, map_x=1.0, map_y=2.0)
        s.add(st)
        s.flush()
        s.add(Batch(station_id=st.id, crop_id=crop.id, planted_on=today - timedelta(days=40)))
        c = Capture(station_id=st.id, taken_at=datetime(today.year, today.month, today.day, 1), image_path="/data/images/secret.jpg",
                    quality_ok=True)
        s.add(c)
        s.flush()
        s.add(Analysis(capture_id=c.id, label="ใบเหลือง", confidence=0.9, note="RAW-NOTE-SECRET"))
        if demo:
            s.add(AppSetting(key="demo", value="1"))
        s.commit()
        stats.rollup(s)


class _Refs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts, self.links, self.imgs, self.anchors, self.ids, self.tags = [], [], [], [], {}, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if "id" in a:
            self.ids[a["id"]] = (tag, a)
        if tag == "script":
            self.scripts.append(a)
        elif tag == "link":
            self.links.append(a)
        elif tag == "img":
            self.imgs.append(a)
        elif tag == "a":
            self.anchors.append(a)


def parse(html):
    p = _Refs()
    p.feed(html)
    return p


# --- pages are public --------------------------------------------------------

@pytest.mark.parametrize("path", PAGES)
def test_pages_are_public_thai_and_link_to_login(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert '<html lang="th">' in r.text
    assert any(a.get("href") == "/login" for a in parse(r.text).anchors)
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert client.post(path).status_code == 405


@pytest.mark.parametrize("path", PAGES)
def test_public_pages_issue_no_cookie(client, monkeypatch, path):
    monkeypatch.setenv("NEXXA_SESSION_SECRET", "test-only-session-secret-0123456789")  # test-only value
    assert "set-cookie" not in client.get(path).headers


@pytest.mark.parametrize("path", PAGES)
def test_pages_show_nothing_forbidden_or_user_supplied(client, eng, path):
    seed(eng, name=EVIL, demo=True)
    html = client.get(path).text
    for secret in SECRETS:
        assert secret not in html
    assert EVIL not in html and "<script>alert" not in html and "alert(1)" not in html
    assert "<img" not in html or all(not i.get("src", "").endswith((".jpg", ".png")) for i in parse(html).imgs)
    # the API (not the page) carries the name, JSON-encoded as data, for the script to put in with textContent
    assert client.get("/api/public/twin").json()["stations"][0]["name"] == EVIL


@pytest.mark.parametrize("path", PAGES)
def test_pages_have_no_inline_script_style_or_handler(client, path):
    html = client.get(path).text
    p = parse(html)
    assert all(s.get("src") for s in p.scripts)
    assert "<style" not in html.lower() and not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    assert "javascript:" not in html.lower()


# --- vendored libraries, no external loads -------------------------------------

def _vendor_version():
    text = (STATIC / "vendor" / "VENDOR.md").read_text(encoding="utf-8")
    m = re.search(r"^## Chart\.js (\d+\.\d+\.\d+)\s*$", text, re.M)
    assert m, "VENDOR.md must record the pinned Chart.js version"
    assert "https://" in text and "MIT" in text
    return m.group(1)


def test_chartjs_is_vendored_pinned_and_licensed():
    ver = _vendor_version()
    assert ver.startswith("4.")
    folder = STATIC / "vendor" / f"chartjs-{ver}"
    assert (folder / "LICENSE.md").is_file() and "MIT License" in (folder / "LICENSE.md").read_text(encoding="utf-8")
    js = (folder / "chart.umd.min.js").read_text(encoding="utf-8")
    assert f"Chart.js v{ver}" in js[:400]
    assert not re.search(r"\beval\s*\(|new Function\s*\(", js)  # works under a CSP without 'unsafe-eval'
    assert [p.name for p in (STATIC / "vendor").iterdir() if p.name.startswith("chartjs-")] == [f"chartjs-{ver}"]


ALLOWED_EXTERNAL = {"https://fonts.googleapis.com", "https://fonts.gstatic.com"}


@pytest.mark.parametrize("path", PAGES)
def test_only_local_assets_and_google_fonts(client, path):
    p = parse(client.get(path).text)
    refs = [s["src"] for s in p.scripts] + [lk["href"] for lk in p.links if "href" in lk] + [i["src"] for i in p.imgs if "src" in i]
    for ref in refs:
        if ref.startswith("http"):
            assert any(ref.startswith(o) for o in ALLOWED_EXTERNAL), ref
        else:
            assert ref.startswith("/static/"), ref
            assert (STATIC / ref.removeprefix("/static/")).is_file(), ref
    for a in p.anchors:  # external links (photo credit) never leak the opener or referrer
        if a.get("href", "").startswith("http"):
            assert {"noopener", "noreferrer"} <= set(a.get("rel", "").split()), a
    assert not any(re.search(r"cdn|unpkg|jsdelivr|cdnjs", ref) for ref in refs)


def test_trends_page_loads_the_pinned_chartjs_then_its_own_module(client):
    ver = _vendor_version()
    srcs = [s["src"] for s in parse(client.get("/trends").text).scripts]
    chart = f"/static/vendor/chartjs-{ver}/chart.umd.min.js"
    assert chart in srcs and "/static/js/trends.js" in srcs
    assert srcs.index(chart) < srcs.index("/static/js/trends.js")
    assert chart not in [s["src"] for s in parse(client.get("/").text).scripts]  # the dashboard needs no chart library


@pytest.mark.parametrize("path,script", [("/", "dashboard.js"), ("/trends", "trends.js")])
def test_page_scripts_are_served_as_javascript_modules(client, path, script):
    tag = next(s for s in parse(client.get(path).text).scripts if s["src"].endswith(script))
    assert tag.get("type") == "module"
    for name in (script, "public-lib.js"):
        r = client.get(f"/static/js/{name}")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]


# --- empty state, demo banner, structure ----------------------------------------

def test_dashboard_empty_data_shows_a_designed_empty_state(client):
    html = client.get("/").text
    p = parse(html)
    empty = p.ids["dashboard-empty"][1]
    assert "hidden" not in empty
    assert "ยังไม่มีข้อมูลจากโรงเรือน" in html and "เข้าสู่ระบบ" in html
    assert "hidden" in p.ids["dashboard-main"][1] and "hidden" in p.ids["kpi-grid"][1]
    assert "hidden" in p.ids["demo-banner"][1]


def test_dashboard_with_data_hides_the_empty_state(client, eng):
    seed(eng)
    p = parse(client.get("/").text)
    assert "hidden" in p.ids["dashboard-empty"][1]
    assert "hidden" not in p.ids["dashboard-main"][1] and "hidden" not in p.ids["kpi-grid"][1]


@pytest.mark.parametrize("path", PAGES)
def test_demo_banner_only_when_demo_flag_is_set(client, eng, path):
    seed(eng)
    html = client.get(path).text
    assert "hidden" in parse(html).ids["demo-banner"][1]
    with Session(eng) as s:
        s.add(AppSetting(key="demo", value="1"))
        s.commit()
    html = client.get(path).text
    banner = parse(html).ids["demo-banner"][1]
    assert "hidden" not in banner
    assert "ข้อมูลตัวอย่าง (DEMO)" in html


def test_dashboard_reserves_the_3d_mount_point(client):
    p = parse(client.get("/").text)
    tag, a = p.ids["twin-root"]
    assert tag == "div" and a.get("aria-label") and a.get("role") in ("region", "img", "group")
    html = client.get("/").text
    assert "แผนผัง 3 มิติกำลังพัฒนา" in html  # fallback text inside the reserved box
    for ident in ("kpi-stations", "kpi-captures", "kpi-abnormal", "kpi-review", "kpi-harvest", "station-list", "activity-list",
                  "lot-list", "error-banner", "updated-at", "fresh-pill"):
        assert ident in p.ids, ident
    assert "hidden" in p.ids["error-banner"][1]


def test_dashboard_has_badge_and_row_templates_for_the_script(client):
    p = parse(client.get("/").text)
    for kind in ("ok", "warn", "danger", "info", "neutral"):
        assert f"tpl-badge-{kind}" in p.ids, kind
    for ident in ("tpl-station", "tpl-lot", "tpl-activity"):
        assert p.ids[ident][0] == "template", ident


def test_trends_page_controls_and_charts(client):
    html = client.get("/trends").text
    p = parse(html)
    ranges = [a for t, a in p.tags if t == "button" and "data-days" in a]
    assert [a["data-days"] for a in ranges] == ["7", "30", "100"]
    assert [a["aria-pressed"] for a in ranges] == ["false", "true", "false"]
    assert p.ids["station-filter"][0] == "select"
    canvases = [a for t, a in p.tags if t == "canvas"]
    assert len(canvases) == 4
    for a in canvases:
        assert a.get("role") == "img" and a.get("aria-label") and a.get("aria-describedby")
        assert a["aria-describedby"] in p.ids
    assert html.count("<details") == 4 and html.count("<summary") == 4
    for ident in ("trends-empty", "trends-error", "trends-status", "demo-banner"):
        assert ident in p.ids, ident
    assert p.ids["trends-error"][1].get("role") == "alert" and "hidden" in p.ids["trends-error"][1]
    assert p.ids["trends-status"][1].get("aria-live") == "polite"


def test_trends_empty_state_is_server_rendered_when_there_is_no_data(client, eng):
    p = parse(client.get("/trends").text)
    assert "hidden" not in p.ids["trends-empty"][1] and "hidden" in p.ids["trends-charts"][1]
    seed(eng)
    p = parse(client.get("/trends").text)
    assert "hidden" in p.ids["trends-empty"][1] and "hidden" not in p.ids["trends-charts"][1]


def test_hidden_attribute_always_wins_over_component_display_rules():
    # empty state vs charts: `.stack-lg { display: grid }` must not keep a `hidden` block on screen
    css = (STATIC / "css" / "nexxa.css").read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", css)


# --- JavaScript hygiene -----------------------------------------------------------

DANGEROUS = [r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function",
             r"setTimeout\s*\(\s*[\"'`]", r"setInterval\s*\(\s*[\"'`]", r"setAttribute\(\s*[\"']style[\"']", r"\.cssText",
             r"createContextualFragment", r"srcdoc", r"javascript:", r"\bimportScripts\b", r"localStorage\.setItem\([^)]*\b(name|station)"]


@pytest.mark.parametrize("name", APP_JS)
def test_app_scripts_never_write_markup_or_run_strings(name):
    src = (JS / name).read_text(encoding="utf-8")
    for pattern in DANGEROUS:
        assert not re.search(pattern, src), (name, pattern)
    src = src.replace("http://www.w3.org/2000/svg", "")  # the SVG namespace is a name, not a request
    assert "http://" not in src and not re.search(r"https://(?!fonts)", src)


def test_dom_scripts_put_data_in_with_textcontent():
    for name in ("dashboard.js", "trends.js"):
        src = (JS / name).read_text(encoding="utf-8")
        assert "textContent" in src, name
    dash = (JS / "dashboard.js").read_text(encoding="utf-8")
    for url in ("/api/public/summary", "/api/public/twin"):
        assert url in dash
    assert "visibilitychange" in dash and "60" in dash
    trends = (JS / "trends.js").read_text(encoding="utf-8")
    assert "/api/public/trends" in trends and "prefers-reduced-motion" in trends and "MutationObserver" in trends
    assert "data-theme" in trends and "destroy()" in trends


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: JavaScript syntax and pure-function tests cannot run")
    return node


@pytest.mark.parametrize("name", APP_JS)
def test_app_scripts_pass_node_check(name):
    r = subprocess.run([_node(), "--check", str(JS / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_pure_helpers_pass_their_node_tests():
    env = {**os.environ, "PUBLIC_LIB": str(JS / "public-lib.js")}
    r = subprocess.run([_node(), "--test", "tests/js/public_lib.test.mjs"], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=SERVER, env=env)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1500:]
    passed = re.findall(r"^\S+ pass (\d+)\s*$", r.stdout, re.M)  # summary line of the node:test spec reporter
    assert passed and int(passed[-1]) >= 15 and re.search(r"^\S+ fail 0\s*$", r.stdout, re.M), r.stdout[-500:]

