#!/usr/bin/env python3
"""
Steam Trend Alert v2
--------------------
Pings your phone (ntfy and/or Discord) when a Steam game is blowing up,
with an optional free AI-written Roblox game idea.

What it watches:
  🚀 LAUNCHES  new games (released in the last 60 days) passing a player threshold
               or doubling their players overnight
  🧪 DEMOS     free demos with lots of players - warning BEFORE the game launches
  📈 WISHLIST  upcoming games suddenly jumping into Steam's top wishlisted list

Every game gets a ROBLOX FIT score (0-10) from its Steam tags. Co-op, simulator,
incremental and party games score high; visual novels, MMOs and sports sims
score low. Low scores are filtered out. Adult/NSFW games are always blocked.

Uses only the Python standard library - nothing to install.

Settings (environment variables, all optional - see README.md):
  NTFY_TOPIC, NTFY_SERVER, DISCORD_WEBHOOK_URL, GEMINI_API_KEY, GEMINI_MODEL
  MIN_PLAYERS        live players for a launch alert        (default 10000)
  MIN_DEMO_PLAYERS   live players for a demo alert          (default 3000)
  GROWTH_FACTOR      alert if players grew this many x      (default 2)
  MIN_FOR_GROWTH     ...but only from at least this many    (default 1000)
  MIN_FIT_SCORE      minimum Roblox fit score, 0-10         (default 4)
  WISHLIST_TOP       wishlist surge = now in the top N...   (default 25)
  WISHLIST_FROM      ...after being below rank N (or unranked) (default 75)
  MAX_AGE_DAYS       "new" = released in the last N days    (default 60)
  MAX_ALERTS         max games per notification             (default 5)
  REALERT_FACTOR     re-alert if a game grows this many x since its alert (default 3)
  NOTIFY_WHEN_EMPTY  "true" = message even when nothing is trending (for testing)

Run locally without sending anything:   python steam_trends.py --dry-run
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(HERE, "history.json")
USER_AGENT = "steam-trend-alert/2.0 (personal hobby script)"


# ---------------------------------------------------------------- settings
def env_num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, "").strip() or default)
    except ValueError:
        return cast(default)


MIN_PLAYERS = env_num("MIN_PLAYERS", 10000, int)
MIN_DEMO_PLAYERS = env_num("MIN_DEMO_PLAYERS", 3000, int)
GROWTH_FACTOR = env_num("GROWTH_FACTOR", 2.0)
MIN_FOR_GROWTH = env_num("MIN_FOR_GROWTH", 1000, int)
MIN_FIT_SCORE = env_num("MIN_FIT_SCORE", 4, int)
WISHLIST_TOP = env_num("WISHLIST_TOP", 25, int)
WISHLIST_FROM = env_num("WISHLIST_FROM", 75, int)
MAX_AGE_DAYS = env_num("MAX_AGE_DAYS", 60, int)
MAX_ALERTS = env_num("MAX_ALERTS", 5, int)
REALERT_FACTOR = env_num("REALERT_FACTOR", 3.0)
NOTIFY_WHEN_EMPTY = os.environ.get("NOTIFY_WHEN_EMPTY", "").lower() == "true"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
DRY_RUN = "--dry-run" in sys.argv

TODAY = date.today()


def log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- http
def http_json(url, data=None, headers=None, retries=3, timeout=30):
    """GET (or POST JSON if data given) and return parsed JSON, or None on failure."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            log(f"  HTTP {e.code} for {short(url)} (try {attempt}/{retries})")
            if e.code in (400, 401, 403, 404):
                return None
            time.sleep(30 if e.code == 429 else 3 * attempt)
        except Exception as e:  # network error, bad JSON, timeout...
            log(f"  error for {short(url)}: {e} (try {attempt}/{retries})")
            time.sleep(3 * attempt)
    return None


def short(url):
    return re.sub(r"key=[^&]+", "key=***", url)[:110]


