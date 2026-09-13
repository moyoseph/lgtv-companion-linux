# Common dev tasks. Needs `just` (https://github.com/casey/just) and `uv`.
# Run `just` to list recipes.

# Lint + type-check + tests (what CI runs)
check: lint typecheck test

# Run the test suite
test:
    uv run pytest -q

# Lint with ruff
lint:
    uv run ruff check src tests

# Type-check
typecheck:
    uv run mypy src

# Tests with a coverage report
cov:
    uv run pytest -q --cov=lgtvcompanion --cov-report=term-missing

# Auto-fix lint issues
fix:
    uv run ruff check --fix src tests

# Build the wheel + sdist
build:
    uv build
