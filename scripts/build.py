#!/usr/bin/env python3
"""
把新的点位条目（new_levels.json）并入 index.html，重建 CSV/TSV/XLSX，并推送到 GitHub。
用法: build.py <workdir> [--push] [--rebuild]

2026-11-06 重写要点（修复「四张表全空白」事故）：
1) 数据块改为**原子重建**：不再用 replace_decl 对 4 个 const 声明做逐个字符串切片替换
   （旧写法在 `const DATA=` 后漏掉分号，产出 `]] const T3=`，JS 直接 SyntaxError，
    整块脚本不执行 → 四张表同时空白）。现在一次性把 DATA/T2/T3/T2I 重新序列化成
   固定顺序、每条都以分号结尾的完整块，整体替换。
2) **推送前强制语法闸门**：对每个 <script> 块跑 `node --check`，任何一块报错就中止，
   绝不让坏产物上线。若环境无 node，则退化为括号/引号配平自检。
3) 静态文字里的计数（条点位 / 条推文原文 / 张原图 / 共 N 张）全部由数据实时回写，
   不再留下与实际不符的旧数字。
"""
import os, re, json, csv, base64, shutil, datetime, urllib.request, urllib.error, urllib.parse, sys, subprocess, tempfile, time

DECL_ORDER = ("DATA", "T2", "T3", "T2I")

# ---- 时区口径：全表推文日期统一按北京时间(UTC+8) ----
BJ = datetime.timezone(datetime.timedelta(hours=8))


def snow_bj(ts):
    """由推文 snowflake ID 推出北京时间字符串"""
    return datetime.datetime.fromtimestamp(((int(ts) >> 22) + 1288834974657) / 1000,
                                           BJ).strftime("%Y-%m-%d %H:%M")


def norm_date(link, cur):
    """按推文链接里的 snowflake ID 重算北京时间；无链接则保留原值"""
    m = re.search(r'/status/(\d+)', str(link))
    if not m:
        return cur
    try:
        return snow_bj(m.group(1))
    except Exception:
        return cur



def slice_json(text, start):
    """从 text[start] 起截取配平的 JSON 值（[] 或 {}），正确跳过字符串内的括号"""
    depth = 0; i = start; instr = False; esc = False
    while i < len(text):
        ch = text[i]
        if instr:
            if esc: esc = False
            elif ch == '\\': esc = True
            elif ch == '"': instr = False
        else:
            if ch == '"': instr = True
            elif ch in '[{': depth += 1
            elif ch in ']}':
                depth -= 1
                if depth == 0: return text[start:i+1], i+1
        i += 1
    raise ValueError("JSON 未闭合")


def api(token, path, method="GET", body=None, retries=4):
    """path 可为 api.github.com 下的相对路径，也可为完整 URL。
    偶发 SSL EOF / 连接重置时重试；仍失败则抛出，由调用方决定。"""
    url = path if path.startswith("http") else "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "deeptutor", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as f:
            return f.status, f.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        if retries > 1:
            time.sleep(2 * (retries - 1))
            return api(token, path, method, body, retries - 1)
        return None, "EXC %s %s" % (type(e).__name__, e)


def get_sha(token, repo, path):
    st, raw = api(token, "/repos/%s/contents/%s" % (repo, urllib.parse.quote(path)))
    return json.loads(raw).get("sha") if st == 200 else None


