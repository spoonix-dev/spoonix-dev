#!/usr/bin/env python3
"""
render_cards.py - Windows XP (Luna / Olive Green) cards for the Spoonix GitHub profile.

Zero dependencies: Python 3.11+ standard library only, so GitHub Actions never needs pip.

Writes, into ./cards:
    hero.svg  heatmap.svg  tile-repos.svg  tile-contribs.svg  tile-stars.svg  activity.svg

Environment variables (all optional)
    GITHUB_TOKEN   raises the REST rate limit (GitHub Actions provides it automatically)
    GH_PAT         personal access token -> use the GraphQL contribution calendar
    STATIC=1       render the final state immediately (no animation at all)
    DEMO=1         offline preview with synthetic data (no network needed)
    AVATAR=github  fetch the GitHub avatar instead of using assets/spoonix.png
    CARDS_DIR      output directory override (default: <repo>/cards)

The output is deterministic for a given set of data (no timestamps), so the workflow only
commits when something actually changed.
"""
import base64
import datetime as dt
import html
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

# ----------------------------------------------------------------------------- identity
USER = "spoonix-dev"
NAME = "Spoonix"
ROLE = "Open-source developer • Python & Go"
BIO = "Building AI proxies, system monitors & developer tools."

# ----------------------------------------------------------------------------- config
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.environ.get("CARDS_DIR") or os.path.join(ROOT, "cards")
AVATAR_FILE = os.path.join(ROOT, "assets", "spoonix.png")
AVATAR_URL = f"https://avatars.githubusercontent.com/{USER}?s=240"
REPOS_URL = f"https://api.github.com/users/{USER}/repos?per_page=100&type=owner"
CONTRIB_URL = f"https://github.com/users/{USER}/contributions"
GRAPHQL_URL = "https://api.github.com/graphql"
UA = f"spoonix-xp-cards/1.0 (+https://github.com/{USER}/{USER})"

STATIC = os.environ.get("STATIC") == "1"
DEMO = os.environ.get("DEMO") == "1"
GH_PAT = os.environ.get("GH_PAT") or None
GH_TOKEN = os.environ.get("GITHUB_TOKEN") or None

# Animation start (seconds) per card, top to bottom in the README.
STAGGER = {"hero": 0.0, "heatmap": 0.45, "tiles": 2.0, "activity": 2.6}

# ----------------------------------------------------------------------------- palette
TB = ("#87d457", "#4da329", "#357d19", "#22570e")      # Luna green title bar, light -> dark
BODY, PANEL, BORDER = "#ece9d8", "#f7f5ec", "#919b9c"
INK, MUTED, DARK = "#1d2b14", "#5b5a4c", "#1b5e20"
LEVELS = ["#e4e1d0", "#9be9a8", "#40c463", "#30a14e", "#1b5e20"]
FONT_UI = 'Tahoma,"Segoe UI",Verdana,Geneva,sans-serif'
FONT_MONO = 'Consolas,"Lucida Console","Courier New",monospace'
MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
F = 4  # window frame width

esc = html.escape


# ============================================================================= data
def http(url, data=None, headers=None, tries=3, timeout=30):
    """GET (or POST when data is given) with retries. Returns (body_bytes, headers)."""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    hdrs.update(headers or {})
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (400, 401, 403, 404, 422):
                break  # retrying will not help
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed for {url}: {last}")


def api_headers(token):
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_repo_stats():
    """(public repo count, stars earned on non-fork repos)."""
    repos = []
    for page in range(1, 11):
        url = f"{REPOS_URL}&page={page}"
        try:
            body, _ = http(url, headers=api_headers(GH_TOKEN))
        except RuntimeError:
            if not GH_TOKEN:
                raise
            body, _ = http(url, headers=api_headers(None))  # token rejected -> try anonymously
        chunk = json.loads(body)
        if not isinstance(chunk, list):
            raise RuntimeError(f"unexpected repos payload: {str(chunk)[:140]}")
        repos.extend(chunk)
        if len(chunk) < 100:
            break
    stars = sum(int(r.get("stargazers_count") or 0) for r in repos if not r.get("fork"))
    return len(repos), stars


GQL = (
    "query($login:String!,$from:DateTime!,$to:DateTime!){user(login:$login){"
    "contributionsCollection(from:$from,to:$to){contributionCalendar{weeks{"
    "contributionDays{date contributionCount contributionLevel}}}}}}"
)
LEVEL_ENUM = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2,
              "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


def fetch_contrib_graphql():
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=364)).replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    payload = json.dumps({"query": GQL, "variables": {
        "login": USER, "from": start.strftime(fmt), "to": now.strftime(fmt)}}).encode()
    body, _ = http(GRAPHQL_URL, data=payload, headers={
        "Authorization": f"Bearer {GH_PAT}", "Content-Type": "application/json"})
    res = json.loads(body)
    user = ((res.get("data") or {}).get("user"))
    if res.get("errors") or not user:
        raise RuntimeError(f"GraphQL error: {str(res.get('errors'))[:160]}")
    days = {}
    for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]:
        for d in week["contributionDays"]:
            days[dt.date.fromisoformat(d["date"])] = (
                int(d["contributionCount"]), LEVEL_ENUM.get(d.get("contributionLevel")))
    return days


ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')
TIP_RE = re.compile(r"\s*(No|[\d,]+)\s+contributions?\b", re.I)
LEVEL_GUESS = {0: 0, 1: 1, 2: 4, 3: 8, 4: 12}


