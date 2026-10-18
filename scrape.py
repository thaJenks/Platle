#!/usr/bin/env python3
"""
Platle data builder (light version).

Starts with 200 random games that belong to a PlatPursuit badge (200 days of
puzzles), then adds 1 new badge game every day so the queue never runs out.
It writes:
  data/g/<NPWR id>.json   one file per game (everything the 8 hints need)
  data/index.json         titles for autocomplete + ids that can be answers
  data/schedule.json      fixed daily answers, so everyone gets the same puzzle

Progress lives in data/picked.json. A normal daily run loads about 3 pages:
one new game, plus tomorrow's game to refresh its rarity numbers.

Try one page first:
  python scraper/scrape.py --test https://platpursuit.com/games/NPWR22997_00/
See which badge games it finds (no game pages loaded):
  python scraper/scrape.py --discover-only
Normal run:
  python scraper/scrape.py
"""
import argparse
import json
import random
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://platpursuit.com"
# Put a way to reach you here, so PlatPursuit knows who is crawling.
USER_AGENT = "PlatleBot/1.0 (fan-made guessing game; contact: YOUR_EMAIL_HERE)"

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
GAMES = DATA / "g"
BADGE_FILE = DATA / "badge_games.json"   # every game found on badge pages
PICKED = DATA / "picked.json"            # the chosen games + when each was scraped
ID_RE = re.compile(r"/games/(NPWR\d{5}_\d{2})/")
CONCEPT_RE = re.compile(r"/games/(\d+)/")
SKIP_BADGE_SLUGS = {"how-it-works"}
REDISCOVER_DAYS = 60       # re-read the badge pages this often (finds new badges)

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


