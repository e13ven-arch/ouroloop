"""python -m ouroloop.hosts record <plan.json>                this host's manifest, as JSON (bootstrap.sh, start.sh)
python -m ouroloop.hosts pack <dir> <name>[=<source>] ...   write what host init sends into <dir> (the worker image
                                                             builds from it; standard library only)"""
from __future__ import annotations

import json
import sys

from . import manifest, plan


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "record":
        with open(argv[1], encoding="utf-8") as f:
            print(json.dumps(manifest(json.load(f)), indent=2))
        return 0
    if len(argv) >= 2 and argv[0] == "pack":
        trainers = {}
        for item in argv[2:]:
            name, _, src = item.partition("=")
            trainers[name] = {"source": src} if src else {}
        print(plan(trainers, argv[1]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
