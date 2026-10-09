"""Startup checks (docs/design/ACTIVITY.md §3, §7; DESIGN §8): what has to hold before a phase starts, and only of the
platform's three kinds — money, leakage, correctness. A failing check names itself; nothing is paid before they pass.

    money        a paid phase has a budget root in dollars, prices that bound every call, and a final share that holds
                 the in-flight reservations; the actor and the research model are the claude_code CLI on a
                 subscription login, at K's effort, and no call can ask for more output than its reservation bounds
    leakage      the split is the frozen one (K's and the pinned hash) and upstream has not drifted; no test task was
                 read before the final; the memory stream runs only on a line K does not hold out
    correctness  the worker image is the one K pinned and runs this host's worker_runner.py; the egress image under a
                 proxy K; the seed K builds is the seed release's; the code is the code K froze; a round has the frozen
                 gate plan; where memory is read
                 or written, the sidecar's venv is the lock, the model is the pinned one and K's backend pins are this
                 checkout's sidecar files
"""
from __future__ import annotations

from ..adapters import automationbench as ab
from ..evaluate.budget import call_bound_problem, check_pricing
from ..evaluate.release import code_version
from ..harness.package import Limits, check_shape
from ..harness.seed import SeedUnavailable, seed_files

PAID = ("smoke", "baseline", "library", "round", "evaluate")


def money_problems(activity, *, phase: str, needs_model: bool) -> list[str]:
    k = activity.k
    problems = []
    if not needs_model:
        return problems
    budget = k.get("budget") or {}
    pricing = activity.pricing()
    if budget.get("usd") is None:
        problems.append("[budget] usd is not set: a paid phase opens the activity's one budget root in dollars")
    else:
        reason = check_pricing(pricing, {"usd": budget["usd"]}) or call_bound_problem(
            pricing, {"usd": budget["usd"]}, "the activity's budget")
        if reason:
            problems.append(f"the declared budget cannot be enforced: {reason}")
        if budget.get("final_usd") is not None and pricing.bounded:
            n = activity.concurrency
            bound = pricing.bound().get("usd") or 0.0
            if n * bound > float(budget["final_usd"]) + 1e-9:
                problems.append(f"the final share ${budget['final_usd']} cannot hold {n} in-flight reservations of "
                                f"${bound:.4f}")
    for section in ("model", "research_model"):
        table = activity.model_table(section)
        if table["type"] != "claude_code":
            problems.append(f"[{section}] type {table['type']!r}: the actor and R run through the claude_code CLI "
                            f"(PLAN rule 9)")
            continue
        if table["effort"] != k["model"]["max_effort"]:
            problems.append(f"[{section}] effort {table['effort']!r} is not K's {k['model']['max_effort']!r}: every "
                            f"arm runs at one effort (PLAN rule 10)")
        if pricing.max_output_tokens is not None:
            from ..providers.claude_code import REQUESTS_PER_CALL
            if int(table["max_tokens"]) * REQUESTS_PER_CALL > int(pricing.max_output_tokens):
                problems.append(f"[{section}] max_tokens {table['max_tokens']} times the {REQUESTS_PER_CALL} requests "
                                f"one CLI call can make is above [pricing] max_output_tokens "
                                f"{pricing.max_output_tokens}: a reservation would not bound the call")
        if activity.provider_factory is None:
            from ..providers.claude_code import cli_problem
            problem = cli_problem(table)
            if problem:
                problems.append(f"[{section}]: {problem}")
    return problems


def leakage_problems(activity, *, phase: str) -> list[str]:
    problems = []
    if activity.drift:
        problems.append(f"upstream drifted from the frozen split: {activity.drift[:3]}")
    if activity.core.split_hash != activity.splits.hash():
        problems.append(f"K froze split {activity.core.split_hash} and the workspace's split is {activity.splits.hash()}")
    reads = list(activity.events.read("test_read"))
    if phase != "evaluate" and reads:
        problems.append(f"{len(reads)} final run(s) already read the test tasks: this activity's development is over")
    if phase == "library":
        memory = activity.k.get("memory") or {}
        if not memory.get("enabled"):
            problems.append("[memory] enabled = false: this activity runs no memory stream")
        if activity.domain in set(memory.get("heldout_domains") or ()):
            problems.append(f"{activity.domain} is a held-out business line ([memory] heldout_domains): none of its "
                            f"experience enters a library")
    return problems


