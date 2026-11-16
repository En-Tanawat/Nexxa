"""Task 8: 3D greenhouse twin (three.js, vendored) with a 2D SVG fallback on the public dashboard."""
import hashlib
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

SERVER = Path(__file__).resolve().parents[1]
REPO = SERVER.parent
WEB = SERVER / "app" / "web"
STATIC = WEB / "static"
JS = STATIC / "js"
VENDOR = STATIC / "vendor"
THREE_VERSION = "0.185.1"
THREE = VENDOR / f"three-{THREE_VERSION}"
TWIN_JS = ["twin.js", "twin-logic.js", "twin-fallback.js"]
VENDOR_MD = (VENDOR / "VENDOR.md").read_text(encoding="utf-8")
UPSTREAM_ORBIT_SHA = "faabb4e8dfd9235ee4a9fd7c9a3d75f90f1689dbd4944bd6fd32117dacec5f93"  # npm three@0.185.1, unpatched
BARE_IMPORT = b"} from 'three';\n"
RELATIVE_IMPORT = b"} from '../../../build/three.module.min.js';\n"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def client():
    return TestClient(main.app, follow_redirects=False)


# --- vendored three.js -------------------------------------------------------------------------------

def test_three_is_vendored_pinned_and_licensed():
    for rel in ("build/three.module.min.js", "build/three.core.min.js", "examples/jsm/controls/OrbitControls.js", "LICENSE"):
        assert (THREE / rel).is_file(), rel
    assert "The MIT License" in (THREE / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright" in (THREE / "LICENSE").read_text(encoding="utf-8")
    assert [p.name for p in VENDOR.iterdir() if p.name.startswith("three-")] == [f"three-{THREE_VERSION}"]
    # the release is split: the module build imports the core build, which must sit next to it
    assert 'from"./three.core.min.js"' in (THREE / "build" / "three.module.min.js").read_text(encoding="utf-8")
    assert '"185"' in (THREE / "build" / "three.core.min.js").read_text(encoding="utf-8")  # REVISION = "185"
    assert f"## three.js {THREE_VERSION}" in VENDOR_MD
    assert f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/build/three.module.min.js" in VENDOR_MD
    assert "MIT" in VENDOR_MD


def test_vendored_code_needs_no_eval_so_it_runs_under_the_csp():
    for p in (THREE / "build").glob("*.js"):
        assert not re.search(r"\beval\s*\(|new Function\s*\(", p.read_text(encoding="utf-8")), p.name


def _vendor_rows():
    return dict(re.findall(r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|", VENDOR_MD, re.M))


def test_vendor_md_records_the_sha256_of_every_vendored_file():
    rows = _vendor_rows()
    on_disk = {p.relative_to(VENDOR).as_posix(): p for p in VENDOR.rglob("*") if p.is_file() and p.name != "VENDOR.md"}
    assert set(rows) == set(on_disk), set(rows) ^ set(on_disk)
    for rel, path in on_disk.items():
        assert sha256(path) == rows[rel], f"{rel}: bytes on disk differ from VENDOR.md"


def test_the_one_patch_is_documented_and_reversible():
    orbit = THREE / "examples" / "jsm" / "controls" / "OrbitControls.js"
    data = orbit.read_bytes()
    assert data.count(RELATIVE_IMPORT) == 1 and BARE_IMPORT not in data
    assert hashlib.sha256(data.replace(RELATIVE_IMPORT, BARE_IMPORT)).hexdigest() == UPSTREAM_ORBIT_SHA  # nothing else was touched
    assert UPSTREAM_ORBIT_SHA in VENDOR_MD and sha256(orbit) in VENDOR_MD
    assert "from 'three'" in VENDOR_MD and "../../../build/three.module.min.js" in VENDOR_MD
    assert re.search(r"CSP|importmap", VENDOR_MD)  # the reason is written down


def test_vendored_files_are_protected_from_newline_conversion():
    ga = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    rules = {ln.split("#")[0].strip() for ln in ga}
    assert "*.min.js -text" in rules
    assert "server/app/web/static/vendor/** -text" in rules


def _imports(source: str):
    clean = re.sub(r"/\*[\s\S]*?\*/|//.*", "", source)
    pat = r"""(?:^|[;}\s])(?:import|export)\s*(?:[\w${},*\s]*?\bfrom\s*)?["']([^"']+)["']|\bimport\(\s*["']([^"']+)["']\s*\)"""
    return [a or b for a, b in re.findall(pat, clean, re.S)]


def _graph(start: Path, seen=None):
    seen = {} if seen is None else seen
    if start in seen:
        return seen
    seen[start] = _imports(start.read_text(encoding="utf-8"))
    for spec in seen[start]:
        assert spec.startswith(("./", "../")), f"{start.name}: bare/absolute import {spec!r} would need an importmap (blocked by the CSP)"
        target = (start.parent / spec).resolve()
        assert target.is_file(), f"{start.name}: {spec} does not exist"
        assert STATIC in target.parents, f"{start.name}: {spec} leaves /static"
        _graph(target, seen)
    return seen


def test_module_graph_is_relative_complete_and_inside_static():
    graph = _graph(JS / "twin.js")
    names = {p.name for p in graph}
    assert {"twin.js", "twin-logic.js", "twin-fallback.js", "public-lib.js"} <= names
    assert {"three.module.min.js", "three.core.min.js", "OrbitControls.js"} <= names  # three is reachable only through twin.js
    for p in (JS / "dashboard.js", JS / "twin-fallback.js"):
        _graph(p)


def test_every_module_in_the_graph_is_served_as_javascript(client):
    for p in _graph(JS / "twin.js"):
        url = "/static/" + p.relative_to(STATIC).as_posix()
        r = client.get(url)
        assert r.status_code == 200 and "javascript" in r.headers["content-type"], url
        assert r.content == p.read_bytes()


# --- the page ---------------------------------------------------------------------------------------------

class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_dashboard_uses_no_importmap_and_no_inline_script(client):
    html = client.get("/").text
    p = _Tags()
    p.feed(html)
    assert "importmap" not in html.lower()  # an inline importmap is blocked by the CSP (no 'unsafe-inline')
    assert all(a.get("src") for t, a in p.tags if t == "script")
    csp = client.get("/").headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    root = next(a for t, a in p.tags if a.get("id") == "twin-root")
    assert root.get("role") in ("region", "group") and root.get("aria-label")
    assert "แผนผัง 3 มิติ" in html  # reserved box keeps readable text for no-JS / loading


def test_dashboard_loads_twin_lazily_and_survives_its_failure():
    src = (JS / "dashboard.js").read_text(encoding="utf-8")
    assert re.search(r'import\(\s*"\./twin\.js"\s*\)', src)
    assert "catch" in src[src.index('import("./twin.js")'):][:900]
    assert re.search(r'import\(\s*"\./twin-fallback\.js"\s*\)', src), "if twin.js itself cannot load, the 2D schematic still can"
    assert not re.search(r'^import[^;]*twin', src, re.M), "twin.js must never be a static import (a failure would take the dashboard down)"
    assert "twin-root" in src and "update(" in src


# --- JavaScript hygiene -------------------------------------------------------------------------------------

DANGEROUS = [r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new\s+Function",
             r"setTimeout\s*\(\s*[\"'`]", r"setInterval\s*\(\s*[\"'`]", r"setAttribute\(\s*[\"']style[\"']", r"\.cssText",
             r"createContextualFragment", r"srcdoc", r"javascript:", r"\bimportScripts\b", r"\blocalStorage\b", r"\bfetch\s*\(",
             r"XMLHttpRequest", r"WebSocket", r"sendBeacon"]


@pytest.mark.parametrize("name", TWIN_JS)
def test_twin_scripts_never_write_markup_load_external_code_or_phone_home(name):
    src = (JS / name).read_text(encoding="utf-8")
    for pattern in DANGEROUS:
        assert not re.search(pattern, src), (name, pattern)
    src = src.replace("http://www.w3.org/2000/svg", "")  # the SVG namespace is a name, not a request
    assert "http://" not in src and "https://" not in src
    assert not re.search(r"cdn|unpkg|jsdelivr|cdnjs", src, re.I)


def test_twin_script_has_the_required_safety_and_performance_hooks():
    src = (JS / "twin.js").read_text(encoding="utf-8")
    assert "export function mountTwin" in src and "dispose" in src and "update" in src
    assert "textContent" in src and "fillText" in src  # data goes in as text; scene labels are drawn text
    assert "prefers-reduced-motion" in src and "visibilitychange" in src and "IntersectionObserver" in src
    assert "ResizeObserver" in src and "MutationObserver" in src and "data-theme" in src
    assert re.search(r"setPixelRatio\(\s*Math\.min\(\s*[\w.]*devicePixelRatio[^,]*,\s*2\s*\)\s*\)", src)
    assert "webglcontextlost" in src and "webglcontextrestored" in src
    assert "maxPolarAngle" in src and "enableDamping" in src and "autoRotate" in src
    assert "forceContextLoss" in src or "dispose()" in src
    for call in ("geometry.dispose", "material.dispose", "texture.dispose", "renderer.dispose", "controls.dispose"):
        assert call in src, call
    assert "removeEventListener" in src and "disconnect()" in src and "cancelAnimationFrame" in src
    fb = (JS / "twin-fallback.js").read_text(encoding="utf-8")
    assert "createElementNS" in fb and "textContent" in fb and "dispose" in fb


def test_twin_css_reserves_space_has_focus_styles_and_respects_reduced_motion():
    css = (STATIC / "css" / "nexxa.css").read_text(encoding="utf-8")
    assert re.search(r"\.twin-root\s*\{[^}]*min-height:\s*\d+px", css)  # no layout shift when the scene replaces the placeholder
    for cls in (".twin__stage", ".twin__canvas", ".twin__tools", ".twin__panel", ".twin__list", ".twin__chip", ".twin__note", ".twin__empty"):
        assert cls in css, cls
    assert re.search(r"\.twin__(tool|chip)[^{]*:focus-visible", css)
    assert re.search(r"@media \(prefers-reduced-motion: reduce\)[^@]*twin", css, re.S) or "twin" in css.split("prefers-reduced-motion: reduce")[-1]
    for token in ("--twin-sky-top", "--twin-ground", "--twin-label-bg", "--twin-label-fg", "--twin-mark-normal", "--twin-plant-brown"):
        assert css.count(token + ":") == 2, token  # defined once per theme


# --- Node: syntax, pure logic, mount/dispose harness -------------------------------------------------------------

def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: JavaScript syntax and pure-function tests cannot run")
    return node


@pytest.mark.parametrize("name", TWIN_JS)
def test_twin_scripts_pass_node_check(name):
    r = subprocess.run([_node(), "--check", str(JS / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _run_node_tests(test_file: str, env_name: str, target: Path, minimum: int):
    env = {**os.environ, env_name: str(target)}
    r = subprocess.run([_node(), "--test", test_file], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=SERVER, env=env)
    assert r.returncode == 0, r.stdout[-3500:] + r.stderr[-1500:]
    passed = re.findall(r"^\S+ pass (\d+)\s*$", r.stdout, re.M)
    assert passed and int(passed[-1]) >= minimum and re.search(r"^\S+ fail 0\s*$", r.stdout, re.M), r.stdout[-600:]


def test_pure_twin_logic_passes_its_node_tests():
    _run_node_tests("tests/js/twin_logic.test.mjs", "TWIN_LOGIC", JS / "twin-logic.js", 20)


def test_mount_and_dispose_twenty_times_leaks_nothing():
    _run_node_tests("tests/js/twin_mount.test.mjs", "TWIN_JS", JS / "twin.js", 6)
