# PYTHONPATH is explicit: macOS can mark uv's editable-install .pth file hidden, and
# Python then skips it. Being explicit works everywhere.
export PYTHONPATH := src:.

USCIS_THROUGH ?= FY2026 Q3
DB := dist/sponsors.duckdb

.PHONY: setup lint test data release inspect serve

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

release:        ## gzip the database and publish it as a GitHub release
	@TAG=data-$$(uv run python -c "import duckdb, json; \
	print(json.loads(duckdb.connect('$(DB)', read_only=True).execute(\"SELECT value FROM meta WHERE key='lca_through'\").fetchone()[0]))"); \
	gzip -9 -k -f $(DB) && \
	gh release create $$TAG $(DB).gz --title "Data through $${TAG#data-}" \
	  --notes "DOL LCA and PERM decisions through $${TAG#data-}; USCIS Employer Data Hub through $(USCIS_THROUGH)."

serve:          ## run the MCP server on stdio against the local build
	H1B_SPONSOR_DB=$(DB) uv run h1b-sponsor-mcp
