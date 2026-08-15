#!/usr/bin/env python3
"""
NBC Sports Rotoworld NFL Player News -> RSS + CSV

Outputs:
  feed.xml
  news.csv

Designed for:
  https://www.nbcsports.com/fantasy/football/player-news

Notes:
- Uses only public HTML.
- Does not bypass logins/paywalls.
- Parsing is deliberately heuristic because NBC can change page markup.
"""

from __future__ import annotations

import csv
import hashlib
import html
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup, Tag

PAGE_URL = "https://www.nbcsports.com/fantasy/football/player-news"
BASE_URL = "https://www.nbcsports.com"
OUT_CSV = Path("news.csv")
OUT_RSS = Path("feed.xml")
MAX_ITEMS = 100

UA = (
    "Mozilla/5.0 (compatible; RotoworldFeed/1.0; "
    "+personal RSS reader)"
)

ARTICLE_RE = re.compile(
    r"/fantasy/football/player-news/"
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})/"
    r"(?P<slug>[^?#/]+)"
)

TEAM_POSITION_RE = re.compile(
    r"^(?P<team>[A-Z]{2,3})\s+(?P<position>.+?)(?:\s+#\d+)?$"
)

SOURCE_RE = re.compile(r"^Source:\s*(.+)$", re.I)


@dataclass
class NewsItem:
    player_name: str
    team_initials: str
    position: str
    headline: str
    news_snippet: str
    source: str
    rotoworld_author: str
    date: str
    url: str

    @property
    def guid(self) -> str:
        if self.url:
            return self.url
        payload = "|".join(
            [self.player_name, self.headline, self.date, self.news_snippet]
        )
        return "urn:sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clean(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def get(url: str) -> str:
    r = requests.get(
        url,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def article_date_from_url(url: str) -> str:
    m = ARTICLE_RE.search(url)
    if not m:
        return ""
    dt = datetime(
        int(m.group("year")),
        int(m.group("month")),
        int(m.group("day")),
        tzinfo=timezone.utc,
    )
    return dt.strftime("%b %-d, %Y") if sys.platform != "win32" else dt.strftime("%b %#d, %Y")


def candidate_article_links(soup: BeautifulSoup) -> list[str]:
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if ARTICLE_RE.search(href):
            url = urljoin(BASE_URL, href)
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def looks_like_headline(text: str) -> bool:
    t = clean(text)
    if len(t) < 20 or len(t) > 400:
        return False
    bad = (
        "player stats",
        "more ",
        "source:",
        "personalize your",
        "now playing",
    )
    return not any(t.lower().startswith(x) for x in bad)


def find_card_for_article_link(a: Tag) -> Tag | None:
    """
    Walk upward until we find a container that appears to hold exactly one
    Rotoworld news card: player metadata + headline + author/source area.
    """
    node = a
    best = None
    for _ in range(8):
        node = node.parent
        if not isinstance(node, Tag):
            break
        text = clean(node.get_text(" ", strip=True))
        if len(text) > 7000:
            break
        has_player_stats = "Player Stats" in text
        has_more_news = bool(re.search(r"\bMore .+ News\b", text))
        has_source_or_author = "Source:" in text or bool(node.find("h3"))
        if has_player_stats and has_more_news and has_source_or_author:
            best = node
            # Prefer the smallest plausible card.
            if len(text) < 3500:
                return node
    return best


def parse_card(card: Tag, article_url: str) -> NewsItem | None:
    # Player name: usually the first heading/link before "Player Stats".
    player_name = ""
    player_anchor = None
    for a in card.find_all("a"):
        txt = clean(a.get_text(" ", strip=True))
        if not txt:
            continue
        if txt == "Player Stats" or txt.startswith("More "):
            continue
        href = a.get("href", "")
        # Player profile links are typically not the news article URL.
        if href and not ARTICLE_RE.search(href) and len(txt) <= 80:
            player_anchor = a
            player_name = txt
            break

    # Team + position: nearby short text like "NE Cornerback #0".
    team = position = ""
    texts = [clean(s) for s in card.stripped_strings]
    for t in texts:
        m = TEAM_POSITION_RE.match(t)
        if m:
            possible_team = m.group("team")
            possible_pos = clean(m.group("position"))
            if possible_team not in {"NFL", "NBC", "PFT"} and len(possible_pos) < 60:
                team, position = possible_team, possible_pos
                break

    # Headline: prefer h3; otherwise choose a prominent text node.
    headline = ""
    h3 = card.find("h3")
    if h3:
        headline = clean(h3.get_text(" ", strip=True))
    if not headline:
        for tag_name in ("h2", "h4"):
            for h in card.find_all(tag_name):
                t = clean(h.get_text(" ", strip=True))
                if looks_like_headline(t):
                    headline = t
                    break
            if headline:
                break

    # Author/source from text and links.
    source = ""
    author = ""

    source_text_node = None
    for s in card.stripped_strings:
        t = clean(s)
        if SOURCE_RE.match(t):
            source_text_node = t
            source = clean(SOURCE_RE.match(t).group(1))
            break

    # If "Source:" and source link are separate nodes, grab following link.
    if (not source or source.lower() == "source:") and source_text_node:
        pass
    for a in card.find_all("a"):
        txt = clean(a.get_text(" ", strip=True))
        if not txt:
            continue
        parent_text = clean(a.parent.get_text(" ", strip=True)) if a.parent else ""
        if parent_text.lower().startswith("source:"):
            source = txt

    # Author is often "- Nick Shlain" and commonly linked.
    for s in card.stripped_strings:
        t = clean(s)
        if t.startswith("- ") and 2 <= len(t[2:]) <= 60:
            author = clean(t[2:])
            break

    # Blurb: collect paragraphs after headline, excluding UI/meta text.
    paragraphs = []
    for p in card.find_all("p"):
        t = clean(p.get_text(" ", strip=True))
        if not t:
            continue
        low = t.lower()
        if t == headline or t == player_name:
            continue
        if low.startswith("source:") or low.startswith("more "):
            continue
        if "personalize your rotoworld feed" in low:
            continue
        if t == "Player Stats":
            continue
        paragraphs.append(t)

    # Some cards are not wrapped in <p>; fallback via ordered strings.
    news_snippet = clean(" ".join(paragraphs))
    if not news_snippet:
        start = False
        chunks = []
        for t in texts:
            if t == headline:
                start = True
                continue
            if not start:
                continue
            if t.startswith("- ") or t.startswith("Source:") or t.startswith("More "):
                break
            if t not in {"Player Stats"}:
                chunks.append(t)
        news_snippet = clean(" ".join(chunks))

    # Remove a trailing "- Author" if it got absorbed into the blurb.
    if author:
        news_snippet = re.sub(
            r"\s*-\s*" + re.escape(author) + r"\s*$", "", news_snippet
        ).strip()

    if not headline or not news_snippet:
        return None

    return NewsItem(
        player_name=player_name,
        team_initials=team,
        position=position,
        headline=headline,
        news_snippet=news_snippet,
        source=source,
        rotoworld_author=author,
        date=article_date_from_url(article_url),
        url=article_url,
    )


def parse_landing_page(page_html: str) -> list[NewsItem]:
    soup = BeautifulSoup(page_html, "html.parser")
    items = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not ARTICLE_RE.search(href):
            continue
        url = urljoin(BASE_URL, href)
        if url in seen:
            continue
        card = find_card_for_article_link(a)
        if not card:
            continue
        item = parse_card(card, url)
        if item:
            items.append(item)
            seen.add(url)

    return items


def parse_generic_cards(page_html: str) -> list[NewsItem]:
    """
    Fallback for markup where article URLs aren't located inside the visible
    card container. Uses headings and "More [Player] News" markers.
    URL may be empty in this fallback.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    items = []

    for h in soup.find_all(["h2", "h3", "h4"]):
        headline = clean(h.get_text(" ", strip=True))
        if not looks_like_headline(headline):
            continue

        card = h
        for _ in range(6):
            card = card.parent
            if not isinstance(card, Tag):
                break
            txt = clean(card.get_text(" ", strip=True))
            if "Player Stats" in txt and re.search(r"\bMore .+ News\b", txt):
                break
        if not isinstance(card, Tag):
            continue

        # Look for a date-bearing URL anywhere in the card.
        url = ""
        for a in card.find_all("a", href=True):
            if ARTICLE_RE.search(a["href"]):
                url = urljoin(BASE_URL, a["href"])
                break

        item = parse_card(card, url)
        if item and not any(x.headline == item.headline for x in items):
            items.append(item)

    return items


def merge_existing(new_items: list[NewsItem]) -> list[NewsItem]:
    """
    Preserve older rows already in news.csv, then put newest scrape first.
    De-duplicate by URL when available, otherwise by headline/player/date.
    """
    existing = []
    if OUT_CSV.exists():
        with OUT_CSV.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                existing.append(
                    NewsItem(
                        player_name=r.get("Player Name", ""),
                        team_initials=r.get("Team Initials", ""),
                        position=r.get("Position", ""),
                        headline=r.get("Headline", ""),
                        news_snippet=r.get("News Snippet", ""),
                        source=r.get("Source", ""),
                        rotoworld_author=r.get("Rotoworld Author", ""),
                        date=r.get("Date", ""),
                        url=r.get("URL", ""),
                    )
                )

    result = []
    seen = set()

    def key(x: NewsItem):
        return x.url or (x.player_name, x.headline, x.date)

    for item in new_items + existing:
        k = key(item)
        if k in seen:
            continue
        seen.add(k)
        result.append(item)

    return result[:MAX_ITEMS]


def write_csv(items: Iterable[NewsItem]) -> None:
    fields = [
        "Player Name",
        "Team Initials",
        "Position",
        "Headline",
        "News Snippet",
        "Source",
        "Rotoworld Author",
        "Date",
        "URL",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for x in items:
            w.writerow(
                {
                    "Player Name": x.player_name,
                    "Team Initials": x.team_initials,
                    "Position": x.position,
                    "Headline": x.headline,
                    "News Snippet": x.news_snippet,
                    "Source": x.source,
                    "Rotoworld Author": x.rotoworld_author,
                    "Date": x.date,
                    "URL": x.url,
                }
            )


def rss_pubdate(date_text: str) -> str:
    try:
        dt = datetime.strptime(date_text, "%b %d, %Y").replace(tzinfo=timezone.utc)
        return format_datetime(dt)
    except Exception:
        return format_datetime(datetime.now(timezone.utc))


def write_rss(items: list[NewsItem]) -> None:
    ET.register_namespace("rw", "https://example.com/rotoworld-feed")
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:rw": "https://example.com/rotoworld-feed",
    })
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "NBC Sports Rotoworld NFL Player News"
    ET.SubElement(channel, "link").text = PAGE_URL
    ET.SubElement(channel, "description").text = (
        "Personal RSS feed generated from NBC Sports Rotoworld NFL Player News."
    )
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for x in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = x.headline
        ET.SubElement(item, "link").text = x.url or PAGE_URL
        guid = ET.SubElement(item, "guid", {"isPermaLink": "true" if x.url else "false"})
        guid.text = x.guid
        ET.SubElement(item, "pubDate").text = rss_pubdate(x.date)

        desc_parts = [
            f"<p><strong>{html.escape(x.player_name)}</strong>"
            + (f" — {html.escape(x.team_initials)} {html.escape(x.position)}" if x.team_initials else "")
            + "</p>",
            f"<p>{html.escape(x.news_snippet)}</p>",
        ]
        if x.source:
            desc_parts.append(f"<p><strong>Source:</strong> {html.escape(x.source)}</p>")
        if x.rotoworld_author:
            desc_parts.append(
                f"<p><strong>Rotoworld Author:</strong> {html.escape(x.rotoworld_author)}</p>"
            )
        ET.SubElement(item, "description").text = "".join(desc_parts)

        for tag, value in [
            ("player", x.player_name),
            ("team", x.team_initials),
            ("position", x.position),
            ("source", x.source),
            ("author", x.rotoworld_author),
            ("date", x.date),
        ]:
            ET.SubElement(item, f"{{https://example.com/rotoworld-feed}}{tag}").text = value

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(OUT_RSS, encoding="utf-8", xml_declaration=True)


def main() -> None:
    page_html = get(PAGE_URL)

    items = parse_landing_page(page_html)
    if not items:
        items = parse_generic_cards(page_html)

    if not items:
        raise RuntimeError(
            "No Rotoworld news items were parsed. NBC may have changed its HTML. "
            "Open an issue or update the parser selectors."
        )

    merged = merge_existing(items)
    write_csv(merged)
    write_rss(merged)

    print(f"Parsed {len(items)} current items.")
    print(f"Wrote {len(merged)} total items to {OUT_CSV} and {OUT_RSS}.")


if __name__ == "__main__":
    main()
