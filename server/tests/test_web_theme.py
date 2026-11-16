"""Task 4b: foliage + frosted-glass theme with light/dark modes."""
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import cv2
import pytest

from tests.test_web_auth import _client, _csrf, _login, admin, engine  # noqa: F401
from tests.test_web_crud import GET_PATHS, make_batch, make_crop, make_station

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
CSS = re.sub(r"/\*.*?\*/", "", (STATIC / "css" / "nexxa.css").read_text(encoding="utf-8"), flags=re.S)
JS_FILES = ["theme-init.js", "theme-toggle.js", "app.js"]
IMG_CAP = 200 * 1024
THAI = re.compile(r"[฀-๿]")


# --- pages: head order, no inline script, toggle ------------------------------

class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_head = False
        self.head = []  # ("script" | "css", url, attrs) in document order
        self.inline_scripts = self.style_attrs = self.style_tags = 0
        self.toggles, self.metas = [], {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "style" in a:
            self.style_attrs += 1
        if tag == "head":
            self.in_head = True
        elif tag == "style":
            self.style_tags += 1
        elif tag == "script":
            if not a.get("src"):
                self.inline_scripts += 1
            elif self.in_head:
                self.head.append(("script", a["src"], a))
        elif tag == "link" and a.get("rel") == "stylesheet" and self.in_head:
            self.head.append(("css", a["href"], a))
        elif tag == "meta" and a.get("name"):
            self.metas[a["name"]] = a.get("content")
        elif tag == "button" and "data-theme-toggle" in a:
            self.toggles.append(a)

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


@pytest.fixture
def pages(engine, admin):
    crop = make_crop(engine)
    station = make_station(engine, crop_id=crop)
    make_batch(engine, station, crop)
    anon = _client()
    out = {"/login": anon.get("/login")}
    c = _client()
    _login(c)
    _csrf(c.get("/admin").text)  # consume the login flash
    for path in GET_PATHS + ["/admin/stations/99/edit"]:  # last one renders the friendly error page
        out[path] = c.get(path)
    return out


def test_every_page_loads_theme_init_before_any_stylesheet(pages):
    for path, r in pages.items():
        assert r.status_code in (200, 404), path
        p = _Page()
        p.feed(r.text)
        kinds = [k for k, _, _ in p.head]
        assert p.head and p.head[0][:2] == ("script", "/static/js/theme-init.js"), (path, p.head)
        init = p.head[0][2]
        assert "defer" not in init and "async" not in init, "theme-init must run before first paint"
        assert "css" in kinds and any(u == "/static/css/nexxa.css" for _, u, _ in p.head), path
        assert any(u == "/static/js/theme-toggle.js" for _, u, _ in p.head), path
        assert p.metas.get("color-scheme") == "light dark", path


def test_no_inline_script_or_style_on_any_page(pages):
    for path, r in pages.items():
        p = _Page()
        p.feed(r.text)
        assert p.inline_scripts == 0 and p.style_tags == 0 and p.style_attrs == 0, path
        assert "unsafe-inline" not in r.headers["content-security-policy"]


def test_every_page_has_one_accessible_theme_toggle(pages):
    for path, r in pages.items():
        p = _Page()
        p.feed(r.text)
        assert len(p.toggles) == 1, path
        t = p.toggles[0]
        assert t.get("type") == "button" and t.get("aria-pressed") in ("true", "false"), path
        assert THAI.search(t.get("aria-label") or ""), path


def test_login_page_keeps_its_behaviour_hooks(pages):
    html = pages["/login"].text
    assert "ยินดีต้อนรับ" in html
    assert 'name="csrf_token"' in html and 'name="next"' in html and 'data-toggle-password="f-password"' in html
    assert 'src="/static/img/login-fern.webp"' in html and 'alt=""' in html


# --- CSS tokens ---------------------------------------------------------------

def _blocks():
    return re.findall(r"([^{}]+)\{([^{}]*)\}", CSS)


def _vars(body):
    return {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def _theme(name):
    found = [body for sel, body in _blocks() if f'[data-theme="{name}"]' in sel and "--" in body]
    assert len(found) == 1, f"expected one [data-theme={name}] block"
    return _vars(found[0])


ROOT = _vars("".join(body for sel, body in _blocks() if sel.strip() == ":root"))


def test_both_themes_define_the_same_variables():
    LIGHT, DARK = _theme("light"), _theme("dark")
    assert len(LIGHT) >= 40
    assert set(LIGHT) == set(DARK), set(LIGHT) ^ set(DARK)
    for name in ("--color-accent", "--color-foreground", "--color-card", "--glass-bg", "--color-muted-foreground"):
        assert name in LIGHT  # names the existing templates/components rely on
    assert not set(LIGHT) & set(ROOT), "colour tokens live only in the theme blocks"


def test_components_never_hard_code_colours():
    rest = CSS
    for sel, body in _blocks():
        if "[data-theme=" in sel and "--" in body:
            rest = rest.replace(body, "")
    found = re.findall(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", rest)
    assert not found, found


# --- contrast (WCAG 2.x) --------------------------------------------------------

BLACK, WHITE = (0.0, 0.0, 0.0, 1.0), (255.0, 255.0, 255.0, 1.0)


def _color(value, t):
    value = value.strip()
    m = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if m:
        return _color(t[m.group(1)], t)
    if value.startswith("#"):
        h = value[1:]
        h = "".join(ch * 2 for ch in h) if len(h) == 3 else h
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+))?\s*\)", value)
    assert m, f"cannot parse colour {value!r}"
    return (float(m[1]), float(m[2]), float(m[3]), float(m[4]) if m[4] else 1.0)


