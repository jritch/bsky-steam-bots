#!/usr/bin/env python3
"""Post every new Steam game to one of two Bluesky accounts:

  * the "AI" bot   - games whose store page has an AI Generated Content Disclosure
  * the "no AI" bot - games without one

Designed to run on a schedule (GitHub Actions cron). Each run:
  1. reads Steam's newest-releases list,
  2. looks up every unseen game with Steam's store API (IStoreBrowseService/GetItems),
     which returns the developer's AI disclosure as `extra_details.content_survey_ai_notes`,
  3. applies the NSFW policy,
  4. posts to the matching Bluesky account and records the game in state/seen.json.

Usage:
  python bot.py              # normal run
  python bot.py --dry-run    # fetch + classify, print posts, don't post or save state
  python bot.py --seed       # mark everything currently listed as seen, post nothing
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state" / "seen.json"
CONFIG_PATH = ROOT / "config.toml"

SEARCH_URL = "https://store.steampowered.com/search/results/"
GET_ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
HEADER_FALLBACK = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"

USER_AGENT = "steam-bsky-release-bots/1.0 (+https://bsky.app)"
POST_LIMIT = 300            # Bluesky grapheme limit
STATE_RETENTION_DAYS = 60
MAX_POST_ATTEMPTS = 3       # give up on a game after this many failed posts

log = logging.getLogger("steam-bsky")


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    ai_enabled: bool = True
    no_ai_enabled: bool = True
    nsfw_mode: str = "skip"
    nsfw_descriptors: set[int] = field(default_factory=lambda: {3, 4})
    nsfw_tags: set[int] = field(default_factory=lambda: {24904, 12095, 6650, 9130})
    scan_depth: int = 200
    max_age_days: float = 3
    country: str = "US"
    language: str = "english"
    max_posts_per_run: int = 25
    delay_seconds: float = 3
    quote_ai_disclosure: bool = True

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
        b, n, d, p = (raw.get(k, {}) for k in ("bots", "nsfw", "discovery", "posting"))
        cfg = cls(
            ai_enabled=b.get("ai_disclosed_enabled", True),
            no_ai_enabled=b.get("no_ai_disclosure_enabled", True),
            nsfw_mode=(os.environ.get("NSFW_MODE") or n.get("mode", "skip")).strip().lower(),
            nsfw_descriptors=set(n.get("content_descriptors", [3, 4])),
            nsfw_tags=set(n.get("tag_ids", [24904, 12095, 6650, 9130])),
            scan_depth=int(d.get("scan_depth", 200)),
            max_age_days=float(d.get("max_age_days", 3)),
            country=d.get("country", "US"),
            language=d.get("language", "english"),
            max_posts_per_run=int(p.get("max_posts_per_run", 25)),
            delay_seconds=float(p.get("delay_seconds", 3)),
            quote_ai_disclosure=p.get("quote_ai_disclosure", True),
        )
        if cfg.nsfw_mode not in ("skip", "label", "post"):
            raise SystemExit(f"nsfw.mode must be skip, label or post (got {cfg.nsfw_mode!r})")
        return cfg


# --------------------------------------------------------------------------- steam

@dataclass
class Game:
    appid: int
    name: str
    url: str
    release_ts: int
    developers: list[str]
    short_description: str
    price: str
    ai_notes: str            # "" when there's no AI disclosure
    descriptors: set[int]
    tag_ids: set[int]
    header_url: str
    early_access: bool

    @property
    def has_ai_disclosure(self) -> bool:
        return bool(self.ai_notes.strip())


class Steam:
    def __init__(self, cfg: Config, session: requests.Session | None = None):
        self.cfg = cfg
        self.http = session or requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT

    def _get(self, url: str, params: dict, tries: int = 4) -> requests.Response:
        for attempt in range(tries):
            try:
                r = self.http.get(url, params=params, timeout=30)
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r
            except (requests.RequestException, ValueError) as e:
                if attempt == tries - 1:
                    raise
                wait = 5 * 2 ** attempt
                log.warning("Steam request failed (%s); retrying in %ss", e, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def newest_appids(self) -> list[int]:
        """App ids from Steam search, newest release first (games only)."""
        ids: list[int] = []
        for start in range(0, self.cfg.scan_depth, 100):
            r = self._get(SEARCH_URL, {
                "sort_by": "Released_DESC",
                "category1": 998,           # games
                "infinite": 1,
                "start": start,
                "count": min(100, self.cfg.scan_depth - start),
                "cc": self.cfg.country,
                "l": self.cfg.language,
                "ignore_preferences": 1,
            })
            html = r.json().get("results_html", "")
            page = parse_search_appids(html)
            if not page:
                break
            ids.extend(i for i in page if i not in ids)
            time.sleep(1)
        return ids

    def get_items(self, appids: list[int]) -> list[Game]:
        games: list[Game] = []
        for i in range(0, len(appids), 50):
            batch = appids[i:i + 50]
            payload = {
                "ids": [{"appid": a} for a in batch],
                "context": {"language": self.cfg.language, "country_code": self.cfg.country},
                "data_request": {
                    "include_basic_info": True,
                    "include_release": True,
                    "include_assets": True,
                    "include_ratings": True,
                    "include_tag_count": 20,
                    "include_extra_details": True,
                },
            }
            r = self._get(GET_ITEMS_URL, {"input_json": json.dumps(payload)})
            items = (r.json().get("response") or {}).get("store_items") or []
            for item in items:
                g = parse_store_item(item)
                if g:
                    games.append(g)
            time.sleep(1)
        return games


_APPID_RE = re.compile(r'<a[^>]*\bdata-ds-appid="(\d+)', re.I)


def parse_search_appids(html: str) -> list[int]:
    out: list[int] = []
    for m in _APPID_RE.finditer(html):
        a = int(m.group(1))
        if a not in out:
            out.append(a)
    return out


def parse_store_item(item: dict) -> Game | None:
    """Turn a GetItems store_item into a Game, or None if it isn't a visible game."""
    if item.get("success") != 1 or not item.get("visible") or not item.get("appid"):
        return None
    if item.get("type", 0) != 0:     # 0 = game (skip DLC, software, demos, etc.)
        return None
    appid = int(item["appid"])
    release = item.get("release") or {}
    basic = item.get("basic_info") or {}
    extra = item.get("extra_details") or {}
    assets = item.get("assets") or {}

    header = HEADER_FALLBACK.format(appid=appid)
    fmt, hdr = assets.get("asset_url_format"), assets.get("header")
    if fmt and hdr:
        header = ASSET_BASE + fmt.replace("${FILENAME}", hdr)

    if item.get("is_free"):
        price = "Free"
    else:
        price = ((item.get("best_purchase_option") or {}).get("formatted_final_price") or "").strip()

    path = item.get("store_url_path") or f"app/{appid}/"
    tag_ids = set(item.get("tagids") or []) | {t.get("tagid") for t in item.get("tags") or [] if t.get("tagid")}

    return Game(
        appid=appid,
        name=(item.get("name") or f"App {appid}").strip(),
        url=f"https://store.steampowered.com/{path.lstrip('/')}",
        release_ts=int(release.get("steam_release_date") or 0),
        developers=[d.get("name", "").strip() for d in basic.get("developers") or [] if d.get("name")],
        short_description=clean_text(basic.get("short_description") or ""),
        price=price,
        ai_notes=clean_text(extra.get("content_survey_ai_notes") or ""),
        descriptors=set(item.get("content_descriptorids") or []),
        tag_ids=tag_ids,
        header_url=header,
        early_access=bool(item.get("is_early_access") or release.get("is_early_access")),
    ) if not (item.get("is_coming_soon") or release.get("is_coming_soon")) else None


