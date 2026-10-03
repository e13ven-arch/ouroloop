"""The rehearsal's marketing tasks (docs/REHEARSAL.md §2), in the row shape upstream's loader produces: info is a
JSON string that carries task_name, zapier_tools, initial_state and assertions.

Each task moves one contact to a stage. Most end with one closing step of six kinds — A a Slack post, B a tag,
C a follow-up task, D an owner, E a mailing list, F a call note (docs/REHEARSAL.md §10) — and the plain ones (P)
have none. Each kind has one train task and one
development task in each half of the frozen development order, so that what the research slot's arms and its
outer comparison measure both still hold a gap after the first worker segment. The names were chosen so that
seed 0 and the default ratios hash them into the splits the table in docs/REHEARSAL.md gives; the row order fixes
the train rotation and the two development halves.
"""
from __future__ import annotations

import json

from automationbench.task_contract import task_contract_sha256

SYSTEM_PROMPT = ("You are a workflow automation agent. Execute the requested tasks using the available tools. "
                 "Do not ask clarifying questions.")

# number, kind, (contact name, id, what happened, the stage to move them to), the closing step's values. The list is
# in row order: the train tasks E, F, C, A, B, D first, then the development tasks of the first half, then the second.
_TASKS = [
    (10, "E", ("Pablo Ortiz", "c41", "asked for product news", "subscriber"), {"list": "product-news"}),
    (11, "F", ("Quinn Harper", "c42", "called about an invoice", "customer"), {"note": "Called about the March invoice"}),
    (2, "C", ("Lena Park", "c13", "asked for a product demo", "opportunity"), {"title": "Book the product demo"}),
    (0, "A", ("Dana Reyes", "c11", "downloaded the pricing guide", "opportunity"),
     {"text": "pricing guide follow-through", "channel": "marketing"}),
    (1, "B", ("Sam Okafor", "c12", "signed up for the spring webinar", "lead"), {"tag": "webinar"}),
    (3, "P", ("Omar Haddad", "c14", "renewed the annual plan", "customer"), {}),
    (4, "D", ("Iris Tanaka", "c15", "replied to the outbound sequence", "lead"), {"owner": "Priya Shah"}),
    (5, "P", ("Mateo Silva", "c16", "unsubscribed from the newsletter", "other"), {"lookup": True}),
    (6, "P", ("Grace Liu", "c17", "completed onboarding", "customer"), {}),
    (7, "P", ("Noah Brandt", "c18", "attended the partner summit", "evangelist"), {}),
    (8, "P", ("Ava Moreau", "c19", "requested a quote", "opportunity"), {}),
    (13, "A", ("Ben Carter", "c21", "booked a discovery call", "opportunity"),
     {"text": "discovery call booked", "channel": "sales"}),
    (14, "B", ("Chloe Adams", "c22", "joined the beta waitlist", "lead"), {"tag": "beta"}),
    (18, "C", ("Diego Ramos", "c23", "started a free trial", "lead"), {"title": "Check in on the trial"}),
    (19, "D", ("Emma Wilson", "c24", "filled in the contact form", "lead"), {"owner": "Jonas Berg"}),
    (71, "E", ("Rosa Lind", "c27", "subscribed at the trade show", "subscriber"), {"list": "event-updates"}),
    (72, "F", ("Sven Olsen", "c28", "called to change the billing address", "customer"),
     {"note": "Asked to change the billing address"}),
    (27, "P", ("Felix Novak", "c25", "signed the contract", "customer"), {}),
    (31, "P", ("Hana Sato", "c26", "left a five-star review", "evangelist"), {}),
    (34, "A", ("Ivan Petrov", "c31", "visited the pricing page three times", "opportunity"),
     {"text": "pricing interest spotted", "channel": "growth"}),
    (54, "B", ("Julia Costa", "c32", "registered for the user conference", "lead"), {"tag": "conference"}),
    (56, "C", ("Karim Aziz", "c33", "asked about volume discounts", "opportunity"),
     {"title": "Send the volume pricing"}),
    (64, "D", ("Lucy Grant", "c34", "answered the market survey", "lead"), {"owner": "Tom Becker"}),
    (76, "E", ("Tara Quist", "c37", "joined the customer community", "subscriber"), {"list": "community-digest"}),
    (80, "F", ("Uma Brooks", "c38", "called to report a login problem", "customer"),
     {"note": "Reported a login problem on the phone"}),
    (65, "P", ("Marco Rossi", "c35", "upgraded to the team plan", "customer"), {}),
    (67, "P", ("Nina Kowal", "c36", "referred a colleague", "evangelist"), {}),
]


def _closing(kind: str, values: dict) -> tuple[str, list[dict]]:
    """The closing sentence of a request and the assertion that checks it."""
    cid = values["contact_id"]
    if kind == "A":
        return (f' Then post "{values["text"]}" in #{values["channel"]}.',
                [{"type": "slack_message_contains", "text": values["text"], "channel": values["channel"]}])
    if kind == "B":
        return (f" Then tag the contact {values['tag']}.",
                [{"type": "hubspot_contact_tag", "contact_id": cid, "tag": values["tag"]}])
    if kind == "C":
        return (f' Then log a follow-up task titled "{values["title"]}" for the contact.',
                [{"type": "hubspot_task_exists", "contact_id": cid, "title": values["title"]}])
    if kind == "D":
        return (f" Then make {values['owner']} the contact's owner.",
                [{"type": "hubspot_contact_owner", "contact_id": cid, "owner": values["owner"]}])
    if kind == "E":
        return (f" Then add the contact to the {values['list']} mailing list.",
                [{"type": "hubspot_list_member", "contact_id": cid, "list": values["list"]}])
    if kind == "F":
        return (f' Then add the call note "{values["note"]}" to the contact.',
                [{"type": "hubspot_note_exists", "contact_id": cid, "text": values["note"]}])
    return "", []


def _tasks() -> list[dict]:
    out = []
    for number, kind, (name, cid, event, stage), closing in _TASKS:
        start = "Look the contact up first. " if closing.get("lookup") else ""
        sentence, checks = _closing(kind, {**closing, "contact_id": cid})
        request = f"{start}{name} (contact {cid}) {event}. Move them to the {stage} stage.{sentence}"
        initial = {"meta": {"schema_version": "0.1.0"},
                   "slack": {"channels": [{"id": "C1", "name": "marketing"}, {"id": "C2", "name": "sales"},
                                          {"id": "C3", "name": "growth"}], "messages": []},
                   "hubspot": {"contacts": [{"id": cid, "name": name, "stage": "lead" if stage == "subscriber"
                                             else "subscriber", "tags": []}],
                               "tasks": [], "list_members": [], "notes": []}}
        out.append({
            "example_id": 7000 + number,
            "task": f"marketing.reh_{number:03d}",
            "prompt": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}],
            "answer": "",
            "info": {"zapier_tools": ["hubspot_update_contact", "slack_send_channel_message"],
                     "initial_state": initial,
                     "assertions": [{"type": "hubspot_contact_stage", "contact_id": cid, "stage": stage}, *checks]},
        })
    return out


DOMAINS = {"marketing": _tasks}


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
