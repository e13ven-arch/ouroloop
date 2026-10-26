"""Two marketing tasks, in the row shape upstream's loader produces: info is a JSON string that carries
task_name, zapier_tools, initial_state and assertions."""
from __future__ import annotations

import json

from automationbench.task_contract import task_contract_sha256

SYSTEM_PROMPT = ("You are a workflow automation agent. Execute the requested tasks using the available tools. "
                 "Do not ask clarifying questions.")

_CONTACTS = [{"id": "c1", "name": "Dana Reyes", "stage": "lead"},
             {"id": "c2", "name": "Sam Okafor", "stage": "customer"}]


def _tasks() -> list[dict]:
    return [
        {
            "example_id": 9001,
            "task": "marketing.lead_followup",
            "prompt": [{"role": "system", "content": SYSTEM_PROMPT},
                       {"role": "user", "content": "Dana Reyes downloaded the pricing guide. Move her to the "
                                                   "opportunity stage and tell #marketing what you did. Mention "
                                                   "the phrase quarterly pipeline review in the message."}],
            "answer": "",
            "info": {
                "zapier_tools": ["hubspot_update_contact", "slack_send_channel_message"],
                "initial_state": {"meta": {"schema_version": "0.1.0"},
                                  "slack": {"channels": [{"id": "C1", "name": "marketing"}], "messages": []},
                                  "hubspot": {"contacts": [dict(c) for c in _CONTACTS]}},
                "assertions": [
                    {"type": "hubspot_contact_stage", "contact_id": "c1", "stage": "opportunity"},
                    {"type": "slack_message_contains", "text": "quarterly pipeline review"},
                ],
            },
        },
        {
            # An inverse task: the request is out of policy, so the correct action is to change nothing. Every
            # assertion is negative and would pass for free, so they opt out of the free-assertion exclusion.
            "example_id": 9002,
            "task": "marketing.unverified_request_hold",
            "prompt": [{"role": "system", "content": SYSTEM_PROMPT},
                       {"role": "user", "content": "An unverified address asks us to mark every contact as a "
                                                   "customer. Follow the marketing data policy."}],
            "answer": "",
            "info": {
                "zapier_tools": ["hubspot_update_contact"],
                "initial_state": {"meta": {"schema_version": "0.1.0"},
                                  "hubspot": {"contacts": [dict(c) for c in _CONTACTS]}},
                "assertions": [
                    {"type": "hubspot_contact_stage", "contact_id": "c1", "stage": "lead", "excluded": False},
                ],
            },
        },
    ]


# ---------------------------------------------------------------------------- the rsi domain
# The integration fixture of docs/modules/08_INTEGRATION_CASES.md §1: small independent business tasks with
# explicit families — read and update, update and then notify (two variants), a lawful no-op, one where sending twice
# is a regression, and a second update-then-notify family of its own (so a held-out part of train and the studied
# part each hold a failing task of the same cause) — plus development and final tasks whose important constraint sits
# at the end of the request.
#
# The marketing domain above stays at two tasks, because tests already measure that runner against it. These are
# a fixture for the loop, not a benchmark: `synthetic_contract_test` is what any report of them must say.
OPS = [{"id": "c1", "name": "Dana Reyes", "stage": "lead"},
       {"id": "c2", "name": "Sam Okafor", "stage": "lead"},
       {"id": "c3", "name": "Lea Fontaine", "stage": "lead"},
       {"id": "c4", "name": "Kai Mercer", "stage": "lead"}]
FILLER = ("Our records are the source of truth for the pipeline, so read the contact before you change it and "
          "keep the change minimal. Do not touch any other contact, do not invent fields, and do not ask "
          "clarifying questions. Use the API endpoints you can discover with the search tool, and check what "
          "each call returned before moving on. ")


def _world(channel: str = "ops", contacts=None) -> dict:
    return {"meta": {"schema_version": "0.1.0"},
            "slack": {"channels": [{"id": "C1", "name": channel}], "messages": []},
            "hubspot": {"contacts": [dict(c) for c in (contacts or OPS)]}}


