#!/usr/bin/env python3
"""
Steam Trend Alert
-----------------
Finds NEW Steam games that are blowing up and sends you a phone alert
(ntfy and/or Discord), optionally with a free AI-written Roblox game idea.

Uses only the Python standard library - nothing to install.

Settings come from environment variables (see README.md):
  NTFY_TOPIC            ntfy topic name to push to          (optional)
  DISCORD_WEBHOOK_URL   Discord webhook to post to          (optional)
  GEMINI_API_KEY        free Google Gemini key for ideas    (optional)
  GEMINI_MODEL          default: gemini-2.5-flash
  MIN_PLAYERS           alert if live players >= this       (default 10000)
  GROWTH_FACTOR         alert if players grew this many x   (default 2)
  MIN_FOR_GROWTH        ...but only if yesterday had >= this (default 1000)
  MAX_AGE_DAYS          only games released in last N days  (default 60)
  MAX_ALERTS            max games per notification          (default 5)
  REALERT_FACTOR        re-alert a game if it grows this many x
                        since its last alert                (default 3)
  NOTIFY_WHEN_EMPTY     "true" = send a message even if nothing is trending

Run locally without sending anything:
  python steam_trends.py --dry-run
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
USER_AGENT = "steam-trend-alert/1.0 (personal hobby script)"


# ---------------------------------------------------------------- settings
def env_num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, "").strip() or default)
    except ValueError:
        return cast(default)


MIN_PLAYERS = env_num("MIN_PLAYERS", 10000, int)
GROWTH_FACTOR = env_num("GROWTH_FACTOR", 2.0)
MIN_FOR_GROWTH = env_num("MIN_FOR_GROWTH", 1000, int)
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
    """GET (or POST if data given) and return parsed JSON, or None on failure."""
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
            # 429 = rate limited: wait longer and retry
            wait = 30 if e.code == 429 else 3 * attempt
            log(f"  HTTP {e.code} for {short(url)} (try {attempt}/{retries})")
            if e.code in (400, 401, 403, 404) and e.code != 429:
                return None
            time.sleep(wait)
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
    h.setdefault("details", {})   # appid -> cached store info
    h.setdefault("players", {})   # appid -> [[iso_date, count], ...]
    h.setdefault("alerted", {})   # appid -> {"date":..., "players":...}
    return h


def save_history(h):
    # prune: keep 14 days of player counts, drop apps unseen for 30 days
    cutoff = (TODAY - timedelta(days=30)).isoformat()
    for appid in list(h["players"]):
        pts = h["players"][appid][-14:]
        if not pts or pts[-1][0] < cutoff:
            del h["players"][appid]
        else:
            h["players"][appid] = pts
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=1, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------- sources
def candidates_from_sources():
    """Collect app IDs from several free 'popular right now' lists."""
    ids = {}

    # 1) Steam's official Most Played chart (top 100 by daily peak)
    d = http_json("https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/")
    ranks = ((d or {}).get("response") or {}).get("ranks") or []
    for r in ranks:
        if r.get("appid"):
            ids.setdefault(str(r["appid"]), "Most Played")
    log(f"Most Played chart: {len(ranks)} games")

    # 2) Steam store: top sellers + new releases
    d = http_json("https://store.steampowered.com/api/featuredcategories?cc=gb&l=english")
    n = 0
    for key, label in (("top_sellers", "Top Seller"), ("new_releases", "New Release")):
        for it in ((d or {}).get(key) or {}).get("items") or []:
            if it.get("id"):
                ids.setdefault(str(it["id"]), label)
                n += 1
    log(f"Store top sellers/new releases: {n} games")

    # 3) SteamSpy: top 100 by players in the last 2 weeks
    d = http_json("https://steamspy.com/api.php?request=top100in2weeks")
    if isinstance(d, dict):
        for appid in d:
            if appid.isdigit():
                ids.setdefault(appid, "SteamSpy 2-week top")
        log(f"SteamSpy 2-week top: {len(d)} games")

    return ids


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
    """Store info for a game, cached in history so we don't re-ask Steam."""
    cached = h["details"].get(appid)
    # re-check unreleased / unknown-date games weekly, everything else is stable
    if cached and (cached.get("released") or cached.get("checked", "") >
                   (TODAY - timedelta(days=7)).isoformat()):
        return cached

    time.sleep(1.5)  # be polite: Steam limits appdetails to ~200 per 5 min
    d = http_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=gb&l=english")
    entry = (d or {}).get(appid) or {}
    if not entry.get("success"):
        info = {"skip": True, "checked": TODAY.isoformat()}
    else:
        data = entry.get("data") or {}
        rd = data.get("release_date") or {}
        released = None if rd.get("coming_soon") else parse_release(rd.get("date"))
        info = {
            "name": data.get("name", f"App {appid}"),
            "type": data.get("type", ""),
            "released": released.isoformat() if released else None,
            "genres": [g.get("description") for g in data.get("genres") or []][:4],
            "desc": (data.get("short_description") or "")[:400],
            "free": bool(data.get("is_free")),
            "price": ((data.get("price_overview") or {}).get("final_formatted") or ""),
            "checked": TODAY.isoformat(),
        }
        if info["type"] != "game":
            info["skip"] = True
    h["details"][appid] = info
    return info


def current_players(appid):
    d = http_json("https://api.steampowered.com/ISteamUserStats/"
                  f"GetNumberOfCurrentPlayers/v1/?appid={appid}")
    resp = (d or {}).get("response") or {}
    return resp.get("player_count") if resp.get("result") == 1 else None