def clean_text(s: str) -> str:
    s = re.sub(r"\[/?[a-z0-9*]+(=[^\]]*)?\]", "", s, flags=re.I)  # bbcode
    s = re.sub(r"<[^>]+>", "", s)                                   # html
    s = (s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- policy

def nsfw_labels(g: Game, cfg: Config) -> list[str]:
    """Bluesky self-labels for this game; empty list means not NSFW."""
    hits_desc = g.descriptors & cfg.nsfw_descriptors
    hits_tags = g.tag_ids & cfg.nsfw_tags
    if not hits_desc and not hits_tags:
        return []
    return ["porn"] if 3 in hits_desc else ["sexual"]


def is_recent(g: Game, now: float, cfg: Config) -> bool:
    return g.release_ts > 0 and g.release_ts <= now + 3600 and now - g.release_ts <= cfg.max_age_days * 86400


# --------------------------------------------------------------------------- formatting

def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    if n <= 1:
        return s[:max(n, 0)]
    cut = s[:n - 1].rstrip()
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp].rstrip(" ,.;:-")
    return cut + "…"


def compose_text(g: Game, ai_bot: bool, cfg: Config) -> str:
    """Plain post text (the store link goes in the link card)."""
    head = g.name
    meta = []
    if g.developers:
        meta.append("by " + ", ".join(g.developers[:2]))
    if g.price:
        meta.append(g.price)
    if g.early_access:
        meta.append("Early Access")
    lines = [head] + ([" · ".join(meta)] if meta else [])
    base = "\n".join(lines)

    if ai_bot and cfg.quote_ai_disclosure and g.ai_notes:
        prefix = "\n\nAI disclosure: “"
        room = POST_LIMIT - len(base) - len(prefix) - 1
        body = prefix + truncate(g.ai_notes, room) + "”" if room > 20 else ""
    elif g.short_description:
        prefix = "\n\n"
        room = POST_LIMIT - len(base) - len(prefix)
        body = prefix + truncate(g.short_description, room) if room > 20 else ""
    else:
        body = ""
    return truncate(base + body, POST_LIMIT)


