.PHONY: lint test integration-test

lint:
	uv run ruff check src tests
	uv run mypy src

test:
	uv run pytest tests/unit -v

integration-test:
	uv run pytest tests/integration -v