def parse_contrib_html(page):
    """Parse github.com/users/<u>/contributions -> {date: (count, level)}.

    Current markup:  <td data-date="2025-11-23" data-level="2" id="contribution-day-component-0-3">
                     <tool-tip for="contribution-day-component-0-3">3 contributions on November 23rd.</tool-tip>
    Legacy markup:   <rect data-count="3" data-date="2025-11-23" data-level="2">
    """
    tips = {}
    for m in re.finditer(r"<tool-tip\b([^>]*)>(.*?)</tool-tip>", page, re.S):
        attrs = dict(ATTR_RE.findall(m.group(1)))
        text = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
        hit = TIP_RE.match(text)
        if hit and attrs.get("for"):
            tips[attrs["for"]] = 0 if hit.group(1).lower() == "no" else int(hit.group(1).replace(",", ""))
    days, guessed = {}, 0
    for m in re.finditer(r"<(?:td|rect)\b([^>]*)>", page):
        a = dict(ATTR_RE.findall(m.group(1)))
        if "data-date" not in a:
            continue
        level = int(a["data-level"]) if a.get("data-level", "").isdigit() else None
        if a.get("data-count", "").isdigit():
            count = int(a["data-count"])
        elif a.get("id") in tips:
            count = tips[a["id"]]
        else:
            count, guessed = LEVEL_GUESS.get(level or 0, 0), guessed + 1
        days[dt.date.fromisoformat(a["data-date"])] = (count, level)
    if len(days) < 300:
        raise RuntimeError(f"contribution page looked wrong ({len(days)} day cells found)")
    if guessed:
        print(f"warn: {guessed} day(s) had no tooltip; counts estimated from level", file=sys.stderr)
    return days


def fetch_contributions():
    if GH_PAT:
        try:
            return fetch_contrib_graphql()
        except Exception as exc:  # noqa: BLE001 - fall back to the public page
            print(f"warn: GraphQL failed ({exc}); falling back to the public page", file=sys.stderr)
    body, _ = http(CONTRIB_URL, headers={"Accept": "text/html"})
    return parse_contrib_html(body.decode("utf-8", "replace"))


def demo_data():
    rnd = random.Random(7)
    end = dt.date.today()
    days = {}
    for i in range(371):
        d = end - dt.timedelta(days=i)
        quiet = 0.35 if d.weekday() < 5 else 0.62
        c = 0 if rnd.random() < quiet else rnd.choice([1, 1, 2, 3, 4, 6, 9, 14])
        if d.month in (3, 4):
            c = int(c * 1.8)  # a busy stretch so the peak month stands out
        days[d] = (c, None)
    return 14, 37, days


def sniff_mime(blob):
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


def avatar_data_uri():
    """assets/spoonix.png first, then the GitHub avatar. None -> vector fallback."""
    blob = None
    if os.environ.get("AVATAR") != "github" and os.path.isfile(AVATAR_FILE):
        with open(AVATAR_FILE, "rb") as fh:
            blob = fh.read()
    if blob is None and not DEMO:
        try:
            blob, _ = http(AVATAR_URL, tries=2)
        except RuntimeError as exc:
            print(f"warn: avatar fetch failed ({exc}); using vector fallback", file=sys.stderr)
    mime = sniff_mime(blob) if blob else None
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}" if mime else None


# ============================================================================= analysis
def week_start(d):
    """Sunday on or before d (GitHub weeks start on Sunday)."""
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def build_window(days):
    """The trailing 365 days GitHub calls 'the last year', laid out as Sunday-first week columns."""
    end = max(days)
    start = end - dt.timedelta(days=364)
    counts = [days.get(start + dt.timedelta(days=i), (0, None)) for i in range(365)]
    top = max((c for c, _ in counts), default=0)
    series = []
    for i, (c, lvl) in enumerate(counts):
        if lvl is None:
            lvl = 0 if c == 0 or top == 0 else min(4, max(1, math.ceil(4 * c / top)))
        series.append((start + dt.timedelta(days=i), c, lvl))
    by_date = {d: (d, c, l) for d, c, l in series}
    first_col, last_col = week_start(start), week_start(end)
    ncols = (last_col - first_col).days // 7 + 1
    grid = []
    for col in range(ncols):
        grid.append([by_date.get(first_col + dt.timedelta(days=7 * col + row)) for row in range(7)])
    return {"start": start, "end": end, "series": series, "grid": grid}


def compute_stats(win):
    series = win["series"]
    total = sum(c for _, c, _ in series)
    active = sum(1 for _, c, _ in series if c > 0)
    best, run, run_start = (0, None, None), 0, None
    for d, c, _ in series:
        if c > 0:
            if run == 0:
                run_start = d
            run += 1
            if run > best[0]:
                best = (run, run_start, d)
        else:
            run = 0
    busiest = max(series, key=lambda x: (x[1], x[0]))
    sums = {}
    for d, c, _ in series:
        sums[(d.year, d.month)] = sums.get((d.year, d.month), 0) + c
    idx = win["end"].year * 12 + win["end"].month - 1
    months = []
    for i in range(11, -1, -1):
        year, m0 = divmod(idx - i, 12)
        months.append((year, m0 + 1, sums.get((year, m0 + 1), 0)))
    return {"total": total, "active": active, "days": len(series), "streak": best,
            "busiest": busiest, "months": months}