# --------------------------------------------------------------------------- bluesky

class Poster:
    def __init__(self, name: str, handle: str, password: str, dry_run: bool = False):
        self.name, self.handle, self.dry_run = name, handle, dry_run
        self.client = None
        if not dry_run:
            from atproto import Client
            self.client = Client(base_url=os.environ.get("BSKY_PDS") or None)
            self.client.login(handle, password)
            log.info("Logged in to Bluesky as %s (%s bot)", handle, name)

    def post(self, g: Game, text: str, labels: list[str], http: requests.Session) -> None:
        if self.dry_run:
            print(f"--- [{self.name}] {g.url}" + (f"  labels={labels}" if labels else ""))
            print(text)
            print()
            return
        from atproto import models
        thumb = None
        try:
            r = http.get(g.header_url, timeout=20)
            if r.ok and 0 < len(r.content) < 950_000:
                thumb = self.client.upload_blob(r.content).blob
        except Exception as e:  # thumbnail is optional
            log.warning("thumbnail failed for %s: %s", g.appid, e)

        embed = models.AppBskyEmbedExternal.Main(external=models.AppBskyEmbedExternal.External(
            uri=g.url,
            title=g.name[:300],
            description=truncate(g.short_description, 300),
            thumb=thumb,
        ))
        record = models.AppBskyFeedPost.Record(
            created_at=self.client.get_current_time_iso(),
            text=text,
            embed=embed,
            langs=["en"],
            labels=models.ComAtprotoLabelDefs.SelfLabels(
                values=[models.ComAtprotoLabelDefs.SelfLabel(val=v) for v in labels]
            ) if labels else None,
        )
        self.client.app.bsky.feed.post.create(self.client.me.did, record)


# --------------------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict, now: float) -> None:
    cutoff = now - STATE_RETENTION_DAYS * 86400
    seen = {k: v for k, v in state.get("seen", {}).items() if v.get("t", now) >= cutoff}
    state["seen"] = seen
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- main

