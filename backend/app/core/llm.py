"""LLM client: endpoint-per-task, protocol adapters, tool-use structured output.

Design points enforced here (plan §2.2, §9.1):
  * `endpoint_for(task)` is the only place a task picks a model.
  * `thinking` is a two-dimensional (endpoint, task) switch, never global.
  * tool use is the primary structured-output path; fenced JSON is the fallback.
  * exceptions are redacted before they can reach `jobs.last_error` or the UI.
  * the response `model` echo is NOT validated (the gateway strips the
    `-anthropic` suffix); `content` blocks are filtered by `type`, never by index.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from . import db
from .config import SECRETS_FILE, get_settings, llm_token
from .redact import redact, register_secrets

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

_FENCED = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


class LLMError(Exception):
    """Transport/HTTP failure — retryable by the job queue."""


class LLMParseError(LLMError):
    """Model answered but the payload did not validate. Caller must land it in
    `pending_items(stage='route', reason='llm_parse_failed')` — never drop it (§2.2)."""

    def __init__(self, message: str, raw: Any = None):
        super().__init__(message)
        self.raw = raw


@dataclass
class Endpoint:
    endpoint_key: str
    base_url: str
    model: str
    api_protocol: str = "anthropic"
    max_input_tokens: int = 32000
    note: str | None = None


@dataclass
class LLMResponse:
    text: str
    tool_input: dict | None
    model: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    latency_seconds: float
    raw: dict = field(default_factory=dict, repr=False)

    def payload(self) -> Any:
        """Structured result: tool input if present, else the first fenced JSON block,
        else the whole text parsed as JSON."""
        if self.tool_input is not None:
            return self.tool_input
        candidate = self.text.strip()
        match = _FENCED.search(candidate)
        if match:
            candidate = match.group(1).strip()
        return json.loads(candidate)


class LLMClient:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._settings = get_settings()
        self._endpoints: dict[str, Endpoint] = {}
        self._tokens: dict[str, str | None] = {}
        self._client = httpx.Client(transport=transport, timeout=HTTP_TIMEOUT)

    # ── endpoint resolution ─────────────────────────────────────────────

    def refresh_endpoints(self) -> None:
        rows = db.query(
            "SELECT endpoint_key, base_url, model, api_protocol, max_input_tokens, note "
            "FROM llm_endpoints WHERE enabled"
        )
        self._endpoints = {r["endpoint_key"]: Endpoint(**r) for r in rows}
        self._tokens = {
            key: llm_token(key) for key in self._endpoints
        }
        register_secrets(self._tokens.values())

    def endpoint_for(self, task: str) -> Endpoint:
        key = self._settings.endpoint_for(task)
        if key not in self._endpoints:
            self.refresh_endpoints()
        try:
            endpoint = self._endpoints[key]
        except KeyError:
            raise LLMError(f"unknown endpoint {key!r} for task {task!r}") from None
        return endpoint

    def thinking_enabled(self, task: str) -> bool:
        return self._settings.thinking.enabled(task)

    # ── public API ──────────────────────────────────────────────────────

    def complete(
        self,
        task: str,
        *,
        system: str | None = None,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        endpoint = self.endpoint_for(task)
        thinking = self.thinking_enabled(task)
        if endpoint.api_protocol not in _PROTOCOLS:
            raise LLMError(f"unsupported api_protocol {endpoint.api_protocol!r}")
        handler = getattr(self, f"call_{endpoint.api_protocol}")
        try:
            response = handler(endpoint, task, system, messages, tools, tool_choice,
                               max_tokens, thinking)
        except LLMError as exc:
            _record_usage(task, endpoint, thinking, ok=False, error=str(exc))
            raise
        _record_usage(task, endpoint, thinking, response=response)
        return response

    def structured(
        self,
        task: str,
        *,
        system: str | None,
        messages: list[dict],
        tool: dict,
        schema: type[BaseModel],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> BaseModel:
        """tool-use-first structured call with schema validation.

        On validation failure: retry up to `llm.max_retries` times *with the error
        message appended*, then raise LLMParseError carrying the raw payload so the
        caller can persist it (§2.2). Never silently drops the material.
        """
        attempts = self._settings.llm_max_retries + 1
        work = list(messages)
        last_error = ""
        last_response: LLMResponse | None = None
        for attempt in range(attempts):
            if last_error:
                work = list(messages) + [
                    {
                        "role": "user",
                        "content": (
                            f"上一次输出未通过 schema 校验：{last_error}\n"
                            "请严格按 tool 的 input_schema 重新输出，不要解释。"
                        ),
                    }
                ]
            response = self.complete(
                task,
                system=system,
                messages=work,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                max_tokens=max_tokens,
            )
            last_response = response
            try:
                return schema.model_validate(response.payload())
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = redact(str(exc)) or exc.__class__.__name__
                log.warning("%s attempt %d failed validation: %s", task, attempt + 1, last_error)
        raise LLMParseError(
            f"{task}: schema validation failed after {attempts} attempts: {last_error}",
            raw=last_response.raw if last_response else None,
        )

    # ── protocol adapters ───────────────────────────────────────────────

    def _headers(self, endpoint: Endpoint) -> dict:
        token = self._tokens.get(endpoint.endpoint_key)
        if not token:
            # ★ 别把没 token 的请求发出去。实测 dida 网关在这种情况下回的是
            #   `403 {"message":"Incorrect SceneId provided."}`——看着像网关坏了
            #   或权限不够，其实是本地没配，白折腾一轮才想到去看 token。
            raise LLMError(
                f"endpoint {endpoint.endpoint_key!r} 没有配 token：设环境变量 "
                f"MEM_LLM_TOKEN__{endpoint.endpoint_key.upper().replace('-', '_')}，"
                f"或写进 {SECRETS_FILE} 的 [llm_tokens]（§2.1：token 不进代码库/不进 PG）")
        headers = {"content-type": "application/json", "accept": "application/json"}
        headers["authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token
        return headers

    def _post(self, url: str, endpoint: Endpoint, body: dict) -> dict:
        try:
            response = self._client.post(url, headers=self._headers(endpoint), json=body)
        except httpx.HTTPError as exc:
            # httpx exception strings can carry request headers → redact (§2.1)
            raise LLMError(redact(f"{type(exc).__name__}: {exc}")) from None
        if response.status_code >= 400:
            raise LLMError(
                redact(f"HTTP {response.status_code} from {endpoint.endpoint_key}: "
                       f"{response.text[:2000]}")
            )
        try:
            return response.json()
        except ValueError as exc:
            # ★ 200 但 body 空/非 JSON：dida 网关偶发空响应（探测实证的形态之一，
            #   2026-11-10 §14 item2）。必须转成 LLMError——JSONDecodeError 冒出去
            #   既不被 job 队列当可重试，也躲过 _record_usage（监控盲点，
            #   llm_usage 里"从没见过"这个形态不等于它没发生过）。
            raise LLMError(
                f"HTTP 200 from {endpoint.endpoint_key} but non-JSON body "
                f"({len(response.content)} bytes) — 网关空响应，可重试") from exc

    def call_anthropic(self, endpoint, task, system, messages, tools, tool_choice,
                       max_tokens, thinking) -> LLMResponse:
        body: dict[str, Any] = {"model": endpoint.model, "max_tokens": max_tokens}
        if system:
            body["system"] = system
        body["messages"] = messages
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or {"type": "auto"}
        if not thinking:
            # the gateway defaults to thinking ON; it must be explicitly disabled
            body["thinking"] = {"type": "disabled"}

        started = time.perf_counter()
        raw = self._post(f"{endpoint.base_url.rstrip('/')}/v1/messages", endpoint, body)
        elapsed = time.perf_counter() - started

        blocks = raw.get("content") or []
        text_parts, tool_input = [], None
        for block in blocks:  # ★ filter by type — a thinking block may precede tool_use
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "tool_use" and tool_input is None:
                tool_input = block.get("input")
        usage = raw.get("usage") or {}
        return LLMResponse(
            text="\n".join(p for p in text_parts if p),
            tool_input=tool_input,
            model=raw.get("model", endpoint.model),  # ★ not validated: suffix is stripped
            stop_reason=raw.get("stop_reason", ""),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_seconds=round(elapsed, 3),
            raw=raw,
        )

    def call_openai(self, endpoint, task, system, messages, tools, tool_choice,
                    max_tokens, thinking) -> LLMResponse:
        converted = []
        if system:
            converted.append({"role": "system", "content": system})
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            converted.append({"role": msg.get("role", "user"), "content": content})

        body: dict[str, Any] = {"model": endpoint.model, "max_tokens": max_tokens,
                                "messages": converted}
        if tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t.get("description", ""),
                              "parameters": t.get("input_schema", {})}}
                for t in tools
            ]
            if tool_choice and tool_choice.get("name"):
                body["tool_choice"] = {"type": "function",
                                       "function": {"name": tool_choice["name"]}}

        started = time.perf_counter()
        raw = self._post(f"{endpoint.base_url.rstrip('/')}/v1/chat/completions", endpoint, body)
        elapsed = time.perf_counter() - started

        choice = (raw.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_input = None
        calls = message.get("tool_calls") or []
        if calls:
            try:
                tool_input = json.loads(calls[0].get("function", {}).get("arguments") or "null")
            except json.JSONDecodeError:
                tool_input = None
        usage = raw.get("usage") or {}
        return LLMResponse(
            text=message.get("content") or "",
            tool_input=tool_input,
            model=raw.get("model", endpoint.model),
            stop_reason=choice.get("finish_reason", ""),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            latency_seconds=round(elapsed, 3),
            raw=raw,
        )

    def call_ollama(self, endpoint, task, system, messages, tools, tool_choice,
                    max_tokens, thinking) -> LLMResponse:
        body: dict[str, Any] = {
            "model": endpoint.model,
            "messages": ([{"role": "system", "content": system}] if system else []) + messages,
            "stream": False,
            "think": bool(thinking),
            "options": {"num_predict": max_tokens},
        }
        started = time.perf_counter()
        raw = self._post(f"{endpoint.base_url.rstrip('/')}/api/chat", endpoint, body)
        elapsed = time.perf_counter() - started
        message = raw.get("message") or {}
        return LLMResponse(
            text=message.get("content") or "",
            tool_input=None,
            model=raw.get("model", endpoint.model),
            stop_reason=raw.get("done_reason", ""),
            input_tokens=int(raw.get("prompt_eval_count") or 0),
            output_tokens=int(raw.get("eval_count") or 0),
            latency_seconds=round(elapsed, 3),
            raw=raw,
        )

    def close(self) -> None:
        self._client.close()


_PROTOCOLS = ("anthropic", "openai", "ollama")


def _record_usage(task: str, endpoint: Endpoint, thinking: bool, *,
                  response: "LLMResponse | None" = None,
                  ok: bool = True, error: str = "") -> None:
    """每次调用往 `llm_usage` 追加一行（migration 0003），token 监控的唯一数据源。

    ★ 尽力而为，绝不影响主路径：池没开（单测/离线脚本）或表还没迁移就静默跳过；
      写失败只打日志。error 入库前过 redact（§2.1）。
    ★ pytest 里一律不记：测试连的是真实库（conftest 注入真 DSN），不挡会把
      mock 调用的假 token/假错误写进监控表，污染预算统计（2026-10-27 踩过）。
    """
    try:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        if db._pool is None:  # noqa: SLF001 — 探测池是否已初始化，没开就不记
            return
        db.execute(
            "INSERT INTO llm_usage (task, endpoint_key, model, thinking,"
            "                        input_tokens, output_tokens, latency_ms, ok, error, job_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (task, endpoint.endpoint_key,
             response.model if response else endpoint.model,
             thinking,
             response.input_tokens if response else 0,
             response.output_tokens if response else 0,
             int(response.latency_seconds * 1000) if response else 0,
             ok, redact(error)[:2000] or None, _job_ctx.get()),
        )
    except Exception:  # noqa: BLE001 — 监控不能反噬主路径
        log.debug("llm_usage record skipped", exc_info=True)


_client: LLMClient | None = None


# 当前 job 归属（migration 0004）：worker 领任务时 set，跑完 reset。
# 本线程内所有 llm_usage 行都带上 job_id，任务队列页才能按 job 下钻调用时间线。
_job_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "llm_job_id", default=None)


def set_job(job_id: int | None):
    return _job_ctx.set(job_id)


def clear_job(token) -> None:
    _job_ctx.reset(token)


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
