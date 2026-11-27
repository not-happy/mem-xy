"""两通道召回 + RRF + 加权（§6.2 / §6.4）。★ 读路径，预算 400ms。

三条硬约束，都是从实测里长出来的，别顺手改：

1. **`enable_iterative_scan` 必须在事务里调**（§6.3）。HNSW 是"先取 ef_search 个候选，
   再套 WHERE 过滤，被过滤掉的不补回来"——不开迭代扫描，`LIMIT 5` 会静默只返回 1–2 条，
   症状是"记忆系统好像不记事"，极难定位。
2. **编码走全局 `max_seq_length=128`**（§2.4 ①）。读路径 encode p95 239ms 就是按 128 测的，
   放到默认的 8192 会直接击穿预算。
3. **`scope='project'` 分支必须带 `org_key`**（§6.1）。两家公司都有 `web-app` 时会互相召回。

融合用 RRF 不用加权和：两路分数量纲不同，加权和要反复调参，RRF 只看排名。
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field

from psycopg import Connection

from ..core import db
from ..core.config import get_settings
from ..core.embedding import get_embedding_service
from ..ingest.attribution import Attribution, Resolver
from . import keywords as kw

log = logging.getLogger(__name__)

# role → 半衰期（天），§4.2 的表
HALF_LIFE_DAYS = {"fact": 90.0, "pointer": 180.0, "profile": 365.0, "preference": 365.0}

# scope 权重：越贴近当前工作现场越靠前（§6.4）
SCOPE_WEIGHT = {"project": 1.0, "org": 0.85, "user": 0.7}

# ★ mention 项必须封顶（§6.4 / #14）。无上界的 log() 跑一段时间后必然压过时间衰减，
#   一条被反复纠正的高频错误记忆会永远排在新事实前面。衰减降权 ≠ 遗忘。
MENTION_CAP = 1.6
MENTION_COEF = 0.15

_VISIBILITY = """ status = 'active'
      AND id <> ALL(%(exclude)s::bigint[])
      AND ( scope = 'user'
         OR (scope = 'org'     AND org_key = %(org)s)
         OR (scope = 'project' AND org_key = %(org)s AND project_key = %(proj)s) )"""

# ★ 两通道**各自直查 `memories`**，不套公共 CTE、不用窗口函数——§6.4 的参考实现里
#   `row_number() OVER (ORDER BY embedding <=> q)` 会强制一次排序，把 HNSW 索引彻底废掉
#   （被引用两次的 CTE 还会被物化）。名次在 Python 侧按返回顺序编，效果一样、索引能用。
_DENSE_SQL = f"""
SELECT id, embedding <=> %(qvec)s::vector AS dist
  FROM memories
 WHERE {_VISIBILITY} AND embedding IS NOT NULL
 ORDER BY embedding <=> %(qvec)s::vector
 LIMIT %(pool)s
"""

# ★ 关键词比对**大小写不敏感**。踩过的坑：路由 LLM 吐的 keywords 是原样标识符
#   （`bookingFlowService`），而 query 侧统一小写化，`&&` 直接对不上 ⇒ 整个通道静默失效，
#   症状和"没有相关记忆"一模一样。
#   代价是 `keywords` 上那个 GIN 索引用不上了（PG 的 `lower()` 不吃数组）。
#   在个人量级（几千条记忆）这是一次亚毫秒的顺序扫描，不是瓶颈——瓶颈是 dense 那一路。
#   真长大了再改：写入侧统一小写 + 一次性回填，把 `&&` 和 GIN 拿回来。
_KEYWORDS_SQL = f"""
SELECT id,
       (SELECT count(*) FROM unnest(keywords) w WHERE lower(w) = ANY(%(qkw)s::text[])) AS overlap
  FROM memories
 WHERE {_VISIBILITY}
   AND EXISTS (SELECT 1 FROM unnest(keywords) w WHERE lower(w) = ANY(%(qkw)s::text[]))
 ORDER BY overlap DESC, id
 LIMIT %(pool)s
