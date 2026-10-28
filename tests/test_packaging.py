import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_registry_entry_matches_package():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    server = json.loads((ROOT / "server.json").read_text())
    pkg = server["packages"][0]
    assert server["version"] == pkg["version"] == project["version"]
    assert pkg["identifier"] == project["name"]
    assert len(server["description"]) <= 100  # registry limit
    # The registry proves we own the PyPI package by finding this line in its README.
    assert f"<!-- mcp-name: {server['name']} -->" in (ROOT / "README.md").read_text()
