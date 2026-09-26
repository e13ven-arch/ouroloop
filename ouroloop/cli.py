"""Command line: ouroloop run | suite | decide | smoke | demo | research | evolve | deploy | calibrate | gate | models |
serve | host | hook | ledger."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import config
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
    if args.config:
        return config.build_runtime(config.load(args.config), Path(args.config).parent).backends[args.backend]
    spec = {"type": args.backend}
    if args.backend == "jev" and args.model:
        spec["model"] = args.model
    if args.backend == "tde":
        spec["run"] = args.run
    return config.build_backend(spec)


def cmd_decide(args) -> int:
    q = Question(args.type, args.question, json.loads(args.criteria) if args.criteria else None)
    ans = _backend(args).decide([Request(args.state, q)])[0]
    print(json.dumps({"label": ans.label, "probs": ans.probs, "model": ans.model, "usage": ans.usage},
                     ensure_ascii=False))
    return 0


def cmd_smoke(args) -> int:
    """All three primitives against one state, in one batched call where the backend supports it."""
    answers = _backend(args).decide([Request(SMOKE_STATE, q) for q in SMOKE_QUESTIONS.values()])
    ok = True
    for (name, q), ans in zip(SMOKE_QUESTIONS.items(), answers):
        valid = set(ans.probs) == set(q.labels()) and abs(sum(ans.probs.values()) - 1.0) < 1e-6
        ok &= valid
        print(f"{name:<12} {q.type:<6} label={ans.label:<10} p={ans.confidence:.2f} model={ans.model} "
              f"{'ok' if valid else 'MALFORMED'}")
    return 0 if ok else 1


def _print_round(root, res) -> None:
    for point, m in res.report.points.items():
        print(f"report  {point}: n={m['n']} brier={m['brier']:.3f} accuracy={m['accuracy']:.3f}")
    print(f"report  {len(res.report.clusters)} error groups")
    for e in res.evaluated:
        ci = f"[{e['ci'][0]:+.3f}, {e['ci'][1]:+.3f}]" if e.get("ci") else "-"
        delta = f"{e['delta']:+.3f}" if e.get("delta") is not None else "-"
        what = e["hypothesis"] if e["kind"] == "prompt" else json.dumps(e["change"], ensure_ascii=False)
        print(f"{e['gate']:<22} {e['kind']:<7} delta={delta} ci={ci}  {what[:90]}")
    print(f"workspace: {root}")


def cmd_demo(args) -> int:
    if args.scenario:           # a scenario pack's multi-generation loop: examples/<scenario>/
        from .generations import run as run_generations
        path = Path(args.config or f"examples/{args.scenario}/demo.toml")
        if not path.exists():
            print(f"no {path}: run from the ouroloop checkout, or pass --config", file=sys.stderr)
            return 2
        run_generations(config.load(path), path.resolve().parent, generations=args.generations, root=args.dir)
        return 0
    from .demo import run
    root, res = run(args.dir, judge=args.judge, seed=args.seed)
    _print_round(root, res)
    return 0


def _load_runtime(path: str):
    cfg = config.load(path)
    return cfg, config.build_runtime(cfg, Path(path).resolve().parent)


def cmd_research(args) -> int:
    from .research.loop import run_rounds
    cfg, rt = _load_runtime(args.config)
    agent, llm, judge, kw = config.build_research(cfg, rt, Path(args.config).resolve().parent)
    for i, res in enumerate(run_rounds(rt, agent, llm, judge, rounds=args.rounds, patience=args.patience, **kw), 1):
        print(f"--- round {i}")
        _print_round(rt.root, res)
    return 0


def cmd_suite(args) -> int:
    from .agent.prompt import PromptStore
    from .agent.suite import load_suite, validate
    from .evolve.harness import SuiteRunner
    cfg = config.load(args.config) if Path(args.config).exists() else None
    if args.tasks:
        path = Path(args.tasks)
    elif cfg is not None:
        path = Path(args.config).resolve().parent / cfg.get("suite", {}).get("path", "tasks")
    else:
        print(f"no {args.config}: pass --config, or --tasks for --validate", file=sys.stderr)
        return 2
    tasks = [t for t in load_suite(path) if not args.task or t.name in args.task]
    if args.validate:
        bad = 0
        for t in tasks:
            before, after = validate(t)
            ok = before < 1 and after in (None, 1.0)
            bad += not ok
            print(f"{t.name:<24} {'held-out' if t.held_out else 'dev':<8} files={before:.2f} "
                  f"solution={'-' if after is None else f'{after:.2f}'} {'ok' if ok else 'CHECK THIS TASK'}")
        return 1 if bad else 0
    if cfg is None:
        print(f"no {args.config}: suite runs need an [llm]", file=sys.stderr)
        return 2
    rt = config.build_runtime(cfg, Path(args.config).resolve().parent)
    config.register_builtin_points(rt)
    runner = SuiteRunner(rt.root, *config.build_agent_factory(cfg, rt))
    prompt = PromptStore(rt.root).get()
    print(f"prompt {prompt.hash()}, agent {runner.identity}, {len(tasks)} tasks x {args.repeats}", flush=True)

    def show(r: dict) -> None:
        result = (f"score={r['score']:.2f} turns={r['turns']} stop={r['stop']}" if r["score"] is not None
                  else f"broke: {r['error'][:160]}")
        print(f"  {r['task']:<24} {'held-out' if r['held_out'] else 'dev':<8} {result}", flush=True)

    rows = runner.runs(prompt, tasks, args.repeats, fresh=not args.cached, on_run=show)
    for split, held in (("dev", False), ("held-out", True)):
        scores = [r["score"] for r in rows if r["held_out"] == held and r["score"] is not None]
        if scores:
            print(f"{split}: mean score {sum(scores) / len(scores):.3f} over {len(scores)} runs")
    return 0


def cmd_evolve(args) -> int:
    from .evolve.pipeline import evolve_model
    cfg, rt = _load_runtime(args.config)
    if "trainer" not in cfg:
        print("no [trainer] in the config", file=sys.stderr)
        return 2
    trainer = config.build_trainer(cfg, rt.serving)
    if args.dry_run:
        from .evolve.pipeline import dry_run_model
        print(json.dumps(dry_run_model(rt, trainer, cfg.get("recipe")), ensure_ascii=False, indent=2, default=str))
        return 0
    card = evolve_model(rt, trainer, cfg.get("recipe"))
    print(f"{card['version']}: {card['gate']} - {'; '.join(card.get('reasons', []))}")
    if card.get("deployed") is False:
        print(f"  not deployed, the champion is unchanged: {card['deploy_error']}; retry with `ouroloop deploy "
              f"{card['version']}`")

    def gains(m: dict) -> str:
        def side(x: dict) -> str:
            return (f"fixes={x['fixes']} harms={x['harms']} harmed sessions={x['harmed_sessions']}/"
                    f"{x['sessions_acted']}")
        return (f"n={m['n']} default errors={m['default_errors']}; champion {side(m['champion'])}; candidate "
                f"{side(m['challenger'])}" if m and m['n'] else "n=0")

    for point, m in (card.get("metrics") or {}).items():
        print(f"  {point}: {gains(m)}; held-out tasks, not gated: {gains(m.get('held_out'))}")
    return 0


def cmd_deploy(args) -> int:
    """`ouroloop deploy <version>`: deploy and promote a version the gate accepted whose deploy failed."""
    from .evolve.pipeline import deploy_version
    from .evolve.registry import Registry
    from .ledger import PromotionConflict
    _, rt = _load_runtime(args.config)
    registry = Registry(rt.root)
    try:
        ok = deploy_version(rt, args.version, registry)
    except (ValueError, OSError, PromotionConflict) as e:
        print(e, file=sys.stderr)
        return 1
    card = registry.card(args.version)
    print(f"{args.version}: deployed, the champion" if ok else f"{args.version}: not deployed: {card['deploy_error']}")
    return 0 if ok else 1


def cmd_gate(args) -> int:
    """`ouroloop gate freeze`: a person freezes the gate template of a workspace (PLAN_v0.2 M8)."""
    from .evolve.gate import freeze_template
    cfg = config.load(args.config)
    config.check_frozen(cfg)        # a config that would not start once the gate is frozen
    params = {}
    for item in args.set or []:
        key, _, value = item.partition("=")
        params[key] = json.loads(value)
    template, digest = freeze_template(config.workspace_root(cfg, Path(args.config).resolve().parent), params,
                                       replace_frozen=args.replace)
    print(json.dumps(template, ensure_ascii=False, indent=2))
    print(f"gate template {digest} frozen")
    return 0


def cmd_calibrate(args) -> int:
    """Report only. A threshold lets a point act once a promotion through the frozen gate stores it (M8)."""
    from .evolve.calibrate import calibrate_point
    _, rt = _load_runtime(args.config)
    for name in args.point or list(rt.points):
        cal = calibrate_point(rt, name)
        if cal is None:
            print(f"{name}: not enough calibration data")
            continue
        print(f"{name}: model={cal.model} temperature={cal.temperature:.2f} thresholds={cal.thresholds} n={cal.n} "
              f"accuracy={cal.accuracy:.3f}")
    return 0


def cmd_models(args) -> int:
    from .evolve.registry import Registry
    reg = Registry(Path(args.dir))
    if args.rollback:
        print(f"champion is now {reg.rollback()}; the retired model is revoked")
        return 0
    champion = (reg.champion() or {}).get("version")
    for v in reg.versions():
        if not (reg.dir / v / "card.json").exists():
            continue
        card = reg.card(v)
        job = card.get("job") or {}
        status = card.get("gate", card.get("status", "-"))
        status += " (not deployed)" if card.get("deployed") is False else ""
        print(f"{v}{' *' if v == champion else '  '} {status:<18} "
              f"parent={card.get('parent')} model={card.get('model_id')} job={job.get('id')} "
              f"host={job.get('host') or ('this machine' if job else None)} "
              f"{'; '.join(card.get('reasons', []))[:80]}")
    return 0


def _print_event(kind: str, data: dict) -> None:
    if kind == "assistant":
        if data.get("text"):
            print(f"\n{data['text']}")
        for c in data.get("tool_calls", []):
            print(f"  -> {c['name']} {json.dumps(c['args'], ensure_ascii=False)[:160]}")
    elif kind == "tool" and data.get("is_error"):
        print(f"  x {data['output'][:200]}")
    elif kind == "denied":
        print(f"  denied by rule {data['rule']}")
    elif kind == "retry":
        print(f"  reran automatically: {'succeeded' if data['succeeded'] else 'failed again'}")
    elif kind == "compact":
        print(f"  dropped an old output: {data['target'][:80]}")
    elif kind == "verify":
        print(f"  verification {'passed' if data['passed'] else 'failed'}")
    elif kind == "pushback":
        print(f"  sent back to work: {data['reason'][:200]}")


def _terminal_approve(call) -> bool:
    from .agent.loop import describe
    print(f"\n  {call.name}: {describe(call)[:600]}")
    return input("  allow? [y/N] ").strip().lower() in ("y", "yes")


def _terminal_confirm(task: str, text: str) -> bool | None:
    answer = input("\n  Was the task done? [y/n, Enter to skip] ").strip().lower()
    return True if answer in ("y", "yes") else False if answer in ("n", "no") else None


def cmd_run(args) -> int:
    from .agent import Agent, AgentConfig, Tools
    from .agent.prompt import PromptStore
    if args.config:
        cfg, base = config.load(args.config), Path(args.config).resolve().parent
    else:
        cfg, base = {"workspace": {"root": ".ouroloop"}}, Path.cwd()
        if args.judge == "jev":
            cfg["backends"] = {"jev": {"type": "jev"}}
    if args.llm:
        cfg["llm"] = {"type": args.llm, **({"model": args.model} if args.model else {}),
                      **({"base_url": args.base_url} if args.base_url else {}),
                      **({"api_key_env": args.api_key_env} if args.api_key_env else {})}
    if "llm" not in cfg:
        print("no LLM configured: pass --config with an [llm] table, or --llm anthropic|openai_compat",
              file=sys.stderr)
        return 2
    if args.small_model:
        cfg["llm_small"] = {**cfg["llm"], "model": args.small_model}
    rt = config.build_runtime(cfg, base)
    config.register_builtin_points(rt)
    provider = config.build_provider(cfg["llm"])
    tiers = {"large": provider, "small": config.build_provider(cfg["llm_small"])} if "llm_small" in cfg else None
    interactive = sys.stdin.isatty() and not args.yes
    prompt = PromptStore(rt.root).get()     # the champion harness prompt of this workspace
    agent = Agent(provider, rt, Tools(Path.cwd(), descriptions=prompt.tools), tiers=tiers, system=prompt.system,
                  config=AgentConfig(approve="auto" if args.yes else "ask", max_turns=args.max_turns,
                                     verify_command=args.verify),
                  approve_fn=_terminal_approve, confirm_fn=_terminal_confirm if interactive else None,
                  on_event=_print_event)
    res = agent.run(args.task)
    print(f"\n[{res.stop} after {res.turns} turns; session {res.session}; workspace {rt.root}]")
    return 0


def cmd_serve(args) -> int:
    import os
    from .serve import make_server
    _, rt = _load_runtime(args.config)
    try:
        server = make_server(rt, args.host, args.port, os.environ.get("OUROLOOP_TOKEN"))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"serving {sorted(rt.backends)} and points {sorted(rt.points)} on http://{args.host}:{server.server_port}")
    server.serve_forever()
    return 0


def cmd_host(args) -> int:
    """`ouroloop host init|check <target>`: prepare a training host for this ouroloop and the trainer plugins, or say
    what keeps it from running their jobs (docs/TRAINING_HOSTS.md). Plugins and their options come from --trainer and
    the config's [trainer] and [trainers.<name>] tables."""
    from .hosts import HostError, init, read, stale
    cfg = config.load(args.config) if args.config else {}
    options, table = config.plugin_options(cfg), cfg.get("trainer") or {}
    names = args.trainer or [n for n in [table.get("type")] if n and ":" not in n]
    trainers = {name: options.get(name, {}) for name in names}
    ssh_opts = args.ssh_opts or table.get("ssh_opts", "-o BatchMode=yes")
    from .stores import kind
    store = kind("hf" if args.hf else config.store_table(cfg).get("type", "dir"))
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


