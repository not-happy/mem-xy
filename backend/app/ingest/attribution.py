"""归属判定：纯规则，零 LLM（§5.1）。

归属的作用是**检索分区**——让某个供应商仓库的会话只注入该组织的规范和该仓库的怪癖。
判错的后果是"注入了不相干的记忆"（噪声），是质量问题不是事故，所以**不阻塞抽取**。

★ **未命中默认 `personal`，绝不留 NULL**：NULL 会让 §6.1 的可见性谓词无从匹配，
那些记忆永远召不回来，等于白抽。

★ 这里会 `git remote get-url` 读仓库的远端地址。这和 §4.3.1 的解耦原则不冲突：
禁止的是读别的项目里的**规则/记忆文件**（CLAUDE.md 那类），
而远端地址是 §5.1 明确指定的**权威所有权信号**，且只读这一个值。
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

from ..core import db

log = logging.getLogger(__name__)

DEFAULT_ORG = "personal"

# git 调用可能因为目录不存在/不是仓库/网络盘卡住而挂住，必须有超时
_GIT_TIMEOUT = 5.0


@dataclass(frozen=True)
class Attribution:
    org_key: str
    project_key: str | None
    resolved_by: str  # 'rule' | 'human' | 'default'
    match_rule_id: int | None = None


def normalize_cwd(cwd: str | None) -> str:
    """反斜杠→正斜杠，盘符大写。

    ★ 实测同一个目录会以 `C:\\...` 和 `c:\\...` 两种写法出现：44 种原始写法
    归一化后只有 36 个真实目录。不归一化，`workspace_map` 会给同一个目录建两条缓存，
    归属还可能判得不一致。（seed 里的 `cwd_prefix` 规则本来就是正斜杠写法。）
    """
    if not cwd:
        return ""
    out = cwd.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", out):
        out = out[0].upper() + out[1:]
    return out.rstrip("/") or "/"


def remote_url(cwd: str) -> str | None:
    """读 `origin` 的远端地址；不是仓库或 git 不可用时返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


def remote_path(url: str) -> str:
    """从远端地址里取出**主机后面的路径**，`git_remote_path` 规则就是拿它匹配的。

    ★ 必须去掉 host：`codeup.aliyun.com` 是公共代码托管（等同 github.com），
    别的公司也在上面，只匹配 host 会把他司仓库判成 dida。
    两种写法都要认：`https://host/a/b.git` 和 scp 风格 `git@host:a/b.git`。
    """
    if not url:
        return ""
    body = re.sub(r"^[a-z+]+://", "", url, flags=re.I)  # 去 scheme
    if "@" in body.split("/", 1)[0]:
        body = body.split("@", 1)[1]                     # 去 user@
    if ":" in body.split("/", 1)[0]:
        body = body.split(":", 1)[1]                     # scp 风格的 host:
    else:
        body = body.split("/", 1)[1] if "/" in body else ""
    return body.lstrip("/")


def remote_host(url: str) -> str:
    """远端地址的主机名，`git_remote_host` 规则用它匹配（★ seed 里不用，见该规则类型的注释）。"""
    if not url:
        return ""
    body = re.sub(r"^[a-z+]+://", "", url, flags=re.I)
    if "@" in body.split("/", 1)[0]:
        body = body.split("@", 1)[1]
    head = body.split("/", 1)[0]
    return head.split(":", 1)[0]


def repo_name(cwd: str, url: str | None) -> str | None:
    """仓库名：优先远端末段（去 `.git`），退回 cwd 末段。"""
    if url:
        tail = remote_path(url).rstrip("/").rsplit("/", 1)[-1]
        if tail:
            return tail[:-4] if tail.endswith(".git") else tail
    tail = cwd.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def project_key_for(org_key: str, cwd: str, url: str | None) -> str | None:
    """强制拼成 `org/repo`——`project_key` 上有 CHECK `^[^/]+/[^/]+$`。"""
    repo = repo_name(cwd, url)
    if not repo:
        return None
    repo = repo.replace("/", "-").strip()
    if not repo:
        return None
    return f"{org_key}/{repo}"


