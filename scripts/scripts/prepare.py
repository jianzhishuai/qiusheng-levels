#!/usr/bin/env python3
"""
自动更新任务的前置准备：从 GitHub 仓库 main 分支拉取「唯一基线」与依赖。
用法: python3 scripts/prepare.py [workdir]

为什么必须有这个脚本：
旧流程把上一轮临时目录里的 source_index.html 当基线，而那个副本可能早于人工订正
（碳酸锂 90000、说明区 17 张等），一跑就会被旧基线覆盖回去。
本脚本让基线、脚本、原图、已知推文 id 全部来自仓库 main，天然单一事实来源。

2026-11-07 修订（限流事故修复）：
旧版用 Contents API 列目录（GET /contents/<dir>）来发现 scripts/ 与 images/ 的文件，
高频触发 403 rate limit。后果有二：
  1) listing() 静默返回 []，导致 **一个脚本都没下载**，prepare.py 表面"成功"实则失效；
  2) images/ 未就位，后续读图步骤拿不到本地图。
现改为三级降级取清单，任何一级失败都自动降级而非中断：
  L1 Git Trees API（一次 recursive 拿全仓库 blob 路径，请求数最少）
  L2 Contents API（带指数退避重试）
  L3 纯本地推导（scripts 用固定清单；images 从基线 HTML 的 T3/T2I 提取文件名）
且 **脚本下载失败会直接退出**，绝不让流水线带着缺失的脚本继续跑。
"""
import os, sys, json, time, re, urllib.request, urllib.error, urllib.parse

REPO = os.environ.get("GH_REPO", "jianzhishuai/qiusheng-levels")
BRANCH = "main"
RAW = "https://raw.githubusercontent.com/%s/%s/" % (REPO, BRANCH)
API = "https://api.github.com/repos/%s/contents/" % REPO
TREES = "https://api.github.com/repos/%s/git/trees/%s?recursive=1" % (REPO, BRANCH)
FALLBACK_TOKEN = ("outputs/chat/unified_1793846999905_b5c15fa3/turn_1793869362295_2c03005076/"
                  ".credentials/gh_token.txt")
# 仓库必备脚本（L3 降级用固定清单）
SCRIPT_NAMES = ["build.py", "fetch.py", "prepare.py", "state.py", "audit.py"]


def get_token():
    t = (os.environ.get("GH_TOKEN") or "").strip()
    if t:
        return t
    p = os.path.join(os.environ.get("DEEPTUTOR_WORKSPACE_ROOT", ""), FALLBACK_TOKEN)
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    return ""


TOK = get_token()


def _headers():
    h = {"User-Agent": "deeptutor", "Accept": "application/vnd.github+json"}
    if TOK:
        h["Authorization"] = "Bearer " + TOK
    return h


