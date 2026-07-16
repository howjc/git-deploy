.PHONY: release-check

release-check:
	uv lock --check
	uv run pytest -q
	uv run ruff check src tests
	uv run ty check src
	uv build --clear