# ---------------------------------------------------------------- history
def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            h = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        h = {}
    h.setdefault("details", {})     # appid -> cached store info
    h.setdefault("players", {})     # appid -> [[iso_date, count], ...]
    h.setdefault("alerted", {})     # alert key -> {"date":..., "players":...}
    h.setdefault("ranks", {})       # appid -> [[iso_date, wishlist rank], ...]
    h.setdefault("rank_days", [])   # days we've recorded the wishlist chart
    # v1 stored alerts under the bare app ID - convert so they aren't re-sent
    for k in [k for k in h["alerted"] if k.isdigit()]:
        h["alerted"]["launch:" + k] = h["alerted"].pop(k)
    return h


def save_history(h):
    cutoff = (TODAY - timedelta(days=30)).isoformat()
    for key in ("players", "ranks"):
        for appid in list(h[key]):
            pts = h[key][appid][-14:]
            if not pts or pts[-1][0] < cutoff:
                del h[key][appid]
            else:
                h[key][appid] = pts
    h["rank_days"] = sorted(set(h["rank_days"]))[-14:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=1, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------- sources
def search_list(filter_name):
    """App IDs, in rank order, from a Steam store search list (e.g. popularwishlist)."""
    d = http_json("https://store.steampowered.com/search/results/?json=1&count=100"
                  f"&filter={filter_name}&cc=gb&l=english&ignore_preferences=1")
    ids = []
    for it in (d or {}).get("items") or []:
        m = re.search(r"/apps/(\d+)/", it.get("logo", ""))
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def candidates_from_sources():
    """Returns ({appid: source label}, [wishlist appids in rank order])."""
    ids = {}

    d = http_json("https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/")
    ranks = ((d or {}).get("response") or {}).get("ranks") or []
    for r in ranks:
        if r.get("appid"):
            ids.setdefault(str(r["appid"]), "Most Played")
    log(f"Most Played chart: {len(ranks)}")

    d = http_json("https://store.steampowered.com/api/featuredcategories?cc=gb&l=english")
    n = 0
    for key, label in (("top_sellers", "Top Seller"), ("new_releases", "New Release"),
                       ("coming_soon", "Coming Soon")):
        for it in ((d or {}).get(key) or {}).get("items") or []:
            if it.get("id"):
                ids.setdefault(str(it["id"]), label)
                n += 1
    log(f"Store featured lists: {n}")

    d = http_json("https://steamspy.com/api.php?request=top100in2weeks")
    if isinstance(d, dict):
        for appid in d:
            if appid.isdigit():
                ids.setdefault(appid, "SteamSpy 2-week top")
        log(f"SteamSpy 2-week top: {len(d)}")

    wishlist = search_list("popularwishlist")
    for appid in search_list("popularcomingsoon") + wishlist:
        ids.setdefault(appid, "Popular Upcoming")
    log(f"Wishlist/upcoming charts: {len(wishlist)} ranked")

    return ids, wishlist


DATE_FORMATS = ["%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y",
                "%d %b %Y", "%b %d %Y", "%Y-%m-%d", "%b %Y", "%B %Y"]


def parse_release(text):
    text = (text or "").replace(".", "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def get_details(appid, h):
    """Store info for an app, cached so we don't keep asking Steam."""
    cached = h["details"].get(appid)
    week_ago = (TODAY - timedelta(days=7)).isoformat()
    # released games are stable; re-check unreleased/unknown ones weekly
    if cached and (cached.get("released") or cached.get("checked", "") > week_ago):
        return cached

    time.sleep(1.5)  # Steam allows ~200 appdetails calls per 5 minutes
    d = http_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=gb&l=english")
    entry = (d or {}).get(appid) or {}
    if not entry.get("success"):
        info = {"skip": True, "checked": TODAY.isoformat()}
    else:
        data = entry.get("data") or {}
        rd = data.get("release_date") or {}
        coming = bool(rd.get("coming_soon"))
        released = None if coming else parse_release(rd.get("date"))
        descriptors = (data.get("content_descriptors") or {}).get("ids") or []
        info = {
            "name": data.get("name", f"App {appid}"),
            "type": data.get("type", ""),
            "coming_soon": coming,
            "released": released.isoformat() if released else None,
            "genres": [g.get("description") for g in data.get("genres") or []][:5],
            "categories": [c.get("description") for c in data.get("categories") or []],
            "desc": (data.get("short_description") or "")[:400],
            "free": bool(data.get("is_free")),
            "price": ((data.get("price_overview") or {}).get("final_formatted") or ""),
            # 3 = Adult Only Sexual Content, 4 = Frequent Nudity or Sexual Content
            "adult": any(i in (3, 4) for i in descriptors),
            "demo_ids": [str(x["appid"]) for x in data.get("demos") or [] if x.get("appid")],
            "fullgame": str((data.get("fullgame") or {}).get("appid") or ""),
            "fullgame_name": (data.get("fullgame") or {}).get("name") or "",
            "checked": TODAY.isoformat(),
        }
        if info["type"] not in ("game", "demo"):
            info["skip"] = True
    h["details"][appid] = info
    return info


def get_tags(appid, info):
    """Player-voted Steam tags (most voted first) via SteamSpy, cached weekly."""
    week_ago = (TODAY - timedelta(days=7)).isoformat()
    if info.get("tags_checked", "") > week_ago:
        return info.get("tags") or []
    time.sleep(1.0)  # SteamSpy allows ~1 request per second
    d = http_json(f"https://steamspy.com/api.php?request=appdetails&appid={appid}", retries=2)
    tags = (d or {}).get("tags")
    if isinstance(tags, dict):
        info["tags"] = [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])][:20]
        info["tags_checked"] = TODAY.isoformat()
    elif d is not None:
        info["tags"] = []
        info["tags_checked"] = TODAY.isoformat()
    return info.get("tags") or []


