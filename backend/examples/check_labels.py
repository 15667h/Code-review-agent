"""标注行号核对：把 labels.json 指向的源码行打印出来，便于人工逐条确认。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def check(case: str) -> int:
    base = EXAMPLES / case
    payload = json.loads((base / "labels.json").read_text(encoding="utf-8"))
    print(f"===== {case} =====")
    bad = 0
    for item in payload["labels"]:
        path = base / "repo" / item["file"]
        lines = path.read_text(encoding="utf-8").splitlines()
        lineno = item["line"]
        if not (1 <= lineno <= len(lines)) or not lines[lineno - 1].strip():
            print(f"[BAD] {item['code']} {item['file']}:{lineno} 越界或空行")
            bad += 1
            continue
        ctx = lines[lineno - 1].strip()
        print(f"{item['code']:<10} {item['file']}:{lineno:<4} | {ctx}")
        print(f"{'':<10} 说明：{item['description']}")
    return bad


if __name__ == "__main__":
    targets = sys.argv[1:] or ["m1-order-desk", "m2-report-hub", "m3-auth-gateway"]
    total = 0
    for name in targets:
        if (EXAMPLES / name / "labels.json").exists():
            total += check(name)
    print("坏行:", total)
