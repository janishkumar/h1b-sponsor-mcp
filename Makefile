# PYTHONPATH is explicit: macOS can mark uv's editable-install .pth file hidden, and
# Python then skips it. Being explicit works everywhere.
export PYTHONPATH := src:.

USCIS_THROUGH ?= FY2026 Q3
DB := dist/sponsors.duckdb

.PHONY: setup lint test data refresh refresh-check release health inspect serve

setup:
	uv sync --all-extras

lint:
	uv run ruff check src tests

test:
	uv run pytest --cov=h1b_sponsor_mcp --cov-report=term-missing

data:           ## data/raw/{dol,uscis}/ -> dist/sponsors.duckdb
	uv run python -m h1b_sponsor_mcp.build.cli --uscis-through "$(USCIS_THROUGH)"

inspect:        ## a few real answers, to eyeball before releasing
	H1B_SPONSOR_DB=$(DB) uv run python -c "import duckdb; from h1b_sponsor_mcp.queries import Store; \
	s = Store(duckdb.connect('$(DB)', read_only=True)); print(s.data_info()); \
	print(s.compare_companies(['Microsoft', 'Amazon', 'Google', 'Meta']))"

refresh:        ## new files in ~/Downloads -> rebuild -> quality gates -> diff -> publish
	uv run python -m h1b_sponsor_mcp.build.refresh

refresh-check:  ## same, but stop after the gates (publishes nothing)
	uv run python -m h1b_sponsor_mcp.build.refresh --dry-run

release:        ## publish dist/sponsors.duckdb as-is (refresh does this for you)
	uv run python -m h1b_sponsor_mcp.build.refresh publish

health:         ## health of the published release, as the weekly CI job runs it
	uv run python -m h1b_sponsor_mcp.health

serve:          ## run the MCP server on stdio against the local build
	H1B_SPONSOR_DB=$(DB) uv run h1b-sponsor-mcp