def _hook_runtime(path: str | None):
    import os
    from .backends import KeywordBackend
    from .runtime import Runtime
    path = path or os.environ.get("OUROLOOP_CONFIG") or str(Path.home() / ".ouroloop" / "ouroloop.toml")
    if Path(path).exists():
        _, rt = _load_runtime(path)
    else:
        rt = Runtime(Path.home() / ".ouroloop", {"mock": KeywordBackend()}, "mock")
    config.register_builtin_points(rt)
    return rt


def cmd_hook(args) -> int:
    from .adapters.claude_code import ClaudeCodeAdapter, deny, settings_json
    if args.target == "print-settings":
        print(settings_json("ouroloop hook claude-code" + (f" --config {Path(args.config).resolve()}"
                                                           if args.config else "")))
        return 0
    event = None
    try:   # a hook must never break the Claude Code session
        event = json.load(sys.stdin)
        out = ClaudeCodeAdapter(_hook_runtime(args.config), live=args.live,
                                enforce_deny_list=args.enforce_deny_list).handle(event)
    except Exception as e:  # noqa: BLE001 - but a broken runtime must not lift the deny list
        print(f"ouroloop hook: {type(e).__name__}: {e}", file=sys.stderr)
        out = deny(event) if args.enforce_deny_list and isinstance(event, dict) else None
    if out:
        print(json.dumps(out))
    return 0