# ---------------------------------------------------------------- fetching
def get(url, delay):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 60 * (attempt + 1)
                print(f"  {r.status_code} on {url}, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            time.sleep(delay)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            print(f"  retry {url}: {e}", file=sys.stderr)
            time.sleep(15 * (attempt + 1))
    return None


# ---------------------------------------------------------------- discovery
def links_on(html):
    """Game links on a page: {key: title}. Key is an NPWR id or 'c<concept id>'."""
    found = {}
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = clean(a.get("title") or a.get_text(" ", strip=True))
        m = ID_RE.search(href)
        if m:
            found.setdefault(m.group(1), title)
            continue
        m = CONCEPT_RE.search(href)
        if m:
            found.setdefault("c" + m.group(1), title)
    return found


def paged(path, delay, max_pages=20):
    """Load a page and any ?page=2,3… after it, stopping when nothing new appears."""
    pages, seen = [], set()
    for page in range(1, max_pages + 1):
        html = get(f"{BASE}{path}" + (f"?page={page}" if page > 1 else ""), delay)
        if not html:
            break
        sig = frozenset(re.findall(r'href="([^"]+)"', html))
        if sig in seen or (pages and sig <= set().union(*seen)):
            break
        seen.add(sig)
        pages.append(html)
        if f"page={page + 1}" not in html:
            break
    return pages


def discover_badge_games(delay):
    slugs = []
    for html in paged("/badges/", delay):
        for s in re.findall(r'href="(?:https://platpursuit\.com)?/badges/([a-z0-9-]+)/', html):
            if s not in SKIP_BADGE_SLUGS and s not in slugs:
                slugs.append(s)
    print(f"  {len(slugs)} badges found")
    games = {}
    for i, slug in enumerate(slugs, 1):
        for html in paged(f"/badges/{slug}/", delay):
            for key, title in links_on(html).items():
                if title and len(title) < 120:
                    games.setdefault(key, {"title": title, "badges": []})
                    if slug not in games[key]["badges"]:
                        games[key]["badges"].append(slug)
        if i % 20 == 0:
            print(f"  {i}/{len(slugs)} badges read, {len(games)} games")
    return games


def resolve(key, delay):
    """Turn a concept id into a trophy list id by opening its game page."""
    if not key.startswith("c"):
        return key
    html = get(f"{BASE}/games/{key[1:]}/", delay)
    ids = ID_RE.findall(html or "")
    return ids[0] if ids else None


# ---------------------------------------------------------------- parsing
def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def text_of(el):
    return clean(el.get_text(" ", strip=True)) if el else ""


def meta(soup, key):
    tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
    return tag.get("content", "") if tag else ""


def after_label(soup, label, href_pat):
    node = soup.find(string=re.compile(rf"^\s*{label}\s*$"))
    if node:
        a = node.find_next("a", href=re.compile(href_pat))
        if a:
            return text_of(a)
    return None


def find_platinum(soup):
    for img in soup.find_all("img", src=re.compile(r"psnobj")):
        alt = clean(img.get("alt"))
        if not alt:
            continue
        box = img
        for _ in range(8):
            box = box.parent
            if box is None or "PSN Rate" in box.get_text(" "):
                break
        if box is None:
            continue
        t = text_of(box)
        m = re.search(
            re.escape(alt) + r"\s+Platinum\b\s*(.*?)\s*([\d.]+)%\s*PP Rate\s*([\d.]+)%\s*PSN Rate", t
        )
        if m:
            return {
                "plat": alt,
                "platDesc": m.group(1).strip() or None,
                "pp": float(m.group(2)),
                "psn": float(m.group(3)),
                "icon": img.get("src"),
            }
    return None


def count_trophy_groups(soup):
    """Each trophy group (base game + each DLC) is a heading followed by 4 counts."""
    groups = 0
    for h in soup.find_all(["h3", "h4"]):
        nums = []
        for s in h.find_all_next(string=True, limit=20):
            if h in s.parents:
                continue
            st = s.strip()
            if not st:
                continue
            if re.fullmatch(r"\d+", st):
                nums.append(st)
                if len(nums) == 4:
                    break
            elif re.fullmatch(r"(\d+\s+){3}\d+", st):
                nums = st.split()
                break
            else:
                break
        if len(nums) == 4:
            groups += 1
    return groups


def parse_game(html, gid, delay=0, fetch_series=True):
    soup = BeautifulSoup(html, "html.parser")
    full = text_of(soup.body or soup)
    g = {"id": gid}

    h1 = soup.find("h1")
    g["title"] = text_of(h1) or re.sub(r"\s+Trophies$", "", meta(soup, "og:title"))

    desc = meta(soup, "og:description")  # "X on PS5. 36 trophies including 1 platinum."
    m = re.search(r" on ([A-Za-z0-9 ,/&+]+?)\.\s*(\d+) trophies", desc)
    if m:
        g["platforms"] = [p.strip() for p in re.split(r",|/|&|\+| and ", m.group(1)) if p.strip()]
        g["total"] = int(m.group(2))
    g["hasPlat"] = bool(re.search(r"including 1 platinum", desc, re.I))

    plat = find_platinum(soup)
    if plat:
        g.update(plat)
        g["hasPlat"] = True
    else:
        m = re.search(r"Earned by\s*([\d.]+)%\s*of players", full)
        if m:
            g["psn"] = float(m.group(1))

    bd = {}
    for kind in ("Platinum", "Gold", "Silver", "Bronze"):
        m = re.search(rf"\b{kind}\s+(\d+)\s+to earn", full)
        if m:
            bd[kind.lower()] = int(m.group(1))
    if len(bd) == 4:
        g["breakdown"] = bd
        g.setdefault("total", sum(bd.values()))

    groups = count_trophy_groups(soup)
    if groups:
        g["dlc"] = max(groups - 1, 0)

    comp = r"/companies/[^/]+/"
    companies = [text_of(a) for a in soup.find_all("a", href=re.compile(comp))]
    g["developer"] = after_label(soup, "Developer", comp) or (companies[0] if companies else None)
    g["publisher"] = after_label(soup, "Publisher", comp) or (companies[1] if len(companies) > 1 else None)

    m = re.search(r"\bReleased\s*[A-Z][a-z]{2,8}\.? \d{1,2}, (\d{4})", full)
    if m:
        g["released"] = m.group(1)

    genres = []
    for a in soup.find_all("a", href=re.compile(r"/genres/[^/]+/")):
        t = text_of(a)
        if t and t not in genres:
            genres.append(t)
    if genres:
        g["genres"] = genres

    badges, badge_art = [], None
    for img in soup.find_all("img", alt=re.compile(r" badge$")):
        name = clean(img["alt"][: -len(" badge")])
        if name and name not in badges:
            badges.append(name)
        src = img.get("src", "")
        if not badge_art and "/media/badges/" in src:
            badge_art = src
    if badges:
        g["badges"] = badges
    if badge_art:
        g["badgeArt"] = badge_art

    for img in soup.find_all("img", src=re.compile(r"igdb")):
        alt = clean(img.get("alt")).lower()
        if "cover" in alt and "cover" not in g:
            g["cover"] = img["src"]
        elif "screenshot" in alt and "screenshot" not in g:
            g["screenshot"] = img["src"]

    m = re.search(r"([\d,]+)\s+Players\b", full)
    if m:
        g["players"] = int(m.group(1).replace(",", ""))

    # Series: a franchise link on this page, or on the parent "View game" page
    fr = soup.find("a", href=re.compile(r"/franchises/[^/]+/"))
    if fr:
        g["series"] = text_of(fr)
    elif fetch_series and not badges:
        parent = soup.find("a", href=re.compile(r"/games/\d+/"))
        if parent:
            href = parent["href"]
            url = href if href.startswith("http") else BASE + href
            ph = get(url, delay)
            if ph:
                ps = BeautifulSoup(ph, "html.parser")
                fr = ps.find("a", href=re.compile(r"/franchises/[^/]+/"))
                if fr:
                    g["series"] = text_of(fr)
                else:
                    m = re.search(r"Franchise:\s*(.+?)\s{2,}|Franchise:\s*([^\n]+)", ps.get_text("\n"))
                    if m:
                        g["series"] = clean(m.group(1) or m.group(2))

    return {k: v for k, v in g.items() if v not in (None, [], {})}


# ---------------------------------------------------------------- outputs
def load_json(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj, pretty=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None,
                   separators=None if pretty else (",", ":")),
        "utf-8",
    )
    tmp.replace(path)


