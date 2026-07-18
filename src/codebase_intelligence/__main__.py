"""Allow ``python -m codebase_intelligence`` to use the unified CLI."""

from codebase_intelligence.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
