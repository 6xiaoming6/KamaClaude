"""Run one pinned upstream synthetic service over local stdio only."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    upstream = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(upstream))
    from synthetic_servers.base import build_server

    server = build_server(sys.argv[2])
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