def put(token, repo, path, filepath, branch="main", msg=None):
    with open(filepath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    body = {"message": msg or ("Update %s" % path), "content": b64, "branch": branch}
    sha = get_sha(token, repo, path)
    if sha: body["sha"] = sha
    st, raw = api(token, "/repos/%s/contents/%s" % (repo, urllib.parse.quote(path)), "PUT", body)
    return st, raw


# ---------- 语法闸门 ----------
def _balance_ok(src):
    """无 node 时的兜底自检：括号 / 引号配平，且不含裸的 'const' 紧跟标识符"""
    depth = 0; instr = False; esc = False
    for ch in src:
        if instr:
            if esc: esc = False
            elif ch == '\\': esc = True
            elif ch == '"': instr = False
        else:
            if ch == '"': instr = True
            elif ch in '[{(': depth += 1
            elif ch in ']})': depth -= 1
            if depth < 0: return False
    return depth == 0 and not instr


def check_scripts(html):
    """对页面里每个内联 <script> 块做语法校验；有 node 用 node --check，无 node 用配平自检。
    返回 (ok, 报告文本)"""
    blocks = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
    if not blocks:
        return False, "页面里找不到内联 <script> 块"
    node = shutil.which("node")
    reports = []
    ok = True
    for i, b in enumerate(blocks):
        if node:
            fd, p = tempfile.mkstemp(suffix=".js"); os.close(fd)
            try:
                with open(p, "w", encoding="utf-8") as f: f.write(b)
                r = subprocess.run([node, "--check", p], capture_output=True, text=True)
                if r.returncode != 0:
                    ok = False
                    err = [l for l in r.stderr.split("\n") if "Error" in l]
                    reports.append("块#%d 语法错误: %s" % (i, err[0].strip() if err else r.stderr[:200]))
                else:
                    reports.append("块#%d OK (%d 字符)" % (i, len(b)))
            finally:
                os.unlink(p)
        else:
            good = _balance_ok(b)
            ok &= good
            reports.append("块#%d %s (无 node，配平自检)" % (i, "OK" if good else "配平失败"))
    return ok, "\n".join(reports)


# ---------- 数据块原子重建 ----------
def parse_block(html):
    """解析页面里的数据块，返回 (script_start, script_end, {name: value})"""
    m = re.search(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
    if not m: raise SystemExit("找不到内联 <script> 块")
    blk = m.group(1)
    vals = {}
    for name in DECL_ORDER:
        j = blk.find("const %s=" % name)
        if j < 0: raise SystemExit("找不到 const %s=" % name)
        op = "{" if name == "T2I" else "["
        s = blk.find(op, j)
        seg, _ = slice_json(blk, s)
        vals[name] = json.loads(seg)
    return m.start(), m.end(), vals


def render_block(vals):
    """把 4 个声明一次性重新序列化成固定顺序、每条以分号结尾的完整块"""
    parts = []
    for name in DECL_ORDER:
        parts.append("const %s=%s;" % (name, json.dumps(vals[name], ensure_ascii=False)))
    return "\n".join(parts) + "\n"


def rebuild_block(html, vals):
    m = re.search(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
    head = html[:m.start()]
    tail = html[m.end():]
    open_tag = re.match(r'<script[^>]*>', m.group(0)).group(0)
    return head + open_tag + render_block(vals) + "</script>" + tail


# ---------- 导出 ----------
def build_csv(DATA, path):
    head = ["序号","品种","点位","类型","状态","原文含义","推文日期","推文链接","推文原文","图上读数"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\ufeff")
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(head)
        for r in DATA: w.writerow([r[0]] + list(r[1:]))

def build_tsv(DATA, path):
    head = ["序号","品种","点位","类型","状态","原文含义","推文日期","推文链接","推文原文","图上读数"]
    def cl(v): return str(v or "").replace("\t"," ").replace("\n"," ")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t".join(head) + "\n")
        for r in DATA: f.write("\t".join(cl(x) for x in [r[0]] + list(r[1:])) + "\n")

def build_xlsx(DATA, T3, imdir, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.drawing.image import Image as XLImage
    wb = Workbook()
    hdr = Font(bold=True, color="FFFFFF"); fill = PatternFill("solid", fgColor="2F5597")
    def style(ws, widths):
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[chr(64+i)].width = w
        for c in ws[1]:
            c.font = hdr; c.fill = fill; c.alignment = Alignment(vertical="center")
        ws.freeze_panes = "A2"
    ws = wb.active; ws.title = "①关键点位总表"
    ws.append(["序号","品种","点位","类型","状态","原文含义","推文日期","推文链接","推文原文","图上读数/交叉验证"])
    for r in DATA: ws.append([r[0]] + list(r[1:]))
    style(ws, [6,14,12,14,13,32,18,40,66,46])
    for row in ws.iter_rows(min_row=2):
        for c in row: c.alignment = Alignment(vertical="top", wrap_text=True)
    ws2 = wb.create_sheet("②推文原文与配图")
    ws2.append(["#","推文ID","日期","链接","状态","推文原文","配图读数/交叉验证"])
    seen = {}
    for r in DATA:
        link = r[7]
        if link in seen: continue
        seen[link] = 1
        img = "（无配图）"
        for t in T3:
            if t[1] == link.rsplit("/",1)[-1]: img = t[2]
        ws2.append([len(seen), link.rsplit("/",1)[-1], r[6], link, r[4], r[8], img])
    style(ws2, [5,22,18,42,13,72,46])
    for row in ws2.iter_rows(min_row=2):
        for c in row: c.alignment = Alignment(vertical="top", wrap_text=True)
    ws3 = wb.create_sheet("③图片逐张解读")
    ws3.append(["文件名","推文ID","图中标注读数","与正文交叉验证","原图直链"])
    for t in T3: ws3.append(list(t))
    style(ws3, [30,22,60,60,50])
    for row in ws3.iter_rows(min_row=2):
        for c in row: c.alignment = Alignment(vertical="top", wrap_text=True)
    if os.path.isdir(imdir):
        ws4 = wb.create_sheet("④原图")
        ws4.append(["推文ID","原图"])
        for t in T3:
            fp = os.path.join(imdir, t[0])
            if not os.path.exists(fp): continue
            ws4.append([t[1], ""])
            try:
                im = XLImage(fp); im.width = min(im.width, 900); im.height = im.height * (im.width / max(im.width,1)) if False else im.height
                rr = ws4.max_row
                ws4.add_image(im, "B%d" % rr)
            except Exception as e:
                print("[warn] embed %s: %s" % (t[0], e))
    wb.save(path)


def sync_counts(html, DATA, T2, T3):
    """把所有静态计数改成与数据一致，杜绝「说明区写 14 张、实际 17 张」这类漂移"""
    n_pts = len(DATA); n_tw = len(T2) if T2 else len(set(r[7] for r in DATA)); n_img = len(T3)
    sub = re.search(r'<div class="sub">(.*?)</div>', html, re.S)
    if sub:
        s = sub.group(1)
        s = re.sub(r"\d+ 条点位", "%d 条点位" % n_pts, s)
        s = re.sub(r"\d+ 条推文原文", "%d 条推文原文" % n_tw, s)
        # 数据窗口：按 DATA 中实际推文日期的最早/最晚动态计算
        ds = [str(r[6]) for r in DATA if len(r) > 6 and re.match(r"\d{4}-\d{2}-\d{2}", str(r[6]))]
        if ds:
            s = re.sub(r"数据窗口\s*\d{4}-\d{2}-\d{2}\s*~\s*\d{4}-\d{2}-\d{2}",
                       "数据窗口 %s ~ %s" % (min(ds)[:10], max(ds)[:10]), s)
        # 基线时刻：本次构建的北京时间
        s = re.sub(r"基线时刻\s*\d{4}-\d{2}-\d{2}",
                   "基线时刻 %s" % datetime.datetime.now(BJ).strftime("%Y-%m-%d"), s)
        s = re.sub(r"\d+ 张原图", "%d 张原图" % n_img, s)
        html = html.replace(sub.group(0), '<div class="sub">%s</div>' % s)
    html = re.sub(r'共\s*\d+\s*张', '共 %d 张' % n_img, html)
    html = re.sub(r'全部原图（\s*\d+\s*张）', '全部原图（%d 张）' % n_img, html)
    return html


def main():
    wd = sys.argv[1]; push = "--push" in sys.argv
    token = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GH_REPO", "jianzhishuai/qiusheng-levels")
    src = os.path.join(wd, "source_index.html")
    newf = os.path.join(wd, "new_levels.json")
    nl = {"rows": [], "images": []}
    if os.path.exists(newf):
        nl = json.load(open(newf, encoding="utf-8"))
    if not nl.get("rows") and not nl.get("images") and "--rebuild" not in sys.argv:
        print("new_levels.json 无新内容 -> nothing to do"); return
    html = open(src, encoding="utf-8").read()
    try:
        _, _, vals = parse_block(html)
    except SystemExit:
        raise
    except Exception as e:
        print("[ABORT] 无法解析基线数据块: %s: %s" % (type(e).__name__, e))
        sys.exit(2)
    DATA, T2, T3, T2I = vals["DATA"], vals["T2"], vals["T3"], vals["T2I"]
    # ---- 时区统一：基线里所有推文日期按北京时间(UTC+8)重算 ----
    _tz = 0
    for _r in DATA:
        if len(_r) > 7:
            _nd = norm_date(_r[7], _r[6])
            if _nd != _r[6]: _r[6] = _nd; _tz += 1
    for _r in T2:
        if len(_r) > 3:
            _nd = norm_date(_r[3], _r[2])
            if _nd != _r[2]: _r[2] = _nd; _tz += 1
    print("[tz] 基线日期按北京时间(UTC+8)重算，修正 %d 处" % _tz)
    # rows 按推文链接去重，避免同一推文被重复收录
    # 去重键必须是 (链接,品种,点位,类型) 四元组：同一条推文常给出多个点位，
    # 也常对同一标的同一价位给出多个不同判断（例：同一推文里"反弹衰竭做空"与
    # "V型反转概率极低"两点位同为未给具体点位、仅类型不同），漏掉类型会误伤后者。
    # （例：比特币那条同时给 79000/81000/80000），只用链接会把后续点位全部挤掉。
    seen_keys = set()
    for r in DATA:
        if len(r) > 7:
            seen_keys.add((str(r[7]), str(r[1]), str(r[2]), str(r[3])))
    n_new = 0
    for row in nl.get("rows", []):
        # row = [品种,点位,类型,状态,原文含义,推文日期,推文链接,推文原文,图上读数]
        link = str(row[6]) if len(row) > 6 else ""
        key = (link, str(row[0]), str(row[1]), str(row[2]))
        if link and key in seen_keys:
            print("[skip] 该点位已收录，跳过: %s %s %s" % (link, row[0], row[1])); continue
        if len(row) > 6:
            row[5] = norm_date(row[6], row[5] if len(row) > 5 else "")
        DATA.append([str(len(DATA) + 1)] + list(row))
        seen_keys.add(key); n_new += 1
    print("[rows] 新增 %d 行点位 -> DATA=%d" % (n_new, len(DATA)))
    for t in nl.get("images", []):
        T3.append(list(t))
    for t in nl.get("images", []):
        if len(t) >= 5 and t[4] and t[1]:
            T2I[str(t[1])] = t[4]
    # ---- 合并推文原文表 T2：按推文ID去重 ----
    if nl.get("t2"):
        have = {str(r[1]) for r in T2}
        added = 0
        for e in nl["t2"]:
            if len(e) < 4: continue
            tid = str(e[0]); date = e[1]; link = e[2]; text = e[3]
            date = norm_date(link, date)
            if tid in have: continue
            T2.append([str(len(T2) + 1), tid, date, link, text, T2I.get(tid, "")])
            have.add(tid); added += 1
        if added: print("[t2] 新增 %d 条推文原文 -> T2=%d" % (added, len(T2)))
    # ---- 状态更新：按 DATA 序号改写「状态」列(index 4) ----
    if nl.get("updates"):
        byseq = {str(r[0]): r for r in DATA}
        hit = 0
        for u in nl["updates"]:
            seq = str(u[0]); newst = u[1]
            row = byseq.get(seq)
            if not row:
                print("[warn] updates 序号 %s 不在 DATA 中，跳过" % seq); continue
            if row[4] != newst:
                print("[updates] #%s %s：%s -> %s" % (seq, row[1], row[4], newst))
                row[4] = newst
            hit += 1
        print("[updates] 应用 %d 处状态更新" % hit)
    # 原子重建整块
    html = rebuild_block(html, {"DATA": DATA, "T2": T2, "T3": T3, "T2I": T2I})
    html = sync_counts(html, DATA, T2, T3)
    # ---- 语法闸门：不过就不落盘、不推送 ----
    ok, report = check_scripts(html)
    print("[syntax-check]\n" + report)
    if not ok:
        print("[ABORT] 生成的页面脚本存在语法错误，已中止构建，未写文件、未推送")
        sys.exit(2)
    out = os.path.join(wd, "index.html")
    open(out, "w", encoding="utf-8").write(html)
    build_csv(DATA, os.path.join(wd, "秋生trader_关键点位.csv"))
    build_tsv(DATA, os.path.join(wd, "秋生trader_关键点位.tsv"))
    build_xlsx(DATA, T3, os.path.join(wd, "images"), os.path.join(wd, "秋生trader_关键点位与图文溯源.xlsx"))
    print("DATA=%d T2=%d T3=%d -> index.html %d bytes" % (len(DATA), len(T2), len(T3), len(html.encode())))
    if push:
        if not token:
            print("[skip] 未设 GH_TOKEN，跳过推送"); return
        files = ["index.html", "秋生trader_关键点位.csv", "秋生trader_关键点位.tsv", "秋生trader_关键点位与图文溯源.xlsx"]
        extra = os.environ.get("GH_EXTRA_FILES", "")
        if extra:
            for f in extra.split(","):
                f = f.strip()
                if f and os.path.exists(os.path.join(wd, f)): files.append(f)
        for fn in files:
            st, raw = put(token, repo, fn, os.path.join(wd, fn), msg="Update %s" % fn)
            print("  push %-40s %s" % (fn, st))
            if st not in (200, 201):
                print("[ABORT] 推送失败: %s" % raw[:300]); sys.exit(3)
        sdir = os.path.join(wd, "scripts")
        if os.path.isdir(sdir):
            for fn in sorted(os.listdir(sdir)):
                if not fn.endswith(".py"): continue
                st2, _ = put(token, repo, "scripts/" + fn, os.path.join(sdir, fn), msg="Update scripts/%s" % fn)
                print("  push scripts/%-18s %s" % (fn, st2))
        imdir = os.path.join(wd, "images")
        if os.path.isdir(imdir):
            for fn in sorted(os.listdir(imdir)):
                st, raw = put(token, repo, "images/" + fn, os.path.join(imdir, fn), msg="Add image %s" % fn)
                if st not in (200, 201): print("  push images/%s -> %s" % (fn, st))
        print("push done")

if __name__ == "__main__":
    main()
