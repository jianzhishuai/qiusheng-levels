#!/usr/bin/env python3
"""抓取 @Hoyooyoo 推文，输出尚未收录的推文（正文/时间/图片直链）。"""
import urllib.request, urllib.error, urllib.parse, re, json, os, sys, datetime, time

BUDGET = float(os.environ.get('FETCH_BUDGET', '180'))  # 全局时限（秒）
MAX_PAGES = int(os.environ.get('FETCH_PAGES', '6'))      # 每个源的翻页深度
START = time.time()
def budget_left():
    return BUDGET - (time.time() - START)

UA = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
SOURCES = ["https://nitter.cf/Hoyooyoo", "https://nitter.cf/Hoyooyoo/with_replies",
           "https://nitter.poast.org/Hoyooyoo", "https://nitter.poast.org/Hoyooyoo/with_replies"]
# 同一主源的重试次数与退避（nitter 偶发 SSL EOF，重试即可恢复）
RETRIES = 4
BACKOFF = 4

def get(url, timeout=45):
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return f.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(BACKOFF * (attempt + 1))
    raise last

def get_any(sources, timeout=45):
    """逐个尝试数据源，第一个成功的即返回；全部失败抛最后一个异常。"""
    last = None
    for u in sources:
        try:
            html = get(u, timeout)
            if html and "/Hoyooyoo/status/" in html:
                return html, u
        except Exception as e:
            last = e
            print("[warn] %s -> %s" % (u, e), file=sys.stderr)
    if last: raise last
    raise RuntimeError("所有数据源均无有效内容")

def clean(x):
    x = re.sub(r'<br\s*/?>', '\n', x)
    x = re.sub(r'<[^>]+>', '', x)
    for a, b in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&#39;","'"),("&quot;",'"'),("&nbsp;"," ")]:
        x = x.replace(a, b)
    return re.sub(r'\n{3,}', '\n\n', x).strip()

def snow_utc(ts):
    return datetime.datetime.fromtimestamp(((ts >> 22) + 1288834974657)/1000,
                                          datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")

def parse(html):
    """正文按 timeline-item 抽取；配图按 pbs 链接在全文中的位置，
    归属到其前方最近的 /<账号>/status/<id>（Hoyooyoo 之外的账号视为边界，
    避免把引用推文的图错算到本人推文上）。"""
    # ---- 正文 ----
    out = {}
    for b in re.split(r'<div class="timeline-item', html):
        tm = re.search(r'title="([^"]+ UTC)"', b)
        mm = re.search(r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>', b, re.S)
        if not mm:
            continue
        txt = clean(mm.group(1))
        ids = re.findall(r'/Hoyooyoo/status/(\d+)', b)
        if not ids:
            continue
        tid = ids[0]
        raw_dt = tm.group(1) if tm else ""
        try:
            dt = datetime.datetime.strptime(raw_dt, "%b %d, %Y · %I:%M %p UTC").strftime("%Y-%m-%d %H:%M")
        except Exception:
            dt = snow_utc(int(tid))
        if tid in out and out[tid]["text"]:
            continue
        out[tid] = {"id": tid, "date_utc": dt, "text": txt, "images": [],
                    "url": "https://x.com/Hoyooyoo/status/" + tid}
    # ---- 配图归属 ----
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
    return out

def main():
    known = set()
    kf = sys.argv[1] if len(sys.argv) > 1 else "known_ids.txt"
    if os.path.exists(kf):
        known = set(x.strip() for x in open(kf) if x.strip())
    allt = {}
    ok_src = 0
    dump_all = "--all" in sys.argv
    for group in [["https://nitter.cf/Hoyooyoo", "https://nitter.cf/Hoyooyoo/with_replies"],
                  ["https://nitter.poast.org/Hoyooyoo", "https://nitter.poast.org/Hoyooyoo/with_replies"]]:
        try:
            h, u = get_any(group)
            base = u
            for pg in range(MAX_PAGES):
                if pg:
                    cur = allt
                    m = re.search(r'href="([^"]*cursor=[^"]+)"', h)
                    if not m: break
                    nxt = urllib.parse.urljoin(base, m.group(1).replace("&amp;", "&"))
                    if budget_left() <= 12: print("[stop] 预算耗尽，停在第%d页"%pg, file=sys.stderr); break
                    try: h = get(nxt, 30)
                    except Exception as e:
                        print("[warn] 第%d页失败 %s"%(pg+1,e), file=sys.stderr); break
                new = parse(h)
                before = len(allt)
                allt.update(new)
                print("[page %d] %s 累计 %d 条 (本页 %d)" % (pg+1, base, len(allt), len(allt)-before), file=sys.stderr)
                if len(allt) - before == 0: break
            ok_src += 1
            print("[ok] %s" % base, file=sys.stderr)
        except Exception as e:
            print("[warn] 源组失败 %s -> %s" % (group[0], e), file=sys.stderr)
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