def _like(pattern: str) -> re.Pattern[str]:
    """把 SQL LIKE 的 pattern 编译成正则（seed 里存的就是 LIKE 写法，如 `62bc.../%`）。"""
    out = []
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.S)


def compile_rules(rules: list[dict]) -> list[tuple[dict, re.Pattern[str]]]:
    return [(r, _like(r["pattern"])) for r in rules]


def match_rules(
    compiled: list[tuple[dict, re.Pattern[str]]], cwd: str, url: str | None
) -> Attribution | None:
    """按 priority 升序找第一条命中的规则；都不命中返回 None。

    纯函数（规则和 cwd/url 都从外面传进来），所以不连数据库也能测。
    ★ `git remote` 的结果同时喂给两类 git 规则和 project_key 推导，只调一次。
    """
    path = remote_path(url) if url else ""
    host = remote_host(url) if url else ""
    for rule, regex in compiled:
        kind = rule["pattern_type"]
        if kind == "cwd_prefix":
            if not cwd.startswith(rule["pattern"]):
                continue
        elif kind == "git_remote_path":
            if not path or not regex.match(path):
                continue
        elif kind == "git_remote_host":
            # ★ 只匹配 host 会把他司仓库判成 dida（codeup.aliyun.com 是公共托管），
            # 所以 seed 里一条都没有；类型保留是为了日后接自建 GitLab 这种独占 host。
            if not host or not regex.match(host):
                continue
        else:
            continue
        return Attribution(
            org_key=rule["org_key"],
            project_key=rule["project_key"] or project_key_for(rule["org_key"], cwd, url),
            resolved_by="rule",
            match_rule_id=rule["id"],
        )
    return None


class Resolver:
    """一次扫描共用一个实例：规则和 workspace_map 缓存都只读一次。"""

    def __init__(self) -> None:
        self._rules = db.query(
            "SELECT id, pattern_type, pattern, org_key, project_key, priority"
            " FROM org_match_rules WHERE enabled ORDER BY priority, id"
        )
        self._cache: dict[str, Attribution] = {}
        rows = db.query(
            "SELECT cwd, org_key, project_key, resolved_by, match_rule_id"
            " FROM workspace_map WHERE enabled"
        )
        for row in rows:
            self._cache[normalize_cwd(row["cwd"])] = Attribution(
                org_key=row["org_key"],
                project_key=row["project_key"],
                resolved_by=row["resolved_by"],
                match_rule_id=row["match_rule_id"],
            )
        self._compiled = compile_rules(self._rules)
        log.info("attribution: %d rules, %d cached workspaces", len(self._rules), len(rows))

    def resolve(self, cwd: str | None) -> Attribution:
        cwd = normalize_cwd(cwd)
        if not cwd:
            return Attribution(DEFAULT_ORG, None, "default")
        hit = self._cache.get(cwd)
        if hit is not None:
            return hit

        url = remote_url(cwd)
        result = match_rules(self._compiled, cwd, url) or Attribution(
            # 都不命中 → personal 默认桶，但仍要推导出 project_key 供检索分区用
            org_key=DEFAULT_ORG,
            project_key=project_key_for(DEFAULT_ORG, cwd, url),
            resolved_by="default",
        )
        self._persist(cwd, result)
        self._cache[cwd] = result
        return result

    @staticmethod
    def _persist(cwd: str, result: Attribution) -> None:
        """写回 workspace_map 缓存，下次不用再调 git。

        ★ `resolved_by='human'` 的行不能被规则覆盖——人工修正优先（§5.1 边界情形：
        从公司仓库 fork 出来的个人项目会保留原 remote，会被误判成 dida）。
        """
        db.execute(
            "INSERT INTO workspace_map (cwd, org_key, project_key, match_rule_id, resolved_by)"
            " VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (cwd) DO UPDATE SET"
            "   org_key=EXCLUDED.org_key, project_key=EXCLUDED.project_key,"
            "   match_rule_id=EXCLUDED.match_rule_id,"
            "   resolved_by=EXCLUDED.resolved_by, resolved_at=now()"
            " WHERE workspace_map.resolved_by <> 'human'",
            (cwd, result.org_key, result.project_key, result.match_rule_id, result.resolved_by),
        )
