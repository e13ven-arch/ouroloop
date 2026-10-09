"""The scripted bridge the real-sidecar tests use (tests/memory/fake_openai_server.py): OpenAI's chat shape, streamed
and not, behind a token, routed by the op in the model name, with scripted failures and a log of every call. No venv:
the default suite."""
from __future__ import annotations

import json

import httpx

from fake_openai_server import MARKERS, Control, FakeOpenAI


def chat(f, op, *, stream, token=None, content=None):
    body = {"model": f"research/reme:{op}", "stream": stream, "tools": None,
            "messages": [{"role": "user", "content": content or f"You are {MARKERS[op]}.\n# Original Query\n"
                                                                 f"Email every customer about the invoice.\n\n#"}]}
    return httpx.post(f.base_url + "/chat/completions", json=body, trust_env=False,
                      headers={"Authorization": f"Bearer {token or f.token}"})


def test_non_stream_and_stream_replies_carry_remes_fenced_json():
    with FakeOpenAI() as f:
        f.set_step("s1")
        r = chat(f, "success", stream=False)
        text, plain_id = r.json()["choices"][0]["message"]["content"], r.json()["id"]
        assert r.status_code == 200 and text.startswith("```json") and json.loads(text[7:-3])[0]["when_to_use"]
        r = chat(f, "validation", stream=True)
        chunks = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
        joined = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
        assert r.text.rstrip().endswith("data: [DONE]") and json.loads(joined[7:-3])["is_valid"] is True
        assert [(c["op"], c["status"], c["step"]) for c in f.calls("s1")] == [("success", 200, "s1"),
                                                                             ("validation", 200, "s1")]
        # the bridge's Scope.calls shape exactly, and the call id is the completion's id (streamed or not)
        calls = f.calls("s1")
        assert all(set(c) == {"call_id", "step", "op", "status", "ms"} for c in calls)
        assert calls[0]["call_id"] == plain_id and calls[1]["call_id"] == chunks[0]["id"]
        assert all(c["call_id"].startswith("chatcmpl-") for c in calls)
        assert f.requests("s1")[1]["response"] == joined


def test_scripted_replies_and_delays():
    import time
    with FakeOpenAI() as f:
        f.reply_with("validation", "```json\n{\"is_valid\": true, \"score\": \"high\"}\n```")
        f.slow("success", 0.5)
        r = chat(f, "validation", stream=False)
        assert '"score": "high"' in r.json()["choices"][0]["message"]["content"]
        t = time.monotonic()
        assert chat(f, "success", stream=False).status_code == 200 and time.monotonic() - t >= 0.5
        assert f.calls()[1]["ms"] >= 500


def test_tokens_routes_failures_and_prose_are_scripted():
    with FakeOpenAI() as f:
        assert chat(f, "success", stream=False, token="wrong").status_code == 401
        assert chat(f, "success", stream=False, content="a prompt for another op").status_code == 404
        f.fail("failure", 502, times=1)
        assert chat(f, "failure", stream=True).status_code == 502
        assert chat(f, "failure", stream=True).status_code == 200
        f.no_json("validation")
        r = chat(f, "validation", stream=False)
        assert "```json" not in r.json()["choices"][0]["message"]["content"]
        ctl = Control(f.base_url, f.token)
        ctl.set_step("other")
        assert f.step == "other" and [c["status"] for c in ctl.calls()] == [401, 404, 502, 200, 200]