def short_date(d):
    return f"{MON[d.month - 1]} {d.day}"


def nice_axis(peak):
    """(axis_max, step) with four equal intervals and whole-number labels."""
    if peak <= 0:
        return 4, 1
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000):
        if step * 4 >= peak:
            return step * 4, step
    step = 10 ** math.ceil(math.log10(peak / 4))
    return step * 4, step


# ============================================================================= svg basics
def n(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def A(cls, delay=0.0):
    """class + animation-delay attributes; nothing at all in STATIC mode."""
    return "" if STATIC else f' class="{cls}" style="animation-delay:{n(delay)}s"'


def T(x, y, s, size=12, fill=INK, weight=None, anchor=None, cls=None, extra=""):
    wt = f' font-weight="{weight}"' if weight else ""
    an = f' text-anchor="{anchor}"' if anchor else ""
    c = f' class="{cls}"' if cls else ""
    return (f'<text x="{n(x)}" y="{n(y)}" font-size="{n(size)}" fill="{fill}"{wt}{an}{c}{extra}>'
            f"{esc(str(s))}</text>")


KEYFRAMES = (
    "@keyframes rise{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:translateY(0)}}"
    "@keyframes fade{from{opacity:0}to{opacity:1}}"
    "@keyframes pulse{0%{transform:scale(.7);opacity:.8}100%{transform:scale(2.5);opacity:0}}"
    "@keyframes shine{0%,55%{transform:translateX(0)}100%{transform:translateX(480px)}}"
    "@keyframes swing{0%{opacity:0;transform:translateY(30px) rotate(-9deg)}"
    "55%{opacity:1;transform:translateY(-5px) rotate(2deg)}"
    "80%{transform:translateY(1px) rotate(-.6deg)}100%{opacity:1;transform:translateY(0) rotate(0)}}"
    "@keyframes colin{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}"
    "@keyframes grow{from{transform:scaleY(0)}to{transform:scaleY(1)}}"
    "@keyframes nudge{0%,100%{transform:translateX(0)}50%{transform:translateX(5px)}}"
    ".rise{animation:rise .6s cubic-bezier(.2,.8,.2,1) both}"
    ".fade{animation:fade .5s ease-out both}"
    ".halo{transform-box:fill-box;transform-origin:center;animation:pulse 1.9s ease-out infinite}"
    ".shine{animation:shine 5.5s ease-in-out infinite}"
    ".swing{transform-box:fill-box;transform-origin:0% 100%;animation:swing .9s cubic-bezier(.2,.8,.3,1) both}"
    ".col{animation:colin .45s ease-out both}"
    ".bar{transform-box:fill-box;transform-origin:50% 100%;animation:grow .9s cubic-bezier(.2,.8,.2,1) both}"
    ".arrow{animation:nudge 1.1s ease-in-out infinite}"
    ".cs{display:none}"
    "@media (prefers-reduced-motion:reduce){*{animation:none!important}.cf{display:none}.cs{display:inline}}"
)


def style_block():
    base = f"text{{font-family:{FONT_UI}}}.mono{{font-family:{FONT_MONO}}}"
    return f"<style>{base}</style>" if STATIC else f"<style>{base}{KEYFRAMES}</style>"


def defs():
    t0, t1, t2, t3 = TB
    return (
        "<defs>"
        f'<linearGradient id="tb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{t0}"/>'
        f'<stop offset=".22" stop-color="{t1}"/><stop offset=".62" stop-color="{t2}"/>'
        f'<stop offset="1" stop-color="{t3}"/></linearGradient>'
        '<linearGradient id="gb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#b4e68a"/>'
        '<stop offset=".5" stop-color="#5db535"/><stop offset="1" stop-color="#3a8a1e"/></linearGradient>'
        '<linearGradient id="rb" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ec8b73"/>'
        '<stop offset=".5" stop-color="#dc4426"/><stop offset="1" stop-color="#b03216"/></linearGradient>'
        '<linearGradient id="st" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7bd95e"/>'
        '<stop offset=".5" stop-color="#3f9d2b"/><stop offset="1" stop-color="#2a7a1a"/></linearGradient>'
        '<linearGradient id="bn" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff"/>'
        '<stop offset=".85" stop-color="#ece9d8"/><stop offset="1" stop-color="#d6d0c0"/></linearGradient>'
        '<linearGradient id="tg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4da329"/>'
        '<stop offset="1" stop-color="#1b5e20"/></linearGradient>'
        '<linearGradient id="sg" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
        '<stop offset=".5" stop-color="#fff" stop-opacity=".85"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
        '<linearGradient id="bar" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5fd47a"/>'
        '<stop offset="1" stop-color="#30a14e"/></linearGradient>'
        '<linearGradient id="barp" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2e7d32"/>'
        '<stop offset="1" stop-color="#1b5e20"/></linearGradient>'
        '<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2f6fd0"/>'
        '<stop offset="1" stop-color="#a9d4ff"/></linearGradient>'
        '<radialGradient id="led" cx=".35" cy=".3" r=".8"><stop offset="0" stop-color="#e6ffd9"/>'
        '<stop offset=".45" stop-color="#40c463"/><stop offset="1" stop-color="#1b5e20"/></radialGradient>'
        "</defs>"
    )


def svg_doc(w, h, title, desc, body):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-labelledby="ttl dsc">\n'
        f'<title id="ttl">{esc(title)}</title><desc id="dsc">{esc(desc)}</desc>\n'
        f"{style_block()}\n{defs()}\n{body}\n</svg>\n"
    )


