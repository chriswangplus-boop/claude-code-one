"""Regenerate apps/RelayWatch/Tests/Fixtures/journal-sample.jsonl from a real fake-driver run.

Run from plugins/deepseek-relay:

    python3 tools/make_event_fixtures.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from journal_scenarios import write_fixture  # noqa: E402

DEST = ROOT / "apps" / "RelayWatch" / "Tests" / "Fixtures" / "journal-sample.jsonl"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        count = write_fixture(tmp, DEST)
    print(f"wrote {count} events to {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
