#!/usr/bin/env python3
"""
Platle data builder. Crawls PlatPursuit game pages and writes:
  data/g/<NPWR id>.json   one file per game (everything the 8 hints need)
  data/index.json         every title (for autocomplete) + ids that can be answers
  data/schedule.json      fixed daily answers, so everyone gets the same puzzle

It works through the catalog a chunk at a time and remembers progress in
data/cache.json, so it can run every few hours on GitHub Actions.

Try it on one page first:
  python scraper/scrape.py --test https://platpursuit.com/games/NPWR22997_00/
Check that game discovery works:
  python scraper/scrape.py --discover-only
Normal run:
  python scraper/scrape.py --max 2500 --minutes 300
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
CACHE = DATA / "cache.json"
ID_RE = re.compile(r"/games/(NPWR\d{5}_\d{2})/")
REFRESH_DAYS = 30          # re-scrape a game this often (rarity changes)
REDISCOVER_DAYS = 7        # look for newly added games this often
SCHEDULE_AHEAD = 60        # days of daily answers to keep planned

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


# ---------------------------------------------------------------- fetching
def get(url, delay):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 30 * (attempt + 1)
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
            time.sleep(10 * (attempt + 1))
    return None


# ---------------------------------------------------------------- discovery
def ids_from(text):
    return set(ID_RE.findall(text or ""))


def discover(delay, max_pages):
    """Find every game id. Tries the sitemap first, then pages through the catalog."""
    ids = set()
    xml = get(BASE + "/sitemap.xml", delay)
    if xml and "<loc>" in xml:
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)
        ids |= ids_from("\n".join(l.rstrip("/") + "/" for l in locs))
        for sub in [l for l in locs if l.endswith(".xml")]:
            x = get(sub, delay) or ""
            sublocs = re.findall(r"<loc>\s*(.*?)\s*</loc>", x)
            ids |= ids_from("\n".join(l.rstrip("/") + "/" for l in sublocs))
            print(f"  sitemap {sub}: {len(ids)} ids")
    if ids:
        return ids

    print("  no sitemap game links, paging through the catalog instead")
    for listing in ("/games/lists/", "/games/"):
        empty_streak = 0
        for page in range(1, max_pages + 1):
            html = get(f"{BASE}{listing}?page={page}", delay)
            found = ids_from(html)
            new = found - ids
            ids |= found
            print(f"  {listing} page {page}: +{len(new)} ({len(ids)} total)")
            empty_streak = empty_streak + 1 if not new else 0
            if not html or empty_streak >= 2:
                break
        if ids:
            break
    return ids


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
        json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None, separators=None if pretty else (",", ":")),
        "utf-8",
    )
    tmp.replace(path)


def is_answer(g, min_players):
    return bool(g.get("hasPlat") and g.get("plat") and g.get("psn") is not None
                and g.get("players", 0) >= min_players)


def build_outputs(min_players):
    titles, answers = set(), []
    for f in sorted(GAMES.glob("*.json")):
        g = load_json(f, {})
        if not g.get("title"):
            continue
        titles.add(g["title"])
        if is_answer(g, min_players):
            answers.append(g["id"])
    save_json(DATA / "index.json", {
        "updated": date.today().isoformat(),
        "titles": sorted(titles, key=str.lower),
        "answers": answers,
    })

    sched = load_json(DATA / "schedule.json", {"start": date.today().isoformat(), "ids": []})
    start = date.fromisoformat(sched["start"])
    needed = (date.today() - start).days + 1 + SCHEDULE_AHEAD
    valid = set(answers)
    used = set(sched["ids"])
    pool = [a for a in answers if a not in used]
    random.shuffle(pool)
    while len(sched["ids"]) < needed and valid:
        if not pool:  # every answer used once: start a fresh cycle
            pool = list(valid)
            random.shuffle(pool)
        sched["ids"].append(pool.pop())
    save_json(DATA / "schedule.json", sched, pretty=True)
    print(f"index: {len(titles)} titles, {len(answers)} possible answers; schedule: {len(sched['ids'])} days")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", help="parse one game page URL and print the result")
    ap.add_argument("--discover-only", action="store_true")
    ap.add_argument("--max", type=int, default=2500, help="game pages per run")
    ap.add_argument("--minutes", type=float, default=300, help="stop after this long")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    ap.add_argument("--max-list-pages", type=int, default=5000)
    ap.add_argument("--min-players", type=int, default=1,
                    help="only use games with at least this many PlatPursuit players as answers")
    args = ap.parse_args()

    if args.test:
        gid = (ID_RE.search(args.test) or [None, "TEST"])[1]
        html = get(args.test, 0)
        if not html:
            sys.exit("Could not load that page.")
        print(json.dumps(parse_game(html, gid, args.delay), indent=2, ensure_ascii=False))
        return

    cache = load_json(CACHE, {"ids": [], "discovered": None, "done": {}})
    stale = not cache["discovered"] or (
        date.today() - date.fromisoformat(cache["discovered"])).days >= REDISCOVER_DAYS
    if stale or args.discover_only:
        print("Discovering games…")
        found = discover(args.delay, args.max_list_pages)
        if found:
            cache["ids"] = sorted(set(cache["ids"]) | found)
            cache["discovered"] = date.today().isoformat()
            save_json(CACHE, cache)
        print(f"{len(cache['ids'])} games known")
        if args.discover_only:
            return

    cutoff = (date.today() - timedelta(days=REFRESH_DAYS)).isoformat()
    todo = [i for i in cache["ids"] if i not in cache["done"]]
    todo += sorted((i for i in cache["ids"] if i in cache["done"] and cache["done"][i] < cutoff),
                   key=lambda i: cache["done"][i])
    todo = todo[: args.max]
    print(f"Scraping {len(todo)} games this run")

    t0 = time.time()
    for n, gid in enumerate(todo, 1):
        if (time.time() - t0) / 60 > args.minutes:
            print("Time limit reached, stopping for this run")
            break
        html = get(f"{BASE}/games/{gid}/", args.delay)
        if html:
            try:
                g = parse_game(html, gid, args.delay)
                save_json(GAMES / f"{gid}.json", g)
            except Exception as e:  # keep going if one page is odd
                print(f"  parse error {gid}: {e}", file=sys.stderr)
        cache["done"][gid] = date.today().isoformat()
        if n % 50 == 0:
            save_json(CACHE, cache)
            print(f"  {n}/{len(todo)} done")
    save_json(CACHE, cache)
    build_outputs(args.min_players)


if __name__ == "__main__":
    main()