def uses_memory(activity, phase: str) -> bool:
    """Whether this phase needs the memory's sidecar: the library phase, or a package it runs that holds a snapshot
    (a package with memory on and no snapshot reads an empty library, which starts nothing)."""
    if phase == "library" or (phase == "round" and (activity.k.get("memory") or {}).get("enabled")):
        return True                     # a round screens the library's snapshot and reads the stream's entries
    releases = [activity.active_release()] if activity.releases.active_ref() else []
    plan_ref = (activity.config.get("rsi") or {}).get("final_plan_ref")
    if phase == "evaluate" and plan_ref and activity.objects.has(plan_ref):
        releases += [activity.releases.load(r) for r in (activity.objects.get(plan_ref).get("arms") or {}).values()]
    return any(activity.package(r).memory_text.strip() for r in releases)


def memory_problems(activity) -> list[str]:
    """The sidecar this activity would start: K's backend pins are this checkout's files; the venv is the lock and the
    model the pinned one (a test's in-process client stands in for both)."""
    from ..memory import backend_problems, memory_venv, model_problems
    from ..memory.settings import Settings
    try:
        settings = Settings.from_core(activity.k)
    except ValueError as e:
        return [f"[memory]: {e}"]
    problems = [f"K's memory backend: {p}" for p in backend_problems(settings.backend)]
    if getattr(activity, "memory_client_factory", None) is None:
        from ..memory.__main__ import LOCK, venv_problems
        problems += [f"the memory sidecar's venv: {p}" for p in venv_problems(memory_venv(), LOCK)]
        problems += [f"the memory's model: {p}" for p in model_problems(settings.retriever)]
    return problems


def correctness_problems(activity, *, phase: str, needs_worker: bool) -> list[str]:
    problems = []
    if activity.core.code_version != code_version():
        problems.append(f"K froze code {activity.core.code_version} and this checkout is {code_version()}: no code "
                        f"change is merged into a checkout an activity runs from (PLAN rule 13)")
    try:
        files = seed_files(activity.k["harness"]["seed"], core=activity.k)
    except SeedUnavailable as e:
        problems.append(str(e))
        files = None
    if files is not None:
        shape = check_shape(files, limits=Limits.from_core(activity.k), seed=None)
        problems += [f"the seed does not fit K: {p}" for p in shape]
        ref = activity.releases.active_ref()
        if ref is not None:
            from ..harness.package import Package, parse_manifest
            seed = Package(manifest=parse_manifest(files["manifest.json"], files, limits=Limits.from_core(activity.k)),
                           files=files) if not shape else None
            first = activity.releases.history()[-1]
            if seed is not None and seed.ref() != first.package_ref:
                problems.append(f"the seed K builds now ({seed.ref()[:28]}) is not the seed release's W0 "
                                f"({first.package_ref[:28]}): the seed's source changed under the activity")
    if needs_worker:
        worker = activity.worker()
        if worker is None:
            problems.append(f"no qualified worker: Docker or the image {activity.k['worker']['image']} (pinned id "
                            f"{activity.k['worker'].get('image_id')}) is not available; build it with docker build "
                            f"--provenance=false -f docker/worker.Dockerfile -t {activity.k['worker']['image']} .")
        else:
            problem = worker.runner_problem()
            if problem:
                problems.append(f"the worker cannot serve this host: {problem}")
        if activity.k["worker"].get("network") not in (None, "none"):
            from ..runtime.egress import DEFAULT_IMAGE as EGRESS_IMAGE
            import shutil
            import subprocess
            if shutil.which("docker") is None or subprocess.run(["docker", "image", "inspect", EGRESS_IMAGE],
                                                                capture_output=True).returncode != 0:
                problems.append(f"K's network is the egress proxy and the image {EGRESS_IMAGE} is not built "
                                f"(docker build --provenance=false -f docker/egress.Dockerfile -t {EGRESS_IMAGE} .)")
    if phase == "round" and not list(activity.events.read("gate_template")):
        problems.append("no frozen gate plan: run `ouroloop freeze` before a round (the gate's template is fixed "
                        "before any candidate is seen)")
    if phase in PAID and uses_memory(activity, phase):
        problems += memory_problems(activity)
    checks = ab.contract_check(activity.suite, activity.splits.ids("dev")[:1])
    problems += [f"scorer contract: {line}" for line in checks["failed"]]
    return problems


def startup_checks(activity, *, phase: str, needs_model: bool = True, needs_worker: bool | None = None) -> list[str]:
    """Every problem of the three kinds for this phase ([] when it may start)."""
    needs_worker = needs_model if needs_worker is None else needs_worker
    return (money_problems(activity, phase=phase, needs_model=needs_model)
            + leakage_problems(activity, phase=phase)
            + correctness_problems(activity, phase=phase, needs_worker=needs_worker))


def require(problems: list[str], *, phase: str) -> None:
    if problems:
        raise RuntimeError(f"the {phase} phase cannot start: " + "; ".join(problems))
