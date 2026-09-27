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
# explicit families — read and update, update and then notify, a lawful no-op, and one where sending twice is a
# regression — plus development and final tasks whose important constraint sits at the end of the request.
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


DOMAINS = {"marketing": _tasks, "rsi": _rsi_tasks}


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
