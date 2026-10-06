MARK = "FINISH-AUDIT: done"

AUDIT = (
    "Before finishing, run this audit. Write it out, act on every gap with tools (search, write, send, corrective or additional message), "
    "then finish with a final message that contains the line '" + MARK + "'.\n"
    "A. User-flagged items: list every item the request names with a reason to act (deadline, regulatory, priority, 'make sure', 'we need') "
    "or marks as already covered. A positively flagged item needs a matching write or send that cites the user's reason, even if a found threshold would exclude it, "
    "unless a source explicitly forbids that exact item. Never hand the user's own request back as a manual follow-up.\n"
    "B. Rules: list every rule set, routing directive and reference code in every source you read, not only the newest post. "
    "Each must map to an action (e.g. every team named in any routing rule received its message with its code and count).\n"
    "C. Criteria: for each candidate you excluded, re-test it against the full definition of the criterion, including text in brackets or after 'including', 'e.g.', 'such as'. "
    "If a definition clause matches and no explicit exclusion applies, it qualifies, so act on it now.\n"
    "D. Referenced but unlocated: anything a source points to (exceptions, notes, guidelines, a date, a price) that you did not find. Search for it now: "
    "sheet cell notes via GET spreadsheets/{id} with includeGridData=true, other tabs, the event or calendar systems, the remaining stores. Do not assume it is satisfied.\n"
    "E. Outbound content: for each message or write already sent, check that it contains every included row/entity (recount from the source), every identifier or reference code from those rows, "
    "the request's own wording for the deliverable, totals in the source's unit notation (e.g. $4.5B) as well as any expanded form, "
    "counts as '<N> <action word>' (e.g. '6 escalated'), and each draft's specific subject. If something is missing, send a corrected version or update the record.\n"
    "If every item is already satisfied, say so briefly per letter and finish."
)


def _view(ctx):
    v = getattr(ctx, "view", None)
    return v if isinstance(v, dict) else {}


def finish_audit(ctx, event):
    msg = str((event or {}).get("final_message") or "")
    if MARK.lower() in msg.lower():
        return {"action": "continue"}
    view = _view(ctx)
    events = view.get("events") or []
    for e in events:
        try:
            if MARK.lower() in str(e.get("result_excerpt") or "").lower():
                return {"action": "continue"}
        except Exception:
            pass
    return {"action": "feedback", "message": AUDIT}
