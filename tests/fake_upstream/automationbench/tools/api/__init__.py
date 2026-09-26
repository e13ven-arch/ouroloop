"""The api toolset: search, fetch and a local encoder, over the world."""
from __future__ import annotations

import base64
import json
import os
from typing import Optional

from automationbench.schema.world import WorldState

ENDPOINTS = [
    {"id": "slack.postMessage", "method": "POST", "url": "https://slack.com/api/chat.postMessage",
     "service": "slack", "description": "Post a message to a Slack channel."},
    {"id": "hubspot.patchContact", "method": "PATCH", "url": "https://api.hubapi.com/crm/v3/objects/contacts",
     "service": "hubspot", "description": "Update a HubSpot contact's lifecycle stage."},
    {"id": "openai.completions", "method": "POST", "url": "https://api.openai.com/v1/chat/completions",
     "service": "chatgpt", "description": "Ask a model for text."},
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
    payload = json.loads(body) if body else {}
    if endpoint["id"] == "slack.postMessage":
        world.slack.rows("messages").append({"channel": payload.get("channel"), "text": payload.get("text", "")})
        return json.dumps({"ok": True, "ts": str(len(world.slack.rows("messages")))})
    if endpoint["id"] == "hubspot.patchContact":
        for contact in world.hubspot.rows("contacts"):
            if contact.get("id") == payload.get("id"):
                contact["stage"] = payload.get("stage")
                return json.dumps({"ok": True, "id": contact["id"], "stage": contact["stage"]})
        return json.dumps({"error": {"code": 404, "message": "no such contact"}})
    # The live path: upstream falls back to a canned answer when no key is in the environment. The episode hides
    # the key while a tool runs, so a test can see which path was taken.
    return json.dumps({"ok": True, "live": bool(os.environ.get("OPENAI_API_KEY"))})


def base64_encode(text: str) -> str:
    """Encode text to base64url.

    Args:
        text: Plaintext to encode.

    Returns:
        The base64url-encoded string.
    """
    return base64.urlsafe_b64encode(text.encode()).decode("ascii")


API_TOOLS = [api_search, api_fetch, base64_encode]
