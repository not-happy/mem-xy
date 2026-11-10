"""Tests for the protocol quirks actually measured on `dida-gateway` (plan §2.2, decisions-log §C.1)."""

import dataclasses
import json

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.core.llm import LLMClient, LLMError, LLMParseError, Endpoint


class Items(BaseModel):
    items: list[dict]


def make_client(handler, *, thinking=True, token="tok-secret", max_retries=2) -> LLMClient:
    client = LLMClient(transport=httpx.MockTransport(handler))
    client._settings = dataclasses.replace(
        client._settings,
        llm_max_retries=max_retries,
        # fresh ThinkingConfig each time — the module-level Settings singleton is shared
        # across tests and mutating its dict would leak one test's switch into the next.
        thinking=dataclasses.replace(client._settings.thinking, per_task={"route": thinking}),
    )
    client._endpoints = {
        "dida-gateway": Endpoint(
            endpoint_key="dida-gateway",
            base_url="http://dragon-open-api.didainternal.com",
            model="qwen3.8-max-anthropic",
            api_protocol="anthropic",
            max_input_tokens=32000,
        )
    }
    client._tokens = {"dida-gateway": token}
    return client


def anthropic_ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


ROUTE_TOOL = {
    "name": "emit_items",
    "description": "输出抽取结果",
    "input_schema": {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "object"}}},
        "required": ["items"],
    },
}


def test_thinking_off_sends_explicit_disable_flag():
    """The gateway defaults to thinking ON; it must be explicitly turned off."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return anthropic_ok({"content": [{"type": "text", "text": "hi"}], "usage": {}})

    client = make_client(handler, thinking=False)
    client.complete("route", messages=[{"role": "user", "content": "x"}])
    assert seen["thinking"] == {"type": "disabled"}


def test_thinking_on_omits_the_flag():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return anthropic_ok({"content": [{"type": "text", "text": "hi"}], "usage": {}})

    client = make_client(handler, thinking=True)
    client.complete("route", messages=[{"role": "user", "content": "x"}])
    assert "thinking" not in seen


def test_tool_use_is_extracted_even_when_a_thinking_block_comes_first():
    """★ content[0] is not necessarily the tool_use block."""
    payload = {
        "content": [
            {"type": "thinking", "thinking": "let me consider..."},
            {"type": "tool_use", "name": "emit_items",
             "input": {"items": [{"kind": "observation", "content": "禁止使用 Lombok"}]}},
        ],
        "stop_reason": "tool_use",
        "model": "qwen3.8-max",
        "usage": {"input_tokens": 1200, "output_tokens": 930,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    }
    client = make_client(lambda request: anthropic_ok(payload))
    response = client.complete("route", messages=[{"role": "user", "content": "x"}],
                               tools=[ROUTE_TOOL])
    assert response.stop_reason == "tool_use"
    assert response.tool_input["items"][0]["content"] == "禁止使用 Lombok"
    # ★ the gateway strips the `-anthropic` suffix; do not hard-validate the model name
    assert response.model == "qwen3.8-max"
    # ★ take Anthropic's input_tokens, ignore the OpenAI-style extra field
    assert response.input_tokens == 1200
    assert response.output_tokens == 930


def test_structured_returns_validated_model():
    payload = {
        "content": [{"type": "tool_use", "name": "emit_items",
                     "input": {"items": [{"kind": "discard", "confidence": 0.99}]}}],
        "usage": {},
    }
    client = make_client(lambda request: anthropic_ok(payload))
    result = client.structured("route", system="s", messages=[{"role": "user", "content": "x"}],
                               tool=ROUTE_TOOL, schema=Items)
    assert result.items[0]["kind"] == "discard"


def test_structured_retries_with_the_error_then_raises_parse_error():
    """★ parse failure must never drop the material — the caller lands it in pending."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        # the retry must carry the previous validation error back to the model
        if calls["n"] > 1:
            assert "schema 校验" in body["messages"][-1]["content"]
        return anthropic_ok({
            "content": [{"type": "tool_use", "name": "emit_items",
                         "input": {"wrong": "shape"}}],
            "usage": {},
        })

    client = make_client(handler)
    with pytest.raises(LLMParseError) as excinfo:
        client.structured("route", system="s", messages=[{"role": "user", "content": "x"}],
                          tool=ROUTE_TOOL, schema=Items)
    assert calls["n"] == 3  # 1 attempt + 2 retries
    assert excinfo.value.raw is not None


