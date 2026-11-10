"""MCP server 的真连接冒烟（§7.2）。

★ **gated on 一个活着且模型已热的服务**（`localhost:8787/healthz` → 200）。
这一层是五个 tool 到 HTTP 的翻译，桩掉 HTTP 就只剩"我拼对了参数"这种没价值的断言；
真正会出事的地方是——**roots 判错桶**（§6.1 串台）、**服务挂了却回空结果**（模型会
把"连不上"读成"没有相关记忆"，然后基于这个错继续推理，正是 hook 那边不敢做的事）。

一个 stdio server 要起一个 python 进程（约 1.5s），所以断言按会话聚合，不是一条一个。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import requires_db
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

SERVER = str(Path(__file__).resolve().parents[2] / "integrations" / "mcp-server" / "mem_mcp.py")
PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
MARK = "pytest-mcp-"
FIVE_TOOLS = {"memory_recall", "memory_note", "memory_correct", "memory_search", "memory_pending"}


def _service_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/healthz", timeout=3) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


SERVICE_UP = _service_ready()

pytestmark = [
    requires_db,
    pytest.mark.skipif(not SERVICE_UP, reason="需要 localhost:8787 上一个模型已热的真服务"),
]


@asynccontextmanager
async def _session(*, root: str | None = PROJECT_ROOT, env: dict | None = None,
                   cwd: str | None = None):
    """起一个真的 stdio server。`root=None` 模拟"客户端不支持 roots"。"""
    async def roots(context):
        return types.ListRootsResult(roots=[types.Root(uri=Path(root).as_uri(), name="proj")])

    params = StdioServerParameters(
        command=sys.executable, args=[SERVER], cwd=cwd,
        env={**os.environ, **(env or {})})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write,
                                 list_roots_callback=roots if root else None) as session:
            await session.initialize()
            yield session


def _run(coro_fn):
    return asyncio.run(coro_fn())


@pytest.fixture
def db():
    """★ 端到端测试的"数据面"是**在线服务连的库**（生产库），不是 pytest 测试库：
    本文件的写入走 MCP → HTTP → 8787，落在服务自己的库里；conftest 已把 MEM_DSN
    指向 memxy_test（#103），若清理/断言跟着 app.core.db 走就会清错库——测试残留
    在生产库里越积越多（2026-11-10 实踩：7 条 MARK 前缀记忆漏清致断言 7 != 1）。
    所以这里绕开全局池、直连 conftest.SERVICE_DSN；连接只属于本 fixture，用完即关，
    不污染其他测试的池。写入有 MARK 前缀隔离，_cleanup 定向按前缀删，与改造前行为一致。"""
    import psycopg
    from psycopg.rows import dict_row

    import conftest

    if not conftest.SERVICE_DSN:
        pytest.skip("拿不到服务库 DSN（生产库不可达时本文件应已整体 skip）")

    conn = psycopg.connect(conftest.SERVICE_DSN, row_factory=dict_row, autocommit=True)

    class _Direct:
        @staticmethod
        def query(sql, params=None):
            return conn.execute(sql, params).fetchall()

        @staticmethod
        def query_one(sql, params=None):
            return conn.execute(sql, params).fetchone()

        @staticmethod
        def execute(sql, params=None):
            return conn.execute(sql, params).rowcount

    facade = _Direct()
    _cleanup(facade)
    try:
        yield facade
    finally:
        _cleanup(facade)
        conn.close()


def _cleanup(_db):
    ids = [r["id"] for r in _db.query(
        "SELECT id FROM memories WHERE content LIKE %s", (MARK + "%",))]
    if ids:
        _db.execute("DELETE FROM memory_events WHERE memory_id = ANY(%s)", (ids,))
        _db.execute("DELETE FROM memory_feedback WHERE memory_id = ANY(%s)", (ids,))
        _db.execute("DELETE FROM memories WHERE id = ANY(%s)", (ids,))
    _db.execute("DELETE FROM recall_log WHERE session_id LIKE 'mcp-%'")
    _db.execute("DELETE FROM workspace_map WHERE cwd LIKE %s", ("%__memxy_no_such_dir__%",))


def _text(result):
    """★ 工具返回注解是裸 `dict`，FastMCP 不会为它生成 `outputSchema`，
    所以没有 `structuredContent`——结果以 JSON 文本块回来，这里自己解析。
    出错时是 `isError` + 一句纯文本。"""
    raw = "".join(b.text for b in result.content if getattr(b, "text", None))
    return raw if result.isError else json.loads(raw)


def test_the_five_tools_are_exposed_and_context_is_not_an_argument():
    """★ `ctx: Context` 必须被 SDK 摘掉：它要是漏进 schema，模型就会去填一个
    "当前工作目录"的字符串，而那个值本该由客户端提供（填错 = 归错桶）。"""
    async def main():
        async with _session() as session:
            return {t.name: t.inputSchema for t in (await session.list_tools()).tools}

    schemas = _run(main)

    assert set(schemas) == FIVE_TOOLS
    for schema in schemas.values():
        assert "ctx" not in schema.get("properties", {})
    assert schemas["memory_note"]["required"] == ["content"]


def test_note_then_recall_then_correct_round_trip(db):
    """一条记忆从"模型主动记下"到"被召回"到"被纠错"的完整闭环，全走 MCP。"""
    async def main():
        async with _session() as session:
            noted = await session.call_tool("memory_note", {
                "content": MARK + "改签验价必须先 RetrievePnr 再 RetrieveAncillary",
                "why": "pytest 冒烟"})
            memory_id = _text(noted)["memory_id"]
            recalled = await session.call_tool(
                "memory_recall", {"query": "改签验价要先调哪个接口", "k": 10})
            corrected = await session.call_tool("memory_correct", {
                "memory_id": memory_id,
                "correction": MARK + "改签验价要先 RetrievePnr，Ancillary 只在退票时用",
                "note": "原文把两个场景混在一起了"})
            searched = await session.call_tool("memory_search", {"q": MARK})
            pending = await session.call_tool("memory_pending", {"limit": 5})
            return noted, memory_id, recalled, corrected, searched, pending

    noted, memory_id, recalled, corrected, searched, pending = _run(main)

    bucket = _text(noted)
    assert bucket["scope"] == "project" and bucket["org_key"] and bucket["project_key"]
    assert bucket["keywords"], "关键词没抽出来 = keywords 通道对这条记忆永久失效（#72）"

    hit_ids = [i["memory_id"] for i in _text(recalled)["items"]]
    assert memory_id in hit_ids, "刚记下的东西召不回来，多半是 embedding 没落库"

    assert _text(corrected) == {"memory_id": memory_id, "action": "updated",
                                "status": "active",
                                "content": MARK + "改签验价要先 RetrievePnr，Ancillary 只在退票时用"}
    assert _text(searched)["total"] == 1
    assert "by_status" in _text(pending)

    # ★ 轨迹与反馈：§10 的"记忆准确性"指标只有 `memory_feedback(corrected)` 这一个真实来源
    assert [e["action"] for e in db.query(
        "SELECT action FROM memory_events WHERE memory_id=%s ORDER BY id", (memory_id,))] \
        == ["ADD", "UPDATE"]
    assert db.query("SELECT kind FROM memory_feedback WHERE memory_id=%s", (memory_id,)) \
        == [{"kind": "corrected"}]
    assert db.query_one("SELECT count(*) n FROM recall_log WHERE session_id LIKE 'mcp-%'")["n"] == 1


def test_correcting_through_mcp_never_writes_the_veto_pool(db):
    """★ 端到端钉 #77：`polarity='negative'` 只有一个入口（否决 doc_proposal）。
    模型纠一次错就往否决记录里塞一条，同栏位以后所有正经的规矩提案都会被比对② 挡回去。"""
    before = db.query_one("SELECT count(*) n FROM raw_observations"
                          " WHERE polarity='negative'")["n"]

    async def main():
        async with _session() as session:
            first = await session.call_tool("memory_note", {"content": MARK + "整条都是错的"})
            await session.call_tool("memory_correct",
                                    {"memory_id": _text(first)["memory_id"], "note": "不存在这回事"})
            second = await session.call_tool("memory_note", {"content": MARK + "改写的才对"})
            await session.call_tool("memory_correct", {
                "memory_id": _text(second)["memory_id"], "correction": MARK + "改写后的正确说法"})

    _run(main)

    assert db.query_one("SELECT count(*) n FROM raw_observations"
                        " WHERE polarity='negative'")["n"] == before
    # 没给 correction 的那条被软删，不是硬删——轨迹得留着
    assert {r["status"] for r in db.query(
        "SELECT status FROM memories WHERE content LIKE %s", (MARK + "%",))} == {"active", "deleted"}


def test_roots_beat_the_environment_when_picking_the_bucket(db):
    """★ 归属判错的代价是串台（§6.1），而 roots 是唯一反映"这个会话真正打开的目录"的信号。

    这里同时把三个干扰源都摆成错的：server 进程 cwd 在一个空临时目录、`MEMXY_CWD`
    指向一个不存在的路径——只有 roots 指向真项目。归对了才说明优先级没写反。
    """
    async def main():
        async with _session(root=PROJECT_ROOT,
                            env={"MEMXY_CWD": "C:/__memxy_no_such_dir__/elsewhere"},
                            cwd=os.environ.get("TEMP") or "/tmp") as with_roots:
            a = await with_roots.call_tool("memory_note", {"content": MARK + "roots 说了算"})
        # 客户端不支持 roots 时才轮到环境变量
        async with _session(root=None, env={"MEMXY_CWD": PROJECT_ROOT},
                            cwd=os.environ.get("TEMP") or "/tmp") as without_roots:
            b = await without_roots.call_tool("memory_note", {"content": MARK + "退到环境变量"})
        return _text(a), _text(b)

    from_roots, from_env = _run(main)

    assert from_roots["project_key"] == from_env["project_key"]
    assert Path(PROJECT_ROOT).name in from_roots["project_key"], \
        f"roots 指哪儿就该归到哪儿，实际归到了 {from_roots['project_key']}"


def test_a_dead_service_says_so_instead_of_returning_an_empty_result():
    """★ MCP 和 hook 的**根本差别**：hook 失败必须静默（它挂在每轮对话上），
    MCP 失败必须说话——回一个空列表，模型会读成"没有相关记忆"并据此继续推理。"""
    async def main():
        async with _session(env={"MEMXY_URL": "http://127.0.0.1:1",
                                 "MEMXY_TIMEOUT_MS": "1500"}) as session:
            return (await session.call_tool("memory_search", {"q": MARK}),
                    await session.call_tool("memory_pending", {}))

    search, pending = _run(main)

    for result in (search, pending):
        assert result.isError
        assert "连不上" in _text(result)
