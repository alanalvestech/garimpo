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
    return format_datetime(datetime.fromisoformat(iso_day).replace(tzinfo=timezone.utc))


def read_feed(path):
    """Pulls the items already published, so the feed keeps its history."""
    if not path.exists():
        return []
    try:
        channel = ET.parse(path).getroot().find("channel")
    except ET.ParseError:
        return []
    return [
        {tag.tag: (tag.text or "") for tag in node}
        for node in channel.findall("item")
    ]


def edition_time(archive):
    """When this archive was last written, to date the items that just went in."""
    stamped = archive.get("updated_at")
    if not stamped:
        return None
    try:
        return datetime.strptime(stamped, "%Y-%m-%d %H:%M UTC").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def as_feed_items(archive):
    """Turns the newest slice of a category's archive into feed items."""
    items = []
    newest = archive["items"][0]["date"] if archive["items"] else None
    wrote_at = edition_time(archive)
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
        if item.get("published_at") and item["published_at"] != item["date"]:
            year, month, day = item["published_at"].split("-")
            body.append(f"Publicado em {day}/{month}/{year}")
        body += [f"Também em: {u}" for u in item["links"][1:]]
        items.append(
            {
                "title": item["title"],
                "link": link,
                "guid": link,
                "category": archive["category"],
                # When the item arrived here, not when the origin published
                # it: a reader drops whatever is older than its window, and the
                # edition reports yesterday but goes out this morning. The
                # origin date, when it differs, is in the body.
                "pubDate": format_datetime(wrote_at)
                if wrote_at and item["date"] == newest
                else as_rfc822(item["date"]),
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

    # An item that already went out keeps the date it went out with: once
    # published, its place in the feed is settled, and tomorrow's rebuild must
    # not walk it back to midnight.
    published = read_feed(FEED)
    dated = {item["guid"]: item["pubDate"] for item in published}
    for item in fresh:
        if item["guid"] in dated:
            item["pubDate"] = dated[item["guid"]]

    merged, seen = [], set()
    for item in fresh + published:
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
