#!/usr/bin/env python3
"""抓取 @Hoyooyoo 推文，输出尚未收录的推文（正文/时间/图片直链）。

修复记录（2026-11-06）：
1) get_any() 只返回首个成功源 -> /with_replies 回复流从未被抓取，
   新推文长期漏更（表格停在 11-06 的根因）。改为 get_all() 抓全组并合并。
2) 分页 cursor 取页面首个 cursor= 链接，可能不是"加载更多"。改为优先取
   show-more 区块内的链接。
3) 时间统一按北京时间（UTC+8）输出。
"""
import urllib.request, urllib.error, urllib.parse, re, json, os, sys, datetime, time

BUDGET = float(os.environ.get('FETCH_BUDGET', '180'))
MAX_PAGES = int(os.environ.get('FETCH_PAGES', '6'))
START = time.time()
def budget_left():
    return BUDGET - (time.time() - START)

# 2026-11-12 实测：不同 nitter 镜像对 User-Agent 的行为相反。
#   nitter.cf            -> 浏览器 UA 返回完整时间线；curl UA 也可用（曾 429）
#   nitter.tiekoetter.com-> 浏览器 UA 返回空壳(8KB/0 id)；curl/8.4.0 才返回完整内容(59KB/34 id)
# 因此 UA 必须【按源】指定，不能全局共用，否则备用源会被误判为"无有效内容"。
BROWSER_UA = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
CURL_UA    = {"User-Agent":"curl/8.4.0"}
UA = BROWSER_UA
SOURCES = ["https://nitter.cf/Hoyooyoo", "https://nitter.cf/Hoyooyoo/with_replies",
           "https://nitter.poast.org/Hoyooyoo", "https://nitter.poast.org/Hoyooyoo/with_replies"]
RETRIES = 4
BACKOFF = 4

def get(url, timeout=45, ua=None):
    last = None
    headers = ua or BROWSER_UA
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return f.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(BACKOFF * (attempt + 1))
    raise last

def get_all(sources, timeout=45):
    """抓取组内【所有】数据源，返回 [(url, html), ...]；全部失败才抛错。"""
    pages, last = [], None
    for u, ua in sources:
        if budget_left() <= 10:
            print("[stop] 预算不足，跳过 %s" % u, file=sys.stderr); break
        try:
            html = get(u, timeout, ua)
            if html and "/Hoyooyoo/status/" in html:
                pages.append((u, html, ua))
                print("[ok] %s (%d 字节)" % (u, len(html)), file=sys.stderr)
            else:
                print("[warn] %s 无有效内容" % u, file=sys.stderr)
        except Exception as e:
            last = e
            print("[warn] %s -> %s" % (u, e), file=sys.stderr)
    if not pages:
        if last: raise last
        raise RuntimeError("所有数据源均无有效内容")
    return pages

def clean(x):
    x = re.sub(r'<br\s*/?>', '\n', x)
    x = re.sub(r'<[^>]+>', '', x)
    for a, b in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&#39;","'"),("&quot;",'"'),("&nbsp;"," ")]:
        x = x.replace(a, b)
    return re.sub(r'\n{3,}', '\n\n', x).strip()

BJ = datetime.timezone(datetime.timedelta(hours=8))

def snow_utc(ts):
    """推文时间统一按北京时间（UTC+8）输出，与表格口径一致"""
    return datetime.datetime.fromtimestamp(((ts >> 22) + 1288834974657)/1000,
                                          BJ).strftime("%Y-%m-%d %H:%M")

def next_cursor(html, base):
    """优先取 show-more 区块内的 cursor 链接，否则退回页面首个 cursor 链接。"""
    cands = []
    m = re.search(r'<div class="show-more".*?(?=<div class="timeline-item|</body>)', html, re.S)
    scope = m.group(0) if m else html
    for mm in re.finditer(r'href="([^"]*cursor=[^"]+)"', scope):
        cands.append(mm.group(1))
    for mm in re.finditer(r'href="([^"]*cursor=[^"]+)"', html):
        if mm.group(1) not in cands:
            cands.append(mm.group(1))
    if not cands:
        return None
    return urllib.parse.urljoin(base, cands[0].replace("&amp;", "&"))

ALLT = {}

# 判据说明（修正 2026-11-08）：
#   旧实现用文本黑名单 UI_NOISE 丢弃正文等于"搜索我的推文"的区块，理由是"nitter 搜索框 UI"。
#   但实测 nitter 页面中 search-form 元素数为 0，而 status 2118878555438448832 经
#   fxtwitter 核实 author=Hoyooyoo、replying_to=yidiyuehan —— 它是作者本人的真实回复推文，
#   文本黑名单会误杀真实推文（并永久丢失其正文）。
#   现改为作者归属判据：正文必须落在本区块内某个 /Hoyooyoo/status/<id> 之后才收录，
#   从根本上排除他人推文与 nitter 界面元素，不再依赖具体文案。
UI_NOISE = set()          # 保留常量名以兼容外部引用，判定不再依赖文本


