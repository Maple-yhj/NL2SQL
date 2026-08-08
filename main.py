"""Source-tree shim for the installable ``data-agent`` command."""

from data_agent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
