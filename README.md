# video-doc-weekly-orchestrator (水球泡 / 且曼周报一条龙聚合技能)

> **一个入口、一份状态、同一套证据**。从周报制作、原作品链接核验、飞书文档发布覆盖，到抖音与 B 站批量自动收藏与跨期库检索的一体化技能。

---

## 1. 技能背景与核心痛点解法

本技能深度融合了过去割裂的三个独立流程（周报制作、抖音批量收藏、B站批量收藏），针对过去频繁出现的痛点提供了系统化根治方案：

1. **视频找不到 / 错链 / 11位截断 ID 自动找回**：
   - 内置 `scripts/resolve_links.py` 原作找回与健康检查引擎。
   - 自动识别 11 位数字截断并结合录屏邻帧 OCR、作者主页及跨平台检索定位真实 19 位抖音 ID 或 BVID。
   - 严格执行“标题、作者、关键内容/画面”三位一体核验门禁，绝不盲猜补全数字，杜绝相似题材冒充原作。

2. **疑难未决条目的人工核验闭环（原录屏截图 + 候选搜索）**：
   - **自动抽取录屏原帧**：对实在找不到或需人工复核的条目，执行 `scripts/weekly.py capture-screenshots --run <dir>`，自动调用 `ffmpeg` 从录像对应时间戳（`timestamp_seconds`）抓取高清帧截图保存至 `evidence/screenshots/`。
   - **搜索候选先行挂载**：通过文字搜索最相关的候选链接并标注为“待核验候选”（`text_search_candidate`）。
   - **直观双重视图**：在生成的 Markdown（`document.md`）和 HTML 检索台（`index.html`）中自动展示“查看录屏截图”与候选链接，方便人工一键对照原作者卡片/画面进行确认。

3. **周报发布后自动串行收藏（抖音 + B站）**：
   - 发布飞书并回读核验后，直接触发内置的 `scripts/collect_runner.py`。
   - 先抖音后 B站，强制维持 `>=5` 秒间隔节流写入，逐条落盘真实 UI 回执。
   - 收集完成后执行两轮全分页快照回读对账（`reconcile`），确保目标夹逐 ID 缺失为 0。

4. **跨期检索与知识库沉淀**：
   - 运行完成后自动归档入库至本地 SQLite 总库（`catalog.sqlite`）并生成自包含搜索界面的 `index.html`。
   - 支持随时通过 `weekly.py search --catalog ... --query ...` 毫秒级检索往期所有周报、主题、原视频及飞书文档位置。

---

## 2. 目录结构

```
video-doc-weekly-orchestrator/
├── SKILL.md                 # Codex 技能定义主入口
├── README.md                # 技能详细说明与跨电脑迁移指南
├── .gitignore               # 过滤缓存与临时文件
├── requirement.md           # 架构需求与安全门禁
├── decision-record.md       # 技术架构决策记录 (ADR)
├── agents/
│   └── openai.yaml          # Agent 元数据与接口声明
├── assets/                  # 示例与骨架数据
│   ├── manifest.example.json
│   ├── observations.example.json
│   ├── publication.example.json
│   ├── snapshot.example.json
│   ├── target.example.json
│   └── write-result.example.json
├── references/              # 操作规范与参考手册
│   ├── evidence-format.md   # 证据格式与字段规范
│   ├── issue-ledger.md      # 历史反例与问题账本
│   ├── local-profile.md     # 本机环境与路径约定
│   ├── operations.md        # 完整操作手册 (SOP)
│   ├── platforms.md         # 平台交互与防风控规范
│   └── source-recovery.md   # 原作品检索与截图核验机制
├── scripts/                 # 核心执行引擎
│   ├── weekly.py            # 统一 CLI 控制台主入口
│   ├── weekly_core.py       # 状态机、门禁校验、发布与渲染核心
│   ├── resolve_links.py     # 自动化链接健康检查、截图抓取与候选查找
│   ├── collect_runner.py    # 双平台串行收藏自动化编排
│   ├── extract_feishu.py    # 飞书文档结构化提取
│   ├── probe_bilibili.py    # B站元数据只读探测
│   └── validate_weekly_delivery.py # 交付物静态审计验证
└── tests/                   # 单元测试与测试用例
    ├── test_workflow.py     # 47 项完备自动化回归测试
    └── eval-cases.md        # 评测场景与边界测试用例
```

---

## 3. 在其他电脑上的迁移与使用指南

### A. 环境依赖
1. **Python 3.10+**
2. **FFmpeg**（用于视频帧抽帧与原录像截图）：需配置在系统 `PATH` 中。
3. **lark-cli**（用于飞书文档读写）：通过 npm 或本地二进制安装，确保登录态有效。

### B. 快速安装部署
将本仓库克隆至新电脑的 Skills 目录即可直接识别：
```bash
# 克隆到本地 Skills 路径（以 D:/AI-Skills/skills 为例）
git clone https://github.com/2392772541/video-doc-weekly-orchestrator.git D:/AI-Skills/skills/video-doc-weekly-orchestrator

# 或者克隆到 Codex 个人技能根目录
git clone https://github.com/2392772541/video-doc-weekly-orchestrator.git ~/.codex/skills/video-doc-weekly-orchestrator
```

### C. 运行回归测试验证
```bash
cd video-doc-weekly-orchestrator
python -m unittest discover -s tests -p "test_*.py"
# 预期输出：Ran 47 tests ... OK
```

---

## 4. 常用命令清单

所有命令均由 `scripts/weekly.py` 统管：

```bash
# 1. 环境与内存体检（检测空闲内存与进程）
python scripts/weekly.py doctor

# 2. 工作流初始化
python scripts/weekly.py init --run ./workflow --manifest ./seed.json --mode full

# 3. 扫描缺失链接与截断短 ID
python scripts/weekly.py resolve-links --manifest ./reviewed-seed.json --output ./evidence/unresolved.json

# 4. 针对疑难未决项抓取原录像高清截图
python scripts/weekly.py capture-screenshots --run ./workflow [--keys item-key1 item-key2]

# 5. 门禁审计
python scripts/weekly.py audit --run ./workflow

# 6. 渲染生成 Markdown 与飞书 XML
python scripts/weekly.py render --run ./workflow

# 7. 飞书文档发布覆盖与读回核验
lark-cli docs +update --doc "<doc_id>" --command overwrite --content "@report/weekly.xml" --as user
lark-cli docs +fetch --doc "<doc_id>" --as user > evidence/feishu-fetch-full.json
python scripts/weekly.py published --run ./workflow --input ./observed-publication.json

# 8. 收藏规划与串行写入
python scripts/weekly.py plan --run ./workflow --platform douyin
python scripts/weekly.py begin-write --run ./workflow --platform douyin --id <id> --plan-id <plan_id>
python scripts/weekly.py end-write --run ./workflow --attempt-id <attempt_id> --input ./receipt.json

# 9. 双轮快照对账
python scripts/weekly.py reconcile --run ./workflow --platform douyin
python scripts/weekly.py reconcile --run ./workflow --platform bilibili

# 10. 完成归档入库与跨期索引更新
python scripts/weekly.py finish --run ./workflow --catalog ~/.weekly-library/catalog.sqlite

# 11. 跨期总库检索
python scripts/weekly.py search --catalog ~/.weekly-library/catalog.sqlite --query "快乐发生"
```

---

## 5. 开源协议与维护
- Maintainer: 2392772541
- License: MIT / Private Personal Automation Pipeline
