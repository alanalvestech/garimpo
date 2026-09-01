"""Reads the latest file of each source, translates it with Gemini, writes data/.

Every category has its own folder, and the folder keeps only the most recent day
the source published:

    data/<Category>/YYYY-MM-DD.md
    data/<Category>/YYYY-MM-DD.json

The date in the name is the source file's, not the day the collection ran: a
source that lags shows up with its own date. When a new day comes in, the
previous one leaves the folder and lives on in the git history. A day already
saved is skipped, so running again neither rewrites it nor spends a call.

Usage:
    GEMINI_API_KEY=... python scripts/collect.py

Environment:
    GEMINI_API_KEY  required
    GEMINI_MODEL    defaults to gemini-2.5-flash
    GITHUB_TOKEN    optional, only raises the GitHub API rate limit
    RADAR_FORCE     "1" rewrites the day that already exists
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
FORCE = os.environ.get("RADAR_FORCE") == "1"
CHAR_LIMIT = 60000  # trims a huge file before sending it to the model

RULES = """- Drop items with no link, plus ads, footers, tables of contents and indexes.
- Keep the links intact, in the order they appear, and always point to the
  item's original page, never to the file you are reading.
- Do not invent information that is not in the text.
- If there is nothing usable in the file, return an empty list.

How to write, in every field:

- Lead with the fact. No warm-up, no scene setting, no restating the title.
- Length follows content. One sentence that says it beats two that pad it.
- Active voice, concrete verbs. Cut any adjective or adverb that does not
  change what the reader would decide.
- Never use an em dash or an en dash. Use a comma, a colon, parentheses or a
  full stop instead.
- Banned shapes: "não é X, é Y", a punchy line closing the text, a dramatic
  colon, vague attribution ("especialistas apontam", "estudos mostram"), and
  renaming the same thing every sentence to sound varied.
- No LinkedIn, coaching or sales register: no motivational imperative, no
  rhetorical question, no promise of transformation, no empty superlative
  ("revolucionário", "definitivo", "o único"), no call to action.
- The test for every sentence: does it inform or does it perform? If it only
  performs, cut it. Write like someone answering a technical colleague, not
  like someone competing for attention in a feed.

Content:

---
{content}
---
"""

SUMMARY_PROMPT = (
    """You get the raw content of a daily file from a tech news aggregator, written in English or Chinese.

Return the list of items in the file, in Brazilian Portuguese, each with:

- title: the item's title, translated.
- summary: at most two sentences with what the item says.
- authors: the item's authors, when the text names them. Skip it otherwise.
- date: the item's own publication date as YYYY-MM-DD, only when the text
  states it. Never guess it and never copy the file's date. Skip it otherwise.
- links: every link of that item.

Rules:

"""
    + RULES
)

TITLE_PROMPT = (
    """You get the raw content of a daily file from a tech news aggregator, written in English or Chinese.

Return the list of items in the file, each with:

- title: the item's title, translated to Brazilian Portuguese.
- date: the item's own publication date as YYYY-MM-DD, only when the text
  states it. Never guess it and never copy the file's date. Skip it otherwise.
- links: every link of that item.

Do not write a summary: only the title and the links.

Rules:

"""
    + RULES
)

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "authors": {"type": "ARRAY", "items": {"type": "STRING"}},
            "date": {"type": "STRING"},
            "links": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["title", "links"],
    },
}


def http_json(url):
    req = urllib.request.Request(url, headers=headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


TEXT_CACHE = {}  # the same file feeds several categories, download it once


def http_text(url):
    if url not in TEXT_CACHE:
        req = urllib.request.Request(url, headers=headers())
        with urllib.request.urlopen(req, timeout=60) as r:
            TEXT_CACHE[url] = r.read().decode("utf-8", errors="replace")
    return TEXT_CACHE[url]


def headers():
    h = {"User-Agent": "radar", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def extract_section(text, section):
    """Returns (heading, body) of the block whose heading holds `section`."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            if start is not None:
                return lines[start], "\n".join(lines[start + 1 : i]).strip()
            if section.lower() in line.lower():
                start = i
    if start is None:
        return None
    return lines[start], "\n".join(lines[start + 1 :]).strip()


