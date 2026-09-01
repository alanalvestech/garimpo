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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

import discover

sys.path.insert(0, str(Path(__file__).resolve().parent))

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


ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})")
ARXIV_BATCH = 200  # the API takes more, but the URL gets long past this


def arxiv_dates(ids):
    """Maps arXiv id to publication date, from the arXiv API.

    Free, no key. The API asks for one request every three seconds, so the ids
    go in batches instead of one call per paper. The answer also carries the
    abstract, the authors and the categories, all left out: the date is the one
    thing the aggregator does not state.
    """
    found = {}
    for start in range(0, len(ids), ARXIV_BATCH):
        batch = ids[start : start + ARXIV_BATCH]
        if start:
            time.sleep(3)
        url = (
            "http://export.arxiv.org/api/query"
            f"?id_list={','.join(batch)}&max_results={len(batch)}"
        )
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                xml = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  [error] arXiv API: {e}", file=sys.stderr)
            continue
        for entry in xml.split("<entry>")[1:]:
            paper = re.search(r"<id>https?://arxiv\.org/abs/([\d.]+)", entry)
            published = re.search(r"<published>(\d{4}-\d{2}-\d{2})", entry)
            if paper and published:
                found[paper.group(1)] = published.group(1)
    return found


def fill_arxiv_dates(items):
    """Fills the date of items linking to arXiv, straight from the publisher."""
    wanted = {}
    for item in items:
        for link in item["links"]:
            m = ARXIV_ID.search(link)
            if m:
                wanted.setdefault(m.group(1), []).append(item)
                break
    if not wanted:
        return
    dates = arxiv_dates(list(wanted))
    for paper, targets in wanted.items():
        if paper in dates:
            for item in targets:
                # The publisher's date beats the file's day.
                item["date"] = dates[paper]
    print(f"  {len(dates)}/{len(wanted)} datas vindas da API do arXiv")


GITHUB_REPO = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/?$")

TRANSLATION_SCHEMA = {"type": "ARRAY", "items": {"type": "STRING"}}

TRANSLATION_PROMPT = """Translate each line to Brazilian Portuguese, keeping the
same order and the same number of lines. Keep product names, library names and
code identifiers as they are. Never use an em dash. Answer with the list only.

Lines:

{content}
"""


def translate_lines(lines):
    """Translates short texts in one call, keeping the order."""
    payload = {
        "contents": [
            {"parts": [{"text": TRANSLATION_PROMPT.format(content="\n".join(lines))}]}
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": TRANSLATION_SCHEMA,
        },
    }
    answer = gemini(payload)
    return answer if len(answer) == len(lines) else lines


def fill_github_repos(items, known=None):
    """Gives repo items the description its own owner wrote, translated.

    The aggregator's file has a description too, in Chinese and written by the
    aggregator, whose repository declares no license. This one comes from the
    GitHub API: the owner's own words about their project, first-hand.
    """
    targets = {}
    for item in items:
        for link in item["links"]:
            m = GITHUB_REPO.search(link)
            if m:
                targets[item["title"]] = (item, f"{m.group(1)}/{m.group(2)}")
                break
    if not targets:
        return

    known = known or {}
    described = []
    for item, full_name in targets.values():
        repo = known.get(full_name)
        if repo is None:
            try:
                repo = http_json(f"https://api.github.com/repos/{full_name}")
            except Exception as e:
                print(f"  [error] GitHub API {full_name}: {e}", file=sys.stderr)
                continue
        if repo.get("description"):
            described.append((item, repo))

    if not described:
        return
    traduzidas = translate_lines([r["description"] for _, r in described])
    for (item, repo), description in zip(described, traduzidas):
        stars = f"{repo['stargazers_count']:,}".replace(",", ".")
        marks = [f"{stars} estrelas"]
        if repo.get("language"):
            marks.append(repo["language"])
        item["summary"] = f"{description} ({', '.join(marks)})"
    print(f"  {len(described)}/{len(targets)} descrições vindas da API do GitHub")


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


def gemini(payload):
    """Posts to Gemini and returns the parsed JSON the schema asked for."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent"
    )
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
        return json.loads(response["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, json.JSONDecodeError):
        print(f"  [error] unexpected Gemini response: {response}", file=sys.stderr)
        return []


def translate(content, mode, file_date):
    """Returns the file's items, each with title, links and (if full) summary."""
    prompt = SUMMARY_PROMPT if mode == "full" else TITLE_PROMPT
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
    items = []
    for item in gemini(payload):
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


def as_br(iso):
    """2026-08-28 as 28/08/2026, which is how the reader writes a date."""
    year, month, day = iso.split("-")
    return f"{day}/{month}/{year}"


def to_markdown(items):
    """Builds the .md body out of the same items that go into the .json.

    The title carries the first link, so reading the file is one click away
    from the original. Extra links land on their own line, named by host.
    """
    blocks = []
    for item in items:
        first, *rest = item["links"]
        lines = [f"### [{item['title']}]({first})", ""]
        if item.get("authors"):
            lines.append(f"*{', '.join(item['authors'])}*")
            lines.append("")
        if item.get("summary"):
            lines.append(item["summary"])
            lines.append("")

        footer = []
        if item.get("date"):
            footer.append(as_br(item["date"]))
        # Only the extra links: the first one is already in the title.
        footer += [f"[{domain(u)}]({u})" for u in rest]
        if footer:
            lines.append(" · ".join(footer))
        blocks.append("\n".join(lines).rstrip())
    return "\n\n".join(blocks)


def already_saved(path):
    """Items already written for that day, so a second source adds to them."""
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("items", [])
    except (json.JSONDecodeError, OSError):
        return []


