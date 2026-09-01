"""Finds repositories worth looking at, from three places at once.

Each one has its own kind of noise, so each one has its own filter:

    trackawesomelist  a list dumping dozens of entries in one day is a
                      reorganization, not a find, and an old repo with
                      thousands of stars is a re-listing
    youtube_channel   a channel that reads trending out loud; what the video
                      says is not taken as true, only the repos it points at
    github_search     raw and the noisiest: a repo with a pile of stars, one
                      contributor and one commit is a scam, so it is checked
                      before it is kept

Called by collect.py through the `kind` field in config/sources.yaml.
"""

import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

REPO_IN_LINK = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)")
IGNORED_OWNERS = {"sponsors", "topics", "features", "orgs", "collections"}

LIST_DUMP = 10  # more entries than this from one list means it reorganized
OLD_ENOUGH = 730  # days; an older repo with many stars is a re-listing
MANY_STARS = 5000
SAME_OWNER = 3  # the same owner this many times in a day is a dump


def repo_from(url):
    """owner/name out of a GitHub URL, or None when it is not a repository."""
    m = REPO_IN_LINK.search(url)
    if not m:
        return None
    owner, name = m.group(1), m.group(2).removesuffix(".git")
    if owner in IGNORED_OWNERS or name in ("", "."):
        return None
    return f"{owner}/{name}"


def as_item(full_name):
    return {"title": full_name.split("/")[1], "links": [f"https://github.com/{full_name}"]}


def drop_owner_dumps(names):
    """Drops owners that show up too many times: that is one person dumping."""
    counted = {}
    for name in names:
        counted.setdefault(name.split("/")[0], []).append(name)
    kept = []
    for owner, repos in counted.items():
        if len(repos) >= SAME_OWNER:
            print(f"  {owner}: {len(repos)} repos no mesmo dia, despejo, fora")
            continue
        kept += repos
    return kept


def from_trackawesomelist(source, http_json, http_text, today):
    """Reads the day's digest of what entered each awesome list."""
    url = f"https://www.trackawesomelist.com/{today:%Y/%m/%d}/"
    try:
        html = http_text(url)
    except urllib.error.HTTPError as e:
        print(f"  digest do dia não publicado: HTTP {e.code}")
        return []

    # The page groups entries by list, and the list is what tells a real find
    # from a reorganization, so the split has to happen before the dedup.
    blocks = re.split(r"<h2[^>]*>", html)[1:]
    names = []
    for block in blocks:
        found = []
        for url_found in re.findall(r"https?://github\.com/[\w.\-/]+", block):
            name = repo_from(url_found)
            if name and name not in found:
                found.append(name)
        if len(found) > LIST_DUMP:
            title = re.sub(r"<[^>]+>", " ", block.split("</h2>")[0]).strip()
            print(f"  {title[:40]}: {len(found)} itens de uma vez, refresh, fora")
            continue
        names += found

    kept = []
    for name in dict.fromkeys(names):
        try:
            repo = http_json(f"https://api.github.com/repos/{name}")
        except Exception:
            continue
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00"))
        ).days
        if repo["stargazers_count"] >= MANY_STARS and age > OLD_ENOUGH:
            continue  # old and famous: it was re-listed, not found
        kept.append(name)
    return [as_item(n) for n in kept]


def from_youtube(source, http_text, seen):
    """Pulls the repos out of the description of videos not seen yet.

    The repos are in the description, not in the video, and the source of an
    item is the video's URL, never the channel's.
    """
    feed = http_text(
        "https://www.youtube.com/feeds/videos.xml"
        f"?channel_id={source['channel']}"
    )
    ids = re.findall(r"<yt:videoId>([^<]+)</yt:videoId>", feed)
    fresh = [v for v in ids if v not in seen][: source.get("max_videos", 3)]
    if not fresh:
        print("  nenhum vídeo novo no canal")
        return [], []

    names = []
    for video in fresh:
        url = f"https://www.youtube.com/watch?v={video}"
        try:
            done = subprocess.run(
                ["yt-dlp", "--skip-download", "--print", "%(description)s", url],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"  [error] yt-dlp {video}: {e}")
            continue
        if done.returncode != 0:
            print(f"  [error] yt-dlp {video}: {done.stderr.strip()[:120]}")
            continue
        for line in done.stdout.splitlines():
            for url_found in re.findall(r"https?://github\.com/[\w.\-/]+", line):
                name = repo_from(url_found)
                if name:
                    names.append(name)
    return [as_item(n) for n in dict.fromkeys(names)], fresh


def looks_real(name, repo, http_json):
    """A repo with stars but no work behind it is a scam, so check the work."""
    try:
        contributors = http_json(
            f"https://api.github.com/repos/{name}/contributors?per_page=3"
        )
        commits = http_json(f"https://api.github.com/repos/{name}/commits?per_page=10")
    except Exception:
        return False
    if not isinstance(contributors, list) or not isinstance(commits, list):
        return False
    if len(contributors) < 2:
        print(f"  {name}: um contribuidor só, fora")
        return False
    if len(commits) < 5:
        print(f"  {name}: {len(commits)} commits, fora")
        return False
    if not repo.get("description"):
        print(f"  {name}: sem descrição, fora")
        return False
    return True


def from_github_search(source, http_json, today):
    """New repos ordered by stars, over a few windows, checked one by one."""
    windows = source.get("windows", [1, 7, 30])
    sizes = source.get("per_page", [10, 20, 30])
    candidates, fetched = {}, {}
    for days, size in zip(windows, sizes):
        since = today - timedelta(days=days)
        url = (
            "https://api.github.com/search/repositories"
            f"?q=created:>{since:%Y-%m-%d}&sort=stars&order=desc&per_page={size}"
        )
        try:
            answer = http_json(url)
        except Exception as e:
            print(f"  [error] search janela de {days}d: {e}")
            continue
        for repo in answer.get("items", []):
            candidates.setdefault(repo["full_name"], repo)

    kept = []
    for name in drop_owner_dumps(list(candidates)):
        repo = candidates[name]
        if looks_real(name, repo, http_json):
            fetched[name] = repo
            kept.append(name)
    print(f"  {len(kept)}/{len(candidates)} passaram na checagem")
    return [as_item(n) for n in kept], fetched
