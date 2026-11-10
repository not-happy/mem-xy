"""query 侧关键词抽取（§6.4）。★ 零 LLM，零分词器。

为什么不引中文分词：`zhparser`/`pg_jieba` 在 Windows 上编译痛苦且易碎，PG 内置
`to_tsvector` 对中文不分词（整句一个 token，无效）——见 §6.2 的排除清单。
所以这里只有两个来源：

1. **标识符**：`[A-Za-z_][A-Za-z0-9_]{2,}`。这是 keywords 通道存在的**全部理由**——
   `bookingFlowService`、`pgvector`、`RetrievePnr` 这类精确名字，dense 经常糊掉。
2. **已知词表**：org 名、仓库名、`doc_sections` 的 aliases/label。中文 query 走的是这一路：
   "婴儿票" 抽不出标识符，但如果它在某个栏位的 aliases 里，就能命中。

★ 词表是运行时输入，所以带 60s TTL 缓存：召回是 400ms 预算里的活，
   不能每次都去查三张表，但也不能永不刷新（UI 改了 aliases 要能生效）。
"""

from __future__ import annotations

import re
import time

from ..core import db

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

# 纯英文虚词/泛用词：留着会把 `&&` 重叠变成噪声（每条记忆都可能带 `the`）。
# ★ 只收"明显不该当检索键"的，不做通用停用词表——中文那半边靠词表，不靠这里。
STOPWORDS = frozenset({
    "and", "the", "for", "with", "from", "this", "that", "not", "are", "was", "were",
    "you", "your", "our", "its", "has", "have", "had", "will", "would", "can", "could",
    "should", "please", "thanks", "todo", "fixme", "null", "true", "false", "none",
    "str", "int", "list", "dict", "set", "bool", "self", "class", "def", "return",
    "import", "lambda", "assert", "pytest", "http", "https", "com", "org", "www",
})

_VOCAB_TTL_SECONDS = 60.0
_cache: tuple[float, frozenset[str]] | None = None


def load_vocab(*, force: bool = False) -> frozenset[str]:
    """org 名 + 仓库名 + 栏位词表。全部小写化，匹配时按子串找。"""
    global _cache
    now = time.monotonic()
    if not force and _cache is not None and now - _cache[0] < _VOCAB_TTL_SECONDS:
        return _cache[1]

    words: set[str] = set()
    for row in db.query("SELECT org_key FROM orgs"):
        words.add(row["org_key"])
    # project_key 是 `org/repo` 复合形式，两半都要：query 里通常只出现 repo 名
    for row in db.query("SELECT DISTINCT project_key FROM workspace_map"
                        " WHERE project_key IS NOT NULL AND enabled"):
        words.update(part for part in row["project_key"].split("/") if part)
    for row in db.query("SELECT section_key, label, aliases FROM doc_sections WHERE enabled"):
        words.add(row["section_key"])
        words.add(row["label"])
        words.update(row["aliases"] or [])

    vocab = frozenset(w.lower() for w in words if len(w) >= 2)
    _cache = (now, vocab)
    return vocab


def extract(text: str, *, vocab: frozenset[str] | None = None, limit: int = 32) -> list[str]:
    """返回去重后的关键词数组，直接喂给 SQL 的 `keywords && %(qkw)s`。

    ★ 空数组是**合法结果**（纯中文短句 + 词表没命中）：这时 keywords 通道整个不参与，
      RRF 只剩 dense 一路。不能为了"凑一个通道"去塞泛用词，那会把不相关的记忆拉进来。
    """
    if not text:
        return []
    vocab = load_vocab() if vocab is None else vocab
    lowered = text.lower()

    found: dict[str, None] = {}          # dict 保序，set 不保
    for ident in IDENT_RE.findall(text):
        low = ident.lower()
        if low not in STOPWORDS:
            found.setdefault(low, None)
    for word in vocab:
        if word in lowered:
            found.setdefault(word, None)
    return list(found)[:limit]
