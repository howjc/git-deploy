.PHONY: release-check

release-check:
	uv lock --check
	uv run pytest -q
	uvx ruff check src tests
	uvx ty check src
	uv build --clear
