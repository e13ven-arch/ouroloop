"""The import boundaries of docs/design/CONTRACTS.md §1.

- An attempt (`evaluate.attempt.run_attempt`) leaves no `ouroloop.improver`, `ouroloop.memory.sidecar` or
  `ouroloop.train` in sys.modules, and neither do the activity phases an attempt runs under (ACTIVITY.md §9).
- The control plane never imports package code: nothing of a package's vendor/, agent/, tools/, middlewares/ or
  tests/ is a module of the host process; it runs in the worker only.
- adapters/ imports nothing of the framework, and harness, runtime, evaluate, activity and contracts import only the
  packages below them.

The run-time checks each start a fresh interpreter, because this process has imported whatever the other tests use
(tests/harness imports the seed's own tests, for one).
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ouroloop.harness import seed as sd

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
PACKAGE = REPO / "ouroloop"

# Off the hot path: the improver and the memory's sidecar client (stages 2 and 3) and the training stack.
OFF_PATH = ("ouroloop.improver", "ouroloop.memory.sidecar", "ouroloop.train")
# A package's code directories (HARNESS §2): they run in the worker, never in the control plane.
PACKAGE_DIRS = ("vendor", "agent", "tools", "middlewares", "tests")

# CONTRACTS §1: what each package may import of the framework, besides itself.
LAYERS = {
    "contracts": set(),
    "harness": {"contracts"},
    "runtime": {"contracts", "harness"},
    "evaluate": {"contracts", "harness", "runtime", "adapters"},
    "memory": {"contracts", "harness", "runtime"},
    "activity": {"contracts", "harness", "runtime", "evaluate", "adapters", "memory", "improver"},
}
# providers/ has no place in CONTRACTS §1 (RUNTIME §10 leaves it as it was). It builds on runtime.chat, and these
# packages reach it only inside a function: runtime to tell an account error, evaluate for ProviderError, activity to
# build K's providers. A module-level import of it from them is refused like any other edge.
FUNCTION_LEVEL_ONLY = {"runtime": {"providers"}, "evaluate": {"providers"}, "memory": {"providers"},
                       "activity": {"providers"}}


# ---------------------------------------------------------------------------- run-time checks, in a fresh process
SCRIPT = r"""
import json, sys
from pathlib import Path
upstream, tmp = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, upstream)
import ouroloop
import rsi
from ouroloop.activity import cli  # noqa: F401 - the CLI module is on the path of `ouroloop bench` too
from ouroloop.activity import phases
from ouroloop.evaluate.attempt import make_agent_factory, run_attempt

def modules():
    return {name: getattr(m, "__file__", None) for name, m in list(sys.modules.items())}

quiet = lambda _line: None
# one attempt, through evaluate.attempt.run_attempt and the runner shell, the seed in a ProcessWorker
activity = rsi.activity(tmp / "attempt", upstream)
release = activity.active_release()
run = activity.run_manifest(candidate="boundary", role="base", split="train", release=release)
make = make_agent_factory(provider=activity.actor_provider(activity.account("boundary")),
                          package=activity.package(release), core=activity.k, worker=activity.worker(),
                          root=activity.root, pricing=activity.pricing(), release_ref=release.ref(),
                          core_ref=activity.core_ref)
attempt = run_attempt(activity.suite, rsi.TRAIN[1], make, run=run, splits=activity.splits, release_ref=release.ref())
attempt.episode.close()
print(json.dumps({"ouroloop": ouroloop.__file__, "termination": attempt.result.termination,
                  "validity": attempt.result.validity, "success": attempt.result.success,
                  "steps": attempt.result.steps, "modules": modules()}))
# the phases an attempt runs under: the smoke with its attempt, freeze, a baseline of one train and one dev task, report
activity = rsi.activity(tmp / "phases", upstream)
smoke = phases.smoke(activity, out=quiet)
frozen = phases.freeze(activity, out=quiet)
baseline = phases.baseline(activity, limit=1, out=quiet)
report = phases.report(activity, out=quiet)
print(json.dumps({"smoke_ok": smoke["ok"], "smoke_attempts": len(smoke["attempts"]), "frozen": sorted(frozen),
                  "baseline": [baseline["train"]["attempts"]] + [d["attempts"] for d in baseline["dev"]],
                  "report": sorted(report), "modules": modules()}))
"""


def loaded(prefix: str, modules) -> list[str]:
    return sorted(m for m in modules if m == prefix or m.startswith(prefix + "."))


def package_code(modules: dict) -> list[str]:
    """The modules of this process that are package code: a file of the seed's source tree, a module named by a
    vendored package, a module the worker runner names (`pkg_`, `pkgtest_`), or a file whose path ends in one of the
    package's code files wherever it was unpacked."""
    root = sd.seed_dir("stirrup")
    files = sd.read_tree(root)
    code = sorted(p for p in files if p.split("/")[0] in PACKAGE_DIRS and p.endswith(".py"))
    vendored = sorted({p.split("/")[1] for p in files if p.startswith("vendor/")})
    assert code and vendored == ["stirrup"]
    found = []
    for name, file in modules.items():
        path = Path(file).resolve().as_posix() if file else ""
        if (path.startswith(sd.SEEDS.resolve().as_posix() + "/") or name.split(".")[0] in vendored
                or name.startswith(("pkg_", "pkgtest_")) or any(path.endswith("/" + p) for p in code)):
            found.append(name)
    return sorted(found)


