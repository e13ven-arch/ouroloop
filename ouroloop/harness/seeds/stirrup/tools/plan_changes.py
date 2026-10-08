"""plan_changes: the agent's structured plan of changes, and what it must leave alone (MEMORY_AGENT_ITERATION §4.2).

LangChain's write_todos (libs/langchain_v1/langchain/agents/middleware/todo.py: a list of {content, status} with
status pending / in_progress / completed, and parallel calls refused) with one field more, `excluded`: every entity
or item the request says must not be changed, contacted or mentioned. It changes nothing in the world and makes no
broker call; middlewares/write_guard.py reads the latest accepted call from its after_tool event and shows the plan
beside the ledger before the agent finishes, and a hook R writes may scan outgoing messages for the excluded items.
"""
STATUSES = ("pending", "in_progress", "completed")
MAX_ITEMS = 100
MAX_CHARS = 1000                      # of one change's content or one excluded item
STATE = {"turn": None}


def _error(text: str) -> dict:
    return {"output": text, "is_error": True}


def run(ctx, args):
    turn = (getattr(ctx, "view", None) or {}).get("turn")
    if turn is not None and STATE["turn"] == turn:
        return _error("plan_changes was already called in this turn: call it once a turn, with the whole plan")
    changes, excluded = args.get("changes"), args.get("excluded", [])
    if not isinstance(changes, list) or len(changes) > MAX_ITEMS:
        return _error(f"changes is a list of at most {MAX_ITEMS} {{content, status}} objects")
    for i, change in enumerate(changes):
        if not isinstance(change, dict) or sorted(set(change) - {"content", "status"}):
            return _error(f"changes[{i}] is an object with content and status only")
        if not isinstance(change.get("content"), str) or not change["content"].strip() \
                or len(change["content"]) > MAX_CHARS:
            return _error(f"changes[{i}].content is a non-empty string of at most {MAX_CHARS} characters")
        if change.get("status") not in STATUSES:
            return _error(f"changes[{i}].status is one of {list(STATUSES)}")
    if not isinstance(excluded, list) or len(excluded) > MAX_ITEMS or \
            not all(isinstance(x, str) and x.strip() and len(x) <= MAX_CHARS for x in excluded):
        return _error(f"excluded is a list of at most {MAX_ITEMS} non-empty strings")
    STATE["turn"] = turn
    return f"Plan recorded: {len(changes)} changes, {len(excluded)} excluded"