def run(cfg: Config, dry_run: bool = False, seed: bool = False, steam: Steam | None = None,
        posters: dict[str, Poster] | None = None, now: float | None = None) -> dict:
    now = now or time.time()
    steam = steam or Steam(cfg)
    state = load_state()
    first_run = "seen" not in state
    seen: dict = state.setdefault("seen", {})

    appids = steam.newest_appids()
    log.info("Steam newest list: %d apps", len(appids))
    fresh = [a for a in appids if str(a) not in seen]
    games = steam.get_items(fresh) if fresh else []
    log.info("%d unseen, %d released games looked up", len(fresh), len(games))

    if seed or (first_run and not dry_run):
        for g in games:
            seen[str(g.appid)] = {"t": now, "result": "seeded"}
        save_state(state, now)
        log.info("Seeded %d games; nothing posted. Future runs post new releases.", len(games))
        return {"seeded": len(games)}

    if posters is None:
        posters = {}
        for key, env, enabled in (("ai", "AI", cfg.ai_enabled), ("no_ai", "NO_AI", cfg.no_ai_enabled)):
            if not enabled:
                continue
            h, p = os.environ.get(f"BSKY_{env}_HANDLE"), os.environ.get(f"BSKY_{env}_APP_PASSWORD")
            if dry_run:
                posters[key] = Poster(key, h or key, "", dry_run=True)
            elif h and p:
                posters[key] = Poster(key, h, p)
            else:
                log.warning("No credentials for %s bot (BSKY_%s_HANDLE / BSKY_%s_APP_PASSWORD); skipping it", key, env, env)

    counts = {"ai": 0, "no_ai": 0, "nsfw_skipped": 0, "old_skipped": 0, "failed": 0}
    # oldest first, so the feed reads in release order
    for g in sorted(games, key=lambda x: (x.release_ts, x.appid)):
        key = "ai" if g.has_ai_disclosure else "no_ai"
        retries: dict = state.setdefault("retry", {})

        if not is_recent(g, now, cfg):
            seen[str(g.appid)] = {"t": now, "result": "old"}
            counts["old_skipped"] += 1
            continue
        labels = nsfw_labels(g, cfg)
        if labels and cfg.nsfw_mode == "skip":
            seen[str(g.appid)] = {"t": now, "result": "nsfw"}
            counts["nsfw_skipped"] += 1
            continue
        if cfg.nsfw_mode == "post":
            labels = []
        poster = posters.get(key)
        if poster is None:
            enabled = cfg.ai_enabled if key == "ai" else cfg.no_ai_enabled
            if not enabled:
                seen[str(g.appid)] = {"t": now, "result": f"{key}-disabled"}
            continue
        if counts[key] >= cfg.max_posts_per_run:
            continue  # leave unseen; picked up next run

        try:
            poster.post(g, compose_text(g, key == "ai", cfg), labels, steam.http)
            seen[str(g.appid)] = {"t": now, "result": key}
            retries.pop(str(g.appid), None)
            counts[key] += 1
            if not dry_run:
                time.sleep(cfg.delay_seconds)
        except Exception as e:
            counts["failed"] += 1
            tries = retries.get(str(g.appid), 0) + 1
            log.error("Posting %s (%s) to %s failed (attempt %d): %s", g.name, g.appid, key, tries, e)
            if tries >= MAX_POST_ATTEMPTS:
                seen[str(g.appid)] = {"t": now, "result": "failed"}
                retries.pop(str(g.appid), None)
            else:
                retries[str(g.appid)] = tries  # try again next run

    if not dry_run:
        save_state(state, now)
    log.info("Done: %s", counts)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print posts instead of posting; don't save state")
    ap.add_argument("--seed", action="store_true", help="mark current releases as seen without posting")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    counts = run(Config.load(), dry_run=args.dry_run, seed=args.seed)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
