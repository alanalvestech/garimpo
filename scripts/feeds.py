"""Builds rss.xml out of every category archive in data/.

One feed for everything. A category is not allowed to take the whole thing:
GitHubTrending brings a hundred items a day and Reddit brings four, so without
a cap per category the small ones would never be read. Each contributes at most
PER_CATEGORY of its newest items, and the feed keeps ITEM_CAP after sorting.

Usage:
    python scripts/feeds.py
"""

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FEED = ROOT / "rss.xml"
SITE = "https://github.com/alanalvestech/garimpo"
ITEM_CAP = 120
PER_CATEGORY = 15


def as_rfc822(iso_day):
    """RSS wants dates in RFC 822: 2026-08-28 becomes Fri, 28 Aug 2026 ..."""
    stamp = datetime.fromisoformat(iso_day).replace(tzinfo=timezone.utc)
    return format_datetime(stamp)


def read_feed(path):
    """Pulls the items already published, so the feed keeps its history."""
    if not path.exists():
        return []
    try:
        channel = ET.parse(path).getroot().find("channel")
    except ET.ParseError:
        return []
    return [{tag.tag: (tag.text or "") for tag in node} for node in channel]


def as_feed_items(archive):
    """Turns the newest slice of a category's archive into feed items."""
    items = []
    for item in archive["items"][:PER_CATEGORY]:
        link = item["links"][0]
        body = []
        if item.get("authors"):
            body.append(", ".join(item["authors"]))
        if item.get("summary"):
            body.append(item["summary"])
        marks = []
        if item.get("stars") is not None:
            marks.append(f"{item['stars']:,}".replace(",", ".") + " estrelas")
        if item.get("language"):
            marks.append(item["language"])
        if marks:
            body.append(" · ".join(marks))
        body += [f"Também em: {u}" for u in item["links"][1:]]
        items.append(
            {
                "title": item["title"],
                "link": link,
                "guid": link,
                "category": archive["category"],
                "pubDate": as_rfc822(item.get("date") or item["day"]),
                "description": "\n\n".join(body),
            }
        )
    return items


def sort_key(item):
    try:
        return parsedate_to_datetime(item["pubDate"])
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def main():
    fresh = []
    for archive_path in sorted(DATA_DIR.glob("*.json")):
        archive = json.loads(archive_path.read_text())
        if archive.get("items"):
            fresh += as_feed_items(archive)

    merged, seen = [], set()
    for item in fresh + read_feed(FEED):
        if item["guid"] in seen:
            continue
        seen.add(item["guid"])
        merged.append(item)
    merged.sort(key=sort_key, reverse=True)
    merged = merged[:ITEM_CAP]

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "garimpo"
    ET.SubElement(channel, "link").text = SITE
    ET.SubElement(channel, "description").text = (
        "Notícias diárias de tecnologia, em português."
    )
    ET.SubElement(channel, "language").text = "pt-BR"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc)
    )
    for item in merged:
        node = ET.SubElement(channel, "item")
        for tag in ("title", "link", "description", "category", "pubDate"):
            if item.get(tag):
                ET.SubElement(node, tag).text = item[tag]
        ET.SubElement(node, "guid", {"isPermaLink": "true"}).text = item["guid"]

    ET.indent(rss, space="  ")
    ET.ElementTree(rss).write(FEED, encoding="utf-8", xml_declaration=True)
    print(f"rss.xml: {len(merged)} itens de {len(set(i['category'] for i in merged))} categorias")


if __name__ == "__main__":
    main()