def current_players(appid):
    d = http_json("https://api.steampowered.com/ISteamUserStats/"
                  f"GetNumberOfCurrentPlayers/v1/?appid={appid}")
    resp = (d or {}).get("response") or {}
    return resp.get("player_count") if resp.get("result") == 1 else None


def previous_count(points):
    for day, count in reversed(points):
        if day < TODAY.isoformat():
            return day, count
    return None, None


# ---------------------------------------------------------------- roblox fit + nsfw
COOP_WORDS = {"online co-op", "co-op", "multi-player", "multiplayer", "shared/split screen co-op",
              "lan co-op", "4 player local"}
PVP_WORDS = {"online pvp", "pvp", "lan pvp", "shared/split screen pvp"}
FIT_WEIGHTS = {
    # great on Roblox
    "incremental": 2, "clicker": 2, "idle": 2, "tycoon": 2, "party game": 2,
    "party": 1.5, "parkour": 1.5, "tower defense": 1.5, "battle royale": 1.5,
    "creature collector": 1.5, "cleaning": 1.5, "physics": 1, "funny": 1, "comedy": 1,
    "horror": 1, "survival horror": 1, "survival": 1, "sandbox": 1, "building": 1,
    "base building": 1, "3d platformer": 1, "fighting": 1, "hero shooter": 1,
    "fishing": 1, "cute": 1, "casual": 1, "cooking": 1, "team-based": 1, "pets": 1,
    "extraction shooter": 1, "simulation": 0.5, "crafting": 0.5, "platformer": 0.5,
    "racing": 0.5, "roguelite": 0.5, "action roguelike": 0.5, "open world": 0.5,
    "life sim": 0.5, "farming sim": 0.5,
    # poor fit
    "visual novel": -4, "dating sim": -4, "interactive fiction": -4, "text-based": -3,
    "grand strategy": -3, "4x": -3, "point & click": -2, "hidden object": -2,
    "turn-based strategy": -2, "mmorpg": -2, "football (soccer)": -2,
    "massively multiplayer": -1.5, "turn-based tactics": -1.5, "sports": -1.5,
    "flight": -1.5, "rts": -1.5, "card game": -1, "deckbuilding": -1, "management": -1,
    "city builder": -1, "story rich": -1, "puzzle": -0.5, "singleplayer": -0.5,
    "utilities": -5, "software": -5, "audio production": -5, "design & illustration": -5,
    "game development": -5,
}
NSFW_ANYWHERE = {"hentai", "nsfw"}
NSFW_TOP = {"sexual content", "nudity"}  # only if among a game's top 8 tags


