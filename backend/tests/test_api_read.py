"""§7.1 读路径端点的契约测试。

★ 关键不是"能返回 200"，而是**降级路径要对**：hook 靠状态码决定要不要读本地快照缓存，
  回错一个码就会把"模型还在加载"当成"没有相关记忆"，然后把错误的空结果写进缓存、
  把好的那份盖掉（§4.3.3）。所以 503 那几条比 200 那几条更重要。

不用 `with TestClient(...)`：那会跑 lifespan，开真连接池 + 启动 30s 的模型加载。
"""

import hashlib
import re

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient

from app.retrieve import keywords as kw
from app.retrieve import recall as recall_mod

DIM = 1024
# ★ 测试专用分区（同 test_recall.py 的 live_data_firewall）：本文件跑在活库上，
#   真库 dida/gds-agg 分区已有 195 条真实记忆，会和种子数据竞争 top-k，
#   审计断言（injected == [wanted]）对任何多余命中都是零容忍。
ORG = "pytest-api-org"
PROJECT = f"{ORG}/gds-agg"
SESSION = "pytest-api-read"


class FakeEmbedder:
    def encode(self, texts, *a, **k):
        return [_hash_vector(t) for t in texts]

    def encode_one(self, text):
        return _hash_vector(text)


def _hash_vector(text: str) -> list[float]:
    vector = [0.0] * DIM
    for token in text.replace("，", " ").split():
        slot = int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM
        vector[slot] += 1.0
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


def _stub_service(monkeypatch, *, ready: bool, loading: bool = False):
    """两处都要打桩：`api.read` 用 `ready` 决定要不要回 503，
    `retrieve.recall` 用 `encode_one` 算 query 向量。少一处就会去加载真模型（33s）。"""
    class Stats:
        max_seq_length = 128
        modules = "Transformer,Pooling,Normalize"
        sparse_available = False
        load_seconds = 29.6
        avg_ms_per_row = 22.0

    class Service(FakeEmbedder):
        stats = Stats()

        @property
        def ready(self):
            return ready

        @property
        def loading(self):
            return loading

    service = Service()
    monkeypatch.setattr("app.api.read.get_embedding_service", lambda: service)
    monkeypatch.setattr(recall_mod, "get_embedding_service", lambda: service)
    monkeypatch.setattr(kw, "load_vocab", lambda *, force=False: frozenset({"gds-agg"}))
    return service


@pytest.fixture(autouse=True)
def live_data_firewall(monkeypatch):
    """★ 同 test_recall.py：专用分区挡掉真实 project/org 记忆，但 user 级记忆
    全局可见（§6.1 语义如此），审计用例的 `injected == [wanted]` 是精确断言，
    多漏进来一条就挂——给两通道 SQL 追加 pytest 内容前缀过滤。
    用正则不用 LIKE：psycopg 会把 SQL 文本里裸的 % 当占位符解析。"""
    visible = "status = 'active' AND content ~ '^pytest-api-'"
    for name in ("_DENSE_SQL", "_KEYWORDS_SQL"):
        sql = getattr(recall_mod, name)
        assert "status = 'active'" in sql, f"{name} 结构变了，防火墙需要跟着改"
        monkeypatch.setattr(recall_mod, name, sql.replace("status = 'active'", visible, 1))


@pytest.fixture
def client():
    from app.api.main import app

    return TestClient(app)


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    # living_doc.org_key 有外键指向 orgs：测试专用分区的 org 行真库里不存在。
    _db.execute("INSERT INTO orgs (org_key, name) VALUES (%s,'pytest 专用组织')"
                " ON CONFLICT DO NOTHING", (ORG,))
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM recall_log WHERE session_id=%s", (SESSION,))
    _db.execute("DELETE FROM memories WHERE content LIKE %s", ("pytest-api-%",))
    _db.execute("DELETE FROM living_doc WHERE content LIKE %s", ("pytest-api-%",))
    # orgs 放最后：living_doc.org_key 的外键要求子表先清。
    _db.execute("DELETE FROM orgs WHERE org_key=%s", (ORG,))


def seed_memory(db, content, *, keywords=(), org=ORG, project=PROJECT):
    return db.query_one(
        "INSERT INTO memories (scope, org_key, project_key, subject, role, content,"
        "                       content_hash, keywords, embedding)"
        " VALUES ('project',%s,%s,'project','fact',%s,md5(%s),%s,%s::vector) RETURNING id",
        (org, project, content, content, list(keywords), _hash_vector(content)))["id"]