def replay_items(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def cmd_replay(args) -> int:
    """Ask a backend the recorded decision points and compare with the reference answers (evals/replay_tde)."""
    from .ledger import now
    from .types import brier
    items = replay_items(args.file)
    backend = _backend(args)
    import random
    answers = backend.decide([
        Request(it["state"], Question(it["type"], it["instructions"], it.get("criteria"))
                .presented(random.Random(it["id"])))     # order shuffled per item, reproducibly
        for it in items])
    rows = []
    for it, ans in zip(items, answers):
        ref = it["reference"]
        rows.append({"id": it["id"], "ref_kind": it["ref_kind"], "reference": ref, "label": ans.label,
                     "agree": ans.label == ref, "p_reference": round(ans.probs.get(ref, 0.0), 4),
                     "brier": round(brier(ans.probs, {ref: 1.0}), 4), "probs": ans.probs})
        print(f"{'ok ' if ans.label == ref else 'MISS'} {it['id']:<30} ref={ref:<30} got={ans.label:<30} "
              f"p(ref)={ans.probs.get(ref, 0.0):.2f}")
    summary = {}
    for kind in ("all", "human", "outcome"):
        part = [r for r in rows if kind == "all" or r["ref_kind"] == kind]
        if part:
            summary[kind] = {"n": len(part), "agree": sum(r["agree"] for r in part),
                             "mean_p_reference": round(sum(r["p_reference"] for r in part) / len(part), 4),
                             "mean_brier": round(sum(r["brier"] for r in part) / len(part), 4)}
            print(f"{kind:<8} agree {summary[kind]['agree']}/{len(part)}  mean p(ref) {summary[kind]['mean_p_reference']:.2f}"
                  f"  mean Brier {summary[kind]['mean_brier']:.3f}")
    if args.out:
        usage = [a.usage for a in answers if a.usage]
        Path(args.out).write_text(json.dumps({"ts": now(), "backend": backend.name,
                                              "model": answers[0].model if answers else "", "summary": summary,
                                              "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
                                              "items": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"results: {args.out}")
    return 0


def ledger_stats(rows) -> dict[str, dict]:
    """Per point: decisions, how many carry a trainable label, how often the backend's own answer was among the
    right answers, and what the best constant answer would have scored.

    The baseline is not decoration. A point where one answer is always right is one a constant answer gets right
    every time, so an agreement figure there says nothing about the model; `degenerate` marks those, and
    callers should refuse to report agreement for them.
    """
    from .evolve.replay import right
    stats: dict[str, dict] = {}
    labels: dict[str, Counter] = {}
    for r in rows:
        point = r.decision.get("point")
        s = stats.setdefault(point, {"decisions": 0, "labelled": 0, "scored": 0, "agree": 0})
        s["decisions"] += 1
        target, probs = r.target(), r.decision.get("probs")
        if target is None:
            continue
        s["labelled"] += 1
        good = right(target[0])
        labels.setdefault(point, Counter()).update(good)
        if probs and not r.decision.get("abstained"):      # an abstention's uniform probabilities predict nothing
            s["scored"] += 1
            predicted = r.decision["candidates"][max(range(len(probs)), key=probs.__getitem__)]
            s["agree"] += predicted in good
    for point, s in stats.items():
        seen, n = labels.get(point, Counter()), s["labelled"]
        s["baseline"] = max(seen.values()) / n if n else None      # the best constant answer
        s["degenerate"] = bool(n) and max(seen.values()) == n      # one answer always right: agreement is meaningless
    return stats


def cmd_ledger(args) -> int:
    from .ledger import Ledger
    for point, s in sorted(ledger_stats(Ledger(Path(args.dir) / "ledger").rows()).items()):
        line = (f"{point:<24} decisions={s['decisions']:<6} labelled={s['labelled']:<6} "
                f"({s['labelled'] / s['decisions']:.0%})  ")
        if not s["scored"]:
            print(line + "agreement=-")
        elif s["degenerate"]:
            print(line + f"agreement=NOT REPORTABLE ({s['scored']} scored, one answer right every time; "
                         f"a constant answer scores 100%)")
        else:
            print(line + f"agreement={s['agree'] / s['scored']:.2f} of {s['scored']} "
                         f"(best constant answer {s['baseline']:.2f})")
    return 0


def main(argv: list[str] | None = None) -> int:
    config.load_dotenv(".env")
    p = argparse.ArgumentParser(prog="ouroloop")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("decide", cmd_decide), ("smoke", cmd_smoke), ("replay", cmd_replay)):
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        s.add_argument("--backend", default="mock" if name == "decide" else "jev")
        s.add_argument("--model", help="pin a Jev model version")
        s.add_argument("--run", help="TDE run directory (tde backend)")
        s.add_argument("--config", help="take the backend from this ouroloop.toml")
    rp = sub.choices["replay"]
    rp.add_argument("file", nargs="?", default="evals/replay_tde/decisions.jsonl")
    rp.add_argument("--out", help="write per-item results as JSON")
    d = sub.choices["decide"]
    d.add_argument("--type", choices=["noul", "choice", "score"], required=True)
    d.add_argument("--question", required=True)
    d.add_argument("--state", required=True)
    d.add_argument("--criteria", help="JSON: noul {true,false}, choice {label: description}, score [levels]")
    m = sub.add_parser("demo", help="one offline research round on synthetic data; with a scenario pack's name, "
                                    "its multi-generation loop")
    m.set_defaults(fn=cmd_demo)
    m.add_argument("scenario", nargs="?", help="a scenario pack: reads examples/<scenario>/demo.toml")
    m.add_argument("--config", help="the scenario's config (default examples/<scenario>/demo.toml)")
    m.add_argument("--generations", type=int, help="how many generations to run (default: the config's)")
    m.add_argument("--dir", help="the workspace (default: the config's)")
    m.add_argument("--judge", choices=["mock", "jev"], default="mock")
    m.add_argument("--seed", type=int, default=0)
    a = sub.add_parser("run", help="run the agent on a task in the current directory")
    a.set_defaults(fn=cmd_run)
    a.add_argument("task")
    a.add_argument("--config", help="ouroloop.toml with [llm], backends and points")
    a.add_argument("--llm", choices=["anthropic", "openai_compat"], help="LLM provider when not in the config")
    a.add_argument("--model")
    a.add_argument("--base-url", help="for openai_compat, e.g. https://open.bigmodel.cn/api/paas/v4")
    a.add_argument("--api-key-env", help="for openai_compat, the variable that holds the key, e.g. ZHIPUAI_API_KEY")
    a.add_argument("--small-model", help="a cheaper model of the same provider, used when route picks 'small'")
    a.add_argument("--verify", help="a shell command that exits 0 when the task is done, e.g. 'pytest -q'")
    a.add_argument("--judge", choices=["mock", "jev"], default="mock", help="decision backend without a config")
    a.add_argument("--yes", action="store_true", help="run gated tools without asking")
    a.add_argument("--max-turns", type=int, default=30)
    su = sub.add_parser("suite", help="run the agent on a task suite with the champion harness prompt")
    su.set_defaults(fn=cmd_suite)
    su.add_argument("--config", default="ouroloop.toml")
    su.add_argument("--tasks", help="directory of task files (default: the [suite] path)")
    su.add_argument("--task", action="append", help="only this task; repeatable")
    su.add_argument("--repeats", type=int, default=1)
    su.add_argument("--cached", action="store_true", help="reuse recorded runs instead of running again")
    su.add_argument("--validate", action="store_true", help="check each task against its files and solution, no LLM")
    r = sub.add_parser("research", help="research rounds on a configured workspace")
    r.set_defaults(fn=cmd_research)
    r.add_argument("--config", default="ouroloop.toml")
    r.add_argument("--rounds", type=int, default=1)
    r.add_argument("--patience", type=int, default=2, help="stop after this many rounds without a promotion")
    e = sub.add_parser("evolve", help="train a new decision model from the ledger and gate it")
    e.set_defaults(fn=cmd_evolve)
    e.add_argument("--config", default="ouroloop.toml")
    e.add_argument("--dry-run", action="store_true", help="prepare and validate the job on the trainer, no training")
    dp = sub.add_parser("deploy", help="deploy and promote a version the gate accepted whose deploy failed")
    dp.set_defaults(fn=cmd_deploy)
    dp.add_argument("version")
    dp.add_argument("--config", default="ouroloop.toml")
    c = sub.add_parser("calibrate", help="fit and report each point's temperature and thresholds; writes nothing")
    c.set_defaults(fn=cmd_calibrate)
    c.add_argument("--config", default="ouroloop.toml")
    c.add_argument("--point", action="append")
    sv = sub.add_parser("serve", help="HTTP decision service for harnesses on other machines; a model server")
    sv.set_defaults(fn=cmd_serve)
    sv.add_argument("--config", default="ouroloop.toml")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    ho = sub.add_parser("host", help="prepare a training host (init), or check that it runs this ouroloop")
    ho.set_defaults(fn=cmd_host)
    ho.add_argument("action", choices=["init", "check"])
    ho.add_argument("target", help='"local" (this machine) or user@host over ssh')
    ho.add_argument("--trainer", action="append", help="a trainer plugin to install, e.g. tde; repeatable "
                                                        "(default: the config's [trainer] type)")
    ho.add_argument("--config", help="an ouroloop.toml: its [trainer] and [trainers.<name>] options")
    ho.add_argument("--ssh-opts", help='e.g. "-i ~/.ssh/key -o BatchMode=yes" (default: the config\'s)')
    ho.add_argument("--hf", action="store_true", help='also make it ready for a [store] of type "hf"')
    hk = sub.add_parser("hook", help="Claude Code hooks: record decisions and outcomes from normal sessions")
    hk.set_defaults(fn=cmd_hook)
    hk.add_argument("target", choices=["claude-code", "print-settings"])
    hk.add_argument("--config", help="ouroloop.toml (default: $OUROLOOP_CONFIG or ~/.ouroloop/ouroloop.toml)")
    hk.add_argument("--live", action="store_true", help="ask the decision backend at hook time")
    hk.add_argument("--enforce-deny-list", action="store_true", help="deny commands on the built-in deny list")
    g = sub.add_parser("gate", help="freeze the gate template that every promotion goes through")
    g.set_defaults(fn=cmd_gate)
    g.add_argument("action", choices=["freeze"])
    g.add_argument("--config", default="ouroloop.toml")
    g.add_argument("--set", action="append", metavar="KEY=JSON", help='change a parameter, e.g. min_sessions=40')
    g.add_argument("--replace", action="store_true", help="freeze again; the next round freezes a new record")
    mo = sub.add_parser("models", help="list trained models, or roll back the champion")
    mo.set_defaults(fn=cmd_models)
    mo.add_argument("--dir", default=".")
    mo.add_argument("--rollback", action="store_true")
    led = sub.add_parser("ledger", help="decision counts per point")
    led.set_defaults(fn=cmd_ledger)
    led.add_argument("--dir", default=".")
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