def is_nsfw(info, tags):
    low = [t.lower() for t in tags]
    return (info.get("adult") or any(t in NSFW_ANYWHERE for t in low)
            or any(t in NSFW_TOP for t in low[:8]))


def roblox_fit(info, tags):
    """0-10 score for how well a game's loop would work as a Roblox game."""
    words = {w.lower() for w in list(tags)[:15] + (info.get("genres") or []) + (info.get("categories") or [])}
    score, good, bad = 5.0, [], []
    if words & COOP_WORDS:
        score += 2
        good.append("co-op/multiplayer")
    if words & PVP_WORDS:
        score += 1.5
        good.append("PvP")
    if not (words & (COOP_WORDS | PVP_WORDS)):
        score -= 1.5
        bad.append("single-player")
    for w in sorted(words, key=lambda x: -abs(FIT_WEIGHTS.get(x, 0))):
        wt = FIT_WEIGHTS.get(w)
        if wt:
            score += wt
            (good if wt > 0 else bad).append(w)
    score = max(0, min(10, round(score)))
    why = ", ".join(good[:3]) if score >= 5 else ", ".join(bad[:3])
    return score, why


# ---------------------------------------------------------------- AI idea
def roblox_idea(game):
    if not GEMINI_API_KEY:
        return None
    prompt = (
        "A Steam game is trending.\n"
        f"Name: {game['name']}\nGenres/tags: {', '.join(game['genres'])}\n"
        f"Description: {game['desc']}\n\n"
        "In under 40 words, pitch an ORIGINAL Roblox game inspired by its core "
        "gameplay loop. Give it a new catchy title, a different theme, and say what "
        "makes it fun/replayable on Roblox (multiplayer, progression, trading, etc.). "
        "Do not use the original game's name, characters or setting. Format: Title - pitch"
    )
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={urllib.parse.quote(GEMINI_API_KEY)}")
    d = http_json(url, data={"contents": [{"parts": [{"text": prompt}]}]}, retries=2)
    try:
        text = d["candidates"][0]["content"]["parts"][0]["text"]
        return " ".join(text.replace("*", "").split())[:300]
    except (TypeError, KeyError, IndexError):
        return None


# ---------------------------------------------------------------- notify
KIND_ICON = {"launch": "🔥", "demo": "🧪", "wishlist": "📈"}


def format_line(g):
    icon = KIND_ICON[g["kind"]]
    if g["players"] is not None:
        growth = f" ({g['players'] / g['prev']:.1f}x since {g['prev_day']})" if g.get("prev") else ""
        head = f"{icon} {g['name']} - {g['players']:,} playing now{growth}"
    else:
        head = f"{icon} {g['name']} - #{g['rank']} most wishlisted"
    when = {"launch": (f"Released {(TODAY - date.fromisoformat(g['released'])).days} days ago"
                       if g.get("released") else "Released recently"),
            "demo": "DEMO - full game not out yet",
            "wishlist": "Not out yet"}[g["kind"]]
    price = "Free" if g.get("free") else (g.get("price") or "")
    lines = [
        head,
        f"   {when} · {', '.join(g['genres'][:3]) or 'n/a'}" + (f" · {price}" if price else ""),
        f"   Roblox fit: {g['fit']}/10" + (f" ({g['fit_why']})" if g["fit_why"] else ""),
        f"   Why: {g['reason']}",
        f"   https://store.steampowered.com/app/{g['link_appid']}",
    ]
    if g.get("idea"):
        lines.append(f"   💡 Roblox idea: {g['idea']}")
    return "\n".join(lines)


