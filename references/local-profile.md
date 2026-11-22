# 本机固定入口与实测锚点（Codex / Antigravity 共用）

本用户的周报与收藏跨聊天、跨工具（Codex / Antigravity）共用同一套引擎与总库，绝不把当前聊天工作目录当成新的总库位置。

## 1. 本地固定路径
- **引擎主仓（GitHub 同步源）**：`D:\AI-Skills\skills\video-doc-weekly-orchestrator`（远端：`https://github.com/2392772541/video-doc-weekly-orchestrator.git`）
- **OCR 抽帧提取叶技能**：`C:\Users\win10\.codex\skills\video-doc-weekly-ocr`
- **飞书 CLI 参考文档**：`D:\AI-Skills\skills\lark-doc\SKILL.md`、`D:\AI-Skills\skills\lark-drive\SKILL.md`、`D:\AI-Skills\skills\lark-shared\SKILL.md`（或执行 `lark-cli skills read lark-doc ...`）
- **Python 解释器**：`E:\app\Miniconda\python.exe`（已安装 `rapidocr_onnxruntime`、`paddleocr`、`cv2`、`PIL`、`playwright`、`pytest`）
- **跨期总库 SQLite**：`E:\工程项目\codex项目合集\个人用\.weekly-library\catalog.sqlite`
- **跨期双击检索页**：`E:\工程项目\codex项目合集\个人用\.weekly-library\index.html`
- **历史工作区与各期 run**：`E:\工程项目\codex项目合集\且曼\runs\`（如 `vol302_20261117`）
- **抖音已观看黑名单**：`E:\工程项目\codex项目合集\且曼\douyin_watched_vids.json`
- **常见录屏输入目录**：`E:\BaiduNetdiskDownload\B站视频下载\`

## 2. 飞书与双平台默认锚点（仅作定位参考，执行前须实时核验登录身份与目录唯一性）
- **飞书目标目录**：`水球泡周报`
  - `folder_token`：`BUpLfakmWlrR2LdSbnpcsaWmnOc`
  - 目录链接：`https://my.feishu.cn/drive/folder/BUpLfakmWlrR2LdSbnpcsaWmnOc`
  - 历史维护子目录：`维护`（`OTPHfIr0SlPoW9dWLlqc3Klnn2f`）
  - 最新已发布示例：`且曼内刊 - Vol.302 20261116`（`https://my.feishu.cn/docx/QBIhd5Xm7otjmRx3jZhcE71Znde`）
- **抖音单一总夹**：`水球泡视频周报`
  - 历史实测 `collects_id_str`：`7672986870204225339`（每次写入前仍需以当前登录页实测为准）
  - 入口链接：`https://www.douyin.com/user/self?showSubTab=favorite_folder&showTab=favorite_collection`
- **B站单一总夹**：`水球泡周报视频`
  - 历史实测 `fid`：`4103329683`
  - 入口链接：`https://space.bilibili.com/3546569697659483/favlist?fid=4103329683`

## 3. Antigravity 与 Codex 运行时差异映射
- **浏览器收藏执行**：
  - Codex 使用 `cua_repl` 执行 `evidence/collect_bilibili.js` 与 `evidence/collect_douyin.js`；
  - **Antigravity 无 `cua_repl`**：改用原生 `browser` 子代理执行 `collect-plan` 生成的纯页内脚本 `evidence/collect_bilibili_inpage.js` 与 `evidence/collect_douyin_inpage_helper.js`，或连接本机 Chrome/Edge CDP（`127.0.0.1:9222`）执行 `evidence/collect_*_cdp.py`。
- **缺失链接联网反查**：
  - Antigravity 优先使用原生 `search_web` 与 `browser` 子代理直查核验，同时保留 `weekly.py chatgpt-sheet` / `--chatgpt-results` 作为可选辅助通道。

## 4. 数据与安全边界
- 新一期 run 推荐放在 `E:\工程项目\codex项目合集\且曼\runs\volXXX_YYYYMMDD` 或实际项目的 `weekly_YYYY_MM_DD/workflow/`，但 `index` / `finish` 务必统一指定上述同一个 `--catalog`。
- 查找或“继续上次”先 `search` 总库，从结果的 `run_path` 找回同一期状态，先查 `status`，不重新 `init`。
- 迁机/盘符变化时先定位原库或其备份再修改本文件，不偷偷切到当前 PWD 创建空库。库内与配置文件绝不保存 Cookie、Token 或密码。
