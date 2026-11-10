#!/usr/bin/env python3
"""mem-xy 的 MCP server（§7.2）。★ **它是 HTTP 的薄适配器，不含业务逻辑**（decisions-log #78）：
归属推导、scope 校验、重算 embedding、写 `memory_events`、纠错不写否决池——这些全在
`backend/app/library.py` 里。这里只做三件事：拼请求、把 cwd/session 从 MCP 上下文取出来、
把错误翻译成模型看得懂的一句话。

和 `integrations/hooks/mem-hook.py` 的**关键差别**：

    hook   失败必须静默（退出码 0，不打印）——它挂在每轮对话上，报错会打断用户。
    MCP    失败必须**说话**——模型主动调工具却拿到空结果，它会以为"没有相关记忆"，
           然后基于这个错误的空继续推理。所以这里一律 `ToolError`，把原因讲清楚。

    hook   超时 400ms（§1.2 的注入预算）。
    MCP    超时 15s：工具调用不在那个预算里，`memory_note` 还要等一次 embedding（~240ms）
           加一次 INSERT；慢一点没代价，但**不能无限等**，否则模型会卡在那儿。

接入（项目 `.mcp.json` 或 `~/.claude.json`）：

    {"mcpServers": {"mem-xy": {
       "command": "python",
       "args": ["<绝对路径>/integrations/mcp-server/mem_mcp.py"]}}}

环境变量（都可选）：`MEMXY_URL`、`MEMXY_TIMEOUT_MS`、`MEMXY_CWD`。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import anyio
import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

# ★ 写死 127.0.0.1 不写 localhost：localhost 会先解析出 ::1，没人监听时要白等一轮超时
#   才回落到 IPv4（hook 那边实测 402ms → 801ms）。
DEFAULT_URL = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT_MS = 15_000
ROOTS_TIMEOUT_SECONDS = 2.0

mcp = FastMCP(
    "mem-xy",
    instructions=(
        "长期记忆库：跨会话记住用户偏好、项目规矩、踩过的坑。"
        "该主动用的三个时机：① 用户说「记住 / 以后都 / 别再」→ memory_note；"
        "② 注入进来的记忆和当前代码或用户说法对不上 → memory_correct"
        "（知道对的写法就给 correction，整条都错就不给）；"
        "③ 要判断「这事以前是不是定过规矩」→ memory_recall。"
        "注意：注入进来的记忆可能过期，与当前代码冲突时以代码为准。"
    ),
)

_ROOTS: str | None = None          # 一个 stdio server 只服务一个客户端，探一次就够


# ─────────────────────────── HTTP ───────────────────────────

def _base_url() -> str:
    return os.environ.get("MEMXY_URL", DEFAULT_URL).rstrip("/")


def _timeout() -> float:
    try:
        return int(os.environ.get("MEMXY_TIMEOUT_MS", DEFAULT_TIMEOUT_MS)) / 1000
    except ValueError:
        return DEFAULT_TIMEOUT_MS / 1000


async def _call(method: str, path: str, *, body: dict | None = None,
                params: dict | None = None) -> dict | list:
    """发一个请求。非 2xx 一律翻成 `ToolError`。

    ★ **redact 是服务端的活，这里不复刻一遍**（复刻就是两份规则，#78）：
      HTTP 错误只转述服务端已经过 redact 的 `error.message`，**绝不**回吐原始响应体；
      传输层异常没有服务端可依靠，就把 URL 整个换成占位符——`MEMXY_URL` 里万一写了
      `http://user:pass@host` 也不会跟着进模型上下文。
    """
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.request(method, f"{_base_url()}{path}",
                                            json=body, params=_clean(params))
    except Exception as exc:  # noqa: BLE001 — 拒连/超时/DNS 全都归成一句人话
        raise ToolError(f"记忆服务连不上（{_safe(exc)}）。先确认 {_public_url()} 上"
                        "有没有在跑 `python -m app.commands.serve`。") from None

    if response.status_code < 400:
        return response.json()

    try:
        error = response.json().get("error", {})
    except Exception:  # noqa: BLE001 — 反代可能吐 HTML；那种情况下没有可信的 message 可用
        error = {}
    code = error.get("code", "")
    if response.status_code == 503 and code == "model_loading":
        # ★ 这句要说清"稍后重试"，否则模型会当成"记忆系统坏了"并劝用户去修。
        raise ToolError("记忆服务正在预热向量模型（冷启动实测 11–33s），过十几秒重试即可。")
    message = error.get("message") or f"HTTP {response.status_code}"
    raise ToolError(f"记忆服务拒绝了这次调用{f'（{code}）' if code else ''}：{message}")


def _safe(exc: Exception) -> str:
    text = str(exc).replace(_base_url(), "<mem-xy>").strip()
    return f"{type(exc).__name__}: {text[:160]}" if text else type(exc).__name__


def _public_url() -> str:
    """`MEMXY_URL` 允许写成 `http://user:pass@host`，而这句话会进模型上下文。"""
    url = _base_url()
    parsed = urlparse(url)
    if not parsed.username and not parsed.password:
        return url
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname or ''}{port}"


def _clean(params: dict | None) -> dict | None:
    return None if params is None else {k: v for k, v in params.items() if v is not None}


# ─────────────────────────── cwd / session ───────────────────────────

async def _cwd(ctx: Context, explicit: str | None) -> str | None:
    """当前工作目录 → 服务端拿去判 org/project 归属（§5.1）。

    ★ 顺序是 显式入参 > MCP roots > 环境变量 > 进程 cwd。roots 排在环境变量前面，
      因为它反映的是**这个会话真正打开的目录**，而 `CLAUDE_PROJECT_DIR` 指的是配置文件
      所在的项目——用户在别的仓库里开一个会话时，后者会把记忆归错桶（§6.1 要防的串台）。
    """
    if explicit:
        return explicit
    return await _first_root(ctx) or os.environ.get("MEMXY_CWD") \
        or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


async def _first_root(ctx: Context) -> str | None:
    global _ROOTS
    if _ROOTS is not None:
        return _ROOTS or None
    try:
        # ★ list_roots 是往客户端发一个请求，SDK 默认等 60s。客户端不支持或用户没批，
        #   工具调用就被挂住了——这里限 2s，超时就退回环境变量那条路。
        with anyio.fail_after(ROOTS_TIMEOUT_SECONDS):
            result = await ctx.session.list_roots()
    except Exception:  # noqa: BLE001 — 不支持 roots / 超时 / 被拒，都不值得让工具失败
        _ROOTS = ""
        return None
    for root in result.roots:
        parsed = urlparse(str(root.uri))
        if parsed.scheme == "file":
            # ★ 必须走 `url2pathname`，不能 `Path(parsed.path)`：Windows 上
            #   `file:///C:/x` 的 path 是 `/C:/x`，直接塞给 Path 会得到 `C:x`
            #   （盘符相对路径），归属判定跟着全错。UNC（`//server/share`）也是它处理。
            _ROOTS = str(Path(url2pathname(parsed.path)).resolve())
            return _ROOTS
    _ROOTS = ""
    return None


def _session_id(ctx: Context) -> str:
    """★ MCP 没有"一轮对话"的概念，一个 server 进程 ≈ 一个客户端窗口。
    拿它当 `recall_log.session_id`，配合 `source='mcp'`（hook 那边是 `source='hook'`），
    审计时分得清哪些记忆是模型主动查的、哪些是自动注入的。"""
    return f"mcp-{getattr(ctx.session, 'session_id', None) or os.getpid()}"


# ─────────────────────────── 五个 tool ───────────────────────────

@mcp.tool()
async def memory_recall(ctx: Context, query: str, k: int = 5, cwd: str | None = None) -> dict:
    """按语义 + 关键词召回相关记忆（跨会话）。拿不准「这事以前定过规矩吗」时用这个。

    ★ 结果**可能过期**：与当前代码冲突时以代码为准，并考虑用 `memory_correct` 纠掉。
    """
    payload = await _call("POST", "/v1/recall", body={
        "query": query, "k": k, "cwd": await _cwd(ctx, cwd),
        "session_id": _session_id(ctx), "source": "mcp"})
    return {"items": [{"memory_id": i["memory_id"], "text": i["text"], "score": i["score"]}
                      for i in payload.get("items", [])],
            "latency_ms": payload.get("latency_ms")}


@mcp.tool()
async def memory_note(ctx: Context, content: str, scope: str = "project", role: str = "fact",
                      keywords: list[str] | None = None, why: str | None = None,
                      cwd: str | None = None) -> dict:
    """记一条长期记忆。用户说「记住 / 以后都这样 / 别再犯」时调用。

    `scope`：`project` 只在这个仓库生效（默认）/ `org` 整个组织（如 dida 下所有 repo）
    / `user` 跟着人走、跨所有项目。★ 拿不准就写 `project`——放大范围的代价是把一条
    项目规矩泄漏到别的项目去。
    `role`：`fact` 事实 / `preference` 偏好 / `profile` 用户画像 / `pointer` 指路（东西在哪）。
    """
    row = await _call("POST", "/v1/memories", params={"actor": "model"}, body={
        "content": content, "scope": scope, "role": role, "keywords": keywords,
        "why": why, "cwd": await _cwd(ctx, cwd)})
    return {"memory_id": row["id"], "scope": row["scope"], "org_key": row["org_key"],
            "project_key": row["project_key"], "keywords": row["keywords"]}


@mcp.tool()
async def memory_correct(ctx: Context, memory_id: int, correction: str | None = None,
                         note: str | None = None) -> dict:
    """纠正一条错的记忆。给了 `correction` 就改写它，不给就整条作废。

    ★ 发现注入进来的记忆和当前代码 / 用户说法对不上时用这个，别只是嘴上说一句"这条过期了"
    ——不纠掉，下一轮它还会被召回。`note` 写清为什么错，它是评测和 prompt 反哺的原料。
    """
    return await _call("POST", f"/v1/memories/{memory_id}/correct",
                       body={"correction": correction, "note": note, "actor": "model"})


@mcp.tool()
async def memory_search(ctx: Context, q: str | None = None, role: str | None = None,
                        scope: str | None = None, status: str = "active", limit: int = 20,
                        all_scopes: bool = False, cwd: str | None = None) -> dict:
    """浏览记忆库（关键词粗筛，**不是**语义检索——那个用 `memory_recall`）。

    默认只看当前 org/project 能看见的；`all_scopes=true` 看全库。
    """
    payload = await _call("GET", "/v1/memories", params={
        "q": q, "role": role, "scope": scope, "status": status, "limit": limit,
        "all_scopes": str(all_scopes).lower(), "cwd": await _cwd(ctx, cwd)})
    return {"total": payload["total"], "items": [
        {k: m[k] for k in ("id", "scope", "role", "subject", "content", "why",
                           "org_key", "project_key", "updated_at") if k in m}
        for m in payload["items"]]}


@mcp.tool()
async def memory_pending(ctx: Context, limit: int = 20) -> dict:
    """有多少条抽取结果在等人裁决（规矩类的一律要人点头，§5.4）。

    用户问「攒了多少没审的」时用；也可以主动提一句，免得待办一直堆着没人管。
    """
    payload = await _call("GET", "/v1/pending", params={"limit": limit})
    return {"total": payload["total"], "by_status": payload["by_status"],
            "items": [{k: i[k] for k in ("id", "stage", "status", "reason",
                                         "raw_content", "suggestion") if k in i}
                      for i in payload["items"]]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