# ============================================================================= xp widgets
ICON_SPOON = ('<ellipse cx="8" cy="5.2" rx="4" ry="5" fill="#f4f7ef" stroke="#22570e"/>'
              '<path d="M8 10.2V15" stroke="#22570e" stroke-width="3" stroke-linecap="round"/>'
              '<path d="M8 10.2V15" stroke="#f4f7ef" stroke-width="1.6" stroke-linecap="round"/>')
ICON_GRID = "".join(
    f'<rect x="{1 + 5 * i}" y="{1 + 5 * j}" width="4" height="4" fill="{LEVELS[(i * 2 + j) % 5]}" stroke="#22570e" stroke-width=".5"/>'
    for i in range(3) for j in range(3))
ICON_EXE = ('<rect x="1" y="2" width="14" height="12" fill="#f4f3ee" stroke="#22570e"/>'
            '<rect x="1" y="2" width="14" height="4" fill="url(#tb)"/>'
            '<rect x="3.5" y="8" width="9" height="1.6" fill="#40c463"/><rect x="3.5" y="11" width="6" height="1.6" fill="#9be9a8"/>')
ICON_BARS = "".join(
    f'<rect x="{2 + 4 * i}" y="{15 - h}" width="3" height="{h}" fill="#40c463" stroke="#22570e" stroke-width=".5"/>'
    for i, h in enumerate((6, 10, 7, 13)))


def chrome(w, h, title, icon="", t=30, bw=21, fs=13):
    """Luna window frame: rounded green title bar, beige body, min / max / close buttons."""
    r, k = 8, bw / 21
    o = [
        f'<path d="M0.5 {n(h - .5)}V{r}Q0.5 0.5 {r} 0.5H{n(w - r)}Q{n(w - .5)} 0.5 {n(w - .5)} {r}V{n(h - .5)}Z" '
        'fill="#357d19" stroke="#22570e"/>',
        f'<path d="M1 {t}V{r}Q1 1 {r} 1H{n(w - r)}Q{n(w - 1)} 1 {n(w - 1)} {r}V{t}Z" fill="url(#tb)"/>',
        f'<path d="M{r + 2} 1.5H{n(w - r - 2)}" stroke="#fff" stroke-opacity=".35"/>',
        f'<rect x="{F}" y="{t}" width="{w - 2 * F}" height="{h - t - F}" fill="{BODY}"/>',
    ]
    tx = 10
    if icon:
        o.append(f'<g transform="translate(8 {n((t - 16) / 2)})">{icon}</g>')
        tx = 30
    ty = t / 2 + fs * 0.36
    o.append(T(tx + 1, ty + 1, title, fs, "#0a2c05", "700", extra=' fill-opacity=".6"'))
    o.append(T(tx, ty, title, fs, "#fff", "700"))
    by = (t - bw) / 2
    xc = w - 6 - bw
    xm = xc - bw - 2
    xn = xm - bw - 2
    for x in (xn, xm):
        o.append(f'<rect x="{n(x)}" y="{n(by)}" width="{bw}" height="{bw}" rx="{n(3 * k)}" '
                 'fill="url(#gb)" stroke="#fff" stroke-width="1.2"/>')
    o.append(f'<rect x="{n(xc)}" y="{n(by)}" width="{bw}" height="{bw}" rx="{n(3 * k)}" '
             'fill="url(#rb)" stroke="#fff" stroke-width="1.2"/>')
    o.append(f'<rect x="{n(xn + 5 * k)}" y="{n(by + 14 * k)}" width="{n(8 * k)}" height="{n(3 * k)}" fill="#fff"/>')
    o.append(f'<rect x="{n(xm + 5 * k)}" y="{n(by + 5 * k)}" width="{n(10 * k)}" height="{n(10 * k)}" '
             f'fill="none" stroke="#fff" stroke-width="{n(1.4 * k)}"/>'
             f'<rect x="{n(xm + 5 * k)}" y="{n(by + 5 * k)}" width="{n(10 * k)}" height="{n(3 * k)}" fill="#fff"/>')
    o.append(f'<path d="M{n(xc + 6 * k)} {n(by + 6 * k)}L{n(xc + 15 * k)} {n(by + 15 * k)}'
             f'M{n(xc + 15 * k)} {n(by + 6 * k)}L{n(xc + 6 * k)} {n(by + 15 * k)}" '
             f'stroke="#fff" stroke-width="{n(2.2 * k)}" stroke-linecap="round"/>')
    return "".join(o)


def menubar(w, y, items):
    o = [f'<rect x="{F}" y="{y}" width="{w - 2 * F}" height="22" fill="{BODY}"/>',
         f'<path d="M{F} {y + 22.5}H{w - F}" stroke="#d3cfbf"/>']
    x = 14
    for item in items:
        o.append(T(x, y + 15, item, 11.5, "#000"))
        x += 14 + len(item) * 6.4
    return "".join(o)


