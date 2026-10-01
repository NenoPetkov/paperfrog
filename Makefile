.PHONY: install test lint typecheck audit paper readiness
install:
	uv sync --frozen

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

audit:
	uv run pip-audit --skip-editable

paper:
	uv run paperfrog run paper

readiness:
	uv run paperfrog report readiness