# ─────────────── 降级路径（★ 不 gated on DB：这几条一个字节都不该碰库） ───────────────

def test_recall_returns_503_model_loading_before_the_embedder_is_warm(client, monkeypatch):
    _stub_service(monkeypatch, ready=False, loading=True)
    response = client.post("/v1/recall", json={"query": "随便问点什么"})
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "model_loading"
    assert "loading=True" in body["message"]


def test_bootstrap_does_not_wait_for_the_model(client, monkeypatch):
    """★ 稳定层是纯 SQL，不需要向量。让它跟 recall 一起等模型就绪，
    等于把 SessionStart 也拖进 33s 冷启动——而那是整个会话的第一件事。"""
    _stub_service(monkeypatch, ready=False)
    response = client.get("/v1/context/bootstrap",
                          params={"org_key": ORG, "project_key": PROJECT})
    assert response.status_code == 200
    assert "markdown" in response.json()


def test_an_empty_query_is_a_422_not_a_500(client, monkeypatch):
    _stub_service(monkeypatch, ready=True)
    assert client.post("/v1/recall", json={"query": ""}).status_code == 422


def test_the_error_body_never_leaks_the_dsn(client, monkeypatch):
    """★ §2.1：psycopg 的异常字符串带连接串（即 DB 口令），而这个响应体会被
    hook 打日志、被 UI 展示。泄 token/口令是 bug，不是"日志详细"。"""
    _stub_service(monkeypatch, ready=True)

    def boom(**_):
        raise RuntimeError(
            "connection to postgresql://postgres:sup3rs3cret@127.0.0.1:5432/memxy failed")

    monkeypatch.setattr(recall_mod, "recall", boom)
    body = client.post("/v1/recall", json={"query": "x"}).json()["error"]
    assert body["code"] == "recall_failed"
    assert "sup3rs3cret" not in body["message"]
    assert "***REDACTED***" in body["message"]


# ─────────────────────────────── 正常路径 ───────────────────────────────

@requires_db
def test_recall_returns_items_with_the_memory_id_embedded_in_the_text(client, db, monkeypatch):
    """★ `text` 里带 `[记忆 #id · scope]` 前缀：注入到对话之后，用户/AI 要能指着它说
    "这条不对"，MCP 的 correct 工具就是靠这个 id 找回去的（§7.2）。"""
    _stub_service(monkeypatch, ready=True)
    wanted = seed_memory(db, "pytest-api-验价必须先 RetrievePnr", keywords=["RetrievePnr"])

    response = client.post("/v1/recall", json={
        "query": "RetrievePnr 验价", "session_id": SESSION,
        "org_key": ORG, "project_key": PROJECT})
    assert response.status_code == 200
    payload = response.json()
    hit = next(i for i in payload["items"] if i["memory_id"] == wanted)
    assert hit["text"].startswith(f"[记忆 #{wanted} · project]")
    assert hit["channels"]["keywords"] == 1
    assert payload["recall_log_id"] is not None
    assert payload["latency_ms"] >= 0


@requires_db
def test_recall_writes_the_audit_row_and_the_log_endpoint_joins_the_content_back(
        client, db, monkeypatch):
    _stub_service(monkeypatch, ready=True)
    wanted = seed_memory(db, "pytest-api-审计对照", keywords=["审计对照"])
    # ★ B2 相关性门槛后，query 必须和 content 共享 token 才能过 0.5 的余弦线
    #   （hash 词袋对 '审计对照' vs 'pytest-api-审计对照' 余弦恒 0）。直接拿
    #   记忆原文当 query——本用例考的是审计落行与正文回拼，不是召回排序。
    client.post("/v1/recall", json={"query": "pytest-api-审计对照", "session_id": SESSION,
                                    "org_key": ORG, "project_key": PROJECT})

    rows = client.get("/v1/recall-log", params={"session_id": SESSION}).json()
    assert len(rows) == 1
    assert rows[0]["injected"] == [{"id": wanted, "text": "pytest-api-审计对照"}]
    # ★ 日志表里存的是 id，正文是现查出来的——不是第二份真相源
    assert "content" not in rows[0]