def test_an_attempt_and_the_phases_load_no_off_path_or_package_code(fake_upstream, tmp_path):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), str(TESTS)])}
    run = subprocess.run([sys.executable, "-c", SCRIPT, fake_upstream, str(tmp_path)], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stderr[-4000:]
    attempt, activity = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    # this checkout's code ran, and the path really ran: a valid attempt, the smoke's attempt, a baseline of two
    assert Path(attempt["ouroloop"]).resolve().is_relative_to(REPO)
    assert (attempt["termination"], attempt["validity"], attempt["success"]) == ("finished", "valid", True)
    assert attempt["steps"] > 1
    assert activity["smoke_ok"] and activity["smoke_attempts"] == 1 and activity["baseline"] == [1, 1]
    assert "core_ref" in activity["frozen"] and "baseline" in activity["report"]
    assert "ouroloop.runtime.runner" in attempt["modules"] and "ouroloop.activity.phases" in activity["modules"]
    for snapshot in (attempt, activity):
        assert [m for prefix in OFF_PATH for m in loaded(prefix, snapshot["modules"])] == []
        assert package_code(snapshot["modules"]) == []


# ---------------------------------------------------------------------------- the dependency direction
def module_of(path: Path) -> str:
    parts = list(path.relative_to(REPO).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imports(path: Path) -> list[tuple[str, int, bool]]:
    """(module imported, line, inside a function) for every import of a file, relative ones resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    here = module_of(path).split(".")
    package = here if path.name == "__init__.py" else here[:-1]
    out: list = []

    def visit(node, in_function: bool):
        for child in ast.iter_child_nodes(node):
            nested = in_function or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if isinstance(child, ast.Import):
                out.extend((a.name, child.lineno, in_function) for a in child.names)
            elif isinstance(child, ast.ImportFrom):
                base = package[:len(package) - child.level + 1] if child.level else []
                target = ".".join(base + ([child.module] if child.module else []))
                out.append((target, child.lineno, in_function))
                if not child.module:                     # `from .. import x`: x may be a module
                    out.extend((f"{target}.{a.name}", child.lineno, in_function) for a in child.names)
            visit(child, nested)
    visit(tree, False)
    return out


def framework_edges(package: str) -> list[tuple[str, str, bool]]:
    """(file:line, the framework package imported, inside a function) for every import from `package` of another
    ouroloop package. The seeds under harness/seeds/ are package code, not the harness package's."""
    root = PACKAGE / package
    files = [root.with_suffix(".py")] if not root.is_dir() else \
        [f for f in sorted(root.rglob("*.py")) if "seeds" not in f.relative_to(root).parts]
    assert files, package
    edges = []
    for f in files:
        for target, line, in_function in imports(f):
            parts = target.split(".")
            if parts[0] != "ouroloop" or len(parts) < 2 or parts[1] == package or parts[1] == "__version__":
                continue
            edges.append((f"{f.relative_to(REPO)}:{line}", parts[1], in_function))
    return edges


def test_adapters_import_nothing_of_the_framework():
    edges = [(where, target) for where, target, _ in framework_edges("adapters")]
    climbs = [f"{f.relative_to(REPO)}:{node.lineno}" for f in sorted((PACKAGE / "adapters").rglob("*.py"))
              for node in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
              if isinstance(node, ast.ImportFrom) and node.level > 1]
    imported = [t for f in sorted((PACKAGE / "adapters").rglob("*.py")) for t, _, _ in imports(f)]
    assert "automationbench" in {t.split(".")[0] for t in imported}     # the reader works: it sees upstream
    assert edges == [] and climbs == []


@pytest.mark.parametrize("package", sorted(LAYERS))
def test_each_package_imports_only_the_packages_below_it(package):
    allowed, lazy = LAYERS[package], FUNCTION_LEVEL_ONLY.get(package, set())
    edges = framework_edges(package)
    wrong = [(where, target) for where, target, in_function in edges
             if target not in allowed and not (in_function and target in lazy)]
    assert wrong == [], f"{package} may import {sorted(allowed)}"
    assert package == "contracts" or {t for _, t, _ in edges} & allowed      # the reader sees the real edges


IMPORT_ALL = r"""
import importlib, json, pkgutil, sys
name, out = sys.argv[1], sys.argv[2]
top = importlib.import_module("ouroloop." + name)
for info in pkgutil.iter_modules(getattr(top, "__path__", []), top.__name__ + "."):
    importlib.import_module(info.name)
with open(out, "w") as f:           # not stdout: runtime.worker_runner takes fd 1 for its protocol when imported
    json.dump({"ouroloop": sys.modules["ouroloop"].__file__, "modules": sorted(sys.modules)}, f)
"""


@pytest.mark.parametrize("package", sorted(LAYERS))
def test_importing_a_package_loads_nothing_above_it(package, tmp_path):
    """Every module of the package imported in a fresh interpreter: the framework packages it pulls in, at any depth,
    are the ones below it (a package outside the table, reached at module level, would show here)."""
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    out = tmp_path / "modules.json"
    run = subprocess.run([sys.executable, "-c", IMPORT_ALL, package, str(out)], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-4000:]
    got = json.loads(out.read_text())
    modules = got["modules"]
    assert Path(got["ouroloop"]).resolve().is_relative_to(REPO)
    pulled = {m.split(".")[1] for m in modules if m.startswith("ouroloop.")} - {package}
    assert f"ouroloop.{package}" in modules
    assert pulled <= LAYERS[package], f"ouroloop.{package} loads {sorted(pulled - LAYERS[package])}"
    assert [m for prefix in OFF_PATH for m in loaded(prefix, modules)] == []