def is_answer(g):
    return bool(g.get("hasPlat") and g.get("plat") and g.get("psn") is not None)


def update_schedule(picked):
    """One game per day. New games join the back of the queue, so the
    schedule always reaches about 200 days ahead and nothing repeats."""
    sched = load_json(DATA / "schedule.json", None)
    if not sched or not sched.get("ids"):
        ids = list(picked["ids"])
        random.shuffle(ids)
        sched = {"start": date.today().isoformat(), "ids": ids}
    queued = set(sched["ids"])
    for gid in picked["ids"]:           # newly added games go on the end
        if gid not in queued:
            sched["ids"].append(gid)
            queued.add(gid)
    today_n = (date.today() - date.fromisoformat(sched["start"])).days + 1
    # Safety net: if the queue ever runs short, reuse the games played longest ago
    while len(sched["ids"]) < today_n + 7 and sched["ids"]:
        sched["ids"].append(sched["ids"][len(sched["ids"]) - len(queued)])
    save_json(DATA / "schedule.json", sched, pretty=True)
    ahead = len(sched["ids"]) - today_n
    print(f"schedule: puzzle #{today_n} today, {ahead} days planned ahead")
    return sched, today_n


def build_outputs(picked, badge_games):
    titles = set()
    for gid in picked["ids"]:
        g = load_json(GAMES / f"{gid}.json", {})
        if g.get("title"):
            titles.add(g["title"])
    # Every badge game can be typed as a guess, even the ones not scraped
    titles |= {v["title"] for v in badge_games.values() if v.get("title")}
    save_json(DATA / "index.json", {
        "updated": date.today().isoformat(),
        "titles": sorted(titles, key=str.lower),
        "answers": picked["ids"],
    })
    print(f"index: {len(titles)} guessable titles, {len(picked['ids'])} games with full data")


