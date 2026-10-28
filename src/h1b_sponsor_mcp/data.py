"""Find the database, or download it from the repo's newest data release (`data-*` tag).

Order: $H1B_SPONSOR_DB (a local file, e.g. one you built), else the cached copy in the
user data dir. The cache is refreshed when a newer release exists, checked at most once
a day so the server never waits on the network more than that.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import time
import urllib.request
from pathlib import Path

from platformdirs import user_data_dir

log = logging.getLogger(__name__)

REPO = os.environ.get("H1B_SPONSOR_REPO", "janishkumar/h1b-sponsor-mcp")
ASSET = "sponsors.duckdb.gz"
CHECK_EVERY_SECONDS = 24 * 3600


class DataUnavailable(RuntimeError):
    pass


def cache_dir() -> Path:
    return Path(user_data_dir("h1b-sponsor-mcp", appauthor=False))


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "h1b-sponsor-mcp"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(url, headers={"User-Agent": "h1b-sponsor-mcp"})
    with urllib.request.urlopen(req, timeout=300) as resp, gzip.open(resp) as gz, \
            tmp.open("wb") as out:
        shutil.copyfileobj(gz, out, length=1 << 20)
    tmp.replace(dest)


def _latest_release() -> tuple[str, str]:
    # Not releases/latest: the repo also has code releases (v*), which carry no database.
    rels = _get_json(f"https://api.github.com/repos/{REPO}/releases?per_page=30")
    return pick_data_release(rels)


def pick_data_release(releases: list[dict]) -> tuple[str, str]:
    """Newest published `data-*` release that has the database attached."""
    data_rels = [r for r in releases if r.get("tag_name", "").startswith("data-")
                 and not r.get("draft") and not r.get("prerelease")]
    for rel in sorted(data_rels, key=lambda r: r["tag_name"], reverse=True):
        for asset in rel.get("assets", []):
            if asset["name"] == ASSET:
                return rel["tag_name"], asset["browser_download_url"]
    raise DataUnavailable(f"no data-* release with {ASSET} in github.com/{REPO}")


def locate(refresh: bool = True) -> Path:
    override = os.environ.get("H1B_SPONSOR_DB")
    if override:
        path = Path(override).expanduser()
        if not path.exists():
            raise DataUnavailable(f"H1B_SPONSOR_DB points at {path}, which doesn't exist")
        return path

    base = cache_dir()
    base.mkdir(parents=True, exist_ok=True)
    db, state_file = base / "sponsors.duckdb", base / "release.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    due = time.time() - state.get("checked_at", 0) > CHECK_EVERY_SECONDS
    if db.exists() and not (refresh and due):
        return db
    try:
        tag, url = _latest_release()
        if not db.exists() or tag != state.get("tag"):
            log.info("downloading data release %s", tag)
            _download(url, db)
        state_file.write_text(json.dumps({"tag": tag, "checked_at": time.time()}))
    except Exception as exc:  # network down, rate limited, no release yet
        if db.exists():
            log.warning("update check failed (%s); using cached data", exc)
            return db
        raise DataUnavailable(
            f"Couldn't download the sponsorship database from github.com/{REPO} ({exc}). "
            "Check your connection, or set H1B_SPONSOR_DB to a local sponsors.duckdb."
        ) from exc
    return db