def sunken(x, y, w, h, fill=PANEL):
    return (f'<rect x="{n(x + .5)}" y="{n(y + .5)}" width="{n(w - 1)}" height="{n(h - 1)}" rx="2" '
            f'fill="{fill}" stroke="{BORDER}"/>'
            f'<path d="M{n(x + 1.5)} {n(y + h - 1.5)}V{n(y + 1.5)}H{n(x + w - 1.5)}" fill="none" '
            'stroke="#716f64" stroke-opacity=".28"/>')


def groupbox(x, y, w, h, label):
    lw = len(label) * 6.3 + 8
    return (f'<rect x="{n(x + .5)}" y="{n(y + .5)}" width="{n(w - 1)}" height="{n(h - 1)}" rx="4" fill="none" stroke="#d0d0bf"/>'
            f'<rect x="{n(x + 1.5)}" y="{n(y + 1.5)}" width="{n(w - 1)}" height="{n(h - 1)}" rx="4" fill="none" stroke="#fff"/>'
            f'<rect x="{n(x + 9)}" y="{n(y - 6)}" width="{n(lw)}" height="14" fill="{BODY}"/>'
            + T(x + 13, y + 4.5, label, 11.5, "#22570e"))


def seg(x, y, w, h):
    return (f'<path d="M{x} {y + h}V{y}H{x + w}" fill="none" stroke="#aca899"/>'
            f'<path d="M{x} {y + h}H{x + w}V{y}" fill="none" stroke="#fff"/>')


def tab_shape(x, y, w, h, fill):
    return (f'<path d="M{n(x + .5)} {n(y + h)}V{n(y + 3)}Q{n(x + .5)} {n(y + .5)} {n(x + 3)} {n(y + .5)}'
            f'H{n(x + w - 3)}Q{n(x + w - .5)} {n(y + .5)} {n(x + w - .5)} {n(y + 3)}V{n(y + h)}" '
            f'fill="{fill}" stroke="{BORDER}"/>')


def tabs(x, y, pw, ph, specs, active):
    """Tab strip sitting on a panel whose top-left corner is (x, y)."""
    o = [f'<rect x="{x + .5}" y="{y + .5}" width="{pw - 1}" height="{ph - 1}" fill="{PANEL}" stroke="{BORDER}"/>']
    spans, cx = [], x
    for label, w in specs:
        spans.append((cx, w, label))
        cx += w
    for i, (tx, w, label) in enumerate(spans):
        if i != active:
            o.append(tab_shape(tx, y - 20, w, 20, "#e7e4d3"))
            o.append(T(tx + w / 2, y - 6, label, 11.5, "#2a2a22", anchor="middle"))
    tx, w, label = spans[active]
    o.append(tab_shape(tx - 2, y - 24, w + 4, 25, PANEL))
    o.append(f'<rect x="{tx - 1}" y="{y - .5}" width="{w + 2}" height="2" fill="{PANEL}"/>')
    o.append(f'<path d="M{tx - 1} {y - 23}H{tx + w + 1}" stroke="#e68b2c" stroke-width="2"/>')
    o.append(T(tx + w / 2, y - 8, label, 11.5, "#000", anchor="middle"))
    return "".join(o)


def push_button(x, y, w, h, label, default=False):
    edge = "#1e4d12" if default else "#4a5a3c"
    sw = 1.8 if default else 1
    return (f'<rect x="{x + .5}" y="{y + .5}" width="{w - 1}" height="{h - 1}" rx="3" fill="url(#bn)" '
            f'stroke="{edge}" stroke-width="{sw}"/>' + T(x + w / 2, y + h / 2 + 4, label, 11.5, "#000", anchor="middle"))


def count_text(final, x, y, size, begin, fill, dur=1.3):
    """Big number that counts 0 -> final with SMIL <set> frames (one text node per value)."""
    attrs = f'x="{n(x)}" y="{n(y)}" text-anchor="middle" font-size="{n(size)}" font-weight="700" fill="{fill}"'
    if STATIC:
        return f"<text {attrs}>{final:,}</text>"
    steps = max(2, min(30, final))
    vals = []
    for i in range(steps + 1):
        v = round(final * (1 - (1 - i / steps) ** 3))  # ease-out
        if not vals or v != vals[-1]:
            vals.append(v)
    if vals[-1] != final:
        vals.append(final)
    if len(vals) == 1:
        return f"<text {attrs}>{final:,}</text>"
    dt_ = dur / len(vals)
    frames = []
    for i, v in enumerate(vals):
        if i == 0:
            timing = f'begin="0s" dur="{n(begin + dt_)}s"'
        elif i == len(vals) - 1:
            timing = f'begin="{n(begin + i * dt_)}s" fill="freeze"'
        else:
            timing = f'begin="{n(begin + i * dt_)}s" dur="{n(dt_)}s"'
        frames.append(f'<text {attrs} visibility="hidden"><set attributeName="visibility" to="visible" {timing}/>{v:,}</text>')
    return f'<g class="cf">{"".join(frames)}</g><text class="cs" {attrs}>{final:,}</text>'


