"""Create .env from .env.example with a fresh random SESSION_SECRET.

    python scripts/make_env.py            # refuses to overwrite an existing .env
    python scripts/make_env.py --force

Everything else stays as in .env.example (local development defaults). .env is git-ignored.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_env(example: str) -> str:
    values = {"SESSION_SECRET": secrets.token_urlsafe(48)}
    lines = []
    for line in example.splitlines():
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in values:
            line = f"{key}={values[key]}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    argv = sys.argv[1:] if argv is None else argv
    target = root / ".env"
    if target.exists() and "--force" not in argv:
        print(".env already exists; run with --force to replace it", file=sys.stderr)
        return 1
    target.write_text(build_env((root / ".env.example").read_text(encoding="utf-8")), encoding="utf-8")
    print("wrote .env with a fresh SESSION_SECRET (it is git-ignored; never commit it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
