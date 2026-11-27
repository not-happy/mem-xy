# -*- coding: utf-8 -*-
"""收尾 4 张残余待裁决卡：
- 1353/1362/1372：merge 覆盖文案命中 (source_msg, content_hash) 唯一键
  （A/B 同 source_msg，非 partial UNIQUE 连软删行也占键）→ 改用
  suggestion.content（=A 原文）走 NOOP 合并分支（只并 mention、软删 B）。
- 1370：pair_not_active（3799 已被 #1364 的合并软删）→ reject 关卡，
  3788 留给 dedup_scan 与 2424 重新配对。
"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8787"

# 三张卡的 suggestion.content（=A 原文，从卡面取）
A_TEXTS = {
    1353: "在 personal/dingding 项目中，执行过程 tab 的 .engine-stream 是 display:flex + flex-direction:column 且带 max-height(640px) 的滚动容器，其子元素必须设置 flex-shrink:0，否则事件条数多、内容溢出时浏览器会纵向压缩子块（文字被压成半行），而不是让外层滚动",
    1362: "personal/dingding 项目后端有全局异常处理器 com.dingding.workbench.controller.GlobalExceptionHandler（@RestControllerAdvice，继承 ResponseEntityExceptionHandler）：IllegalArgumentException 映射为 400、IllegalStateException 映射为 409，两者只记 WARN 单行日志；其他未知异常保持 500 + ERROR 全栈；响应体格式与 Spring 默认 /error 一致（timestamp/status/error/message/path）",
    1372: "dingding 项目的任务执行模型：worker 并发数为 1，每个任务基于创建时的 baseRef 建独立 worktree；任务进入 WAIT_APPROVAL 后不占用 worker；审批通过后不自动合并分支，合并由用户手工完成",
}
REJECT_1370 = ("裁决时该卡配对已失效：B(3799) 已先被 #1364 的合并软删（keep 2424），"
               "A(3788) 保留，交由 dedup_scan 后续与 2424 重新配对裁决")


def call(path, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {}
        return e.code, payload


for pid, text in A_TEXTS.items():
    code, resp = call(f"/v1/pending/{pid}/approve?resolved_by=ai_delegated",
                      {"content": text})
    print(f"APPROVE {pid} -> {code} {json.dumps(resp, ensure_ascii=False)[:120]}")

code, resp = call("/v1/pending/1370/reject", {"note": REJECT_1370})
print(f"REJECT  1370 -> {code} {json.dumps(resp, ensure_ascii=False)[:120]}")