# ============================================================================= cards
def hero_card(avatar):
    W, H = 880, 316
    b = STAGGER["hero"]
    s = [f"<g{A('fade', b)}>", chrome(W, H, f"{NAME} \u2014 System Properties", ICON_SPOON)]
    s.append(tabs(14, 58, 852, 216, [("General", 64), ("Computer Name", 104), ("Hardware", 72), ("Advanced", 72)], 0))

    # start button + status badge
    sx, sy = 28, 76
    start = (
        f'<path d="M{sx + 5} {sy}H{sx + 83}A15 15 0 0 1 {sx + 83} {sy + 30}H{sx + 5}A5 5 0 0 1 {sx} {sy + 25}'
        f'V{sy + 5}A5 5 0 0 1 {sx + 5} {sy}Z" fill="url(#st)" stroke="#1a5a0d"/>'
        f'<rect x="{sx + 4}" y="{sy + 2}" width="76" height="11" rx="5" fill="#fff" fill-opacity=".22"/>'
        f'<g transform="translate({sx + 9} {sy + 7}) scale(.95)" fill="none">'
        '<ellipse cx="8" cy="5.2" rx="4" ry="5" fill="#fff"/><path d="M8 10V15.5" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></g>'
        + T(sx + 55, sy + 21, "start", 19, "#0f3a08", "700", anchor="middle", extra=' font-style="italic" fill-opacity=".55"')
        + T(sx + 54, sy + 20, "start", 19, "#fff", "700", anchor="middle", extra=' font-style="italic"')
    )
    bx = 138
    halo = "" if STATIC else f'<circle{A("halo", b + 0.8)} cx="{bx + 20}" cy="{sy + 15}" r="5" fill="#40c463" opacity="0"/>'
    badge = (
        f'<rect x="{bx + .5}" y="{sy + .5}" width="214" height="29" rx="14.5" fill="{PANEL}" stroke="{BORDER}"/>'
        f'<path d="M{bx + 8} {sy + 2}H{bx + 206}" stroke="#716f64" stroke-opacity=".25"/>'
        + halo
        + f'<circle cx="{bx + 20}" cy="{sy + 15}" r="5.5" fill="url(#led)" stroke="#1b5e20" stroke-width=".8"/>'
        + T(bx + 34, sy + 19.5, f"@{USER} \u2022 Active", 12.5, "#1d2b14", "700", cls="mono")
    )
    s.append(f"<g{A('rise', b + 0.25)}>{start}{badge}</g>")

    # title with periodic shine, role, bio
    tx, ty = 30, 178
    title = (T(tx + 2, ty + 3, NAME, 74, "#22570e", "700", extra=' letter-spacing="-1" fill-opacity=".22"')
             + T(tx, ty, NAME, 74, "url(#tg)", "700", extra=' letter-spacing="-1"'))
    if not STATIC:
        title += (f'<clipPath id="tclip"><text x="{tx}" y="{ty}" font-size="74" font-weight="700" letter-spacing="-1">{NAME}</text></clipPath>'
                  f'<g clip-path="url(#tclip)"><g{A("shine", b + 1.4)}>'
                  f'<polygon points="{tx - 140},{ty - 70} {tx - 90},{ty - 70} {tx - 112},{ty + 12} {tx - 162},{ty + 12}" fill="url(#sg)"/></g></g>')
    s.append(f"<g{A('rise', b + 0.4)}>{title}</g>")
    s.append(f"<g{A('rise', b + 0.55)}>{T(tx, 214, ROLE, 19, '#357d19', '700')}</g>")
    s.append(f"<g{A('rise', b + 0.7)}>{T(tx, 240, BIO, 13, MUTED)}</g>")

    # picture box: Spoonix.bmp
    px, py, pw, ph = 650, 68, 200, 200
    box = [f'<rect x="{px + .5}" y="{py + .5}" width="{pw - 1}" height="{ph - 1}" rx="4" fill="{BODY}" stroke="#22570e"/>',
           f'<path d="M{px + .5} {py + 20}V{py + 4.5}Q{px + .5} {py + .5} {px + 4.5} {py + .5}H{px + pw - 4.5}'
           f'Q{px + pw - .5} {py + .5} {px + pw - .5} {py + 4.5}V{py + 20}Z" fill="url(#tb)"/>',
           f'<rect x="{px + pw - 19}" y="{py + 3}" width="14" height="14" rx="2.5" fill="url(#rb)" stroke="#fff"/>',
           f'<path d="M{px + pw - 15} {py + 7}L{px + pw - 9} {py + 13}M{px + pw - 9} {py + 7}L{px + pw - 15} {py + 13}" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>',
           f'<g transform="translate({px + 5} {py + 2}) scale(.9)">{ICON_EXE}</g>',
           T(px + 25, py + 14.5, "Spoonix.bmp", 11, "#fff", "700")]
    ix, iy, isz = px + 18, py + 30, 164
    box.append(f'<rect x="{ix - 2.5}" y="{iy - 2.5}" width="{isz + 5}" height="{isz + 5}" fill="#fff" stroke="{BORDER}"/>')
    if avatar:
        box.append(f'<image x="{ix}" y="{iy}" width="{isz}" height="{isz}" preserveAspectRatio="xMidYMid slice" '
                   f'href="{avatar}" xlink:href="{avatar}"/>')
    else:
        box.append(f'<clipPath id="ac"><rect x="{ix}" y="{iy}" width="{isz}" height="{isz}"/></clipPath>'
                   f'<g clip-path="url(#ac)"><rect x="{ix}" y="{iy}" width="{isz}" height="{isz}" fill="url(#sky)"/>'
                   f'<ellipse cx="{ix + isz * .4}" cy="{iy + isz * 1.05}" rx="{isz * .9}" ry="{isz * .45}" fill="#4da329"/>'
                   f'<ellipse cx="{ix + isz / 2}" cy="{iy + isz * .42}" rx="{isz * .17}" ry="{isz * .22}" fill="#cfd6cc" stroke="#6b756b"/>'
                   f'<path d="M{ix + isz / 2} {iy + isz * .62}V{iy + isz * .95}" stroke="#cfd6cc" stroke-width="{isz * .05}"/></g>')
    s.append(f"<g{A('rise', b + 0.5)}>{''.join(box)}</g>")

    # dialog buttons
    s.append(push_button(620, 283, 75, 23, "OK", True) + push_button(700, 283, 75, 23, "Cancel") + push_button(780, 283, 75, 23, "Apply"))
    s.append("</g>")
    return svg_doc(W, H, f"{NAME} - System Properties",
                   f"Windows XP style profile card for @{USER}: {ROLE}. {BIO}", "".join(s))


