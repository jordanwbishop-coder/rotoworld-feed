#!/usr/bin/env python3
from __future__ import annotations

import csv, hashlib, html, re, sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://www.nbcsports.com/fantasy/football/player-news"
SITE = "https://www.nbcsports.com"
PAGES = 6
MAX_ITEMS = 500
CSV_FILE = Path("news.csv")
RSS_FILE = Path("feed.xml")
UA = "Mozilla/5.0 (compatible; RotoworldFeed/2.1)"

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Chicago")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=-6))

TEAM_RE = re.compile(r"^[A-Z]{2,3}$")
NFL_TEAMS = {
    "ARI","ATL","BAL","BUF","CAR","CHI","CIN","CLE","DAL","DEN","DET","GB",
    "HOU","IND","JAX","KC","LAC","LAR","LV","MIA","MIN","NE","NO","NYG",
    "NYJ","PHI","PIT","SEA","SF","TB","TEN","WAS"
}
META_RE = re.compile(r"^(?P<team>[A-Z]{2,3})\s+(?P<pos>.+?)(?:\s+#\d+)?$")
REL_RE = re.compile(
    r"\b(?P<n>\d+)\s*(?P<u>minute|minutes|min|mins|hour|hours|hr|hrs|day|days)\s+ago\b",
    re.I,
)

@dataclass
class Item:
    player_name: str = ""
    team_initials: str = ""
    position: str = ""
    headline: str = ""
    news_snippet: str = ""
    source: str = ""
    rotoworld_author: str = ""
    date: str = ""
    url: str = ""

    @property
    def key(self):
        s = "|".join([
            clean(self.player_name).lower(),
            clean(self.headline).lower(),
            clean(self.news_snippet)[:180].lower(),
        ])
        return hashlib.sha256(s.encode()).hexdigest()

    @property
    def guid(self):
        return self.url or "urn:sha256:" + self.key

def clean(x):
    return re.sub(r"\s+", " ", str(x or "").replace("\xa0", " ")).strip()

def local_now():
    return datetime.now(LOCAL_TZ)

def csv_date(dt):
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"

