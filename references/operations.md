# 操作手册（按阶段加载）

## 0. 执行环境

PowerShell示例，路径可随部署调整。已有Python即可，所有helper仅用标准库；不要因此安装模型、浏览器或升级全局CLI。

```powershell
$py = 'E:\app\Miniconda\python.exe'
$skill = 'D:\AI-Skills\skills\video-doc-weekly-orchestrator'
$cli = Join-Path $skill 'scripts\weekly.py'
$run = Join-Path $PWD 'weekly_YYYY_MM_DD\workflow'  # 替换成本期，不用字面占位符
$catalog = 'E:\工程项目\codex项目合集\个人用\.weekly-library\catalog.sqlite'  # 本机固定总库，不跟随聊天PWD
& $py -B $cli doctor
```

换工作目录仍使用同一 `$catalog`，先搜索总库取得旧期 `run_path`；总库路径不存在时先按 [本机入口](local-profile.md) 排查，不静默创建第二个总库。

doctor不自动开始OCR或任何平台动作。普通文本状态操作很轻；FFmpeg按时间片抽帧、`-threads 2`，OCR单worker，关闭每批自己创建的临时进程但不关闭用户标签/保护服务。默认不启用子Agent。

## 1. 新建 / 续跑

全流程先由OCR整理 seed JSON（见 `assets/manifest.example.json`），**只有完整清单才能full**。从视频清单提取器得到的43条视频不等于含前言、63条编号项和5快讯的整篇周报。

```powershell
& $py -B $cli init --run $run --manifest '.\reviewed-seed.json' --mode full
# 断点续跑不再init
& $py -B $cli status --run $run
```

`init`只迁移文字与原始URL；故意不继承任何旧success/verified，也不把URL直接设成已核验。产物：`run.json`、`evidence/seed-inventory.json`。

已有飞书文档的收藏流程：先遵循lark-doc前置读取，再取得**当前全文XML**与document_id/revision_id：

```powershell
lark-cli docs +fetch --doc '<实际URL或token>' --detail full --format json
# 将原始返回按UTF-8存为 source-fetch.json，不要截断成终端显示片段
& $py -B (Join-Path $skill 'scripts\extract_feishu.py') --input '.\source-fetch.json' --out '.\video-seed.json'
& $py -B $cli init --run $run --manifest '.\video-seed.json' --mode collect
```

仅抖音/仅B站的明确请求，提取时加 `--platform douyin` / `--platform bilibili`。提取器保留h3/h4编号标题与栏目，但所有归属仍待审查，直播入口不当作品。`collection_inventory_only_not_full_report`不得拿来发布全文。

## 2. 来源、结构与核验事件

把本轮原始工具结果、必要截图/源帧、精简只读响应放 `run/evidence/`；不存Cookie、Authorization或登录秘密。不编辑旧证据文件，每次新观察用新文件名。大于12MiB的JSON拆成分页证据，二进制帧逐张保存，不整段base64塞进JSON。

依据 [证据格式](evidence-format.md) 建立事件数组：
- permissions记录实际授权；当前只做audit就不赋予平台写权限。
- register_source绑定视频绝对路径/时长或真实飞书revision。
- source绑定每项同属标题、时间点、原始观察URL、作者与证据。
- candidate保留搜索候选及拒绝理由，不变更原作品身份。
- verify在看过详情并完成原作核对后使用。
- layout保留前言、栏目、各项、快讯与必要原图；inventory确认完整来源和顺序。

```powershell
& $py -B $cli apply --run $run --input '.\observed-events.json'
& $py -B $cli audit --run $run
& $py -B $cli render --run $run
```

apply整批失败不写入run；编辑标题或来源使旧核验失效。source与verify证据均哈希绑定；同项多处重复URL需 `repeat_of` 指向首次出现项。

错误exit2是门禁，不是让Agent改代码/改时间绕过。`audit/latest.json`给出逐项原因。原作确实无法确认时先报告检索证据；只有用户接受后才记录exception并 `render --allow-partial`，状态仍partial。

## 3. 发布并校验

按平台手册发现当前飞书账号、完整目录与同标题文档，将target事件apply。然后：

```powershell
& $py -B $cli publish-plan --run $run
```

仅执行它返回的 `create_once` / `update_existing`；`verify_only`只做回读。helper不替你发远程请求。所有平台写操作由Agent使用受支持工具执行并保存真实输出。

创建XML时按当前lark-doc命令帮助操作；需要文件参数时先cd到report目录，使用 `--content '@weekly.xml'`，**不是@绝对路径**；XML已有title不额外重复指定title。目标目录放置走lark-drive当前支持的参数，不能凭记忆编造CLI选项。

已有文档先fresh fetch检查人工增改并保存revision/备份。局部补链优先定点更新，不覆盖其它正文；整篇重建必须确认包含用户已有内容。创建/更新超时视为未知：不重发，先按标题+期号+内容查找真实结果，再refresh target和publish-plan。

