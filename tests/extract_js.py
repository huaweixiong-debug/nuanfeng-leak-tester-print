#!/usr/bin/env python3
"""Extract the embedded UI script to the evidence directory for node --check.

Frozen output path: <evidence>/embedded_ui.js (evidence = project parent).
Never touches the project tree, never starts a browser or server.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re


PROJECT = Path(__file__).resolve().parent.parent
EVIDENCE = PROJECT.parent
DEFAULT_OUTPUT = EVIDENCE / "embedded_ui.js"


def extract_script(source: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", source, flags=re.S)
    if not blocks:
        raise SystemExit("no <script> block found in webui_server.py")
    return "\n".join(blocks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    source = (PROJECT / "webui_server.py").read_text(encoding="utf-8")
    script = extract_script(source)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(script, encoding="utf-8")
    print(f"wrote {output} ({len(script)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
