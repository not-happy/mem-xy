# -*- coding: utf-8 -*-
"""批量执行待裁决卡判定：读 decisions.json，逐卡调 approve/reject API。
用法: python apply_decisions.py [--dry-run]
"""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8787"
DECISIONS = r"C:/Users/yong.xu/own/mem-xy/assets/adjudication/decisions.json"
RESOLVED_BY = "ai_delegated"


def post(path, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
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


def main():
    dry = "--dry-run" in sys.argv
    with open(DECISIONS, "r", encoding="utf-8") as f:
        decisions = json.load(f)

    ok_appr = fail_appr = skip_appr = 0
    ok_rej = fail_rej = skip_rej = 0
    results = []

    for item in decisions["approve"]:
        pid = item["id"]
        body = {}
        if item.get("section_key"):
            body["section_key"] = item["section_key"]
        if item.get("content"):
            body["content"] = item["content"]
        if dry:
            print(f"APPROVE {pid} dry body={json.dumps(body, ensure_ascii=False)[:80]}")
            continue
        code, resp = post(f"/v1/pending/{pid}/approve?resolved_by={RESOLVED_BY}", body)
        if code == 200:
            ok_appr += 1
            results.append({"id": pid, "op": "approve", "code": code,
                            "job_id": resp.get("job_id")})
            print(f"APPROVE {pid} -> 200 job={resp.get('job_id')}")
        elif code == 409:
            skip_appr += 1
            print(f"APPROVE {pid} -> 409 not_pending (skip)")
        else:
            fail_appr += 1
            print(f"APPROVE {pid} -> {code} {json.dumps(resp, ensure_ascii=False)[:160]}")
        time.sleep(0.05)

    for item in decisions["reject"]:
        pid = item["id"]
        if dry:
            print(f"REJECT  {pid} dry note={item.get('note','')[:60]}")
            continue
        code, resp = post(f"/v1/pending/{pid}/reject", {"note": item.get("note", "")})
        if code == 200:
            ok_rej += 1
            results.append({"id": pid, "op": "reject", "code": code})
            print(f"REJECT  {pid} -> 200")
        elif code == 409:
            skip_rej += 1
            print(f"REJECT  {pid} -> 409 not_pending (skip)")
        else:
            fail_rej += 1
            print(f"REJECT  {pid} -> {code} {json.dumps(resp, ensure_ascii=False)[:160]}")
        time.sleep(0.05)

    print("=" * 40)
    print(f"approve: ok={ok_appr} skip409={skip_appr} fail={fail_appr}")
    print(f"reject : ok={ok_rej} skip409={skip_rej} fail={fail_rej}")
    if not dry:
        with open(r"C:/Users/yong.xu/own/mem-xy/assets/adjudication/apply_results.json",
                  "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