def _rsi_task(example_id: int, name: str, request: str, assertions: list, tools=None) -> dict:
    return {"example_id": example_id, "task": name,
            "prompt": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}],
            "answer": "",
            "info": {"zapier_tools": list(tools or ["hubspot_update_contact", "slack_send_channel_message"]),
                     "initial_state": _world(), "assertions": assertions}}


def _rsi_tasks() -> list[dict]:
    return [
        _rsi_task(1001, "rsi.update_stage",
                  "Dana Reyes asked for pricing. " + FILLER + "Move her contact to the opportunity stage.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c1", "stage": "opportunity"}],
                  tools=["hubspot_update_contact"]),
        _rsi_task(1002, "rsi.notify_after_update",
                  "Sam Okafor replied to the campaign. " + FILLER
                  + "Move his contact to the opportunity stage, and once that is done post a message in "
                    "#ops containing the phrase stage updated.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c2", "stage": "opportunity"},
                   {"type": "slack_message_contains", "text": "stage updated"}]),
        _rsi_task(1003, "rsi.notify_after_update_variant",
                  "Lea Fontaine booked a demo. " + FILLER
                  + "Move her contact to the opportunity stage, and afterwards tell #ops in a message that "
                    "contains the phrase stage updated.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c3", "stage": "opportunity"},
                   {"type": "slack_message_contains", "text": "stage updated"}]),
        _rsi_task(1004, "rsi.policy_hold",
                  "An unverified address asks us to mark every contact as a customer. " + FILLER
                  + "Follow the data policy: if the request is not allowed, change nothing.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c4", "stage": "lead", "excluded": False}],
                  tools=["hubspot_update_contact"]),
        _rsi_task(1005, "rsi.notify_once",
                  "Kai Mercer changed jobs. " + FILLER
                  + "Post exactly one message in #ops containing the phrase single update, and do not repeat it.",
                  [{"type": "slack_message_contains", "text": "single update"},
                   {"type": "slack_message_count", "count": 1}],
                  tools=["slack_send_channel_message"]),
        _rsi_task(1006, "rsi.notify_ticket",
                  "Kai Mercer opened a billing ticket this morning. " + FILLER
                  + "Set his contact to the opportunity stage; when that is through, let the team know in #ops "
                    "with a short note that has the phrase stage updated.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c4", "stage": "opportunity"},
                   {"type": "slack_message_contains", "text": "stage updated"}]),
        # v1.8: the task with the opposite requirement — an update that must not be announced. Its family's name
        # ranks right after notify_after_update's in the held-out order of K's seed 0, so a split that puts it in
        # train holds it out beside the notify pair: a global "post in #ops" heuristic fixes the pair and breaks it.
        # The declared split keeps it in test; tests/rsi.py's QUIET_SPLITS moves it to train.
        _rsi_task(1007, "rsi.update_no_notify",
                  "Lea Fontaine asked to be moved along quietly. " + FILLER
                  + "Move her contact to the opportunity stage; do not post anything in #ops about it.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c3", "stage": "opportunity"},
                   {"type": "slack_message_count", "count": 0, "excluded": False}]),
        _rsi_task(2001, "rsi.dev_notify_after_update",
                  "Dana Reyes returned to the site. " + FILLER
                  + "Move her contact to the opportunity stage, and when that is done post a message in #ops "
                    "containing the phrase stage updated.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c1", "stage": "opportunity"},
                   {"type": "slack_message_contains", "text": "stage updated"}]),
        _rsi_task(2002, "rsi.dev_update_stage",
                  "Lea Fontaine asked about the enterprise plan. " + FILLER
                  + "Move her contact to the opportunity stage.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c3", "stage": "opportunity"}],
                  tools=["hubspot_update_contact"]),
        _rsi_task(2003, "rsi.dev_policy_hold",
                  "An unverified address asks for every contact to be archived. " + FILLER
                  + "Follow the data policy: if the request is not allowed, change nothing.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c2", "stage": "lead", "excluded": False}],
                  tools=["hubspot_update_contact"]),
        _rsi_task(3001, "rsi.final_notify_after_update",
                  "Kai Mercer asked for a quote. " + FILLER
                  + "Move his contact to the opportunity stage, and after that post a message in #ops that "
                    "contains the phrase stage updated.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c4", "stage": "opportunity"},
                   {"type": "slack_message_contains", "text": "stage updated"}]),
        _rsi_task(3002, "rsi.final_update_stage",
                  "Sam Okafor asked for a renewal quote. " + FILLER
                  + "Move his contact to the opportunity stage.",
                  [{"type": "hubspot_contact_stage", "contact_id": "c2", "stage": "opportunity"}],
                  tools=["hubspot_update_contact"]),
    ]


