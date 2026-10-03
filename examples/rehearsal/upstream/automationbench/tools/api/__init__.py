"""The api toolset: search, fetch and a local encoder, over the rehearsal's world (docs/REHEARSAL.md §2)."""
from __future__ import annotations

import base64
import json
from typing import Optional

from automationbench.schema.world import WorldState

ENDPOINTS = [
    {"id": "hubspot.patchContact", "method": "PATCH", "url": "https://api.hubapi.com/crm/v3/objects/contacts",
     "service": "hubspot", "description": "Update a HubSpot contact's lifecycle stage."},
    {"id": "slack.postMessage", "method": "POST", "url": "https://slack.com/api/chat.postMessage",
     "service": "slack", "description": "Post a message to a Slack channel."},
    {"id": "hubspot.tagContact", "method": "POST", "url": "https://api.hubapi.com/crm/v3/objects/contacts/tags",
     "service": "hubspot", "description": "Add a tag to a HubSpot contact."},
    {"id": "hubspot.createTask", "method": "POST", "url": "https://api.hubapi.com/crm/v3/objects/tasks",
     "service": "hubspot", "description": "Create a follow-up task for a HubSpot contact."},
    {"id": "hubspot.assignOwner", "method": "PATCH", "url": "https://api.hubapi.com/crm/v3/objects/contacts/owner",
     "service": "hubspot", "description": "Set the owner of a HubSpot contact."},
]


def api_search(query: str, top_k: int = 5) -> str:
    """Search available API endpoints by keyword.

    Args:
        query: Space-separated keywords.
        top_k: Maximum number of results to return (default 5).

    Returns:
        JSON with matching endpoints.
    """
    words = {w for w in query.lower().split()}
    ranked = sorted(ENDPOINTS, key=lambda e: -len(words & set(f"{e['id']} {e['description']}".lower().split())))
    return json.dumps(ranked[:top_k])


def _contact(world: WorldState, contact_id):
    return next((c for c in world.hubspot.rows("contacts") if c.get("id") == contact_id), None)


def api_fetch(world: WorldState, method: str, url: str, params: Optional[str] = None,
              body: Optional[str] = None) -> str:
    """Call an API endpoint by its full URL.

    Args:
        world: The current world state (injected automatically).
        method: HTTP method.
        url: Full API URL from api_search results.
        params: Query parameters as a JSON string.
        body: Request body as a JSON string.

    Returns:
        JSON string mimicking the real API response.
    """
    endpoint = next((e for e in ENDPOINTS if e["url"] == url and e["method"] == method), None)
    if endpoint is None:
        return json.dumps({"error": {"code": 404, "message": f"no endpoint {method} {url}"}})
    allowed = world.meta.allowed_services
    if allowed is not None and endpoint["service"] not in allowed:
        return json.dumps({"error": {"code": 401, "message": f"No {endpoint['service']} account is connected"}})
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return json.dumps({"error": {"code": 400, "message": "the body is not JSON"}})
    if endpoint["id"] == "slack.postMessage":
        world.slack.rows("messages").append({"channel": payload.get("channel"), "text": payload.get("text", "")})
        return json.dumps({"ok": True, "ts": str(len(world.slack.rows("messages")))})
    if endpoint["id"] == "hubspot.createTask":
        if _contact(world, payload.get("contact_id")) is None:
            return json.dumps({"error": {"code": 404, "message": "no such contact"}})
        world.hubspot.rows("tasks").append({"contact_id": payload.get("contact_id"), "title": payload.get("title")})
        return json.dumps({"ok": True, "task": len(world.hubspot.rows("tasks"))})
    contact = _contact(world, payload.get("id"))
    if contact is None:
        return json.dumps({"error": {"code": 404, "message": "no such contact"}})
    if endpoint["id"] == "hubspot.patchContact":
        contact["stage"] = payload.get("stage")
        return json.dumps({"ok": True, "id": contact["id"], "stage": contact["stage"]})
    if endpoint["id"] == "hubspot.tagContact":
        contact.setdefault("tags", []).append(payload.get("tag"))
        return json.dumps({"ok": True, "id": contact["id"], "tags": contact["tags"]})
    contact["owner"] = payload.get("owner")
    return json.dumps({"ok": True, "id": contact["id"], "owner": contact["owner"]})


def base64_encode(text: str) -> str:
    """Encode text to base64url.

    Args:
        text: Plaintext to encode.

    Returns:
        The base64url-encoded string.
    """
    return base64.urlsafe_b64encode(text.encode()).decode("ascii")


API_TOOLS = [api_search, api_fetch, base64_encode]