def _over(top, bottom):
    a = top[3]
    return tuple(top[i] * a + bottom[i] * (1 - a) for i in range(3)) + (1.0,)


def _lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def contrast(fg, bg):
    fg = _over(fg, bg)
    hi, lo = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_function_matches_wcag_reference_values():
    assert round(contrast(BLACK, WHITE), 2) == 21.0
    assert round(contrast(_color("#777777", {}), WHITE), 2) == 4.48
    assert round(contrast(WHITE, _color("#15803D", {})), 2) == 5.02


def _tint(color, pct):
    return color[:3] + (float(pct.rstrip("%")) / 100,)


def _pairs(mode):
    """Every text/background pair, with the photo replaced by its worst-case pixel (black under dark text
    in light mode, white under light text in dark mode) so the result holds at any background position."""
    t = _theme(mode)
    col = lambda name: _color(t[name], t)  # noqa: E731
    ground = _over(col("--bg-scrim"), BLACK if mode == "light" else WHITE)
    sheet = _over(col("--glass-bg-soft"), ground)          # admin content sheet
    card = _over(col("--glass-bg"), sheet)                 # cards and tables inside the sheet
    panel = _over(col("--glass-bg-strong"), ground)        # sidebar, topbar, login form side, toasts
    surfaces = {"sheet": sheet, "card": card, "panel": panel}
    pairs = []
    for sname, surf in surfaces.items():
        for text in ("--color-foreground", "--color-muted-foreground", "--color-subtle-foreground", "--color-accent-strong"):
            pairs.append((f"{text} on {sname}", col(text), surf))
    for text in ("--color-danger-fg", "--color-ok-fg", "--color-warn-fg", "--color-info-fg"):
        pairs.append((f"{text} on card", col(text), card))
    field = _over(col("--input-bg"), card)
    pairs += [("input text", col("--color-foreground"), field), ("placeholder", col("--color-placeholder"), field),
              ("login input text", col("--color-foreground"), _over(col("--input-bg"), panel)),
              ("table head", col("--color-muted-foreground"), _over(col("--table-head-bg"), card)),
              ("nav current", col("--color-foreground"), _over(col("--nav-active-bg"), panel))]
    tones = {"ok": "--color-accent", "warn": "--color-warn", "danger": "--color-destructive",
             "info": "--color-info", "neutral": "--color-neutral"}
    for kind, tone in tones.items():
        fg = col(f"--color-{kind}-fg")
        for sname in ("card", "panel"):
            pairs.append((f"badge {kind} on {sname}", fg, _over(_tint(col(tone), ROOT["--tint-badge"]), surfaces[sname])))
        if kind != "neutral":
            for base in ("panel", "sheet"):  # alerts: tint over a strong-glass base, on the login panel or the sheet
                alert_bg = _over(_tint(col(tone), ROOT["--tint-alert"]), _over(col("--glass-bg-strong"), surfaces[base]))
                pairs += [(f"alert {kind} title on {base}", fg, alert_bg), (f"alert {kind} text on {base}", col("--color-subtle-foreground"), alert_bg)]
    pairs += [("primary button top", col("--color-on-accent"), col("--color-accent")),
              ("primary button bottom", col("--color-on-accent"), col("--color-accent-deep")),
              ("danger button hover", col("--color-on-destructive"), col("--color-destructive")),
              ("code", col("--color-code-fg"), _over(col("--color-deep"), card)),
              ("login art text", col("--art-text"), _over(col("--art-scrim"), WHITE))]
    return pairs


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_text_contrast_is_aa_in_both_modes(mode):
    failures = [(name, round(contrast(fg, bg), 2)) for name, fg, bg in _pairs(mode) if contrast(fg, bg) < 4.5]
    assert not failures, failures


# --- images and scripts ----------------------------------------------------------

def test_every_image_is_credited_light_and_decodes():
    credits = (STATIC / "img" / "CREDITS.md").read_text(encoding="utf-8")
    images = [p for p in (STATIC / "img").iterdir() if p.name != "CREDITS.md"]
    assert {p.name for p in images} >= {"foliage-bg.webp", "login-fern.webp", "mark.svg"}
    for p in images:
        line = next((ln for ln in credits.splitlines() if p.name in ln), "")
        assert re.search(r"CC0|CC BY|Public domain|own work", line, re.I), f"{p.name}: no licence in CREDITS.md"
        assert re.search(r"https://commons\.wikimedia\.org/|own work", line, re.I), f"{p.name}: no source"
        assert p.stat().st_size <= IMG_CAP, p.name
        if p.suffix == ".webp":
            assert cv2.imread(str(p)) is not None, p.name


def test_referenced_static_files_exist():
    refs = re.findall(r'url\("?(/static/[^")]+)', CSS)
    assert refs
    for ref in refs:
        assert (STATIC / ref.removeprefix("/static/")).is_file(), ref


def test_theme_scripts_are_safe_and_valid():
    for name in JS_FILES:
        src = (STATIC / "js" / name).read_text(encoding="utf-8")
        assert "innerHTML" not in src and "eval(" not in src, name
    for name in ("theme-init.js", "theme-toggle.js"):
        src = (STATIC / "js" / name).read_text(encoding="utf-8")
        assert "nexxa-theme" in src and "localStorage" in src and "try" in src and "catch" in src, name
    init = (STATIC / "js" / "theme-init.js").read_text(encoding="utf-8")
    assert "prefers-color-scheme" in init and "data-theme" in init
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    for name in JS_FILES:
        r = subprocess.run([node, "--check", str(STATIC / "js" / name)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
