# 自动更新流水线 · 运行手册

## 目录
```
scripts/fetch.py    抓取 nitter.cf -> new_tweets.json（只含未收录推文）
scripts/state.py    维护 known_ids.txt 已收录基线
scripts/build.py    并入新点位 -> 重建 index.html/CSV/TSV/XLSX -> 推 GitHub
source_index.html   当前线上版本基线
known_ids.txt       已收录推文 id
new_levels.json     新点位条目（由 AI 识别后写入）
.credentials/       GitHub token（本地，不上传）
```

## 一次完整流程
1. `python3 scripts/fetch.py known_ids.txt new_tweets.json`
2. 若 new_count=0 -> 结束，无需动作
3. 对 new_tweets.json 中每条新推文：
   - 下载配图到 images/
   - 用图像理解工具读出图上精确标注（点位/区间/量级）
   - 与推文正文交叉验证，抽出「品种 / 点位 / 类型 / 原文含义 / 推文日期 / 链接 / 原文 / 图上读数」
4. 写入 new_levels.json（rows=点位行，images=T3 图解读行）
5. `GH_TOKEN=$(cat .credentials/gh_token.txt) python3 scripts/build.py . --push`
6. `python3 scripts/state.py add known_ids.txt <新增id...>`
7. 把 source_index.html 同步为新的 index.html（作为下次基线）

## 关键约束
- 抓取源仅 nitter.cf（x.com 直连拿不到正文）；失败则本次跳过，不破坏已有内容
- 推文 id 用 Twitter snowflake 解时间戳：`(id>>22)+1288834974657`
- build.py 的 DATA 段必须能被 `const DATA=[...]; const T3=` 正则命中，勿改动该拼接形式
- 推 GitHub 用 Contents API PUT，已存在文件必须带 sha（build.py 已处理）
- Pages 为 legacy 构建，push 后约 30-90 秒生效
- 绝不把 token 写进 index.html 或任何上传文件


## 状态列（2026-11-05 追加）
DATA 每行第 5 个字段（index 4）= 状态，取值与配色：
| 状态 | class | 配色 | 含义 |
|---|---|---|---|
| 已兑现 | st-done | 绿色 #d3f0d8 / 字 #146c2e | 原文写明已到/已触及/已验证，价格已触及 |
| 待验证 | st-pend | 橙色 #fff4e5 | 11/4 之后给出的新点位，待走势确认 |
| 已失效 | st-dead | 红色 #fdeaea + 整行淡红底 | 原文写明跌破即离场/失效，条件已触发 |
| 历史参考 | st-hist | 紫色 #f3e8ff | 早期点位，仅供回溯 |
| （兜底） | st-ok | 浅绿 #eaf6ec | 其他 |

### 状态维护规则
- 新点位一律初始为「待验证」
- 原文出现「已到 / 已触及 / 已兑现 / 已到目标 / 验证 / 归0 / 开出」等完成信号 → 改「已兑现」
- 原文出现「跌破…离场 / 会崩盘 / 失效」且条件已触发 → 改「已失效」
- 更早日期且无完成信号 → 「历史参考」
- 颜色由 CSS class `.st-*` 控制，stCls() 做文本→class 映射

### build.py 变更
- 支持 10 字段 DATA；CSV/TSV/XLSX 表头均含「状态」
- XLSX ②表链接索引 r[6]→r[7]，日期 r[5]→r[6]，原文 r[7]→r[8]
- JSON 切分改用 slice_json() 括号配平（原正则会被嵌套数组截断）
- 支持 `--rebuild`：无新点位时也重建并推送全部产物


## 2026-11-06 修复：四张表全空白

事故提交 `b065c82f77`（2026-11-05 17:37 自动更新）产出的数据块在
`const DATA=[...]` 之后**漏掉分号**，直接接 `const T3=` → `SyntaxError: Unexpected token 'const'`
→ 整个 `<script>` 块一条语句都不执行 → render1/2/3/gallery 全部拿不到 DATA → 四张表同时空白。

新 `scripts/build.py` 三处改动：
1. **数据块原子重建**：不再逐个切片替换 const 声明，改为把 DATA/T2/T3/T2I 一次性重新序列化，
   固定顺序 `DATA → T2 → T3 → T2I`，每条以 `;` 结尾，整体替换。
2. **推送前语法闸门**：对每个内联 `<script>` 块跑 `node --check`（无 node 则退化为括号配平自检），
   任一块报错即 `exit 2`，不写文件、不推送。基线 JSON 解析失败同样 `exit 2`。
3. **计数自动同步**：说明区与页头的「条点位 / 条推文原文 / 张原图 / 共 N 张」全部由数据实时回写。

`scripts/build.py` 与 `scripts/fetch.py` 已入库 GitHub，定时任务可直接从仓库取脚本。
