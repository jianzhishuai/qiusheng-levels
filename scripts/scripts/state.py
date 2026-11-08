#!/usr/bin/env python3
"""维护已收录推文 id 基线。"""
import json, os, sys
def load(path):
    if os.path.exists(path):
        return set(x.strip() for x in open(path) if x.strip())
    return set()
def save(path, ids):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(ids, key=lambda x: int(x), reverse=True)) + "\n")
if __name__ == "__main__":
    cmd = sys.argv[1]; p = sys.argv[2]
    if cmd == "add":
        ids = load(p)
        for i in sys.argv[3:]: ids.add(i)
        save(p, ids); print("known=%d" % len(ids))
    elif cmd == "init-from-html":
        html = open(sys.argv[3], encoding="utf-8").read()
        ids = set(re.findall(r'x\.com/Hoyooyoo/status/(\d+)', html))
        save(p, ids); print("init known=%d" % len(ids))