def parse(html, seen=None):
    """正文按 timeline-item 抽取；配图按 pbs 链接在全文中的位置，
    归属到其前方最近的 /<账号>/status/<id>（Hoyooyoo 之外的账号视为边界）。

    seen: 已见过的推文 id 集合。传入时，本页已见过的推文仍会被解析并
    补全图片（正文可能先于图片出现），但不会被计入"本页新增"。
    返回 (本页推文字典, 本页新增 id 集合)。
    """
    out = {}
    for b in re.split(r'<div class="timeline-item', html):
        tm = re.search(r'title="([^"]+ UTC)"', b)
        mm = re.search(r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>', b, re.S)
        if not mm:
            continue
        txt = clean(mm.group(1))
        if txt in UI_NOISE:
            continue   # 兼容位：当前为空集，实际判定走作者归属
        # 作者归属：正文必须出现在本区块内某个 /Hoyooyoo/status/<id> 之后。
        # 取正文位置之前最近的 Hoyooyoo status 链接作为归属 id；
        # 若正文之前没有任何 Hoyooyoo status 链接，则该区块不是作者推文，丢弃。
        head = b[:mm.start()]
        ids = re.findall(r'/Hoyooyoo/status/(\d+)', head)
        if not ids:
            continue
        tid = ids[-1]
        raw_dt = tm.group(1) if tm else ""
        try:
            _dt = datetime.datetime.strptime(raw_dt, "%b %d, %Y · %I:%M %p UTC").replace(tzinfo=datetime.timezone.utc)
            dt = _dt.astimezone(BJ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            dt = snow_utc(int(tid))
        if tid in out and out[tid]["text"]:
            continue
        out[tid] = {"id": tid, "date_utc": dt, "text": txt, "images": [],
                    "url": "https://x.com/Hoyooyoo/status/" + tid}
    marks = []
    for m in re.finditer(r'href="/([A-Za-z0-9_]+)/status/(\d+)', html):
        marks.append((m.start(), "S", m.group(1), m.group(2)))
    for m in re.finditer(r'pbs\.twimg\.com/media/([A-Za-z0-9_\-]+\.\w+)', html):
        marks.append((m.start(), "M", None, m.group(1)))
    marks.sort()
    cur = None
    for pos, kind, acct, val in marks:
        if kind == "S":
            cur = val if acct == "Hoyooyoo" else None
        else:
            if cur and val not in out.get(cur, {}).get("images", []):
                out.setdefault(cur, {"id": cur, "date_utc": snow_utc(int(cur)),
                                     "text": "", "images": [],
                                     "url": "https://x.com/Hoyooyoo/status/" + cur})
                out[cur]["images"].append(val)
    # 跨页补图：推文正文可能在第1页、图片在后续页才出现，回填到已有条目
    if seen and ALLT:
        for tid, val in out.items():
            prev = ALLT.get(tid)
            if prev is not None and val.get("images"):
                for im in val["images"]:
                    if im not in prev["images"]:
                        prev["images"].append(im)
    fresh = set(out.keys()) - (seen if seen is not None else set())
    return out, fresh

def main():
    known = set()
    kf = sys.argv[1] if len(sys.argv) > 1 else "known_ids.txt"
    if os.path.exists(kf):
        known = set(x.strip() for x in open(kf) if x.strip())
    allt = {}
    global ALLT
    ALLT = allt
    dump_all = "--all" in sys.argv
    for group in [[("https://nitter.cf/Hoyooyoo", BROWSER_UA),
                   ("https://nitter.cf/Hoyooyoo/with_replies", BROWSER_UA)],
                  [("https://nitter.tiekoetter.com/Hoyooyoo", CURL_UA),
                   ("https://nitter.tiekoetter.com/Hoyooyoo/with_replies", CURL_UA)],
                  [("https://nitter.poast.org/Hoyooyoo", BROWSER_UA),
                   ("https://nitter.poast.org/Hoyooyoo/with_replies", BROWSER_UA)]]:
        try:
            pages = get_all(group)
        except Exception as e:
            print("[warn] 源组失败 %s -> %s" % (group[0][0], e), file=sys.stderr)
            continue
        for base, h0, gua in pages:
            h = h0
            for pg in range(MAX_PAGES):
                if pg:
                    nxt = next_cursor(h, base)
                    if not nxt: break
                    if budget_left() <= 12:
                        print("[stop] 预算耗尽，停在第%d页" % pg, file=sys.stderr); break
                    try: h = get(nxt, 30, gua)
                    except Exception as e:
                        print("[warn] 第%d页失败 %s" % (pg+1, e), file=sys.stderr); break
                new, fresh = parse(h, set(allt.keys()))
                before = len(allt)
                allt.update(new)
                print("[page %d] %s 累计 %d 条 (本页新增 %d)" % (pg+1, base, len(allt), len(fresh)), file=sys.stderr)
                # 仅当本页【完全没有新推文】且已翻过至少2页才停止，
                # 避免单页重复导致提前退出（实测 nitter 每页都有新内容）
                if pg >= 1 and not fresh and len(allt) == before:
                    print("[stop] 本页无新增且无新图，停止翻页", file=sys.stderr)
                    break
                if budget_left() <= 0: break
    if not allt:
        print("[fatal] 所有数据源均失败，本次跳过（不破坏已有内容）", file=sys.stderr)
        json.dump({"all_count": 0, "new_count": 0, "new": [], "error": "all_sources_failed"},
                  open(sys.argv[2] if len(sys.argv) > 2 else "new_tweets.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        sys.exit(2)
    new = {k: v for k, v in allt.items() if k not in known} if not dump_all else dict(allt)
    new = dict(sorted(new.items(), key=lambda kv: int(kv[0]), reverse=True))
    outp = sys.argv[2] if len(sys.argv) > 2 else "new_tweets.json"
    json.dump({"all_count": len(allt), "new_count": len(new), "new": list(new.values())},
              open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("total=%d known=%d new=%d -> %s" % (len(allt), len(known), len(new), outp))
    for v in new.values():
        print("  %s %s %s | %s" % (v["id"], v["date_utc"], ",".join(v["images"]) or "-",
                                   " ".join(v["text"].split())[:70]))

if __name__ == "__main__":
    main()