图片：源图/截图保留源时间与所属项。按lark-doc media-insert上传到**同一个已创建/既有目标文档**，回读真实`img src`，绑定源图和上传/回读证据后加入layout的image块；刷新inventory、render与target，再update_existing同token，图片置于所属项后。初次只有文字的阶段不是最终发布成功，图片补齐前不收藏。未知上传结果同样先回读查重，不能盲重插。

发布后保存完整fetch JSON、全目录归属、样式观察；按 `assets/publication.example.json` 填入路径与当前值：

```powershell
& $py -B $cli published --run $run --input '.\observed-publication.json'
```

校验全文/链接顺序/图片标识、唯一同标题文档、目录、revision与样式证据，不只比较链接数量。XML可含平台生成的block-id包装，但实质内容不应静默漂移。

## 4. 收藏（两个平台串行）

### 自动化串行收藏脚本辅助

技能内置了自动化收藏执行器 `scripts/collect_runner.py`，支持一键生成差集与安全的浏览器批处理执行脚本：

```powershell
# 针对指定平台自动生成包含5秒等待间隔的执行脚本
& $py -B (Join-Path $skill 'scripts\collect_runner.py') --run $run --platform douyin --generate-script
& $py -B (Join-Path $skill 'scripts\collect_runner.py') --run $run --platform bilibili --generate-script
```



full模式先有已通过的publication；collect模式每个确认URL必须与**当前源飞书文档**相同。补链修复不能只改本地收藏清单：原文更新需要相应授权，备份并定点修复后重新fetch，register新source_id/revision、重绑source再verify；若只有收藏授权，先问是否同步修改原文。

每个平台 target事件 → 全量快照 → 导入：

```powershell
& $py -B $cli snapshot --run $run --input '.\observed-folder-before.json'
& $py -B $cli plan --run $run --platform douyin
# plan输出missing_ids；existing_ids跳过；下面两个ID必须取实际返回
& $py -B $cli begin-write --run $run --platform douyin --id '<missing_id>' --plan-id '<plan_id>'
# 现在才对这条作品执行收藏/加入指定夹，保存真实回执
& $py -B $cli end-write --run $run --attempt-id '<attempt_id>' --input '.\observed-write-result.json'
```

`begin-write`必须先落盘，随后才点UI/调用写API。API全业务码0可acknowledged；UI重新打开同一作品、确认收藏状态及指定夹选中可ui_confirmed；只有toast/超时/失去连接用uncertain。失败和未知状态都会拦住后续写入，先全夹fresh snapshot；得到新plan才可以重试仍缺失的ID。同plan同项永不重复。

每条至少5秒间隔，一次计划15分钟有效，过期做全夹回读并重新plan。即使已ack，再plan前也必须有晚于最后一次写的fresh snapshot。账号/收藏夹不可中途切换；写后要切换须另开run，不清除历史。

所有写入结束后：全分页第1轮 → 等至少5秒 → 全分页第2轮。二者开始时间都必须晚于最后一次写入。没有新增写入时，两轮仍需真实读取。

```powershell
& $py -B $cli snapshot --run $run --input '.\observed-round1.json'
& $py -B $cli snapshot --run $run --input '.\observed-round2.json'
& $py -B $cli reconcile --run $run --platform douyin
# 随后用相同流程处理bilibili，不能把抖音结果当B站结果
```

API快照要完整游标链和真实终止；UI快照要实际目标可见、各页已加载、末页明确到底/分页结束证据。不把只读40条列表或滚不动算读完整。

## 5. 完成、搜索、恢复

```powershell
& $py -B $cli finish --run $run --catalog $catalog
& $py -B $cli index --run $run --catalog $catalog    # partial也可生成检索库
& $py -B $cli search --catalog $catalog --query '牙科'
```

- 单期 `index.html` 与 `.weekly-library/index.html` 可离线打开，多关键词用空格；支持期号、标题、作者、完整ID。总页最近5000项，SQLite保留更多。
- `library.json`保留原始链、已确认链、候选、核验日期、源证据、收藏目标和run路径，避免下周再次从零搜索。
- complete必须全文audit通过、发布当前且两平台都通过；例外缺失始终partial，audit-only仅audit_passed。
- 同期检索更新用同一个run，不每次生成新run造成跨期库重复。
- `.weekly.lock`锁冲突先检查记录的PID是否仍运行；不得自动删除活进程的锁。若确实上次崩溃遗留，备份后移走该锁；run保留，再status和平台回读。
- `run.previous.json`是上个命令快照；不要退回它来“忘掉”已发生的远程写入。原始证据与线上状态优先。

## 只读B站探测

```powershell
& $py -B (Join-Path $skill 'scripts\probe_bilibili.py') --ids '<实际BVID1>' '<实际BVID2>' --out '.\probe.json'
```

无Cookie、每项节流、风险码立即停止。metadata_accessible仅可访问元信息，永远不自动给identity_verified；还要独立对照原作。旧validator入口保留，但旧manifest没有逐项v2证据会失败，这是预期保护。