@requires_db
def test_the_debug_endpoint_leaves_no_audit_row(client, db, monkeypatch):
    """调试召回不是真注入。混进 `recall_log` 会让 UI 上的"本轮注入了什么"失真。"""
    _stub_service(monkeypatch, ready=True)
    seed_memory(db, "pytest-api-调试对照", keywords=["调试对照"])
    payload = {"query": "调试对照", "session_id": SESSION,
               "org_key": ORG, "project_key": PROJECT}

    debug = client.post("/v1/recall/debug", json=payload).json()
    assert debug["recall_log_id"] is None
    # ★ 中文 query 抽不出标识符、词表也没命中 ⇒ 空数组。这是合法的：
    #   keywords 通道整个不参与，RRF 只剩 dense 一路。
    assert debug["keywords"] == []
    assert "调试对照" in debug["query_text"]
    assert debug["over_budget"] is False
    assert db.query_one("SELECT count(*) c FROM recall_log WHERE session_id=%s",
                        (SESSION,))["c"] == 0


@requires_db
def test_exclude_ids_stops_the_same_memory_being_injected_every_turn(client, db, monkeypatch):
    _stub_service(monkeypatch, ready=True)
    a = seed_memory(db, "pytest-api-排除对照", keywords=["排除对照"])
    # ★ B2 后同上：query 带上 content 原文，保证 dense 过得了 0.5 余弦线。
    body = {"query": "pytest-api-排除对照", "session_id": SESSION,
            "org_key": ORG, "project_key": PROJECT}

    assert a in [i["memory_id"] for i in client.post("/v1/recall", json=body).json()["items"]]
    body["exclude_ids"] = [a]
    assert a not in [i["memory_id"] for i in client.post("/v1/recall", json=body).json()["items"]]


@requires_db
def test_recall_log_feedback_round_trip_and_audit_visibility(client, db, monkeypatch):
    """B6（ROADMAP）：整轮"有用/没用"标记——写 recall_log.feedback、
    GET /recall-log 带出来、再次 POST 可改判、null 可撤销。"""
    _stub_service(monkeypatch, ready=True)
    seed_memory(db, "pytest-api-反馈对照", keywords=["反馈对照"])
    log_id = client.post("/v1/recall", json={"query": "反馈对照", "session_id": SESSION,
                                             "org_key": ORG, "project_key": PROJECT}
                         ).json()["recall_log_id"]

    # 没标记时是 NULL（GET 带出键，值为 null）
    rows = client.get("/v1/recall-log", params={"session_id": SESSION}).json()
    assert rows[0]["feedback"] is None

    # 标没用 → 审计行带出；再改有用 → 覆盖；撤销 → 回 NULL
    assert client.post(f"/v1/recall-log/{log_id}/feedback",
                       json={"feedback": "useless"}).json() == {"id": log_id, "feedback": "useless"}
    assert client.get("/v1/recall-log", params={"session_id": SESSION}).json()[0]["feedback"] == "useless"
    assert client.post(f"/v1/recall-log/{log_id}/feedback",
                       json={"feedback": "useful"}).json()["feedback"] == "useful"
    assert client.post(f"/v1/recall-log/{log_id}/feedback",
                       json={"feedback": None}).json()["feedback"] is None
    assert client.get("/v1/recall-log", params={"session_id": SESSION}).json()[0]["feedback"] is None


@requires_db
def test_recall_log_feedback_rejects_bad_kind_and_missing_row(client, db):
    """非法值 422（pydantic pattern 兜着，不进 SQL）；行不存在 404 不是 500。"""
    assert client.post("/v1/recall-log/1/feedback",
                       json={"feedback": "great"}).status_code == 422
    assert client.post("/v1/recall-log/999999999/feedback",
                       json={"feedback": "useful"}).status_code == 404
    body = client.post("/v1/recall-log/999999999/feedback",
                       json={"feedback": "useful"}).json()["error"]
    assert body["code"] == "log_not_found"


@requires_db
def test_bootstrap_renders_confirmed_rules_grouped_by_section(client, db, monkeypatch):
    _stub_service(monkeypatch, ready=False)      # ★ 故意：稳定层不该依赖模型
    db.execute("INSERT INTO living_doc (org_key, project_key, section_key, content, authority)"
               " VALUES (%s,%s,'code_style','pytest-api-禁止使用 Lombok','confirmed')",
               (ORG, PROJECT))

    payload = client.get("/v1/context/bootstrap",
                         params={"org_key": ORG, "project_key": PROJECT}).json()
    label = db.query_one("SELECT label FROM doc_sections WHERE section_key='code_style'")["label"]
    assert f"## {label}" in payload["markdown"]
    assert "- pytest-api-禁止使用 Lombok（本仓库专属）" in payload["markdown"]
    assert payload["sections"][0]["items"] == ["pytest-api-禁止使用 Lombok（本仓库专属）"]
    assert payload["cache_key"] == f"bootstrap-{ORG}-{PROJECT.replace('/', '-')}"
    assert payload["generated_at"]
