"""tests/test_m_<family>.py: a method's tests, on synthetic data only (docs/design/V0_5_METHOD_LIBRARY.md §2.7).

Package tests run in the worker beside the package; a value that occurs in only one train task is refused by the
literal check here as anywhere in a package, so build the data the test needs yourself. A test loads the method's
module by path (the worker has no import path for the package) and calls it with a fake ctx whose call_tool answers
from a dict.
"""
import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path):
    spec = importlib.util.spec_from_file_location("t_" + path.replace("/", "_").replace(".py", ""),
                                                  os.path.join(ROOT, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Res:
    def __init__(self, output, is_error=False):
        self.output, self.is_error = output, is_error


class FakeCtx:
    """call_tool answers from `answers`: {(tool, url or query): payload}; anything else is a 404."""

    def __init__(self, answers, task="synthetic task"):
        self.answers, self.view, self.calls = answers, {"task": task, "events": []}, []

    def call_tool(self, name, args=None):
        args = dict(args or {})
        self.calls.append((name, args))
        key = (name, args.get("url") or args.get("query"))
        if key not in self.answers:
            return Res(json.dumps({"error": {"code": 404, "message": "not found"}}), True)
        return Res(json.dumps(self.answers[key]))


def test_the_template_loads_its_own_helpers():
    # Replace with tests of your method: synthetic records in, evidence with sources and an `unclassified` part out,
    # and a wrong argument answered with is_error (never an exception).
    ctx = FakeCtx({("api_fetch", "https://example.invalid/rows"): {"values": [["name", "amount"], ["a", "1"]]}})
    assert json.loads(ctx.call_tool("api_fetch", {"url": "https://example.invalid/rows"}).output)["values"]
