# -*- coding: utf-8 -*-
"""打印 living_docs.json 全文，供重复对照判定。"""
import json
import sys

sys.stdout.reconfigure(encoding='utf-8')
d = json.load(open('C:/tmp/living_docs.json', encoding='utf-8'))
for org, items in d.items():
    print('=' * 20, org)
    for it in items:
        print(f"--- [{it.get('section_key')}] proj={it.get('project_key')} auth={it.get('authority')} id={it.get('id')}")
        print(it.get('content', ''))
        print()
