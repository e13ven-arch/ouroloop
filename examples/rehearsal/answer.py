"""Answer the manual research provider's requests by hand (docs/REHEARSAL.md §5).

    python answer.py DIR pending                 the requests with no reply yet, oldest first
    python answer.py DIR show RID                one request: its kind, the system prompt's tail, the user JSON
    python answer.py DIR reply RID FILE          the reply whose text is FILE's content, stop "end"
    python answer.py DIR raw RID FILE            FILE is the whole reply object, for fault injection

The script writes the reply file atomically and checks only the envelope; what the reply says is the answerer's.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

KINDS = (("failure", "note"), ("parent", "revision"), ("current_prompts", "slot"), ("notes", "summary"))


def kind_of(request: dict) -> str:
    try:
        user = json.loads(request["messages"][0]["content"])
    except (KeyError, IndexError, ValueError):
        return "?"
    return next((k for key, k in KINDS if key in user), "?")


def pending(root: Path) -> list[Path]:
    answered = {p.stem for p in (root / "responses").glob("*.json")}
    return sorted(p for p in (root / "requests").glob("*.json") if p.stem not in answered)


def write(root: Path, rid: str, reply: dict) -> Path:
    if not (root / "requests" / f"{rid}.json").exists():
        raise SystemExit(f"no request {rid}")
    out = root / "responses" / f"{rid}.json"
    if out.exists():
        raise SystemExit(f"{rid} already has a reply")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(reply, ensure_ascii=False, indent=2))
    os.replace(tmp, out)
    return out


def main(argv: list[str]) -> None:
    root, cmd, *rest = Path(argv[0]), argv[1], *argv[2:]
    if cmd == "pending":
        for p in pending(root):
            print(p.stem, kind_of(json.loads(p.read_text())))
    elif cmd == "show":
        request = json.loads((root / "requests" / f"{rest[0]}.json").read_text())
        print("kind:", kind_of(request))
        print("system (tail):", request["system"][-600:])
        print(json.dumps(json.loads(request["messages"][0]["content"]), indent=2, ensure_ascii=False))
    elif cmd == "reply":
        text = Path(rest[1]).read_text()
        json.loads(text)                     # the answer is JSON; a deliberate non-JSON reply goes through raw
        print(write(root, rest[0], {"text": text, "stop": "end"}))
    elif cmd == "raw":
        print(write(root, rest[0], json.loads(Path(rest[1]).read_text())))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
