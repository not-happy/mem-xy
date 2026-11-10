"""内容指纹。`memories` 与 `raw_observations` 都靠 `UNIQUE(source_msg, content_hash)`
做**硬幂等**：崩溃重放、job 重试都不会产生重复行（§5.5）。"""

from __future__ import annotations

import hashlib


def content_hash(text: str) -> str:
    """★ 归一化只做"去首尾空白 + 丢掉空行"，别的都不碰。
    再激进一点（大小写、标点、词序）就会把真实差异也抹掉，而这条约束唯一的职责
    是拦住"同一条源消息被重放两次"——那种重放产生的文本是逐字相同的。"""
    normalized = "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