def month_labels(grid):
    marks, prev = [], None
    for c, col in enumerate(grid):
        first = next(cell for cell in col if cell)
        key = (first[0].year, first[0].month)
        if key != prev:
            marks.append((c, MON[first[0].month - 1]))
            prev = key
    out = []
    for i, (c, label) in enumerate(marks):
        if i + 1 < len(marks) and marks[i + 1][0] - c < 3:
            continue  # crowded by the next label (partial first month)
        out.append((c, label))
    return out


def heatmap_card(win, total):
    grid = win["grid"]
    W, H = 880, 242
    b = STAGGER["heatmap"]
    cell, pitch = 12, 15
    x0, y0 = 52, 88
    s = [f"<g{A('fade', b)}>", chrome(W, H, r"C:\spoonix-dev\contributions.exe", ICON_GRID),
         menubar(W, 30, ["File", "Edit", "View", "Favorites", "Tools", "Help"]),
         sunken(14, 60, 852, 146)]
    for c, label in month_labels(grid):
        s.append(T(x0 + c * pitch, 79, label, 10.5, MUTED))
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        s.append(T(x0 - 7, y0 + row * pitch + 10, label, 10, MUTED, anchor="end"))
    for c, col in enumerate(grid):
        cells = "".join(
            f'<rect x="{n(x0 + c * pitch)}" y="{n(y0 + r * pitch)}" width="{cell}" height="{cell}" rx="2.5" fill="{LEVELS[cl[2]]}"/>'
            for r, cl in enumerate(col) if cl)
        s.append(f"<g{A('col', b + 0.35 + c * 0.028)}>{cells}</g>")
    # status bar: total + legend
    s.append(seg(8, 214, 588, 24) + seg(600, 214, 272, 24))
    s.append(T(16, 230, f"{total:,} contributions in the last year", 11, "#1d2b14"))
    lx = 866 - 30 - 4 - (5 * 15 - 3)
    s.append(T(lx - 6, 230, "Less", 10.5, MUTED, anchor="end"))
    s.extend(f'<rect x="{lx + i * 15}" y="220" width="12" height="12" rx="2.5" fill="{LEVELS[i]}"/>' for i in range(5))
    s.append(T(866, 230, "More", 10.5, MUTED, anchor="end"))
    s.append("</g>")
    return svg_doc(W, H, "Contribution graph",
                   f"{total:,} GitHub contributions by @{USER} over the last year, shown as a green heatmap.", "".join(s))


TILE_LINKS = {
    "tile-repos.svg": f"https://github.com/{USER}?tab=repositories",
    "tile-contribs.svg": f"https://github.com/{USER}",
    "tile-stars.svg": f"https://github.com/{USER}?tab=repositories&sort=stargazers",
}


def tile_card(idx, title, label, value):
    W, H, w, h = 288, 154, 282, 148
    b = STAGGER["tiles"] + idx * 0.16
    o = [chrome(w, h, title, ICON_EXE, t=26, bw=18, fs=12), sunken(10, 34, 262, 76),
         count_text(value, 141, 82, 48, b + 0.5, DARK),
         T(141, 102, label, 11.5, MUTED, "700", anchor="middle"),
         seg(8, 118, 196, 22), seg(208, 118, 66, 22),
         T(14, 133, "Click to open", 10.5, "#3a3a2e"),
         f'<g transform="translate(236 129)"><g{A("arrow")}><path d="M0 0H10M6.5 -3.6L10 0L6.5 3.6" fill="none" '
         'stroke="#1b5e20" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></g></g>']
    body = f'<g transform="translate(3 2)"><g{A("swing", b)}>{"".join(o)}</g></g>'
    return svg_doc(W, H, f"{label}: {value:,}", f"{label} for @{USER}: {value:,}.", body)


