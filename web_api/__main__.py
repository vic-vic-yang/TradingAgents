"""Start the HTTP API: ``python -m web_api`` (uses ``python -m uvicorn`` internally)."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="TradingAgents FastAPI server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="Disable auto-reload (useful in production)",
    )
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        "web_api.app:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
    )


if __name__ == "__main__":
    main()
