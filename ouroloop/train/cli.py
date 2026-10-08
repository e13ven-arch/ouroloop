"""python -m ouroloop.train decide | smoke --jev | host: the commands of the training stack.

    decide --type T --question Q --state S [--criteria JSON]   one decision from a backend
    smoke --jev                                                 the three primitives against one state, in one batched
                                                                call where the backend supports it: exit 0 when every
                                                                answer is a well-formed distribution over its labels
    host init|check <target>                                    prepare a training host, or say what keeps it from
                                                                running this ouroloop's jobs (docs/TRAINING_HOSTS.md)

The backend is --backend (mock, jev, tde, llm or a "module:Class"), or the table of that name in --config's [backends].
The training chain's own smoke (`smoke --offline`, `smoke --real`; docs/design/V2_TRAINING_PIPELINE.md §5) joins
`smoke` as another mode.
"""
from __future__ import annotations

import argparse
import json
import sys

from .. import config
from .config import build_backend, build_serving, plugin_options, store_table
from .types import Question, Request

SMOKE_STATE = ("Customer: I was charged twice for my March invoice and the second charge is still pending. "
               "I need this fixed before my card statement closes on Friday.")
SMOKE_QUESTIONS = {
    "urgent": Question("noul", "The message conveys urgency or time-sensitivity"),
    "team": Question("choice", "Which team should handle this",
                     {"billing": "Payment or subscription issues", "technical": "Bugs or integration problems",
                      "sales": "Pricing or account questions"}),
    "frustration": Question("score", "How frustrated the customer appears",
                            ["Calm, just stating facts", "Frustrated but civil", "Very angry, strong language"]),
}


def _backend(args):
    """The backend named --backend: built from its type, or from that table of --config's [backends]; None (and why,
    on stderr) when the config has no such table."""
    if args.config:
        cfg = config.load(args.config)
        tables = cfg.get("backends", {"mock": {"type": "mock"}})
        if args.backend not in tables:
            print(f"python -m ouroloop.train {args.cmd}: {args.config} has no [backends.{args.backend}] "
                  f"(it has {', '.join(sorted(tables))})", file=sys.stderr)
            return None
        return build_backend(tables[args.backend], build_serving(cfg))
    spec = {"type": args.backend}
    if args.backend == "jev" and args.model:
        spec["model"] = args.model
    if args.backend == "tde":
        spec["run"] = args.run
    return build_backend(spec)


def cmd_decide(args) -> int:
    q = Question(args.type, args.question, json.loads(args.criteria) if args.criteria else None)
    backend = _backend(args)
    if backend is None:
        return 2
    ans = backend.decide([Request(args.state, q)])[0]
    print(json.dumps({"label": ans.label, "probs": ans.probs, "model": ans.model, "usage": ans.usage},
                     ensure_ascii=False))
    return 0


def cmd_smoke(args) -> int:
    """All three primitives against one state, in one batched call where the backend supports it."""
    backend = _backend(args)
    if backend is None:
        return 2
    answers = backend.decide([Request(SMOKE_STATE, q) for q in SMOKE_QUESTIONS.values()])
    ok = True
    for (name, q), ans in zip(SMOKE_QUESTIONS.items(), answers):
        valid = set(ans.probs) == set(q.labels()) and abs(sum(ans.probs.values()) - 1.0) < 1e-6
        ok &= valid
        print(f"{name:<12} {q.type:<6} label={ans.label:<10} p={ans.confidence:.2f} model={ans.model} "
              f"{'ok' if valid else 'MALFORMED'}")
    return 0 if ok else 1


def cmd_host(args) -> int:
    """`host init|check <target>`: prepare a training host for this ouroloop and the trainer plugins, or say what
    keeps it from running their jobs (docs/TRAINING_HOSTS.md). Plugins and their options come from --trainer and the
    config's [trainer] and [trainers.<name>] tables."""
    from .jobs.hosts import HostError, init, read, stale
    from .stores import kind
    cfg = config.load(args.config) if args.config else {}
    options, table = plugin_options(cfg), cfg.get("trainer") or {}
    names = args.trainer or [n for n in [table.get("type")] if n and ":" not in n]
    trainers = {name: options.get(name, {}) for name in names}
    ssh_opts = args.ssh_opts or table.get("ssh_opts", "-o BatchMode=yes")
    store = kind("hf" if args.hf else store_table(cfg).get("type", "dir"))
    try:
        if args.action == "init":
            m, log = init(args.target, trainers, ssh_opts, store)
            print(log, end="", file=sys.stderr)
        else:
            m = read(args.target, ssh_opts)
    except HostError as e:
        print(e, file=sys.stderr)
        return 1
    if m:
        print(json.dumps(m, indent=2))
    why = stale(m, trainers, store)
    print(f"{args.target}: {'not ready: ' + '; '.join(why) if why else 'ready'}", file=sys.stderr)
    return 1 if why else 0


def main(argv: list[str] | None = None) -> int:
    config.load_dotenv(".env")
    p = argparse.ArgumentParser(prog="python -m ouroloop.train")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("decide", cmd_decide), ("smoke", cmd_smoke)):
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        s.add_argument("--backend", default="mock" if name == "decide" else "jev")
        s.add_argument("--model", help="pin a Jev model version")
        s.add_argument("--run", help="TDE run directory (tde backend)")
        s.add_argument("--config", help="take the backend from this config's [backends]")
    mode = sub.choices["smoke"].add_mutually_exclusive_group(required=True)
    mode.add_argument("--jev", action="store_true",
                      help="the three primitives (noul, choice, score) against one state: the backend answers each "
                           "with probabilities over exactly its labels")
    d = sub.choices["decide"]
    d.add_argument("--type", choices=["noul", "choice", "score"], required=True)
    d.add_argument("--question", required=True)
    d.add_argument("--state", required=True)
    d.add_argument("--criteria", help="JSON: noul {true,false}, choice {label: description}, score [levels]")
    ho = sub.add_parser("host", help="prepare a training host (init), or check that it runs this ouroloop")
    ho.set_defaults(fn=cmd_host)
    ho.add_argument("action", choices=["init", "check"])
    ho.add_argument("target", help='"local" (this machine) or user@host over ssh')
    ho.add_argument("--trainer", action="append", help="a trainer plugin to install, e.g. tde; repeatable "
                                                        "(default: the config's [trainer] type)")
    ho.add_argument("--config", help="a config: its [trainer] and [trainers.<name>] options")
    ho.add_argument("--ssh-opts", help='e.g. "-i ~/.ssh/key -o BatchMode=yes" (default: the config\'s)')
    ho.add_argument("--hf", action="store_true", help='also make it ready for a [store] of type "hf"')
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
