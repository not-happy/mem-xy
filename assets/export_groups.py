# -*- coding: utf-8 -*-
"""导出三类卡的完整素材供判定：
1) dedup merge_pair 卡：两段原文全文（content_a/content_b）
2) section 卡：原文全文 + 22 栏位词表 + 现有栏位命中
3) route 卡：原文全文
"""
import json, sys, urllib.request

BASE = "http://127.0.0.1:8787"

def get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

rows = json.load(open("C:/tmp/pending_sheet.json", encoding="utf-8"))
by_id = {r["id"]: r for r in rows}
# 重拉原始 suggestion 全文
items, offset = [], 0
while True:
    page = get(f"{BASE}/v1/pending?status=pending&limit=500&offset={offset}")
    batch = page["items"]; items.extend(batch); offset += len(batch)
    if offset >= page["total"] or not batch: break

raw = {i["id"]: i for i in items}

# 1) dedup 卡
dedup = [i for i in items if i["stage"] == "dedup"]
lines = []
for i in sorted(dedup, key=lambda x: x["id"]):
    s = i.get("suggestion") or {}
    lines.append(f"### dedup #{i['id']} sim={s.get('similarity')} org={i.get('org_key')}/{i.get('project_key') or '-'}")
    lines.append(f"  A(id={s.get('memory_ids',[None,None])[0] if s.get('memory_ids') else None}): {s.get('content_a','')}")
    lines.append(f"  B(id={s.get('memory_ids',[None,None])[1] if s.get('memory_ids') and len(s.get('memory_ids',[]))>1 else None}): {s.get('content_b','')}")
    lines.append(f"  建议 content: {s.get('content','')}")
    lines.append("")
open("C:/tmp/dedup_cards.txt", "w", encoding="utf-8").write("\n".join(lines))

# 2) section 卡 + 栏位词表
sec = [i for i in items if i["stage"] == "section"]
lines = []
for i in sorted(sec, key=lambda x: x["id"]):
    s = i.get("suggestion") or {}
    lines.append(f"### section #{i['id']} org={i.get('org_key')}/{i.get('project_key') or '-'} sec={s.get('section_key')}")
    lines.append(f"  原文: {i.get('raw_content','')}")
    lines.append(f"  内容: {s.get('content','')}")
    lines.append(f"  why: {s.get('why','')}")
    lines.append("")
open("C:/tmp/section_cards.txt", "w", encoding="utf-8").write("\n".join(lines))

# 栏位词表（sections 视图同源）
try:
    secs = get(f"{BASE}/v1/sections")
    if isinstance(secs, dict): secs = secs.get("items", secs)
    with open("C:/tmp/sections_vocab.json", "w", encoding="utf-8") as f:
        json.dump(secs, f, ensure_ascii=False, indent=1)
    print("sections vocab keys:", list(secs[0].keys()) if isinstance(secs, list) and secs else secs if not isinstance(secs, list) else "empty")
except Exception as e:
    print("sections fetch failed:", e)

# 3) route 卡全文
rt = [i for i in items if i["stage"] == "route"]
lines = []
for i in sorted(rt, key=lambda x: x["id"]):
    s = i.get("suggestion") or {}
    lines.append(f"### route #{i['id']} org={i.get('org_key')}/{i.get('project_key') or '-'} kind={s.get('kind')} scope={s.get('scope')}/{s.get('subject')}/{s.get('role')}")
    lines.append(f"  原文: {i.get('raw_content','')}")
    lines.append(f"  内容: {s.get('content','')}")
    lines.append(f"  why: {s.get('why','')}")
    lines.append("")
open("C:/tmp/route_cards.txt", "w", encoding="utf-8").write("\n".join(lines))

print(f"dedup={len(dedup)} section={len(sec)} route={len(rt)}")
