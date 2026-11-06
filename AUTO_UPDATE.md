# 自动更新流水线（定时任务用）

## 工作目录
本文件所在目录即工作目录，记为 WD。绝对路径 = $DEEPTUTOR_WORKSPACE_ROOT + WD 的相对路径。

## 每轮步骤
1. `cd WD && python3 scripts/fetch.py known_ids.txt new_tweets.json`
   - 退出码 2 = 所有数据源失败（nitter 抖动），本轮跳过，不改动线上内容
   - new_count=0 → 无新内容，结束
2. 对 new_tweets.json 每条新推文：
   - 配图下载到 images/：`https://pbs.twimg.com/media/<ID>.<ext>?name=large`
   - 用 mcp_minimax_understand_image 读图上精确标注（点位/区间/量级）
   - 与正文交叉验证，抽出 [品种, 点位, 类型, 状态, 原文含义, 推文日期, 推文链接, 推文原文, 图上读数]
   - 无具体点位的方向性判断也收录，点位写「未给具体点位」，状态「待验证」
3. 写 new_levels.json（**每次覆盖写，不要追加**）：{"rows":[...], "images":[图片文件,推文ID,图上关键标注,读图结论,原图直链]}
4. `GH_TOKEN=$(cat .credentials/gh_token.txt) GH_REPO=jianzhishuai/qiusheng-levels python3 scripts/build.py . --push`
   - 失败则不要继续，本轮结束并报告
5. `cp index.html source_index.html`
6. `python3 scripts/state.py add known_ids.txt <本轮全部新推文id...>`（含无点位的闲聊推文，避免重复抓取）
7. 校验线上：拉 https://jianzhishuai.github.io/qiusheng-levels/ 与本地 index.html 比对一致性

## 已修复的坑（2026-11-05）
- build.py 原先**不回写 DATA/T3 到 HTML**，新增点位只进 CSV/TSV/XLSX，页面永远不变。
  已加 `replace_decl(html,"DATA",...)` 与 `replace_decl(html,"T3",...)`。每轮构建务必确认页面 DATA 行数已增加。

## 硬约束
- build.py 会把 new_levels.json 的 rows **追加**到 DATA 尾部；new_levels.json 必须在构建成功后清空为 {"rows":[],"images":[]}，否则重复行
- source_index.html 是唯一基线，每轮成功后用 index.html 覆盖
- 绝不把 token 写入任何上传文件
- 已知推文 id 见 known_ids.txt（由 state.py 自动维护，勿手改）


## 2026-11-06 更新（重要）

- **build.py 必须在推送前通过语法闸门**：坏产物一律 `exit 2`，不会上线。
  若某轮构建报 `[ABORT] 生成的页面脚本存在语法错误`，本轮**不要重试 push**，先修基线。
- **基线来源**：每轮开始先 `git clone` / 下载本仓库最新 `index.html` 作为 `source_index.html`，
  不要再从上一轮临时目录恢复脚本（脚本已入库 `scripts/`）。
- 跑完 build.py 后必须确认 `source_index.html` 与 `index.html` 一致（cp 覆盖），
  防止下一轮拿旧基线重放已修内容。

## 2026-11-07 更新

- **翻页终止条件修正**：`fetch.py` 原以「本页新增=0」作为翻页终止条件，
  但 nitter 各页存在重复推文（单页重复 1~3 条），导致计数虚低而**提前停止翻页**，
  漏抓更早的推文。现改为：本页无新推文**且**至少已翻 2 页才停止。
- **跨页补图**：推文正文可能出现在第 1 页、配图出现在后续页；
  `parse()` 现会把后页图片回填到已收录推文（`ALLT` 全局引用）。
- **`parse()` 返回值改为 `(dict, fresh_ids)`**：`fresh_ids` 为本页相对已收录集合的新增推文 id。
- **回复流必须抓取**：旧版 `get_any()` 只返回首个成功源，
  `/with_replies` 回复流从未被请求过 —— 这是「表格停在 11-06」的根因。
  现为 `get_all()`，抓取组内全部源并合并。
- **推送必须单次提交**：逐文件 PUT 会触发多次 Pages 构建并互相打断
  （历史 run 连续 cancelled/errored）。现用 Git Data API：
  blob → tree → commit → ref，**一次提交 = 一次构建**。
- **时间口径统一北京时间（UTC+8）**：`fetch.py` 输出与 `build.py` 的 `norm_date()`
  均按 UTC+8；抓取后请用 `python3 scripts/audit.py .` 体检（应输出「不一致 0」）。

## 2026-11-07 修订：audit.py 崩溃 + prepare.py 限流失效

### audit.py：无日期行时抛 TypeError（已修）
`empty.append((r[0], r[1], r[2], "无链接"))` 是 **4 元组**，
但 `print("         - #%s %s %s" % x)` 只有 **3 个占位符**，
只要基线里存在「无日期且无链接」的行（线上 #55 缅A大盘 5650/3810、#56 5950），
audit.py 就在打印 [nodate] 时抛 `TypeError: not all arguments converted during string formatting`
而**整个审计中断**——恰好在最需要它报警的时候失声。
现改为 4 个占位符并打印 `[无链接]` 标签；同时补上 slice_json 返回 None、
行宽不足时的健壮性保护，退出码非 0 表示基线不可用。

### prepare.py：Contents API 限流导致「静默失效」（已修）
旧版用 `GET /contents/<dir>` 列目录发现 scripts/ 与 images/。该接口高频即 403，
而 `listing()` 遇 403 只 `print` 一行 warning 后 **返回 []**，
于是：**一个脚本都没下载，但 prepare.py 照样打印「[ready] 基线已刷新」退出 0**，
流水线带着缺失的脚本继续跑，images/ 也未就位，读图步骤拿不到本地图。

现改为三级降级取清单 + 脚本缺失即中止：
- **L1 Git Trees API**：`GET /git/trees/main?recursive=1` 一次拿全仓库 blob 路径（请求数最少）
- **L2 Contents API**：带指数退避重试
- **L3 纯本地推导**：scripts 用固定清单；images 从基线 HTML 的 T3 常量第 0 列 +
  pbs 直链精确提取（括号配平扫描，非正则贪婪匹配），自动排除 favicon 噪声

并新增两条硬约束：
1. **脚本下载失败直接退出（码 3）**，绝不让流水线带着缺失脚本继续；
2. 图片缺失只计数不阻断（图片非本轮必需），但必须打印失败文件名。

实测（2026-11-07）：L1 → 77 文件 / images 52 张全到位；
模拟 403 时 L2 接管 57 文件；L1+L2 全挂时 L3 降级仍推出 5 个脚本 + 31 张基线引用图。