def test_fenced_json_is_the_fallback_when_tool_use_is_absent():
    payload = {"content": [{"type": "text",
                            "text": '```json\n{"items": [{"kind": "atomic_fact"}]}\n```'}],
               "usage": {}}
    client = make_client(lambda request: anthropic_ok(payload))
    response = client.complete("route", messages=[{"role": "user", "content": "x"}])
    assert response.tool_input is None
    assert response.payload() == {"items": [{"kind": "atomic_fact"}]}


def test_http_error_message_is_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream blew up")

    client = make_client(handler, token="tok-super-secret")
    with pytest.raises(LLMError) as excinfo:
        client.complete("route", messages=[{"role": "user", "content": "x"}])
    assert "tok-super-secret" not in str(excinfo.value)


def test_transport_error_is_redacted():
    def handler(request: httpx.Request) -> httpx.Request:
        raise httpx.ConnectError("cannot reach host", request=request)

    client = make_client(handler, token="tok-super-secret")
    with pytest.raises(LLMError):
        client.complete("route", messages=[{"role": "user", "content": "x"}])


def test_http_200_with_empty_body_is_retryable_llm_error():
    """★ 网关偶发"200 空响应"（§14 item2 探测背景）：必须转 LLMError——
    可被 job 队列当可重试，且走 complete() 的失败分支记进 llm_usage；
    裸 JSONDecodeError 冒出去则两边都漏（监控里看不见 ≠ 没发生过）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    client = make_client(handler)
    with pytest.raises(LLMError, match="non-JSON body"):
        client.complete("route", messages=[{"role": "user", "content": "x"}])


def test_http_200_with_garbage_body_is_retryable_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>gateway hiccup</html>",
                              headers={"content-type": "text/html"})

    client = make_client(handler)
    with pytest.raises(LLMError, match="non-JSON body"):
        client.complete("route", messages=[{"role": "user", "content": "x"}])


def test_a_missing_token_fails_locally_instead_of_sending_a_bare_request():
    """★ 实测踩过的坑：token 没配时请求照发，dida 网关回的是
    `403 Incorrect SceneId provided.`——看着像网关权限问题，其实是本地没配。
    错误信息必须直接说清去哪儿配。"""
    reached = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return anthropic_ok({"content": [], "usage": {}})

    client = make_client(handler, token=None)
    with pytest.raises(LLMError, match="MEM_LLM_TOKEN__DIDA_GATEWAY"):
        client.complete("route", messages=[{"role": "user", "content": "x"}])
    assert reached == [], "no request may leave the process without a token"


def test_unknown_protocol_is_rejected():
    client = make_client(lambda request: anthropic_ok({"content": [], "usage": {}}))
    client._endpoints["dida-gateway"].api_protocol = "carrier-pigeon"
    with pytest.raises(LLMError, match="unsupported api_protocol"):
        client.complete("route", messages=[{"role": "user", "content": "x"}])


def test_openai_adapter_maps_tools_and_usage():
    payload = {
        "model": "qwen3.8-max",
        "choices": [{"finish_reason": "tool_calls", "message": {
            "content": None,
            "tool_calls": [{"function": {"name": "emit_items",
                                          "arguments": '{"items": [{"kind": "observation"}]}'}}],
        }}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 210},
    }
    client = make_client(lambda request: httpx.Response(200, json=payload))
    client._endpoints["dida-gateway"].api_protocol = "openai"
    response = client.complete("route", system="sys",
                               messages=[{"role": "user", "content": "x"}], tools=[ROUTE_TOOL])
    assert response.tool_input == {"items": [{"kind": "observation"}]}
    assert response.input_tokens == 900
    assert response.output_tokens == 210