def parse_iso(s):
    try:
        dt = datetime.fromisoformat(clean(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ)
    except Exception:
        return None

def parse_date_text(text):
    t = clean(text)

    # Absolute date anywhere in text.
    m = re.search(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
        r"[a-z]*\s+\d{1,2},\s+\d{4})\b", t, re.I
    )
    if m:
        val = m.group(1).replace("Sept ", "Sep ")
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(val, fmt).replace(tzinfo=LOCAL_TZ)
            except ValueError:
                pass

    now = local_now()
    low = t.lower()

    if "yesterday" in low:
        return now - timedelta(days=1)
    if "today" in low or "just now" in low:
        return now

    m = REL_RE.search(t)
    if m:
        n = int(m.group("n"))
        u = m.group("u").lower()
        if u.startswith(("minute", "min")):
            return now - timedelta(minutes=n)
        if u.startswith(("hour", "hr")):
            return now - timedelta(hours=n)
        if u.startswith("day"):
            return now - timedelta(days=n)
    return None

def get(url):
    r = requests.get(
        url,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text

def strings(card):
    return [clean(x) for x in card.stripped_strings if clean(x)]

def find_cards(soup):
    """
    Rotoworld cards contain a 'Player Stats' marker. Walk upward to the
    smallest ancestor that also contains 'More ... News'.
    """
    cards, seen = [], set()
    for node in soup.find_all(string=lambda x: clean(x) == "Player Stats"):
        cur = node.parent
        chosen = None
        for _ in range(10):
            if not isinstance(cur, Tag):
                break
            text = clean(cur.get_text(" ", strip=True))
            if len(text) > 7000:
                break
            if re.search(r"\bMore .+ News\b", text):
                chosen = cur
                if len(text) < 4000:
                    break
            cur = cur.parent
        if chosen is not None and id(chosen) not in seen:
            seen.add(id(chosen))
            cards.append(chosen)
    return cards

def player_from_card(card, ss):
    # Player profile link is usually the first short useful link.
    for a in card.find_all("a", href=True):
        t = clean(a.get_text(" ", strip=True))
        if not t or t == "Player Stats" or t.startswith("More ") or len(t) > 80:
            continue
        href = clean(a.get("href"))
        if "player-news?p=" not in href:
            return t

    # Fallback: text immediately before team metadata.
    for i, t in enumerate(ss):
        if ((TEAM_RE.fullmatch(t) and t in NFL_TEAMS) or (META_RE.fullmatch(t) and META_RE.fullmatch(t).group("team") in NFL_TEAMS)) and i:
            p = ss[i - 1]
            if len(p) <= 80 and p != "Player Stats":
                return p
    return ""

def team_position(ss):
    # Combined: "MIN Tight End #87"
    for t in ss:
        m = META_RE.fullmatch(t)
        if m and m.group("team") in NFL_TEAMS:
            pos = re.sub(r"\s+#\d+$", "", m.group("pos")).strip()
            return m.group("team"), pos

    # Split: "MIN" then "Tight End" then "#87"
    for i, t in enumerate(ss):
        if TEAM_RE.fullmatch(t) and t in NFL_TEAMS:
            for p in ss[i+1:i+4]:
                if p == "Player Stats" or p.startswith("More "):
                    break
                if re.fullmatch(r"#\d+", p):
                    continue
                if 2 <= len(p) <= 45 and not p.endswith("."):
                    return t, re.sub(r"\s+#\d+$", "", p).strip()
    return "", ""

def headline_snippet(card, ss, player):
    # Find start just after personalization text.
    start = 0
    for i, t in enumerate(ss):
        if "personalize your rotoworld feed" in t.lower():
            start = i + 1
            break

    skip = {
        "Player Stats", "Headline", "Injury", "Recap", "Transaction",
        "Link copied to clipboard!"
    }

    headline = ""
    headline_idx = -1

    # Prefer visible heading.
    for h in card.find_all(["h2", "h3", "h4", "h5"]):
        t = clean(h.get_text(" ", strip=True))
        if 20 <= len(t) <= 500 and t != player and not t.startswith("More "):
            headline = t
            try: headline_idx = ss.index(t)
            except ValueError: pass
            break

    # Fallback to first sentence-sized text after personalization.
    if not headline:
        for i, t in enumerate(ss[start:], start):
            low = t.lower()
            if t in skip or t == player or t.startswith("More ") or low.startswith("source:"):
                continue
            if TEAM_RE.fullmatch(t) or re.fullmatch(r"#\d+", t):
                continue
            if len(t) >= 20:
                headline, headline_idx = t, i
                break

    if not headline:
        return "", ""

    # First substantial text after headline is the analysis.
    snippet = ""
    for t in ss[headline_idx + 1:]:
        low = t.lower()
        if t in skip or t.startswith("More ") or low.startswith("source:"):
            if snippet:
                break
            continue
        if t == player or TEAM_RE.fullmatch(t) or re.fullmatch(r"#\d+", t):
            if snippet:
                break
            continue
        if t.startswith("- ") and snippet:
            break
        if len(t) >= 30 and t != headline:
            snippet = t
            break

    return headline, snippet

def source_author(card, ss, snippet):
    source = ""
    author = ""

    for i, t in enumerate(ss):
        if t.lower().startswith("source:"):
            source = clean(t.split(":", 1)[1])
            if not source and i + 1 < len(ss):
                source = ss[i + 1]
            break

    # Typical Rotoworld byline at end: " ... - Nick Shlain"
    m = re.search(
        r"\s-\s([A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){1,3})\s*$",
        snippet
    )
    if m:
        author = m.group(1)
    else:
        for i, t in enumerate(ss):
            low = t.lower()
            if low.startswith("by ") and 3 <= len(t[3:]) <= 80:
                author = clean(t[3:])
                break
            if low in {"author", "rotoworld author"} and i + 1 < len(ss):
                candidate = clean(ss[i + 1])
                if 3 <= len(candidate) <= 80:
                    author = candidate
                    break
            if t.startswith("- ") and 3 <= len(t[2:]) <= 80:
                candidate = clean(t[2:])
                # Avoid treating source/publication names as people.
                if " " in candidate and not candidate.lower().startswith(("espn", "nfl", "nbc")):
                    author = candidate
                    break

    if author:
        snippet = re.sub(r"\s*-\s*" + re.escape(author) + r"\s*$", "", snippet).strip()

    return source, author, snippet

def card_date(card):
    # 1. Best source: machine-readable <time datetime="">
    for tag in card.find_all("time"):
        dt = parse_iso(tag.get("datetime"))
        if dt:
            return csv_date(dt)
        dt = parse_date_text(tag.get_text(" ", strip=True))
        if dt:
            return csv_date(dt)

    # 2. Other machine-readable attributes.
    for tag in card.find_all(True):
        for attr in ("datetime", "data-date", "data-datetime", "data-timestamp"):
            val = tag.get(attr)
            if not val:
                continue
            dt = parse_iso(val)
            if dt:
                return csv_date(dt)
            sval = clean(val)
            if sval.isdigit() and len(sval) >= 10:
                try:
                    dt = datetime.fromtimestamp(int(sval[:10]), timezone.utc).astimezone(LOCAL_TZ)
                    return csv_date(dt)
                except Exception:
                    pass

    # 3. Visible absolute/relative time.
    dt = parse_date_text(card.get_text(" ", strip=True))
    if dt:
        return csv_date(dt)

    # 4. Last resort for a newly-seen item.
    return csv_date(local_now())

def card_url(card):
    candidates = []
    for a in card.find_all("a", href=True):
        href = clean(a.get("href"))
        if not href or href.startswith("#"):
            continue
        full = urljoin(SITE, href)
        if "nbcsports.com" not in full or "?p=" in full:
            continue
        if full.rstrip("/") == BASE.rstrip("/"):
            continue
        text = clean(a.get_text(" ", strip=True))
        candidates.append((full, text))

    # Prefer a specific Rotoworld/player-news link.
    for full, _ in candidates:
        if "/fantasy/football/player-news/" in full:
            return full

    # Otherwise use a specific NBC Sports link only if it is not navigation.
    for full, text in candidates:
        if text and text not in {"Player Stats", "More News"}:
            if "/fantasy/football/" in full:
                return full
    return ""

def parse_page(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    out, seen = [], set()

    for card in find_cards(soup):
        ss = strings(card)
        player = player_from_card(card, ss)
        team, pos = team_position(ss)
        headline, snippet = headline_snippet(card, ss, player)
        if not headline or not snippet:
            continue

        source, author, snippet = source_author(card, ss, snippet)
        item = Item(
            player_name=player,
            team_initials=team,
            position=pos,
            headline=headline,
            news_snippet=snippet,
            source=source,
            rotoworld_author=author,
            date=card_date(card),
            url=card_url(card),
        )

        if item.key not in seen:
            seen.add(item.key)
            out.append(item)

    return out

def scrape_six_pages():
    all_items, seen = [], set()
    for p in range(1, PAGES + 1):
        url = f"{BASE}?p={p}"
        print(f"Scraping page {p}/{PAGES}: {url}")
        items = parse_page(get(url))
        print(f"  found {len(items)} items")
        for x in items:
            if x.key not in seen:
                seen.add(x.key)
                all_items.append(x)
    return all_items

def load_existing():
    if not CSV_FILE.exists():
        return []
    result = []
    with CSV_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("Headline") or not r.get("News Snippet"):
                continue
            result.append(Item(
                r.get("Player Name", ""),
                r.get("Team Initials", ""),
                r.get("Position", ""),
                r.get("Headline", ""),
                r.get("News Snippet", ""),
                r.get("Source", ""),
                r.get("Rotoworld Author", ""),
                r.get("Date", ""),
                r.get("URL", ""),
            ))
    return result

def merge(new):
    old = {x.key: x for x in load_existing()}
    out, seen = [], set()

    for x in new:
        if x.key in old:
            y = old[x.key]
            x.team_initials = x.team_initials or y.team_initials
            x.position = x.position or y.position
            x.source = x.source or y.source
            x.rotoworld_author = x.rotoworld_author or y.rotoworld_author
            x.date = x.date or y.date
            x.url = x.url or y.url
        if x.key not in seen:
            seen.add(x.key)
            out.append(x)

    for x in old.values():
        if x.key not in seen:
            seen.add(x.key)
            out.append(x)

    return out[:MAX_ITEMS]

def write_csv(items):
    fields = [
        "Player Name", "Team Initials", "Position", "Headline",
        "News Snippet", "Source", "Rotoworld Author", "Date", "URL"
    ]
    with CSV_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in items:
            w.writerow({
                "Player Name": x.player_name,
                "Team Initials": x.team_initials,
                "Position": x.position,
                "Headline": x.headline,
                "News Snippet": x.news_snippet,
                "Source": x.source,
                "Rotoworld Author": x.rotoworld_author,
                "Date": x.date,
                "URL": x.url,
            })

def pubdate(s):
    dt = parse_date_text(s) or local_now()
    return format_datetime(dt.astimezone(timezone.utc))

def write_rss(items):
    ns = "https://example.com/rotoworld-feed"
    ET.register_namespace("rw", ns)
    rss = ET.Element("rss", {"version": "2.0", "xmlns:rw": ns})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "NBC Sports Rotoworld NFL Player News"
    ET.SubElement(ch, "link").text = BASE
    ET.SubElement(ch, "description").text = "Unofficial personal Rotoworld NFL player-news feed."
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for x in items:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = x.headline
        ET.SubElement(it, "link").text = x.url or BASE
        g = ET.SubElement(it, "guid", {"isPermaLink": "true" if x.url else "false"})
        g.text = x.guid
        ET.SubElement(it, "pubDate").text = pubdate(x.date)

        meta = " ".join(v for v in (x.player_name, x.team_initials, x.position) if v)
        desc = []
        if meta:
            desc.append(f"<p><strong>{html.escape(meta)}</strong></p>")
        desc.append(f"<p>{html.escape(x.news_snippet)}</p>")
        if x.source:
            desc.append(f"<p><strong>Source:</strong> {html.escape(x.source)}</p>")
        if x.rotoworld_author:
            desc.append(f"<p><strong>Rotoworld Author:</strong> {html.escape(x.rotoworld_author)}</p>")
        ET.SubElement(it, "description").text = "".join(desc)

        for tag, val in {
            "player": x.player_name,
            "team": x.team_initials,
            "position": x.position,
            "source": x.source,
            "author": x.rotoworld_author,
            "date": x.date,
        }.items():
            ET.SubElement(it, f"{{{ns}}}{tag}").text = val

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(RSS_FILE, encoding="utf-8", xml_declaration=True)

def main():
    current = scrape_six_pages()
    if not current:
        raise RuntimeError("No Rotoworld items parsed from pages 1-6.")
    items = merge(current)
    write_csv(items)
    write_rss(items)
    print(f"Parsed {len(current)} current items from pages 1-{PAGES}.")
    print(f"Stored {len(items)} total items.")

if __name__ == "__main__":
    main()