def previous_count(points):
    """Most recent player count from a day before today."""
    for day, count in reversed(points):
        if day < TODAY.isoformat():
            return day, count
    return None, None


# ---------------------------------------------------------------- AI idea
def roblox_idea(game):
    if not GEMINI_API_KEY:
        return None
    prompt = (
        "A new Steam game is trending.\n"
        f"Name: {game['name']}\nGenres: {', '.join(game['genres'])}\n"
        f"Description: {game['desc']}\n\n"
        "In under 40 words, pitch an ORIGINAL Roblox game inspired by its core "
        "gameplay loop. Give it a new catchy title, a different theme, and say "
        "what makes it fun/replayable on Roblox (multiplayer, progression, "
        "trading, etc.). Do not use the original game's name, characters or "
        "setting. Format: Title - pitch"
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
def format_line(g):
    growth = ""
    if g["prev"]:
        growth = f" ({g['players'] / g['prev']:.1f}x since {g['prev_day']})"
    age = (TODAY - date.fromisoformat(g["released"])).days
    price = "Free" if g["free"] else (g["price"] or "")
    lines = [
        f"🔥 {g['name']} - {g['players']:,} playing now{growth}",
        f"   Released {age} days ago · {', '.join(g['genres']) or 'n/a'}"
        + (f" · {price}" if price else ""),
        f"   Why flagged: {g['reason']}",
        f"   https://store.steampowered.com/app/{g['appid']}",
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
            payload["click"] = f"https://store.steampowered.com/app/{games[0]['appid']}"
        r = http_json(NTFY_SERVER, data=payload, retries=2)
        ok &= r is not None
        log("ntfy:", "sent" if r is not None else "FAILED")

    if DISCORD_WEBHOOK_URL:
        if games:
            embeds = []
            for g in games[:10]:
                desc = format_line(g).split("\n", 1)[1].replace("   ", "")
                embeds.append({
                    "title": f"🔥 {g['name']} - {g['players']:,} playing",
                    "url": f"https://store.steampowered.com/app/{g['appid']}",
                    "description": desc[:4000],
                    "color": 0xFF5A1F,
                })
            payload = {"content": f"**{title}**", "embeds": embeds}
        else:
            payload = {"content": f"**{title}**\n{body}"[:1900]}
        r = http_json(DISCORD_WEBHOOK_URL + ("&" if "?" in DISCORD_WEBHOOK_URL else "?") + "wait=true",
                      data=payload, retries=2)
        ok &= r is not None
        log("Discord:", "sent" if r is not None else "FAILED")
    return ok


# ---------------------------------------------------------------- main
def main():
    log(f"Steam Trend Alert - {TODAY}  (min players {MIN_PLAYERS:,}, "
        f"growth {GROWTH_FACTOR}x, max age {MAX_AGE_DAYS} days)")
    h = load_history()
    ids = candidates_from_sources()
    if not ids:
        log("Could not reach any Steam data source. Try again later.")
        sys.exit(1)
    log(f"{len(ids)} unique candidate games\n")

    oldest_ok = TODAY - timedelta(days=MAX_AGE_DAYS)
    trending = []
    for appid, source in ids.items():
        info = get_details(appid, h)
        if info.get("skip") or not info.get("released"):
            continue
        if date.fromisoformat(info["released"]) < oldest_ok:
            continue  # not a new game

        players = current_players(appid)
        if players is None:
            continue
        pts = h["players"].setdefault(appid, [])
        pts[:] = [p for p in pts if p[0] != TODAY.isoformat()] + [[TODAY.isoformat(), players]]
        prev_day, prev = previous_count(pts[:-1])
        log(f"  {info['name'][:40]:40} {players:>9,} players  (prev {prev if prev is not None else '-'})")

        reasons = []
        if players >= MIN_PLAYERS:
            reasons.append(f"{players:,}+ live players")
        if prev and prev >= MIN_FOR_GROWTH and players >= prev * GROWTH_FACTOR:
            reasons.append(f"players grew {players / prev:.1f}x")
        if not reasons:
            continue

        last = h["alerted"].get(appid)
        if last and players < last["players"] * REALERT_FACTOR:
            continue  # already told you about this one
        if last:
            reasons.append(f"still exploding ({players / last['players']:.1f}x since last alert)")

        trending.append({**info, "appid": appid, "players": players, "prev": prev,
                         "prev_day": prev_day, "reason": "; ".join(reasons) + f" [{source}]"})

    trending.sort(key=lambda g: g["players"], reverse=True)
    trending = trending[:MAX_ALERTS]
    log(f"\n{len(trending)} game(s) to alert about")

    for g in trending:
        g["idea"] = roblox_idea(g)

    if trending:
        title = (f"{len(trending)} new Steam game{'s' if len(trending) > 1 else ''} "
                 f"blowing up - Roblox ideas inside" if GEMINI_API_KEY else
                 f"{len(trending)} new Steam game{'s' if len(trending) > 1 else ''} blowing up")
        if send(title, trending) and not DRY_RUN:
            for g in trending:
                h["alerted"][g["appid"]] = {"date": TODAY.isoformat(), "players": g["players"]}
    elif NOTIFY_WHEN_EMPTY:
        send("Steam trends: quiet day", [], "No new Steam games broke out today.")

    if not DRY_RUN:
        save_history(h)
    log("Done.")


if __name__ == "__main__":
    main()
