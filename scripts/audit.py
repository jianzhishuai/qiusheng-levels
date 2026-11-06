#!/usr/bin/env python3
"""audit.py <workdir> —— 一屏打印表结构/时区一致性/计数，避免每轮重复摸索。
用法: python3 scripts/audit.py .
"""
import os, re, json, sys, datetime, collections
BJ = datetime.timezone(datetime.timedelta(hours=8))
def snow_bj(ts):
    return datetime.datetime.fromtimestamp(((int(ts) >> 22) + 1288834974657) / 1000, BJ).strftime("%Y-%m-%d %H:%M")
def slice_json(text, n):
    m = re.search(r'const %s\s*=' % n, text)
    if not m: return None
    i = m.end()
    while text[i] in " \t\r\n": i += 1
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
    return json.loads(text[i:j+1])
def main():
    wd = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.join(wd, "index.html")
    if not os.path.exists(path): path = os.path.join(wd, "source_index.html")
    html = open(path, encoding="utf-8").read()
    DATA, T2 = slice_json(html, "DATA"), slice_json(html, "T2")
    T3, T2I = slice_json(html, "T3"), slice_json(html, "T2I")
    print("[source] %s  字节=%d" % (path, len(html.encode())))
    if DATA is None or T2 is None:
        print("[abort] 未能从基线解析出 DATA/T2，基线可能损坏，请检查 source_index.html")
        return 2
    DATA = DATA or []; T2 = T2 or []; T3 = T3 or []; T2I = T2I or []
    print("[counts] DATA=%d T2=%d T3=%d T2I=%d" % (len(DATA), len(T2), len(T3), len(T2I)))
    print("[cols]   DATA 行宽=%s  T2 行宽=%s" % (sorted(collections.Counter(len(r) for r in DATA)), sorted(collections.Counter(len(r) for r in T2))))
    print("[index]  DATA: 品种=1 点位=2 类型=3 状态=4 原文含义=5 推文日期=6 推文链接=7 推文原文=8 图上读数=9")
    print("[index]  T2 : 推文ID=1 日期=2 链接=3 原文=4 配图读数=5")
    bad = []; empty = []; stats = collections.Counter()
    for r in DATA:
        if len(r) < 8: continue
        m = re.search(r'/status/(\d+)', str(r[7]))
        if not m:
            if not str(r[6]).strip(): empty.append((r[0], r[1], r[2], "无链接"))
            continue
        w = snow_bj(m.group(1)); c = str(r[6]).strip()
        stats["一致" if c == w else "不一致"] += 1
        if c != w: bad.append((r[0], c, w))
    for r in T2:
        if len(r) < 4: continue
        m = re.search(r'/status/(\d+)', str(r[3]))
        if not m: continue
        w = snow_bj(m.group(1)); c = str(r[2]).strip()
        stats["一致" if c == w else "不一致"] += 1
        if c != w: bad.append((r[0], c, w))
    print("[tz]     %s   不一致=%d" % (dict(stats), len(bad)))
    for x in bad[:12]: print("         ! %s 显示=%s 应=%s" % x)
    if empty:
        print("[nodate] %d 行无日期且无链接，需人工补来源:" % len(empty))
        for x in empty: print("         - #%s %s %s [%s]" % x)
    m = re.search(r'<div class="sub">(.*?)</div>', html, re.S)
    if m: print("[head]   %s" % m.group(1).strip())
    for probe in ["90000", "1222", "22685", "64.7", "4173.27", "已失效", "已兑现"]:
        if probe in html: print("[probe]  %-10s OK" % probe)
if __name__ == "__main__":
    sys.exit(main() or 0)