def send(title, games, empty_text=None):
    body = empty_text or "\n\n".join(format_line(g) for g in games)
    log("\n===== NOTIFICATION =====\n" + title + "\n" + body + "\n========================")
    if DRY_RUN:
        log("(dry run: nothing sent)")
        return True
    if not NTFY_TOPIC and not DISCORD_WEBHOOK_URL:
        log("No NTFY_TOPIC or DISCORD_WEBHOOK_URL set - nothing sent.")
        return False

    ok = True
    if NTFY_TOPIC:
        payload = {"topic": NTFY_TOPIC, "title": title, "message": body[:3900],
                   "tags": ["video_game"]}
        if games:
            payload["click"] = f"https://store.steampowered.com/app/{games[0]['link_appid']}"
        r = http_json(NTFY_SERVER, data=payload, retries=2)
        ok &= r is not None
        log("ntfy:", "sent" if r is not None else "FAILED")

    if DISCORD_WEBHOOK_URL:
        if games:
            embeds = []
            for g in games[:10]:
                head, rest = format_line(g).split("\n", 1)
                embeds.append({
                    "title": head[:250],
                    "url": f"https://store.steampowered.com/app/{g['link_appid']}",
                    "description": rest.replace("   ", "")[:4000],
                    "color": {"launch": 0xFF5A1F, "demo": 0x7B61FF, "wishlist": 0x2EB67D}[g["kind"]],
                })
            payload = {"content": f"**{title}**", "embeds": embeds}
        else:
            payload = {"content": f"**{title}**\n{body}"[:1900]}
        sep = "&" if "?" in DISCORD_WEBHOOK_URL else "?"
        r = http_json(DISCORD_WEBHOOK_URL + sep + "wait=true", data=payload, retries=2)
        ok &= r is not None
        log("Discord:", "sent" if r is not None else "FAILED")
    return ok


# ---------------------------------------------------------------- main
def record_players(h, appid, players):
    pts = h["players"].setdefault(appid, [])
    pts[:] = [p for p in pts if p[0] != TODAY.isoformat()] + [[TODAY.isoformat(), players]]
    return previous_count(pts[:-1])


def player_reasons(players, prev, threshold):
    reasons = []
    if players >= threshold:
        reasons.append(f"{players:,}+ live players")
    if prev and prev >= MIN_FOR_GROWTH and players >= prev * GROWTH_FACTOR:
        reasons.append(f"players grew {players / prev:.1f}x")
    return reasons


def build_alert(kind, key, link_appid, info, tags, players, prev, prev_day, reasons, source, rank=None):
    fit, why = roblox_fit(info, tags)
    return {
        "kind": kind, "key": key, "link_appid": link_appid,
        "name": info.get("fullgame_name") if kind == "demo" and info.get("fullgame_name") else info["name"],
        "players": players, "prev": prev, "prev_day": prev_day, "rank": rank,
        "released": info.get("released"), "genres": (tags[:3] or info.get("genres") or []),
        "desc": info.get("desc", ""), "free": info.get("free"), "price": info.get("price"),
        "fit": fit, "fit_why": why, "reason": "; ".join(reasons) + f" [{source}]",
    }


