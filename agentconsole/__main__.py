"""Run the console:  python -m agentconsole [--host 127.0.0.1] [--port 8000]

Refuses to bind to anything but a loopback address while DEV_LOGIN_ENABLED is on.
"""

from __future__ import annotations

import argparse
import os
import sys

from agentconsole.config import Settings
from agentconsole.security import is_loopback


def check_bind(settings: Settings, host: str) -> str | None:
    """Return an error message if this bind is not allowed, else None."""
    if settings.dev_login_enabled and not is_loopback(host):
        return f"refusing to bind to {host}: DEV_LOGIN_ENABLED needs a loopback host such as 127.0.0.1"
    return None


def main(argv: list[str] | None = None, run=None) -> int:
    parser = argparse.ArgumentParser(prog="agentconsole", description="Web console for cost-capped-agent")
    parser.add_argument("--host", default="127.0.0.1")
    # Hosts such as Render tell the app its port in PORT.
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args(argv)
    try:
        settings = Settings()
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    problem = check_bind(settings, args.host)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    if run is None:  # pragma: no cover - starts a real server
        import uvicorn

        run = uvicorn.run
    run(
        "agentconsole.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        access_log=False,  # uvicorn's access log includes query strings (OAuth codes); we log paths only
        server_header=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