# ---------------------------------------------------------------- main
def read_badges(delay, force=False):
    badge = load_json(BADGE_FILE, {"read": None, "games": {}})
    old = not badge["read"] or (date.today() - date.fromisoformat(badge["read"])).days >= REDISCOVER_DAYS
    if force or old or not badge["games"]:
        print("Reading badge pages…")
        found = discover_badge_games(delay)
        if found:
            badge = {"read": date.today().isoformat(), "games": found}
            save_json(BADGE_FILE, badge)
    print(f"{len(badge['games'])} badge games known")
    return badge


def add_games(picked, badge, count, delay):
    """Scrape `count` new random badge games that have a platinum."""
    added = 0
    pool = [k for k in badge["games"] if k not in picked["tried"]]
    random.shuffle(pool)
    while added < count and pool:
        key = pool.pop()
        picked["tried"].append(key)
        gid = resolve(key, delay)
        if not gid or gid in picked["ids"]:
            continue
        html = get(f"{BASE}/games/{gid}/", delay)
        if not html:
            continue
        g = parse_game(html, gid, delay)
        save_json(GAMES / f"{gid}.json", g)
        picked["done"][gid] = date.today().isoformat()
        if is_answer(g):
            picked["ids"].append(gid)
            added += 1
            print(f"  added #{len(picked['ids'])}: {g.get('title')}")
        save_json(PICKED, picked)
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", help="parse one game page URL and print the result")
    ap.add_argument("--discover-only", action="store_true")
    ap.add_argument("--start", type=int, default=200, help="games to build up front (days of puzzles)")
    ap.add_argument("--daily", type=int, default=1, help="new games to add each day after that")
    ap.add_argument("--delay", type=float, default=4.0, help="seconds between requests")
    args = ap.parse_args()

    if args.test:
        m = ID_RE.search(args.test)
        html = get(args.test, 0)
        if not html:
            sys.exit("Could not load that page.")
        print(json.dumps(parse_game(html, m.group(1) if m else "TEST", args.delay),
                         indent=2, ensure_ascii=False))
        return

    badge = read_badges(args.delay, force=args.discover_only)
    if args.discover_only:
        for k, v in list(badge["games"].items())[:25]:
            print(f"  {k}  {v['title']}  ({', '.join(v['badges'])})")
        return
    if not badge["games"]:
        sys.exit("No badge games found. Run with --discover-only and share the output.")

    picked = load_json(PICKED, {"ids": [], "done": {}, "tried": [], "last_add": None})
    today = date.today().isoformat()

    # 1. First time: build the starting set (200 days of puzzles)
    if len(picked["ids"]) < args.start:
        print(f"Building the starting set: {len(picked['ids'])}/{args.start}")
        add_games(picked, badge, args.start - len(picked["ids"]), args.delay)
        picked["last_add"] = today
    # 2. Every day after: add one new game to the back of the queue
    elif picked.get("last_add") != today:
        n = add_games(picked, badge, args.daily, args.delay)
        if n < args.daily:  # every known badge game used: look for new badges
            badge = read_badges(args.delay, force=True)
            add_games(picked, badge, args.daily - n, args.delay)
        picked["last_add"] = today
    save_json(PICKED, picked)

    sched, today_n = update_schedule(picked)

    # 3. Refresh tomorrow's game so its rarity numbers are current
    if len(sched["ids"]) > today_n:
        gid = sched["ids"][today_n]
        if picked["done"].get(gid) != today:
            html = get(f"{BASE}/games/{gid}/", args.delay)
            if html:
                save_json(GAMES / f"{gid}.json", parse_game(html, gid, args.delay))
                picked["done"][gid] = today
                save_json(PICKED, picked)

    build_outputs(picked, badge["games"])


if __name__ == "__main__":
    main()