def main():
    log(f"Steam Trend Alert v2 - {TODAY}  (launch {MIN_PLAYERS:,} / demo {MIN_DEMO_PLAYERS:,} players, "
        f"growth {GROWTH_FACTOR}x, min Roblox fit {MIN_FIT_SCORE}/10)")
    h = load_history()
    ids, wishlist = candidates_from_sources()
    if not ids:
        log("Could not reach any Steam data source. Try again later.")
        sys.exit(1)
    log(f"{len(ids)} unique candidates\n")

    oldest_ok = TODAY - timedelta(days=MAX_AGE_DAYS)
    wish_rank = {appid: i + 1 for i, appid in enumerate(wishlist)}
    tracked_before = any(d < TODAY.isoformat() for d in h["rank_days"])
    alerts, checked_demos = [], set()

    def check_demo(demo_id, parent_info, source):
        """Players on a demo -> early-warning alert for the full game."""
        if demo_id in checked_demos:
            return
        checked_demos.add(demo_id)
        players = current_players(demo_id)
        if players is None:
            return
        prev_day, prev = record_players(h, demo_id, players)
        reasons = player_reasons(players, prev, MIN_DEMO_PLAYERS)
        if reasons:
            full_id = parent_info.get("fullgame") or source[1]
            info = dict(parent_info)
            info.setdefault("fullgame_name", parent_info.get("name"))
            alerts.append(("demo:" + demo_id, players, lambda tags: build_alert(
                "demo", "demo:" + demo_id, full_id, info, tags, players, prev, prev_day,
                reasons, source[0]), full_id))

    for appid, source in ids.items():
        info = get_details(appid, h)
        if info.get("skip"):
            continue

        if info["type"] == "demo":
            check_demo(appid, info, (source, info.get("fullgame") or appid))
            continue

        if info.get("coming_soon"):
            # upcoming: check its demos, and whether it's surging up the wishlist chart
            for demo_id in info.get("demo_ids", [])[:2]:
                demo_info = dict(info, fullgame=appid, fullgame_name=info["name"])
                check_demo(demo_id, demo_info, (source, appid))
            rank = wish_rank.get(appid)
            if rank:
                past = [r for d, r in h["ranks"].get(appid, []) if d < TODAY.isoformat()]
                rk = h["ranks"].setdefault(appid, [])
                rk[:] = [p for p in rk if p[0] != TODAY.isoformat()] + [[TODAY.isoformat(), rank]]
                if tracked_before and rank <= WISHLIST_TOP and (not past or min(past) > WISHLIST_FROM):
                    was = f"from #{min(past)}" if past else "from outside the top 100"
                    reasons = [f"jumped to #{rank} most wishlisted ({was})"]
                    alerts.append(("wish:" + appid, None, lambda tags, a=appid, i=info, r=rank, rs=reasons:
                                   build_alert("wishlist", "wish:" + a, a, i, tags, None, None, None,
                                               rs, "Wishlist chart", rank=r), appid))
            continue

        if not info.get("released") or date.fromisoformat(info["released"]) < oldest_ok:
            continue
        players = current_players(appid)
        if players is None:
            continue
        prev_day, prev = record_players(h, appid, players)
        log(f"  {info['name'][:40]:40} {players:>9,} players  (prev {prev if prev is not None else '-'})")
        reasons = player_reasons(players, prev, MIN_PLAYERS)
        if reasons:
            alerts.append(("launch:" + appid, players, lambda tags, a=appid, i=info, p=players, pv=prev,
                           pd=prev_day, rs=reasons, s=source:
                           build_alert("launch", "launch:" + a, a, i, tags, p, pv, pd, rs, s), appid))

    if wishlist:
        h["rank_days"].append(TODAY.isoformat())

    # filter: not already alerted, not NSFW, good Roblox fit
    final = []
    for key, players, make, tag_appid in alerts:
        last = h["alerted"].get(key)
        if last and (players is None or players < last["players"] * REALERT_FACTOR):
            continue
        tag_info = get_details(tag_appid, h) if tag_appid else {}
        tags = get_tags(tag_appid, tag_info) if tag_info and not tag_info.get("skip") else []
        g = make(tags)
        if is_nsfw(tag_info, tags):
            log(f"  blocked (adult content): {g['name']}")
            continue
        if g["fit"] < MIN_FIT_SCORE:
            log(f"  skipped (Roblox fit {g['fit']}/10 - {g['fit_why']}): {g['name']}")
            continue
        if last:
            g["reason"] += f"; still exploding ({players / last['players']:.1f}x since last alert)"
        final.append(g)

    order = {"launch": 0, "demo": 1, "wishlist": 2}
    final.sort(key=lambda g: (order[g["kind"]], -(g["players"] or 0), g.get("rank") or 0))
    final = final[:MAX_ALERTS]
    log(f"\n{len(final)} alert(s)")

    for g in final:
        g["idea"] = roblox_idea(g)

    if final:
        n = len(final)
        kinds = {g["kind"] for g in final}
        what = "game" if kinds == {"launch"} else "trend"
        title = f"{n} Steam {what}{'s' if n > 1 else ''} to watch" + (" - Roblox ideas inside" if GEMINI_API_KEY else "")
        if send(title, final) and not DRY_RUN:
            for g in final:
                h["alerted"][g["key"]] = {"date": TODAY.isoformat(), "players": g["players"] or 0}
    elif NOTIFY_WHEN_EMPTY:
        send("Steam trends: quiet right now", [], "Nothing new is breaking out on Steam right now. (Test message - it works!)")

    if not DRY_RUN:
        save_history(h)
    log("Done.")


if __name__ == "__main__":
    main()
