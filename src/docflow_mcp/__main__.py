"""Entry point for `python -m docflow_mcp` and the `docflow` CLI shim."""

from .server import main

if __name__ == "__main__":
    main()


def _cli() -> None:
    main()