def activity_card(st):
    W, H = 880, 308
    b = STAGGER["activity"]
    months = st["months"]
    peak = max(m[2] for m in months)
    pi = next(i for i, m in enumerate(months) if m[2] == peak) if peak > 0 else None
    axis, step = nice_axis(peak)
    month_total = sum(m[2] for m in months)
    s = [f"<g{A('fade', b)}>", chrome(W, H, "Windows Task Manager", ICON_BARS),
         menubar(W, 30, ["File", "Options", "View", "Windows", "Help"]),
         tabs(14, 80, 852, 192, [("Applications", 82), ("Processes", 72), ("Performance", 86), ("Networking", 78), ("Users", 52)], 2)]

    # chart group
    s.append(groupbox(26, 92, 574, 170, "Contributions per Month"))
    s.append(sunken(38, 106, 550, 146))
    x0, x1, base, ph, bw = 78, 580, 232, 100, 26
    slot = (x1 - x0) / 12
    for i in range(5):
        gy = base - ph * i / 4
        if i:
            s.append(f'<path d="M74 {n(gy)}H584" stroke="#d6d2c0" stroke-dasharray="2 3"/>')
        s.append(T(68, gy + 3.5, step * i, 10, MUTED, anchor="end"))
    s.append(f'<path d="M74 {base}H584" stroke="#919b9c"/>')
    for i, (year, m, val) in enumerate(months):
        cx = x0 + slot * i + slot / 2
        bar_h = max(2, ph * val / axis) if val > 0 else 0
        if bar_h:
            fill = "url(#barp)" if i == pi else "url(#bar)"
            s.append(f'<rect{A("bar", b + 0.4 + i * 0.06)} x="{n(cx - bw / 2)}" y="{n(base - bar_h)}" width="{bw}" '
                     f'height="{n(bar_h)}" rx="2" fill="{fill}" stroke="#1b5e20" stroke-opacity=".55"/>')
        s.append(T(cx, 246, MON[m - 1], 10, DARK if i == pi else MUTED, "700" if i == pi else None, anchor="middle"))
        if i == pi:
            s.append(f"<g{A('fade', b + 0.4 + i * 0.06 + 0.7)}>{T(cx, base - bar_h - 6, val, 12, DARK, '700', anchor='middle')}</g>")

    # stats group
    s.append(groupbox(614, 92, 240, 170, "Stats"))
    s.append(sunken(626, 106, 216, 146))
    streak, s_from, s_to = st["streak"]
    bd, bc = st["busiest"][0], st["busiest"][1]
    rows = [
        ("Longest Streak", f"{streak} day{'s' if streak != 1 else ''}" if streak else "0 days",
         f"{short_date(s_from)} - {short_date(s_to)}" if streak else "no streak yet"),
        ("Active Days", f"{st['active']}", f"of {st['days']} days"),
        ("Busiest Day", short_date(bd) if bc else "-", f"{bc:,} contribution{'s' if bc != 1 else ''}" if bc else "none yet"),
    ]
    for i, (label, value, sub) in enumerate(rows):
        ry = 108 + i * 48
        d = b + 1.1 + i * 0.15
        if i:
            s.append(f'<path d="M634 {ry - 1}H834" stroke="#d6d2c2"/><path d="M634 {ry}H834" stroke="#fff"/>')
        s.append(f"<g{A('rise', d)}>{T(638, ry + 15, label, 11, MUTED, '700')}"
                 f"{T(638, ry + 39, value, 24, DARK, '700')}"
                 f"{T(834, ry + 38, sub, 10.5, MUTED, anchor='end')}</g>")

    # status bar
    s.append(seg(8, 280, 150, 24) + seg(162, 280, 200, 24) + seg(366, 280, 506, 24))
    s.append(T(16, 296, f"Months: {len(months)}", 11, INK))
    s.append(T(170, 296, f"Total: {month_total:,} contributions", 11, INK))
    s.append(T(374, 296, f"Peak: {MON[months[pi][1] - 1]} {months[pi][0]} ({peak:,})" if pi is not None else "Peak: none yet", 11, INK))
    s.append("</g>")
    return svg_doc(W, H, "Contribution activity",
                   f"Monthly GitHub contributions for @{USER} over the last 12 months, with streak and activity stats.", "".join(s))


# ============================================================================= main
def write(name, svg):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(svg)
    print(f"wrote {os.path.relpath(path, ROOT) if path.startswith(ROOT) else path} ({len(svg) / 1024:.1f} KB)")


def main():
    try:
        if DEMO:
            repos, stars, days = demo_data()
            print("DEMO=1: using synthetic data")
        else:
            repos, stars = fetch_repo_stats()
            days = fetch_contributions()
        win = build_window(days)
        st = compute_stats(win)
        avatar = avatar_data_uri()
    except Exception as exc:  # noqa: BLE001 - fail before touching any existing card
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"repos={repos} stars={stars} contributions={st['total']} active_days={st['active']} "
          f"streak={st['streak'][0]} animated={'no' if STATIC else 'yes'}")
    write("hero.svg", hero_card(avatar))
    write("heatmap.svg", heatmap_card(win, st["total"]))
    write("tile-repos.svg", tile_card(0, "01_repos.exe", "Public Repositories", repos))
    write("tile-contribs.svg", tile_card(1, "02_commits.exe", "Yearly Contributions", st["total"]))
    write("tile-stars.svg", tile_card(2, "03_stars.exe", "Total Stars Earned", stars))
    write("activity.svg", activity_card(st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