def write_day(source, entry, items, now, pending=False):
    """Writes the category's .md/.json pair under the source file's date.

    The files hold the items and their links, which point at the original
    publication. The repository the radar read the list from stays out: it is
    the route, not the source.
    """
    day = entry["date"].isoformat()
    folder = DATA_DIR / source["category"]
    folder.mkdir(parents=True, exist_ok=True)

    # Several sources can feed one category, so the day's file is merged, not
    # overwritten, and an item found twice is kept once.
    merged, seen = [], set()
    for item in already_saved(folder / f"{day}.json") + (items or []):
        key = item["links"][0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)

    record = {
        "category": source["category"],
        "date": day,
        "generated_at": now,
        "pending": pending,
        "items": merged,
    }
    (folder / f"{day}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2)
    )

    header = f"# {source['category']} · {day}\n\n"
    if merged:
        body = to_markdown(merged)
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
    """Keeps only the current day: the previous one lives in the git history.

    Only files named after a day are pruned. The category's feed and whatever
    state a collector keeps live in the same folder and stay.
    """
    for old in folder.glob("*.*"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", old.stem) and old.stem != day:
            old.unlink()
            print(f"  removed {old.relative_to(ROOT)}")


def done_today(source, folder, day, has_key):
    """Whether this source already delivered that day, on its own."""
    if FORCE:
        return False
    target = folder / f"{day}.json"
    if target.exists() and needs_retry(target, has_key):
        return False
    return load_state(folder).get(source_key(source)) == day


def mark_done(source, folder, day):
    state = load_state(folder)
    state[source_key(source)] = day
    save_state(folder, state)


def source_key(source):
    """Identifies a source inside a category, for the state file."""
    kind = source.get("kind", "file")
    if kind == "file":
        return f"file:{source['repo']}/{source['path']}:{source.get('section', '')}"
    return f"{kind}:{source.get('channel', '')}"


def load_state(folder):
    path = folder / "state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(folder, state):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True))


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
    fill_arxiv_dates(items)
    fill_github_repos(items)
    if not items:
        print("  nothing usable")
        return False

    write_day(source, entry, items, now)
    return True


def run_file_source(source, folder, now, has_key, shared_day):
    """A source that publishes one file per day in a GitHub repository."""
    print(f"[{source['category']}] {source['repo']}/{source['path']}")
    entry = latest_file(source)
    if entry is None:
        print("  no file with a date in the name")
        return False

    day = (shared_day or entry["date"]).isoformat()
    if done_today(source, folder, day, has_key):
        print(f"  {entry['name']}: already saved")
        prune(folder, day)
        return False

    print(f"  {entry['name']}")
    entry = {**entry, "date": date.fromisoformat(day)}
    if not process(source, entry, now, has_key):
        return False
    mark_done(source, folder, day)
    prune(folder, day)
    return True


def run_discovery(source, folder, now, has_key, shared_day):
    """A source that goes looking for repositories instead of reading a file.

    The day here is the day of the run: these are finds, not an edition someone
    else published.
    """
    kind = source["kind"]
    print(f"[{source['category']}] {kind}")
    today = date.today()
    seen_path = folder / "seen.json"
    known, seen_now = {}, None
    day_of = today

    if kind == "trackawesomelist":
        items, digest_day = discover.from_trackawesomelist(
            source, http_json, http_text, today
        )
        if digest_day is None:
            return False
        day_of = digest_day
    elif kind == "youtube_channel":
        seen = json.loads(seen_path.read_text()) if seen_path.exists() else []
        items, seen_now = discover.from_youtube(source, http_text, set(seen))
        seen_now = (seen_now + seen)[:200]
    elif kind == "github_search":
        items, known = discover.from_github_search(source, http_json, today)
    else:
        sys.exit(f"unknown kind in sources.yaml: {kind}")

    day = (shared_day or day_of).isoformat()
    if done_today(source, folder, day, has_key):
        print("  already saved")
        prune(folder, day)
        return False

    if not items:
        print("  nada novo")
        return False

    entry = {"name": kind, "date": date.fromisoformat(day)}
    if has_key:
        fill_github_repos(items, known)
    write_day(source, entry, items, now, pending=not has_key)
    if seen_now is not None:
        seen_path.write_text(json.dumps(seen_now, indent=2))
    mark_done(source, folder, day)
    prune(folder, day)
    return True


def main():
    has_key = bool(os.environ.get("GEMINI_API_KEY"))
    if not has_key:
        # With no key the day is recorded empty and flagged as pending, and the
        # next collection that has a key rewrites it with the items.
        print("GEMINI_API_KEY not set: writing empty days", file=sys.stderr)

    sources = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())["sources"]
    categories = [s["category"] for s in sources]
    # When a category has more than one source, they all write the same day, the
    # day of the run: each source has its own idea of what day its file is, and
    # a folder holds one day. The item's own date stays on the item.
    shared = {c for c in categories if categories.count(c) > 1}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    written = 0
    failures = 0

    for source in sources:
        folder = DATA_DIR / source["category"]
        shared_day = date.today() if source["category"] in shared else None
        try:
            if source.get("kind", "file") == "file":
                done = run_file_source(source, folder, now, has_key, shared_day)
            else:
                done = run_discovery(source, folder, now, has_key, shared_day)
        except Exception as e:
            # One failing source must not take the others down, otherwise a
            # quota blown midway loses the whole day's collection.
            print(f"  [error] {source['category']}: {e}", file=sys.stderr)
            failures += 1
            continue
        written += 1 if done else 0

    print(f"{written} new files" if written else "nothing new")
    if failures:
        print(f"{failures} file(s) failed", file=sys.stderr)


if __name__ == "__main__":
    main()