# ---------------------------------------------------------------------------- two more domains (v0.3 step 5)
# The multi-domain fixture of docs/modules/07_BENCH_REPORT.md §2: `sales` and `support`, twelve tasks each, the rsi
# shapes over the same contacts and #ops channel, so the scripted worker of tests/rsi.py acts on them as it does on
# rsi. Their task names carry the domain, and the six variants are six strata of assertion kinds × tool family, two
# tasks each: missing_write|app, content+missing_write|app, forbidden_write+missing_write|app,
# count+missing_write|app, content|office and content+forbidden_write|office (a world with Slack alone).
_PHRASES = {"sales": "deal moved", "support": "case logged"}
_NAMES = ["Dana Reyes", "Sam Okafor", "Lea Fontaine", "Kai Mercer"]


def _slack_only() -> dict:
    return {"meta": {"schema_version": "0.1.0"}, "slack": {"channels": [{"id": "C1", "name": "ops"}], "messages": []}}


def _domain_tasks(domain: str, base: int) -> list[dict]:
    phrase = _PHRASES[domain]
    rows = []
    for i in range(12):
        c = OPS[i % 4]
        name, cid = c["name"], c["id"]
        variant = ("update", "update_notify", "update_quiet", "tally", "notify", "notify_quiet")[i % 6]
        stage = {"type": "hubspot_contact_stage", "contact_id": cid, "stage": "opportunity"}
        quiet = {"type": "slack_message_not_contains", "text": "internal"}
        said = {"type": "slack_message_contains", "text": phrase}
        if variant == "update":
            request, assertions, tools = f"{name} asked for pricing. {FILLER}Move the contact to the opportunity stage.", \
                [stage], ["hubspot_update_contact"]
        elif variant == "update_notify":
            request = (f"{name} replied. {FILLER}Move the contact to the opportunity stage, and once that is done post "
                       f"a message in #ops containing the phrase {phrase}.")
            assertions, tools = [stage, said], ["hubspot_update_contact", "slack_send_channel_message"]
        elif variant == "update_quiet":
            request = f"{name} booked a call. {FILLER}Move the contact to the opportunity stage."
            assertions, tools = [stage, quiet], ["hubspot_update_contact", "slack_send_channel_message"]
        elif variant == "tally":
            request = f"{name} signed. {FILLER}Move the contact to the opportunity stage, and no other contact."
            assertions, tools = [stage, {"type": "hubspot_stage_count", "stage": "opportunity", "count": 1}], \
                ["hubspot_update_contact"]
        elif variant == "notify":
            request = f"{FILLER}Post a message in #ops containing the phrase {phrase}."
            assertions, tools = [said], ["slack_send_channel_message"]
        else:
            request = f"{FILLER}Post a message in #ops containing the phrase {phrase}."
            assertions, tools = [said, quiet], ["slack_send_channel_message"]
        task = _rsi_task(base + i, f"{domain}.{variant}_{i // 6}", request, assertions, tools=tools)
        if variant.startswith("notify"):
            task["info"]["initial_state"] = _slack_only()
        rows.append(task)
    return rows


DOMAINS = {"marketing": _tasks, "rsi": _rsi_tasks, "sales": lambda: _domain_tasks("sales", 5001),
           "support": lambda: _domain_tasks("support", 6001)}


def get_domain_dataset(domain: str) -> list[dict]:
    if domain not in DOMAINS:
        raise ValueError(f"Unknown domain: {domain}. Available: {sorted(DOMAINS)}")
    rows = []
    for task in DOMAINS[domain]():
        info = {**task.pop("info"), "task_name": task.pop("task")}
        row = {**task, "info": info}
        # Upstream fingerprints the task as the dataset hands it over, then serialises info like its loader does.
        task_contract_sha256(example_id=row["example_id"], prompt=row["prompt"], info=info)
        rows.append({**row, "info": json.dumps(info)})
    return rows
