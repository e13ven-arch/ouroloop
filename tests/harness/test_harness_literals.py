"""The literal check (docs/design/HARNESS.md §6, §8, §10): built from train rows only, only lines a candidate changed
against the seed are searched, `allowed` exempts."""
from __future__ import annotations

import json

import pytest

from ouroloop.contracts import Objects
from ouroloop.harness import literals as L
from ouroloop.harness import seed as sd

CORE = {"model": {"identity": "claude-opus-5-5", "effort": "medium", "max_effort": "medium"}, "memory": {}}
ROWS = [
    {"task_id": "t1", "assertions": [{"type": "sent", "params": {"to": "dana.reyes@acme.io", "amount": "13,650"}}]},
    {"task_id": "t2", "assertions": [{"type": "sent", "params": {"to": "ops@acme.io", "channel": "C0042"}}],
     "state": {"records": [{"owner": "Sam Okafor", "Stage": "Closed Won"}], "headers": ["Invoice No"]}},
    {"task_id": "t3", "assertions": [{"type": "sent", "params": {"to": "ops@acme.io"}}],
     "state": {"limit": "3000"}},
]


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    return sd.build_seed("stirrup", core=CORE, objects=Objects(tmp_path_factory.mktemp("o")))


def test_the_index_keeps_one_tasks_values_and_drops_shared_ones_headers_and_constants():
    index = L.literal_index(ROWS)
    forbidden = L.forbidden(index)
    assert {"dana.reyes@acme.io", "13,650", "C0042", "Sam Okafor"} <= forbidden
    assert "ops@acme.io" not in forbidden                       # two train tasks: the domain's vocabulary
    assert "Stage" not in forbidden and "Invoice No" not in forbidden     # schema words
    assert "3000" not in forbidden                              # a number no assertion checks: a constant
    assert "Closed Won" not in L.forbidden(index, exempt={"Closed Won"})


def test_the_graders_own_assertions_index_their_values_beside_the_type():
    """A grading row's assertion is upstream's flat dict ({type, its fields}), not an attempt's {type, params}."""
    flat = [{"task_id": "t1", "assertions": [{"type": "google_sheets_row_exists", "spreadsheet_id": "ss_inv_tracker",
                                              "cell_contains": {"Vendor": "Acme", "Invoice": "ACM-2026-0412",
                                                                "Amount": "4,750"}}]},
            {"task_id": "t2", "assertions": [{"type": "google_sheets_row_exists", "spreadsheet_id": "ss_inv_tracker",
                                              "excluded": False}]}]
    index = L.literal_index(flat)
    forbidden = L.forbidden(index)
    assert {"ACM-2026-0412", "4,750"} <= forbidden and "4,750" in index.answers
    assert "ss_inv_tracker" not in forbidden                    # two train tasks
    assert "google_sheets_row_exists" not in index.tasks        # the assertion's type is the grader's schema
    assert "Vendor" in index.headers


def test_only_changed_lines_of_a_seed_file_are_searched(seed):
    forbidden = L.forbidden(L.literal_index(ROWS))
    # an upstream line that already holds a literal is not the candidate's: plant one in the "seed" itself
    base = dict(seed.files)
    base["vendor/stirrup/constants.py"] += "\nOWNER = 'Sam Okafor'\n"
    seedlike = type(seed)(manifest=seed.manifest, files=base)
    assert L.findings(base, seedlike, forbidden) == []
    edited = {**base, "vendor/stirrup/constants.py": base["vendor/stirrup/constants.py"] + "TO = 'dana.reyes@acme.io'\n"}
    found = L.findings(edited, seedlike, forbidden)
    assert [(f["path"], f["literal"]) for f in found] == [("vendor/stirrup/constants.py", "dana.reyes@acme.io")]
    assert found[0]["line"] == len(edited["vendor/stirrup/constants.py"].splitlines())
    assert L.findings(edited, seedlike, forbidden, allowed={"dana.reyes@acme.io"}) == []


def test_a_new_file_its_path_and_the_decoded_manifest_are_searched(seed):
    forbidden = L.forbidden(L.literal_index(ROWS))
    files = {**seed.files, "skills/c0042.md": "Post to C0042 first.",
             "manifest.json": seed.files["manifest.json"].replace('"skills": []',
                                                                  '"skills": [{"name": "x", "path": "skills/c0042.md", '
                                                                  '"description": "for Sam \\u004fkafor"}]')}
    found = {(f["path"], f["line"], f["literal"]) for f in L.findings(files, seed, forbidden)}
    assert ("skills/c0042.md", 1, "C0042") in found
    assert ("manifest.json", None, "Sam Okafor") in found       # hidden behind a JSON escape in the raw text
    assert json.loads(files["manifest.json"])                   # still a manifest


def test_the_seed_searched_whole_finds_nothing_from_these_rows(seed):
    assert L.findings(seed.files, None, L.forbidden(L.literal_index(ROWS))) == []


def test_changed_lines_and_the_description_for_r():
    assert L.changed_lines("a\nb\nc", "a\nc") == {2}
    assert L.changed_lines("a\nb", None) == {1, 2}
    text = L.describe([{"path": "tools/x.py", "line": 3, "literal": "C0042", "kind": "identifier"}])
    assert "tools/x.py:3: 'C0042' (identifier)" in text
