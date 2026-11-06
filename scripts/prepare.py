#!/usr/bin/env python3
"""
自动更新任务的前置准备：从 GitHub 仓库 main 分支拉取「唯一基线」与依赖。
用法: python3 scripts/prepare.py [workdir]

为什么必须有这个脚本：
旧流程把上一轮临时目录里的 source_index.html 当基线，而那个副本可能早于人工订正
（碳酸锂 90000、说明区 17 张等），一跑就会被旧基线覆盖回去。
本脚本让基线、脚本、原图、已知推文 id 全部来自仓库 main，天然单一事实来源。
"""
import os, sys, json, urllib.request, urllib.error, urllib.parse

REPO = os.environ.get("GH_REPO", "jianzhishuai/qiusheng-levels")
BRANCH = "main"
RAW = "https://raw.githubusercontent.com/%s/%s/" % (REPO, BRANCH)
API = "https://api.github.com/repos/%s/contents/" % REPO
FALLBACK_TOKEN = ("outputs/chat/unified_1793846999905_b5c15fa3/turn_1793869362295_2c03005076/"
                  ".credentials/gh_token.txt")


def get_token():
    t = (os.environ.get("GH_TOKEN") or "").strip()
    if t:
        return t
    p = os.path.join(os.environ.get("DEEPTUTOR_WORKSPACE_ROOT", ""), FALLBACK_TOKEN)
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    return ""


TOK = get_token()


def get(url):
    h = {"User-Agent": "deeptutor"}
    if TOK:
        h["Authorization"] = "Bearer " + TOK
    rq = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(rq, timeout=90) as r:
        return r.read()


def listing(path):
    try:
        d = json.loads(get(API + urllib.parse.quote(path)).decode("utf-8"))
        return d if isinstance(d, list) else [d]
    except urllib.error.HTTPError as e:
        print("[warn] 列目录失败 %s: %s" % (path, e))
        return []


def download(path, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    data = get(RAW + urllib.parse.quote(path))
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def main():
    wd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    os.makedirs(os.path.join(wd, "scripts"), exist_ok=True)
    os.makedirs(os.path.join(wd, "images"), exist_ok=True)
    n = download("index.html", os.path.join(wd, "source_index.html"))
    print("[baseline] source_index.html %d 字节（来自仓库 main，唯一基线）" % n)
    n = download("known_ids.txt", os.path.join(wd, "known_ids.txt"))
    print("[state] known_ids.txt %d 字节" % n)
    for it in listing("scripts"):
        if it.get("type") != "file":
            continue
        fn = it["path"].split("/")[-1]
        if not fn.endswith(".py"):
            continue
        download(it["path"], os.path.join(wd, "scripts", fn))
        print("[scripts] %s" % it["path"])
    cnt = 0
    for it in listing("images"):
        if it.get("type") != "file":
            continue
        fn = it["path"].split("/")[-1]
        dest = os.path.join(wd, "images", fn)
        if os.path.exists(dest) and os.path.getsize(dest) == it.get("size"):
            cnt += 1
            continue
        download(it["path"], dest)
        cnt += 1
    print("[images] %d 张原图就位" % cnt)
    print("[ready] 基线已刷新；禁止再使用任何本地残留 index.html 作为基线")


if __name__ == "__main__":
    main()
