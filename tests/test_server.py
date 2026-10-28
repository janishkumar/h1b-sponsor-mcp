import asyncio
import gzip
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from h1b_sponsor_mcp import data


def test_env_override_and_missing_file(tmp_path, monkeypatch):
    db = tmp_path / "x.duckdb"
    db.write_bytes(b"")
    monkeypatch.setenv("H1B_SPONSOR_DB", str(db))
    assert data.locate() == db
    monkeypatch.setenv("H1B_SPONSOR_DB", str(tmp_path / "nope.duckdb"))
    with pytest.raises(data.DataUnavailable):
        data.locate()


def test_download_and_offline_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("H1B_SPONSOR_DB", raising=False)
    monkeypatch.setattr(data, "cache_dir", lambda: tmp_path / "cache")
    asset = tmp_path / "sponsors.duckdb.gz"
    with gzip.open(asset, "wb") as fh:
        fh.write(b"duckdb-bytes")
    monkeypatch.setattr(data, "_latest_release", lambda: ("v1", asset.as_uri()))
    db = data.locate()
    assert db.read_bytes() == b"duckdb-bytes"

    def offline():
        raise OSError("no network")
    monkeypatch.setattr(data, "_latest_release", offline)
    (tmp_path / "cache" / "release.json").write_text('{"tag": "v1", "checked_at": 0}')
    assert data.locate() == db  # stale check, network down: keep using the cache

    db.unlink()
    with pytest.raises(data.DataUnavailable, match="H1B_SPONSOR_DB"):
        data.locate()


def test_data_release_ignores_code_releases():
    def rel(tag, asset=True, **kw):
        assets = [{"name": data.ASSET, "browser_download_url": f"https://x/{tag}"}] if asset else []
        return {"tag_name": tag, "assets": assets, **kw}

    releases = [rel("v0.2.0", asset=False), rel("data-2026-09-30", draft=True),
                rel("data-2026-03-31"), rel("data-2026-06-30"), rel("data-2026-12-31", asset=False)]
    assert data.pick_data_release(releases) == ("data-2026-06-30", "https://x/data-2026-06-30")
    with pytest.raises(data.DataUnavailable):
        data.pick_data_release([rel("v0.1.0", asset=False)])


def test_stdio_server_lists_read_only_tools_and_answers(db_path):
    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "h1b_sponsor_mcp.server"],
            env={**os.environ, "H1B_SPONSOR_DB": str(db_path)},
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            res = await s.call_tool("company_sponsorship", {"company": "Steady Corp"})
            return tools, res.content[0].text

    tools, text = asyncio.run(run())
    assert {t.name for t in tools} == {
        "company_sponsorship", "compare_companies", "top_sponsors", "salary_range",
        "find_employer", "data_info"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)
    assert "Still sponsoring?** 🟢 Yes, active" in text