"""

_HYDRATE_SQL = """
SELECT id, scope, role, content, mention_count, last_seen_at
  FROM memories WHERE id = ANY(%(ids)s::bigint[])
"""

# ★ 本会话提取的记忆，本会话内不再注入（用户裁决 2026-11-02）：内容还在对话上下文里，
#   回注是纯重复噪声——长会话尤其明显（本库实测单会话产出 35 条，每条都可能被召回）。
#   溯源链 memories.source_msg → conversation_log.session_id，走 (session_id,id) 索引，
#   一次小查询，对 400ms 预算无感。source_msg 为空的记忆（人工录入等）不受影响。
_SESSION_BORN_SQL = """
SELECT m.id
  FROM memories m JOIN conversation_log c ON c.id = m.source_msg
 WHERE c.session_id = %(session)s
"""


def _session_born_ids(session_id: str) -> list[int]:
    if not session_id or session_id == "anonymous":
        return []
    return [r["id"] for r in db.query(_SESSION_BORN_SQL, {"session": session_id})]


@dataclass
class Hit:
    memory_id: int
    scope: str
    role: str
    text: str
    score: float
    channels: dict = field(default_factory=dict)
    rrf: float = 0.0
    dense_rank: int | None = None
    keyword_rank: int | None = None

    def as_dict(self) -> dict:
        return {
            "memory_id": self.memory_id, "scope": self.scope, "role": self.role,
            "text": self.text, "score": self.score, "channels": self.channels,
            "rrf": self.rrf, "dense_rank": self.dense_rank,
            "keyword_rank": self.keyword_rank,
        }


@dataclass
class RecallResult:
    items: list[Hit]
    latency_ms: int
    recall_log_id: int | None = None
    org_key: str | None = None
    project_key: str | None = None
    query_text: str = ""
    keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "items": [h.as_dict() for h in self.items],
            "latency_ms": self.latency_ms,
            "recall_log_id": self.recall_log_id,
            "org_key": self.org_key,
            "project_key": self.project_key,
            "query_text": self.query_text,
            "keywords": self.keywords,
        }


_resolver: Resolver | None = None
_resolver_at = 0.0
_RESOLVER_TTL = 60.0


def attribute(cwd: str | None) -> Attribution:
    """cwd → (org_key, project_key)。★ Resolver 会读两张表，所以缓存 60s。

    没见过的 cwd 会去跑一次 `git remote get-url origin`（§5.1 的权威归属信号），
    那一下可能几百毫秒——但结果写回 `workspace_map`，同一个 repo 只付一次。
    """
    global _resolver, _resolver_at
    now = time.monotonic()
    if _resolver is None or now - _resolver_at > _RESOLVER_TTL:
        _resolver = Resolver()
        _resolver_at = now
    return _resolver.resolve(cwd)


def build_query_text(query: str, *, prev_assistant_summary: str | None = None,
                     project_key: str | None = None) -> str:
    """★ 查询文本不是裸 prompt，而是 prompt + 上一轮回复摘要 + 当前 project 名（§6.4）。

    上下文增广几乎零成本，但显著改善短指令的召回——"修复A2 A3" 这种 query 单独编码
    基本召不回任何东西，带上项目名之后至少能命中该 repo 的记忆。

    ★ 但**只喂给 dense 那一路**。关键词抽取用的是 `keyword_text()`（不含 project 名）：
      `dida/gds-agg` 会被标识符正则切成 `dida` / `gds` / `agg` 三个碎片，
      这些碎片去和 `keywords[]` 做重叠，等于给每条同项目的记忆都白送几分，
      把 keywords 通道本来要解决的"精确标识符"能力稀释掉。
      project 名该走的路是词表（`load_vocab` 收了 org 名和 repo 名）——
      用户真的在 query 里打了 `gds-agg`，它自然会以**完整词**的形式命中。
    """
    parts = [query.strip()]
    if prev_assistant_summary:
        parts.append(prev_assistant_summary.strip())
    if project_key:
        parts.append(project_key.replace("/", " "))
    return "\n".join(p for p in parts if p)


def keyword_text(query: str, *, prev_assistant_summary: str | None = None) -> str:
    return "\n".join(p for p in (query.strip(), (prev_assistant_summary or "").strip()) if p)


# ★ A2（S1/H2）：增广内容进得了查询文本，却进不了向量——max_seq_length=128 是
#   全局硬护栏（本文件头注释第 2 条），XLM-R tokenizer 下 128 token ≈ 110–120 个汉字，
#   拼在后面的 summary / project 名会被**右截断**整个丢掉。修法是分段编码再混合：
#   query 段保头部（内部被截也保住的是正文），summary+project 段单独编码，
#   0.75/0.25 加权后重新归一化。代价是 encode 从一次变两次（88ms→~176ms，
#   仍在 400ms 预算内），换来的是增广内容真实生效。
_QUERY_MAX_CHARS = 300    # query 本体只送 300 字符进编码器（纯中文下略超 128 会被内部截，保头）
_TAIL_SUMMARY_CHARS = 120 # summary 段预算（含 project 名行的空间）
_QUERY_WEIGHT = 0.75
_TAIL_WEIGHT = 0.25


def _blend_vectors(query_vec: list[float], tail_vec: list[float] | None) -> list[float]:
    """0.75·query + 0.25·tail 后重新 L2 归一化（两路输入本身都已归一）。"""
    if not tail_vec or len(tail_vec) != len(query_vec):
        return query_vec
    blended = [_QUERY_WEIGHT * q + _TAIL_WEIGHT * t for q, t in zip(query_vec, tail_vec)]
    norm = math.sqrt(sum(v * v for v in blended)) or 1.0
    return [v / norm for v in blended]


def build_query_vector(query: str, *, prev_assistant_summary: str | None = None,
                       project_key: str | None = None) -> list[float]:
    """A2：分段编码 + 加权混合，见 _blend_vectors 注释。"""
    service = get_embedding_service()
    head = query.strip()[:_QUERY_MAX_CHARS]
    qvec = service.encode_one(head)
    tail_parts = []
    if prev_assistant_summary:
        tail_parts.append(prev_assistant_summary.strip()[:_TAIL_SUMMARY_CHARS])
    if project_key:
        tail_parts.append(project_key.replace("/", " "))
    if not tail_parts:
        return qvec
    tail_vec = service.encode_one("\n".join(tail_parts))
    return _blend_vectors(qvec, tail_vec)


def recall(*, query: str, cwd: str | None = None, org_key: str | None = None,
           project_key: str | None = None, session_id: str = "anonymous",
           turn_no: int | None = None, prev_assistant_summary: str | None = None,
           k: int | None = None, exclude_ids: list[int] | None = None,
           source: str | None = None, write_log: bool = True) -> RecallResult:
    """一次召回。`org_key` 缺省时按 `cwd` 判定。

    ★ `write_log=False` 只用于评测 runner：`recall_log` 是"本轮注入了什么"的审计，
      拿评测数据去污染它，UI 上会看到一堆没人真用过的召回。
    """
    settings = get_settings()
    started = time.perf_counter()

    if org_key is None:
        attribution = attribute(cwd)
        org_key, project_key = attribution.org_key, attribution.project_key

    query_text = build_query_text(query, prev_assistant_summary=prev_assistant_summary,
                                  project_key=project_key)
    # ★ A2：分段编码 + 加权混合（summary/project 名不再被 128 token 右截断）。
    #   query_text 仍保留给 recall_log 审计与 debug 口径。
    qvec = build_query_vector(query, prev_assistant_summary=prev_assistant_summary,
                              project_key=project_key)
    qkw = kw.extract(keyword_text(query, prev_assistant_summary=prev_assistant_summary))

    params = {
        "qvec": qvec, "qkw": qkw, "org": org_key, "proj": project_key,
        # ★ 调用方给的 exclude_ids（如"上一轮已注入"）+ 本会话出生的记忆，一起排除。
        "exclude": list(exclude_ids or []) + _session_born_ids(session_id),
        "pool": settings.recall_dense_pool, "rrf_k": settings.recall_rrf_k,
        "k": k or settings.recall_k,
    }
    with db.transaction() as conn:
        db.enable_iterative_scan(conn)
        dense = conn.execute(_DENSE_SQL, params).fetchall()
        # ★ B2 相关性门槛（R1）：dense 名次是"矮子里拔将军"，top-1 恒满分——
        #   但 dist 是未归一化余弦距离，可以绝对判断。超过阈值的不进候选；
        #   全被过滤时本轮注入为空（一等公民结果，寒暄/超短 query 不硬塞）。
        #   keyword 通道是精确标识符命中，天然相关，不过滤。
        min_cos = settings.recall_min_cosine
        if min_cos > 0:
            dense = [r for r in dense if r["dist"] <= min_cos]
        keyword_rows = conn.execute(_KEYWORDS_SQL, params).fetchall()
        fused = fuse(dense, keyword_rows, rrf_k=settings.recall_rrf_k)
        rows = _hydrate(conn, list(fused)) if fused else []
        items = score(rows, fused, now=time.time())[:params["k"]]
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_id = _write_log(conn, query_text, items, latency_ms,
                            session_id=session_id, turn_no=turn_no,
                            org_key=org_key, project_key=project_key,
                            source=source) if write_log else None
        # ★ B3：真注入才续命；放事务最后一条语句，失败即整个召回失败（同事务语义，
        #   但 UPDATE 极简、失败面小）。评测路径 write_log=False 天然不回写。
        _touch_last_seen(conn, [h.memory_id for h in items]) if write_log else None

    return RecallResult(items=items, latency_ms=latency_ms, recall_log_id=log_id,
                        org_key=org_key, project_key=project_key,
                        query_text=query_text, keywords=qkw)


def fuse(dense_rows: list[dict], keyword_rows: list[dict], *, rrf_k: int) -> dict[int, dict]:
    """RRF（§6.2）：只看排名不看分数，因为两路量纲不同（一个是余弦距离，一个是重叠词数）。

    ★ `overlap > 0` 这个过滤是必需的：`keywords && qkw` 已经保证了有重叠，
      但 `ORDER BY overlap DESC` 的名次是**在过滤后**编的，两边口径要一致。
    """
    fused: dict[int, dict] = {}
    for rank, row in enumerate(dense_rows, start=1):
        entry = fused.setdefault(row["id"], {"rrf": 0.0, "dense_rank": None,
                                             "keyword_rank": None})
        entry["rrf"] += 1.0 / (rrf_k + rank)
        entry["dense_rank"] = rank
    kept = [r for r in keyword_rows if r["overlap"] > 0]
    for rank, row in enumerate(kept, start=1):
        entry = fused.setdefault(row["id"], {"rrf": 0.0, "dense_rank": None,
                                             "keyword_rank": None})
        entry["rrf"] += 1.0 / (rrf_k + rank)
        entry["keyword_rank"] = rank
    return fused


def _hydrate(conn: Connection, ids: list[int]) -> list[dict]:
    return conn.execute(_HYDRATE_SQL, {"ids": ids}).fetchall()


def score(rows: list[dict], fused: dict[int, dict], *, now: float) -> list[Hit]:
    """rrf 归一 × scope 权重 × 时间衰减 × mention 加成（§6.4）。

    ★ rrf 归一化用的是**本批融合结果里的最大值**，不是理论上限：
      这样 top-1 恒为 1.0 再乘权重，分数才有跨 query 的可比性。
    """
    if not rows:
        return []
    top = max(fused[row["id"]]["rrf"] for row in rows)
    hits = []
    for row in rows:
        entry = fused[row["id"]]
        rrf_norm = entry["rrf"] / top if top else 0.0
        age_days = max(0.0, (now - row["last_seen_at"].timestamp()) / 86400.0)
        half_life = HALF_LIFE_DAYS.get(row["role"], 90.0)
        decay = math.exp(-math.log(2) * age_days / half_life)
        mention = min(MENTION_CAP, 1 + MENTION_COEF * math.log(1 + row["mention_count"]))
        channels = {}
        if entry["dense_rank"] is not None:
            channels["dense"] = entry["dense_rank"]
        if entry["keyword_rank"] is not None:
            channels["keywords"] = entry["keyword_rank"]
        hits.append(Hit(
            memory_id=row["id"], scope=row["scope"], role=row["role"],
            # ★ 带上 id 和 scope 前缀：注入到对话里之后，用户/AI 要能指着它说"这条不对"，
            #   MCP 的 correct 工具就是靠这个 id 找回去的（§7.2）。
            text=f"[记忆 #{row['id']} · {row['scope']}] {row['content']}",
            score=round(rrf_norm * SCOPE_WEIGHT.get(row["scope"], 0.7) * decay * mention, 4),
            channels=channels, rrf=round(entry["rrf"], 6),
            dense_rank=entry["dense_rank"], keyword_rank=entry["keyword_rank"],
        ))
    # ★ 平分时按 id 兜底排序：不写这一手，同一批数据两次召回的顺序可能不同
    #   （dict 迭代顺序稳定，但 SQL 返回顺序不稳定），UI 上看着就像"记忆在飘"。
    hits.sort(key=lambda h: (-h.score, h.memory_id))
    return hits


def _touch_last_seen(conn: Connection, ids: list[int]) -> int:
    """B3：召回即使用——被实际注入的记忆续命 last_seen_at，衰减才看得到真实活跃度。

    ★ 只在 write_log=True（真注入）时走；eval runner（write_log=False）不回写，
      否则评测跑批会人为抬高被测记忆的新鲜度，基线就失真了。
    ★ 并入召回同一事务（定案，见 decisions-log #114）：事务已存在、UPDATE 走主键
      批量亚毫秒，独立任务反而引入失败面与延迟。
    ★ 只更新 last_seen_at，不碰 updated_at——这不是"记忆被修改"，不该骗过
      revision 展示；使用审计已有 recall_log.injected_ids，不另写 memory_events
      （CHECK 只容 ADD/UPDATE/DELETE/NOOP/RESTORE，扩枚举牵动 UI，不值得）。
    """
    if not ids:
        return 0
    res = conn.execute(
        "UPDATE memories SET last_seen_at = now()"
        " WHERE id = ANY(%s::bigint[]) AND status = 'active'",
        (list(ids),),
    )
    return res.rowcount


def _write_log(conn: Connection, query_text: str, items: list[Hit],
               latency_ms: int, *, session_id: str, turn_no: int | None,
               org_key: str | None, project_key: str | None,
               source: str | None) -> int:
    """★ 只记 id 和分数，不记 content：`recall_log` 会在 UI 里长期展示，
    把记忆正文抄一份进来就是第二份真相源（违反单一真相源），改了就对不上。"""
    return conn.execute(
        "INSERT INTO recall_log (source, session_id, turn_no, query_text, org_key, project_key,"
        "                        hits, injected_ids, latency_ms)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (source, session_id, turn_no, query_text[:4000], org_key, project_key,
         json.dumps([{"id": h.memory_id, "score": h.score, "channels": h.channels}
                     for h in items], ensure_ascii=False),
         [h.memory_id for h in items], latency_ms),
    ).fetchone()["id"]