def get(url, retries=3):
    last = ""
    for a in range(retries):
        try:
            rq = urllib.request.Request(url, headers=_headers())
            with urllib.request.urlopen(rq, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (403, 429):
                time.sleep(2 * (a + 1))
            elif e.code == 404:
                break
            else:
                time.sleep(1)
        except Exception as e:
            last = str(e)[:80]
            time.sleep(1 + a)
    raise IOError("GET %s 失败: %s" % (url, last))


def download(path, dest, retries=3):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    data = get(RAW + urllib.parse.quote(path), retries=retries)
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


# ---------- 清单获取：三级降级 ----------
def paths_from_trees():
    """L1：一次 recursive 树，拿全仓库 blob 路径。请求数最少，不易限流。"""
    try:
        d = json.loads(get(TREES, retries=3).decode("utf-8"))
        if isinstance(d, dict) and d.get("tree"):
            ps = [t["path"] for t in d["tree"] if t.get("type") == "blob"]
            if d.get("truncated"):
                print("[warn] trees API 返回被截断(truncated)，可能漏文件")
            return ps, "trees-api"
    except Exception as e:
        print("[warn] L1 trees API 失败: %s" % str(e)[:100])
    return [], ""


def paths_from_contents():
    """L2：Contents API 列目录，带指数退避。"""
    out = []
    for d in ("scripts", "images"):
        for a in range(3):
            try:
                data = json.loads(get(API + urllib.parse.quote(d), retries=1).decode("utf-8"))
                items = data if isinstance(data, list) else [data]
                out += [it["path"] for it in items if it.get("type") == "file"]
                break
            except Exception as e:
                print("[warn] L2 列目录 %s 失败(%d): %s" % (d, a + 1, str(e)[:80]))
                time.sleep(2 * (a + 1))
    return out, "contents-api"


def slice_json(text, n):
    """括号配平扫描取 const <n> = [...] 的 JSON 值（正则贪婪会切错）。"""
    m = re.search(r'const %s\s*=' % n, text)
    if not m:
        return None
    i = m.end()
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    d = 0; j = i; ins = False; esc = False
    while j < len(text):
        ch = text[j]
        if ins:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': ins = False
        else:
            if ch == '"': ins = True
            elif ch in "[{": d += 1
            elif ch in "]}":
                d -= 1
                if d == 0: break
        j += 1
    try:
        return json.loads(text[i:j + 1])
    except Exception:
        return None


IMG_RE = re.compile(r'^[A-Za-z0-9_\-]+\.(?:jpg|jpeg|png|gif|webp)$')


def paths_from_baseline(html):
    """L3：纯本地推导，不依赖任何 GitHub API。
    scripts 用固定清单；images 从基线 HTML 精确提取：
      - T3 常量第 0 列（表格③实际引用的图片文件名，最权威）
      - pbs 直链里的 media 文件名（兜底）
    注意：降级模式只能拿到「基线引用到的图」，
    仓库 images/ 下早于基线窗口的历史原图可能取不到（不影响本轮：新推文配图由抓取阶段单独下载）。"""
    out = ["scripts/" + s for s in SCRIPT_NAMES]
    names = set()
    t3 = slice_json(html, "T3")
    if isinstance(t3, list):
        for row in t3:
            if isinstance(row, list) and row and isinstance(row[0], str):
                n = os.path.basename(row[0].strip())
                if IMG_RE.match(n):
                    names.add(n)
    for n in re.findall(r'https://pbs\.twimg\.com/media/([A-Za-z0-9_\-]+\.\w+)', html):
        if IMG_RE.match(n):
            names.add(n)
    for n in sorted(names):
        out.append("images/" + n)
    return out, "baseline-derived"


def collect_paths(html):
    for fn in (paths_from_trees, paths_from_contents):
        ps, src = fn()
        if ps:
            print("[list] 清单来源: %s (%d 个文件)" % (src, len(ps)))
            return ps, src
    ps, src = paths_from_baseline(html)
    print("[list] 清单来源: %s (降级, %d 个条目)" % (src, len(ps)))
    return ps, src


def main():
    wd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    os.makedirs(os.path.join(wd, "scripts"), exist_ok=True)
    os.makedirs(os.path.join(wd, "images"), exist_ok=True)
    n = download("index.html", os.path.join(wd, "source_index.html"))
    print("[baseline] source_index.html %d 字节（来自仓库 main，唯一基线）" % n)
    html = open(os.path.join(wd, "source_index.html"), encoding="utf-8").read()
    n = download("known_ids.txt", os.path.join(wd, "known_ids.txt"))
    print("[state] known_ids.txt %d 字节" % n)

    paths, _src = collect_paths(html)
    scripts = sorted({p for p in paths if p.startswith("scripts/") and p.endswith(".py")})
    images = sorted({p for p in paths if p.startswith("images/") and not p.endswith("/")})
    # L3 固定清单兜底：仓库里至少有这 5 个脚本
    for s in SCRIPT_NAMES:
        p = "scripts/" + s
        if p not in scripts:
            scripts.append(p)
    print("[scripts] 待取 %d 个: %s" % (len(scripts), ", ".join(os.path.basename(s) for s in scripts)))

    # 脚本必须全部就位，失败即中止（下游 fetch/build 都依赖它）
    failed = []
    for p in scripts:
        try:
            sz = download(p, os.path.join(wd, p))
            print("[scripts] %-26s %7d 字节" % (p, sz))
        except Exception as e:
            failed.append(p)
            print("[scripts] !! %s 失败: %s" % (p, str(e)[:90]))
    if failed:
        print("[abort] 以下脚本未能下载，流水线无法继续: %s" % ", ".join(failed))
        return 3

    ok = 0; bad = 0
    for p in images:
        dest = os.path.join(wd, p)
        try:
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                ok += 1
                continue
            download(p, dest)
            ok += 1
        except Exception as e:
            bad += 1
            print("[images] !! %s 失败: %s" % (os.path.basename(p), str(e)[:70]))
    print("[images] %d 张原图就位%s" % (ok, ("，%d 张失败（图片缺失不阻断本轮）" % bad) if bad else ""))
    print("[ready] 基线已刷新；禁止再使用任何本地残留 index.html 作为基线")
    return 0


if __name__ == "__main__":
    sys.exit(main())