def date_from_name(name):
    """Pulls a date out of names like 2026-08-31.md or 20260831.md."""
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def latest_file(source):
    """Returns the file with the most recent date in the source's folder."""
    path = source["path"].strip("./")
    url = f"https://api.github.com/repos/{source['repo']}/contents/{path}"
    try:
        entries = http_json(url)
    except urllib.error.HTTPError as e:
        print(f"  [error] {source['repo']}/{path}: HTTP {e.code}", file=sys.stderr)
        return None

    if not isinstance(entries, list):
        return None

    found = []
    for entry in entries:
        if entry.get("type") != "file":
            continue
        name = entry["name"]
        if not any(name.endswith(e) for e in source.get("ext", [".md"])):
            continue
        d = date_from_name(name)
        if d is None:
            continue
        found.append(
            {
                "name": name,
                "date": d,
                "download_url": entry["download_url"],
                "html_url": entry["html_url"],
            }
        )
    if not found:
        return None
    return max(found, key=lambda f: f["date"])


def item_date(value, file_date):
    """Keeps the item's own date only if it is ISO and close to the file's.

    The model is told never to guess a date, and this is the net for when it
    guesses anyway: anything unparseable or far from the file's day is dropped.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if not -365 <= (parsed - file_date).days <= 1:
        return None
    return parsed.isoformat()


def translate(content, mode, file_date):
    """Returns the file's items, each with title, links and (if full) summary."""
    prompt = SUMMARY_PROMPT if mode == "full" else TITLE_PROMPT
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent"
    )
    if len(content) > CHAR_LIMIT:
        print(f"  trimmed to {CHAR_LIMIT} of {len(content)} chars")
    payload = {
        "contents": [
            {"parts": [{"text": prompt.format(content=content[:CHAR_LIMIT])}]}
        ],
        "generationConfig": {
            "temperature": 0.2,
            # The model answers with JSON shaped by the schema, so there is no
            # item-by-item parsing of loose text afterwards.
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            # In the header, not in the query: a URL carrying the key leaks in
            # error messages, tracebacks and proxy logs.
            "x-goog-api-key": os.environ["GEMINI_API_KEY"],
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        response = json.load(r)
    try:
        raw = response["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print(f"  [error] unexpected Gemini response: {response}", file=sys.stderr)
        return []

    items = []
    for item in json.loads(raw):
        if not item.get("title") or not item.get("links"):
            continue
        clean = {"title": no_dashes(item["title"]), "links": item["links"]}
        if mode == "full" and item.get("summary"):
            # Only a source whose license allows republishing gets a summary; on
            # the others the item keeps the minimum: title and link.
            clean["summary"] = no_dashes(item["summary"])
        if item.get("authors"):
            clean["authors"] = item["authors"]
        when = item_date(item.get("date"), file_date)
        if when:
            clean["date"] = when
        items.append(clean)
    return items


def no_dashes(text):
    """Drops em and en dashes: the prompt asks for it, the model still slips."""
    for dash in ("—", "–"):
        text = text.replace(f" {dash} ", ", ").replace(dash, ", ")
    return text


def domain(url):
    """Host of a URL, without www, to label a link by where it lands."""
    return urllib.parse.urlparse(url).netloc.removeprefix("www.")


def to_markdown(items, day):
    """Builds the .md body out of the same items that go into the .json.

    The title carries the first link, so reading the file is one click away
    from the original. Extra links land on their own line, named by host.
    """
    blocks = []
    for item in items:
        first, *rest = item["links"]
        lines = [f"### [{item['title']}]({first})", ""]
        meta = []
        if item.get("authors"):
            meta.append(f"*{', '.join(item['authors'])}*")
        if item.get("date") and item["date"] != day:
            meta.append(item["date"])
        if meta:
            lines.append(" · ".join(meta))
            lines.append("")
        if item.get("summary"):
            lines.append(item["summary"])
            lines.append("")
        if rest:
            # Only the extra links: the first one is already in the title.
            lines.append(" · ".join(f"[{domain(u)}]({u})" for u in rest))
        blocks.append("\n".join(lines).rstrip())
    return "\n\n".join(blocks)


def write_day(source, entry, items, now, pending=False):
    """Writes the category's .md/.json pair under the source file's date.

    The files hold the items and their links, which point at the original
    publication. The repository the radar read the list from stays out: it is
    the route, not the source.
    """
    day = entry["date"].isoformat()
    folder = DATA_DIR / source["category"]
    folder.mkdir(parents=True, exist_ok=True)

    record = {
        "category": source["category"],
        "date": day,
        "generated_at": now,
        "pending": pending,
        "items": items or [],
    }
    (folder / f"{day}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2)
    )

    header = f"# {source['category']} · {day}\n\n"
    if items:
        body = to_markdown(items, day)
    elif pending:
        body = (
            "Coleta pendente. Os itens entram na próxima rodada que tiver o "
            "GEMINI_API_KEY definido."
        )
    else:
        body = "Nada aproveitável neste dia."
    (folder / f"{day}.md").write_text(header + body + "\n")
    print(f"  written data/{source['category']}/{day}.md")


def prune(folder, day):
    """Keeps only the current day: the previous one lives in the git history."""
    for old in folder.glob("*.*"):
        if old.stem != day:
            old.unlink()
            print(f"  removed {old.relative_to(ROOT)}")


def needs_retry(target, has_key):
    """Says whether a saved day should be redone, having been left pending."""
    if not has_key:
        return False
    try:
        return json.loads(target.read_text()).get("pending", False)
    except (json.JSONDecodeError, OSError):
        return False


def process(source, entry, now, has_key):
    """Downloads, cuts the block if any, translates, and writes the day.

    Returns False when there is nothing to write.
    """
    raw = http_text(entry["download_url"]) if has_key else None

    if source.get("section") and raw:
        # The source packs several blocks into one file, and each block becomes
        # a category. With no block there is nothing to write.
        section = extract_section(raw, source["section"])
        if section is None:
            print(f"  block {source['section']} not found")
            return False
        raw = f"{section[0]}\n\n{section[1]}"

    if not has_key:
        write_day(source, entry, None, now, pending=True)
        return True

    items = translate(raw, source["mode"], entry["date"])
    if not items:
        print("  nothing usable")
        return False

    write_day(source, entry, items, now)
    return True


def main():
    has_key = bool(os.environ.get("GEMINI_API_KEY"))
    if not has_key:
        # With no key the day is recorded empty and flagged as pending, and the
        # next collection that has a key rewrites it with the items.
        print("GEMINI_API_KEY not set: writing empty days", file=sys.stderr)

    sources = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())["sources"]
    categories = [s["category"] for s in sources]
    duplicates = {c for c in categories if categories.count(c) > 1}
    if duplicates:
        # Two sources in one category would overwrite each other's file.
        sys.exit(f"duplicate category in sources.yaml: {', '.join(sorted(duplicates))}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    written = 0
    failures = 0

    for source in sources:
        print(f"[{source['category']}] {source['repo']}/{source['path']}")
        entry = latest_file(source)
        if entry is None:
            print("  no file with a date in the name")
            continue

        day = entry["date"].isoformat()
        folder = DATA_DIR / source["category"]
        target = folder / f"{day}.json"
        if target.exists() and not FORCE and not needs_retry(target, has_key):
            print(f"  {entry['name']}: already saved")
            prune(folder, day)
            continue

        print(f"  {entry['name']}")
        try:
            if not process(source, entry, now, has_key):
                continue
        except Exception as e:
            # One failing source must not take the others down, otherwise a
            # quota blown midway loses the whole day's collection.
            print(f"  [error] {entry['name']}: {e}", file=sys.stderr)
            failures += 1
            continue
        prune(folder, day)
        written += 1

    print(f"{written} new files" if written else "nothing new")
    if failures:
        print(f"{failures} file(s) failed", file=sys.stderr)


if __name__ == "__main__":
    main()
