# -*- coding: utf-8 -*-
"""拉取全部待裁决卡，产出紧凑判定表 + 分布统计。"""
import json, sys, urllib.request
from collections import Counter

BASE = "http://127.0.0.1:8787"

def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

items, offset = [], 0
while True:
    page = get(f"{BASE}/v1/pending?status=pending&limit=500&offset={offset}")
    batch = page["items"]
    items.extend(batch)
    offset += len(batch)
    if offset >= page["total"] or not batch:
        break

print(f"total={page['total']} fetched={len(items)}")

def sug(i):  return i.get("suggestion") or {}
def rev(i):  return sug(i).get("llm_review") or {}

# 分布：(stage, 第一遍 kind, 第二遍 kind, 裁决建议)
c = Counter((i["stage"], sug(i).get("kind"), rev(i).get("kind"), (rev(i).get("verdict") or "")) for i in items)
print("\n== (stage, kind1, kind2, verdict) 分布 ==")
for k, n in c.most_common(40):
    print(f"{n:4d}  {k}")

# 判定表（按分歧类型聚类，便于成批一致地判）
def one_line(s, n):
    return " ".join((s or "").split())[:n]

rows = sorted(items, key=lambda i: (i["stage"], sug(i).get("kind") or "", rev(i).get("kind") or "", i["id"]))
out = []
for i in rows:
    s, r = sug(i), rev(i)
    out.append({
        "id": i["id"],
        "stage": i["stage"],
        "kind1": s.get("kind"),
        "kind2": r.get("kind"),
        "conf": r.get("confidence"),
        "disagree": one_line(r.get("why") or "", 120),
        "reason": one_line(i.get("reason") or "", 100),
        "content": one_line(s.get("content") or i.get("raw_content") or "", 300),
        "revised": one_line(r.get("content") or "", 160),
        "scope": s.get("scope"), "subject": s.get("subject"), "role": s.get("role"),
        "section": s.get("section_key"),
        "rev_section": r.get("section_key"),
        "org": i.get("org_key"), "proj": i.get("project_key"),
        "kw": s.get("keywords"),
    })

with open("C:/tmp/pending_sheet.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

# 可读版判定表（我逐条看的就是这份）
lines = []
for r in out:
    lines.append(
        f"#{r['id']} [{r['stage']}] {r['kind1']}→{r['kind2']} conf={r['conf']}"
        f" | {r['org']}/{r['proj'] or '-'} | sec={r['section']}→{r['rev_section']}"
        f" | scope={r['scope']}/{r['subject']}/{r['role']}\n"
        f"  文:{r['content']}\n"
        f"  改:{r['revised']}\n"
        f"  分歧:{r['disagree']} | 卡因:{r['reason']}"
    )
with open("C:/tmp/pending_sheet.txt", "w", encoding="utf-8") as f:
    f.write("\n\n".join(lines))
print(f"\nsheet written: {len(out)} rows -> C:/tmp/pending_sheet.txt / .json")
