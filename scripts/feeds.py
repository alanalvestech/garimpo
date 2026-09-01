"""Builds the RSS feeds in feeds/, one per category plus a combined one.

The feed is its own history: data/ keeps only the latest day, so a reader that
opens once a week would miss whatever left the folder. Each run merges the day
into the existing XML and keeps the newest ITEM_CAP items per feed.

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
FEED_DIR = ROOT / "feeds"
SITE = "https://github.com/alanalvestech/radar"
ITEM_CAP = 50


def as_rfc822(iso_day):
    """RSS wants dates in RFC 822: 2026-08-28 becomes Fri, 28 Aug 2026 ..."""
    stamp = datetime.fromisoformat(iso_day).replace(tzinfo=timezone.utc)
    return format_datetime(stamp)


def read_feed(path):
    """Pulls the items already published in a feed, so history survives."""
    if not path.exists():
        return []
    try:
        channel = ET.parse(path).getroot().find("channel")
    except ET.ParseError:
        return []
    items = []
    for node in channel.findall("item"):
        items.append({tag.tag: (tag.text or "") for tag in node})
    return items


def as_feed_items(record):
    """Turns a day's record into feed items."""
    items = []
    for item in record["items"]:
        link = item["links"][0]
        body = []
        if item.get("authors"):
            body.append(", ".join(item["authors"]))
        if item.get("summary"):
            body.append(item["summary"])
        body += [f"Também em: {u}" for u in item["links"][1:]]
        items.append(
            {
                "title": item["title"],
                "link": link,
                "guid": link,
                "category": record["category"],
                "pubDate": as_rfc822(item.get("date") or record["date"]),
                "description": "\n\n".join(body),
            }
        )
    return items


def sort_key(item):
    try:
        return parsedate_to_datetime(item["pubDate"])
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def write_feed(path, title, description, items):
    """Merges the new items into what the feed already had and writes it."""
    merged, seen = [], set()
    for item in items + read_feed(path):
        if item["guid"] in seen:
            continue
        seen.add(item["guid"])
        merged.append(item)
    merged.sort(key=sort_key, reverse=True)
    merged = merged[:ITEM_CAP]

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = SITE
    ET.SubElement(channel, "description").text = description
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

    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(rss, space="  ")
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)
    print(f"  {path.relative_to(ROOT)}: {len(merged)} itens")


def main():
    everything = []
    for record_path in sorted(DATA_DIR.glob("*/*.json")):
        record = json.loads(record_path.read_text())
        items = as_feed_items(record)
        everything += items
        write_feed(
            FEED_DIR / f"{record['category']}.xml",
            f"radar · {record['category']}",
            f"Notícias de {record['category']}, em português.",
            items,
        )
    write_feed(
        FEED_DIR / "all.xml",
        "radar",
        "Notícias diárias de tecnologia, em português.",
        everything,
    )


if __name__ == "__main__":
    main()
