#!/usr/bin/env python3
"""One local entry point. External publish/favorite actions are performed by the agent."""
from __future__ import annotations
import argparse
import ctypes
import importlib.util
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
import weekly_core as w

# 本机固定入口常量（跨工作目录复用，仅作只读默认定位，绝不缓存凭据）
DEFAULT_GLOBAL_CATALOG = Path(r"E:\工程项目\codex项目合集\个人用\.weekly-library\catalog.sqlite")
DEFAULT_WATCHED_FILE = Path(r"E:\工程项目\codex项目合集\且曼\douyin_watched_vids.json")


def normalize_feishu_doc_token(doc_ref: str) -> str:
    """从粘贴的飞书文档 URL 或裸 token 中提取纯净 token，避免 Windows lark-cli.CMD 批处理拆分 URL 中的 & 参数。"""
    raw = (doc_ref or "").strip()
    m = re.search(r"/(?:docx|doc)/([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    if raw.startswith(("http://", "https://")):
        return raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return raw


def output(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def memory_status():
    if os.name == "nt":
        class Memory(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in ("total_phys", "available_phys", "total_page", "available_page", "total_virtual", "available_virtual", "extended")]
        m = Memory()
        m.length = ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return {"known": False, "heavy_work_allowed": False}
        total, available = m.total_phys, m.available_phys
    elif Path("/proc/meminfo").exists():
        d = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()}
        total, available = d["MemTotal"], d["MemAvailable"]
    else:
        return {"known": False, "heavy_work_allowed": False}
    return {"known": True, "total_gib": round(total / 2**30, 2), "available_gib": round(available / 2**30, 2), "heavy_work_allowed": available >= max(4 * 2**30, total * 0.15), "policy": "one OCR worker; 2 CPU threads; one browser task tab; pause heavy work below max(4 GiB,15% RAM)"}


@contextmanager
def locked(run):
    path = Path(run).resolve()
    w.require((path / "run.json").is_file(), "run_missing_initialize_first")
    lock = path / ".weekly.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as e:
        raise w.GateError("run_locked_do_not_run_parallel_writers: " + str(lock)) from e
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "at": w.now()}, f)
        yield path, w.read_json(path / "run.json")
    finally:
        lock.unlink(missing_ok=True)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Read memory and dependency availability; starts nothing")
    init = sub.add_parser("init", help="Migrate legacy/new item inventory; never trusts old success")
    init.add_argument("--run", type=Path, required=True)
    init.add_argument("--manifest", type=Path, required=True)
    init.add_argument("--mode", choices=("full", "publish", "collect", "audit"), default="full")
    res = sub.add_parser("resolve-links", help="Scan and verify missing or truncated video links")
    res.add_argument("--manifest", type=Path, required=True)
    res.add_argument("--output", type=Path)
    res.add_argument("--apply-resolutions", type=Path)
    search = sub.add_parser("search", help="Query a local cross-issue catalog")
    search.add_argument("--catalog", type=Path, default=None, help="Catalog SQLite path (defaults to WEEKLY_CATALOG_FILE or canonical global catalog)")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=30)
    cap = sub.add_parser("capture-screenshots", help="Extract video frame screenshots from source video recording")
    cap.add_argument("--run", type=Path, required=True)
    cap.add_argument("--keys", nargs="*", default=None)
    emb = sub.add_parser("embed-screenshots", help="Embed uploaded screenshot tokens into layout as image blocks")
    emb.add_argument("--run", type=Path, required=True)
    emb.add_argument("--receipts", type=Path, help="Path to json receipts mapping key to {file_token, file}")
    scan_sc = sub.add_parser("scan-screenshots", help="OCR-scan video screenshots to automatically discover and heal embedded video links")
    scan_sc.add_argument("--run", type=Path, required=True)
    scan_sc.add_argument("--keys", nargs="*", default=None)
    scan_v = sub.add_parser("scan-video", help="Scan recording video timeline with RapidOCR to discover on-screen URLs and titles")
    scan_v.add_argument("--run", type=Path, required=True)
    scan_v.add_argument("--video", type=Path, help="Optional path to video file (defaults to sources in run.json)")
    scan_v.add_argument("--passes", type=int, default=3, choices=(1, 2, 3), help="Number of progressive passes (1: coarse, 2: +dense gap, 3: +tail sweep, default 3)")
    scan_v.add_argument("--step", type=float, default=12.0, help="Sampling step in seconds for Pass 1 (default 12s)")
    scan_v.add_argument("--dense-step", type=float, default=1.0, help="Sampling step for Pass 2 dense gaps (default 1.0s / 1 fps)")
    scan_v.add_argument("--tail-step", type=float, default=2.0, help="Sampling step for Pass 3 tail sweep (default 2.0s)")
    scan_v.add_argument("--max-frames", type=int, default=None, help="Maximum frames to sample per video (default None for full duration)")
    heal = sub.add_parser("auto-heal", help="End-to-end auto-heal: OCR screenshots, recover links, remove redundant images, and rebind layout")
    heal.add_argument("--run", type=Path, required=True)
    heal.add_argument("--keys", nargs="*", default=None)
    pipe = sub.add_parser("pipeline", help="[OFFLINE SIMULATION ONLY] Local sandbox pipeline harness; blocked in production by default")
    pipe.add_argument("--run", type=Path, required=True)
    pipe.add_argument("--manifest", type=Path, required=True)
    pipe.add_argument("--mode", choices=("full", "publish", "collect", "audit"), default="full")
    pipe.add_argument("--feishu-doc", type=str, default=None, help="Forbidden in --offline-simulation mode (prevents accidental live Feishu writes)")
    pipe.add_argument("--offline-simulation", action="store_true", help="Explicitly opt into pure local offline simulation (never use for real weekly production)")
    conv_p = sub.add_parser("convert-preview", help="Convert Feishu doc blue bookmark links to interactive iframe preview cards")
    conv_p.add_argument("--doc", type=str, required=True, help="Feishu Document ID/Token")
    conv_p.add_argument("--delay", type=float, default=0.3, help="Delay between block updates in seconds")
    cg_sheet = sub.add_parser("chatgpt-sheet", help="Generate prompt sheet for ChatGPT deep video search")
    cg_sheet.add_argument("--run", type=Path, required=True)
    s_miss = sub.add_parser("search-missing", help="Multi-channel search (Bilibili/ChatGPT/Web) for missing video links")
    s_miss.add_argument("--run", type=Path, required=True)
    s_miss.add_argument("--chatgpt-results", type=Path, help="Path to json file containing ChatGPT search results to ingest")
    c_plan = sub.add_parser("collect-plan", help="Generate production browser scripts for two-platform collection")
    c_plan.add_argument("--run", type=Path, required=True)
    c_plan.add_argument("--platform", choices=("all", "bilibili", "douyin"), default="all", help="Target platform(s) to plan (default: all configured)")
    c_plan.add_argument("--watched-file", type=Path, help="Path to JSON file containing watched video IDs to exclude (e.g. douyin_watched_vids.json)")
    c_plan.add_argument("--runtime", choices=("all", "antigravity", "codex"), default="all", help="Target browser runtime scripts to emit (default: all)")
    c_plan.add_argument("--no-default-watched", action="store_true", help="Disable fallback lookup of canonical douyin_watched_vids.json")
    imp_col = sub.add_parser("import-collection", help="Import real browser collection result JSON (Bilibili inpage/CDP or Douyin 2-stage + 2-round readback) into run.json")
    imp_col.add_argument("--run", type=Path, required=True)
    imp_col.add_argument("--input", type=Path, required=True, help="Path to browser collection result JSON file")
    imp_col.add_argument("--platform", choices=sorted(w.VIDEO_PLATFORMS), default=None, help="Target platform (optional if declared inside input JSON)")
    imp_col.add_argument("--plan-id", default=None, help="Optional plan_id when input JSON does not carry snapshots.before")
    sync_sb = sub.add_parser("sync-sidebar", help="Synchronize and rebuild Feishu doc sidebar outline with H3 subsections")
    sync_sb.add_argument("--doc", type=str, required=True, help="Feishu Document ID/Token")
    sync_sb.add_argument("--rules", type=Path, help="Optional JSON file defining custom [(sub_title, anchor_keyword), ...]")
    ext_f = sub.add_parser("extract-feishu", help="Extract video occurrences from an existing Feishu document into a seed manifest")
    ext_f.add_argument("--doc", type=str, required=True, help="Feishu Document ID/Token")
    ext_f.add_argument("--out", type=Path, required=True, help="Output manifest JSON path")
    ext_f.add_argument("--platform", choices=sorted(w.VIDEO_PLATFORMS), help="Filter by platform")
    clean_p = sub.add_parser("clean-cache", help="Clean up extracted video frames and temporary OCR caches")
    clean_p.add_argument("--dir", type=Path, default=Path("."), help="Target workspace or run directory to scan and clean")
    clean_p.add_argument("--all", action="store_true", help="Include final evidence screenshots (full purge)")
    clean_p.add_argument("--dry-run", action="store_true", help="Simulate without deleting files")
    tr_p = sub.add_parser("build-transcripts", help="Stage 7: extract audio, transcribe via Tingwu (with RTX 4070 FunASR quota fallback), build companion doc, and publish consolidated entry to zhuanlu")
    tr_p.add_argument("--run", type=Path, required=True, help="Target weekly run directory")
    tr_p.add_argument("--engine", choices=("tingwu", "local"), default="tingwu", help="Primary transcription engine (default: tingwu with auto FunASR fallback on quota limit)")
    tr_p.add_argument("--headless", action="store_true", help="Run persistent Tingwu Chrome headlessly")
    tr_p.add_argument("--no-upload", action="store_true", help="Generate local companion Markdown only without uploading to zhuanlu")
    tr_p.add_argument("--upload-partial", action="store_true", help="Allow uploading to zhuanlu even when --limit leaves pending items")
    tr_p.add_argument("--category", default="直播回放与周刊", help="Target zhuanlu category (default: 直播回放与周刊)")
    tr_p.add_argument("--status", default="未研究", help="Target zhuanlu study status (default: 未研究)")
    tr_p.add_argument("--zhuanlu-workspace", type=Path, default=None, help="Optional override path for 通译转录 workspace")
    tr_p.add_argument("--catalog", type=Path, default=None, help="Optional cross-issue catalog SQLite to refresh index.html with companion link")
    tr_p.add_argument("--limit", type=int, default=None, help="Optional max number of pending video items to process in this invocation")
    tr_p.add_argument("--force", action="store_true", help="Force re-transcribe already checkpointed items")
    tr_p.add_argument("--keep-audio", action="store_true", help="Keep temporary extracted MP3 audio files instead of auto-cleaning")
    for name in ("apply", "audit", "render", "publish-plan", "published", "snapshot", "plan", "begin-write", "end-write", "reconcile", "status", "finish", "index"):
        s = sub.add_parser(name)
        s.add_argument("--run", type=Path, required=True)
        if name in {"apply", "snapshot", "published", "end-write"}:
            s.add_argument("--input", type=Path, required=True)
        if name in {"plan", "begin-write", "reconcile"}:
            s.add_argument("--platform", choices=sorted(w.VIDEO_PLATFORMS), required=True)
        if name == "begin-write":
            s.add_argument("--id", required=True)
            s.add_argument("--plan-id", required=True)
        if name == "end-write":
            s.add_argument("--attempt-id", required=True)
        if name == "render":
            s.add_argument("--allow-partial", action="store_true", help="Only explicitly accepted unresolved originals; remains partial")
        if name in {"index", "finish"}:
            s.add_argument("--catalog", type=Path)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "doctor":
        py_mods = {
            m: bool(importlib.util.find_spec(m))
            for m in ("rapidocr_onnxruntime", "paddleocr", "playwright")
        }
        output({
            "memory": memory_status(),
            "python": sys.executable,
            "python_packages": py_mods,
            "dependencies": {n: shutil.which(n) for n in ("ffmpeg", "ffprobe", "lark-cli", "opencli")},
            "local_assets": {
                "global_catalog": {"path": str(DEFAULT_GLOBAL_CATALOG), "exists": DEFAULT_GLOBAL_CATALOG.is_file()},
                "watched_blacklist": {"path": str(DEFAULT_WATCHED_FILE), "exists": DEFAULT_WATCHED_FILE.is_file()},
            },
            "network_requests": 0,
            "external_writes": 0,
        })
        return 0
    if args.command == "search":
        env_cat = os.environ.get("WEEKLY_CATALOG_FILE", "").strip()
        catalog_path = args.catalog or (Path(env_cat) if env_cat else DEFAULT_GLOBAL_CATALOG)
        output(w.search_catalog(catalog_path, args.query, args.limit))
        return 0
    if args.command == "resolve-links":
        import resolve_links
        data = json.loads(args.manifest.read_text(encoding="utf-8"))
        report = resolve_links.scan_and_report_issues(data)
        if args.apply_resolutions and args.apply_resolutions.is_file():
            resolutions = json.loads(args.apply_resolutions.read_text(encoding="utf-8"))
            applied = 0
            for item in data.get("items", []):
                k = item.get("key")
                t = item.get("title")
                res_entry = resolutions.get(k) or resolutions.get(t)
                if res_entry:
                    c_url = res_entry if isinstance(res_entry, str) else res_entry.get("url")
                    c_title = res_entry.get("title", "") if isinstance(res_entry, dict) else ""
                    c_author = res_entry.get("author", "") if isinstance(res_entry, dict) else ""
                    v = resolve_links.verify_resolution(item, c_url, c_title, c_author)
                    if v["verified"]:
                        item["url"] = v["resolved_url"]
                        item["resolved_by"] = v["method"]
                        applied += 1
            report["applied_resolutions"] = applied
            args.manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        output(report)
        return 0
    if args.command == "pipeline":
        if not getattr(args, "offline_simulation", False):
            output({
                "ok": False,
                "error": "pipeline_blocked_in_production_offline_simulation_only",
                "external_writes": 0,
                "reason": (
                    "weekly.py pipeline 仅用于纯本地离线演练（内部会合成模拟回执与模拟快照），"
                    "严禁用于真实周报生产，防止产生假完成或误改线上飞书文档。"
                ),
                "real_workflow_commands": [
                    "已有飞书周报补录收藏: extract-feishu -> init --mode collect -> apply -> snapshot -> collect-plan --runtime antigravity -> 浏览器执行脚本 -> import-collection -> reconcile -> convert-preview -> finish",
                    "新一期录屏制作周报: init --mode full -> scan-video -> auto-heal/search-missing -> audit -> render -> 飞书发布并回读 published -> snapshot -> collect-plan --runtime antigravity -> 浏览器执行脚本 -> import-collection -> reconcile -> convert-preview -> finish",
                    "仅做纯本地离线沙盒演练时，请显式传入 --offline-simulation（且严禁携带 --feishu-doc）。",
                ],
            })
            return 2
        if args.feishu_doc:
            output({
                "ok": False,
                "error": "offline_simulation_forbids_live_feishu_doc",
                "external_writes": 0,
                "reason": "离线模拟模式严禁传入 --feishu-doc，禁止触发 sync-sidebar 或 convert-preview 线上飞书写操作。",
            })
            return 2
        import pipeline
        res = pipeline.run_pipeline(
            args.run,
            args.manifest,
            args.mode,
            feishu_doc_id=None,
            allow_remote_feishu=False,
        )
        output(res)
        return 0
    if args.command == "convert-preview":
        import feishu_preview_converter
        res = feishu_preview_converter.convert_all_bookmarks_to_previews(normalize_feishu_doc_token(args.doc), delay=args.delay)
        output(res)
        return 0 if res.get("ok") else 2
    if args.command == "sync-sidebar":
        import sync_feishu_sidebar
        rules = None
        if args.rules and args.rules.is_file():
            rules = json.loads(args.rules.read_text(encoding="utf-8"))
        res = sync_feishu_sidebar.sync_document_sidebar(normalize_feishu_doc_token(args.doc), subsections_config=rules)
        output(res)
        return 0 if res.get("ok") else 2
    if args.command == "clean-cache":
        res = w.clean_run_cache(args.dir, include_evidence=args.all, dry_run=args.dry_run)
        output(res)
        return 0
    if args.command == "build-transcripts":
        import weekly_transcripts
        res = weekly_transcripts.run_build_transcripts(
            args.run,
            engine=args.engine,
            headless=args.headless,
            upload_to_zhuanlu=not args.no_upload,
            upload_partial=args.upload_partial,
            zhuanlu_category=args.category,
            zhuanlu_status=args.status,
            zhuanlu_workspace=args.zhuanlu_workspace,
            limit=args.limit,
            force=args.force,
            keep_audio=args.keep_audio,
        )
        if args.catalog and res.get("status") != "need_login":
            with locked(args.run) as (run_p, st):
                res["index"] = w.export_index(st, run_p, args.catalog)
        output(res)
        return 0 if res.get("ok") else 2
    if args.command == "extract-feishu":
        import subprocess, extract_feishu
        lark_bin = shutil.which("lark-cli") or "lark-cli"
        clean_doc = normalize_feishu_doc_token(args.doc)
        # 使用纯净 token 与参数数组调用，彻底避免 Windows lark-cli.CMD 拆分 URL 中的 & 查询参数
        raw_out = subprocess.check_output(
            [lark_bin, "docs", "+fetch", "--doc", clean_doc, "--detail", "with-ids", "--as", "user"],
            text=True,
            encoding="utf-8",
        )
        payload = json.loads(raw_out)
        res = extract_feishu.extract(payload, platform=args.platform)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        w.write_json(args.out, res)
        output({"ok": True, "out": str(args.out), "total_items": res["numbered_item_count"], "title": res["title"]})
        return 0
    if args.command == "scan-video":
        import multi_search_resolver
        import resolve_links
        run_p = args.run.resolve()
        st = w.read_json(run_p / "run.json")
        video_sources = {}
        if args.video and args.video.is_file():
            video_sources["src-cmd"] = args.video.resolve()
        else:
            for sid, sinfo in st.get("sources", {}).items():
                p = sinfo.get("path")
                if p and Path(p).is_file():
                    video_sources[sid] = Path(p).resolve()

        if args.passes >= 2:
            summary = multi_search_resolver.run_three_pass_video_extraction(
                video_sources, st, run_p,
                coarse_step=args.step,
                dense_step=args.dense_step,
                tail_step=args.tail_step
            )
            w.write_json(run_p / "run.json", st)
            output({"scanned_sources": {k: str(v) for k, v in video_sources.items()}, "three_pass_summary": summary})
        else:
            all_discovered = []
            for sid, vp in video_sources.items():
                disc = multi_search_resolver.scan_video_timeline_for_links(vp, step_sec=args.step, max_frames=args.max_frames)
                all_discovered.extend(disc)
            w.write_json(run_p / "frame_urls.json", all_discovered)
            heal_res = resolve_links.auto_heal_from_screenshots(run_p, st)
            output({"scanned_videos": [str(p) for p in video_sources.values()], "discovered_urls_count": len(all_discovered), "auto_healed": heal_res})
        return 0
    if args.command == "chatgpt-sheet":
        import multi_search_resolver
        run_p = args.run.resolve()
        st = w.read_json(run_p / "run.json")
        unresolved = [it for it in st.get("items", []) if it.get("exception") or not it.get("canonical_url")]
        sheet = multi_search_resolver.generate_chatgpt_search_sheet(unresolved)
        p_file = run_p / "evidence" / "chatgpt_search_prompt.txt"
        p_file.parent.mkdir(parents=True, exist_ok=True)
        p_file.write_text(sheet["prompt"], encoding="utf-8")
        sheet["prompt_file"] = str(p_file)
        output(sheet)
        return 0
    if args.command == "search-missing":
        import multi_search_resolver
        run_p = args.run.resolve()
        st = w.read_json(run_p / "run.json")
        applied_results = []
        if args.chatgpt_results and args.chatgpt_results.is_file():
            raw_text = args.chatgpt_results.read_text(encoding="utf-8")
            cg_data = multi_search_resolver.parse_chatgpt_response(raw_text)
            if cg_data:
                app_res = multi_search_resolver.apply_search_resolutions_to_state(run_p, st, cg_data)
                applied_results.append({"source": "chatgpt_results", "result": app_res})
        # Bilibili API search
        unresolved = [it for it in st.get("items", []) if it.get("exception") or not it.get("canonical_url")]
        bili_found = []
        for it in unresolved:
            cand = multi_search_resolver.find_best_bilibili_candidate(it)
            if cand:
                bili_found.append(cand)
        if bili_found:
            b_res = multi_search_resolver.apply_search_resolutions_to_state(run_p, st, bili_found)
            applied_results.append({"source": "bilibili_search", "result": b_res})
        output({"actions": applied_results, "remaining_unresolved": sum(1 for it in st.get("items", []) if it.get("exception") or not it.get("canonical_url"))})
        return 0
    if args.command == "collect-plan":
        import real_collector
        with locked(args.run) as (run_p, st):
            old_hash = w.digest(st)
            scripts = {}
            items = st.get("items", [])

            runtime = getattr(args, "runtime", "all")
            req_platform = getattr(args, "platform", "all")
            if req_platform == "all":
                configured_plats = [
                    p for p in ("bilibili", "douyin")
                    if p in st.get("targets", {}) and p in st.get("permissions", {}).get("collect", [])
                ]
                target_plats = configured_plats or ["bilibili", "douyin"]
            else:
                target_plats = [req_platform]

            # 严格加载抖音已观看黑名单：显式指定缺失或文件 JSON 损坏必须报错，绝不静默清空黑名单
            watched_vids = []
            watched_file = args.watched_file
            explicit_watched = watched_file is not None
            if not watched_file and "douyin" in target_plats:
                env_watched = os.environ.get("WEEKLY_WATCHED_FILE", "").strip()
                if env_watched:
                    watched_file = Path(env_watched)
                    explicit_watched = True
                else:
                    candidates = [run_p / "douyin_watched_vids.json", Path.cwd() / "douyin_watched_vids.json"]
                    if not getattr(args, "no_default_watched", False):
                        candidates.append(DEFAULT_WATCHED_FILE)
                    for cand in candidates:
                        if cand.is_file():
                            watched_file = cand
                            break
            if explicit_watched:
                w.require(watched_file.is_file(), f"watched_file_missing: {watched_file}")
            if watched_file and watched_file.is_file():
                try:
                    raw_watched = json.loads(watched_file.read_text(encoding="utf-8-sig"))
                except Exception as exc:
                    raise w.GateError(f"invalid_watched_file_json: {watched_file}") from exc
                if isinstance(raw_watched, dict):
                    raw_watched = raw_watched.get("watched_vids") if "watched_vids" in raw_watched else raw_watched.get("vids")
                w.require(isinstance(raw_watched, list), f"invalid_watched_file_format_expected_list: {watched_file}")
                watched_vids = raw_watched

            # 建立按 ID 回查原始条目的索引，避免 plan["missing_ids"] 丢掉抖音 /note/ 与 /video/ 区分
            douyin_items_by_id = {}
            for it in items:
                if it.get("platform") == "douyin" and it.get("id"):
                    douyin_items_by_id.setdefault(str(it["id"]), it)

            (run_p / "evidence").mkdir(parents=True, exist_ok=True)
            for plat in target_plats:
                w.require(plat in st.get("targets", {}), f"collection_target_not_resolved: {plat}")
                target = st["targets"][plat]
                if plat == "bilibili":
                    plan = w.plan_collection(st, run_p, plat)
                    planned_ids = list(plan["missing_ids"])
                    entry = {
                        "plan_id": plan["plan_id"],
                        "missing_count": len(plan["missing_ids"]),
                        "target_folder": target["folder_name"],
                        "runtime": runtime,
                    }
                    if runtime in ("all", "codex"):
                        js = real_collector.generate_bilibili_browser_script(target["folder_name"], target["folder_id"], planned_ids)
                        f_path = run_p / "evidence" / "collect_bilibili.js"
                        f_path.write_text(js, encoding="utf-8")
                        entry["script_file"] = str(f_path)
                    if runtime in ("all", "antigravity"):
                        inpage_js = real_collector.generate_bilibili_inpage_js(target["folder_name"], target["folder_id"], planned_ids)
                        inpage_path = run_p / "evidence" / "collect_bilibili_inpage.js"
                        inpage_path.write_text(inpage_js, encoding="utf-8")
                        cdp_py = real_collector.generate_playwright_cdp_collector("bilibili", target["folder_name"], target["folder_id"], planned_ids)
                        cdp_path = run_p / "evidence" / "collect_bilibili_cdp.py"
                        cdp_path.write_text(cdp_py, encoding="utf-8")
                        entry["inpage_script_file"] = str(inpage_path)
                        entry["cdp_script_file"] = str(cdp_path)
                    scripts[plat] = entry
                else:
                    # 先校验抖音收藏前置门禁与写前快照，再把已观看排除项写入受证据保护的 collection_exclusions
                    w.ready_for_collection(st, run_p, "douyin")
                    w.require(st["snapshots"].get("douyin"), "full_fresh_snapshot_required")
                    watched_set = {str(x) for x in watched_vids}
                    dy_all_ids = w.expected_ids(st, "douyin", include_excluded=True)
                    matched_watched_ids = [vid for vid in dy_all_ids if vid in watched_set]
                    dy_exclusions = st.setdefault("collection_exclusions", {"douyin": {}, "bilibili": {}}).setdefault("douyin", {})
                    for old_vid in [k for k, v in list(dy_exclusions.items()) if v.get("reason") == "watched_excluded" and k not in set(matched_watched_ids)]:
                        dy_exclusions.pop(old_vid, None)
                    if watched_vids:
                        latest_snap_ids = set(st["snapshots"]["douyin"][-1]["ids"])
                        dead_ids = {k for k, v in dy_exclusions.items() if v.get("reason") == "dead_link_confirmed"}
                        unwatched_missing_count = sum(
                            1 for vid in dy_all_ids
                            if vid not in watched_set and vid not in dead_ids and vid not in latest_snap_ids
                        )
                        filter_report = {
                            "watched_file": str(watched_file),
                            "total_douyin_items": sum(1 for it in items if it.get("platform") == "douyin"),
                            "watched_excluded_count": len(matched_watched_ids),
                            "watched_excluded_ids": matched_watched_ids,
                            "unwatched_planned_count": unwatched_missing_count,
                        }
                        w.write_json(run_p / "evidence" / "douyin_watched_filtering.json", filter_report)
                        for vid in matched_watched_ids:
                            w.record_collection_exclusion(
                                st,
                                run_p,
                                "douyin",
                                vid,
                                "watched_excluded",
                                detail={"source": Path(watched_file).name, "watched_file": str(watched_file)},
                                evidence_files=["evidence/douyin_watched_filtering.json"],
                            )

                    plan = w.plan_collection(st, run_p, "douyin")
                    unwatched_plan_ids = list(plan["missing_ids"])
                    unwatched_plan_items = [
                        {
                            "id": str(vid),
                            "canonical_url": (douyin_items_by_id.get(str(vid)) or {}).get("canonical_url", ""),
                            "url": (douyin_items_by_id.get(str(vid)) or {}).get("canonical_url")
                            or (douyin_items_by_id.get(str(vid)) or {}).get("original_url")
                            or f"https://www.douyin.com/video/{vid}",
                        }
                        for vid in unwatched_plan_ids
                    ]
                    entry = {
                        "plan_id": plan["plan_id"],
                        "missing_count": len(plan["missing_ids"]),
                        "target_folder": target["folder_name"],
                        "runtime": runtime,
                    }
                    if runtime in ("all", "codex"):
                        js = real_collector.generate_douyin_browser_script(target["folder_name"], target["folder_id"], unwatched_plan_items)
                        f_path = run_p / "evidence" / "collect_douyin.js"
                        f_path.write_text(js, encoding="utf-8")
                        entry["script_file"] = str(f_path)
                    if runtime in ("all", "antigravity"):
                        inpage_js = real_collector.generate_douyin_inpage_helper_js(target["folder_name"], target["folder_id"], unwatched_plan_items)
                        inpage_path = run_p / "evidence" / "collect_douyin_inpage_helper.js"
                        inpage_path.write_text(inpage_js, encoding="utf-8")
                        cdp_py = real_collector.generate_playwright_cdp_collector("douyin", target["folder_name"], target["folder_id"], unwatched_plan_items)
                        cdp_path = run_p / "evidence" / "collect_douyin_cdp.py"
                        cdp_path.write_text(cdp_py, encoding="utf-8")
                        entry["inpage_script_file"] = str(inpage_path)
                        entry["cdp_script_file"] = str(cdp_path)
                    if watched_vids:
                        entry["watched_file"] = str(watched_file)
                        entry["watched_excluded_count"] = len(matched_watched_ids)
                        entry["watched_excluded_ids"] = matched_watched_ids
                        entry["unwatched_planned_count"] = len(unwatched_plan_ids)
                    scripts[plat] = entry

            # 持久化本次生成的 plan_id 与已观看排除记录，便于后续 import-collection / reconcile 直接使用
            if old_hash != w.digest(st):
                st["updated_at"] = w.now()
                w.atomic_text(run_p / "run.previous.json", (run_p / "run.json").read_text(encoding="utf-8"))
                w.write_json(run_p / "run.json", st)
                with (run_p / "events.jsonl").open("a", encoding="utf-8") as f:
                    f.write(w.compact({"at": st["updated_at"], "command": "collect-plan", "before_hash": old_hash, "after_hash": w.digest(st)}) + "\n")
            output({"ok": True, "plans": scripts})
        return 0
    if args.command == "init":
        run = args.run.resolve()
        w.require(not (run / "run.json").exists(), "run_exists_use_resume_not_reinitialize")
        state = w.initialize(w.read_json(args.manifest), args.mode)
        run.mkdir(parents=True, exist_ok=True)
        (run / "evidence").mkdir(exist_ok=True)
        w.write_json(run / "run.json", state)
        w.write_json(run / "evidence" / "seed-inventory.json", w.read_json(args.manifest))
        output({"run": str(run), "items": len(state["items"]), "mode": args.mode, "verified_links": 0, "next": "read assets/observations.example.json and references/operations.md; register source, verify inventory and every link"})
        return 0
    code = 0
    with locked(args.run) as (run, state):
        command = args.command
        old_hash = w.digest(state)
        if command == "apply":
            events = w.read_json(args.input)
            state = w.apply_events(state, run, events)
            result = {"applied": len(events), "content_hash": w.content_hash(state)}
        elif command == "capture-screenshots":
            import resolve_links
            result = resolve_links.capture_screenshots_for_run(run, state, args.keys)
        elif command == "embed-screenshots":
            import resolve_links
            receipts_path = args.receipts or (run / "evidence" / "feishu_image_receipts.json")
            w.require(receipts_path.is_file(), f"receipts_file_missing: {receipts_path}")
            receipts = w.read_json(receipts_path)
            result = resolve_links.embed_captured_screenshots_into_layout(run, state, receipts)
            w.write_json(run / "run.json", state)
            render_res = w.render(state, run, allow_partial=True)
            result["rendered_content_hash"] = render_res["content_hash"]
        elif command in {"scan-screenshots", "auto-heal"}:
            import resolve_links
            result = resolve_links.auto_heal_from_screenshots(run, state, args.keys)
        elif command == "audit":
            result = w.audit(state, run)
            w.write_json(run / "audit" / "latest.json", result)
            code = 0 if result["ok"] else 2
        elif command == "render":
            result = w.render(state, run, args.allow_partial)
        elif command == "publish-plan":
            result = w.publish_plan(state, run)
        elif command == "published":
            result = w.readback_publication(state, run, w.read_json(args.input))
        elif command == "snapshot":
            result = w.import_snapshot(state, run, w.read_json(args.input))
        elif command == "plan":
            result = w.plan_collection(state, run, args.platform)
        elif command == "begin-write":
            result = w.begin_write(state, run, args.platform, args.id, args.plan_id)
        elif command == "end-write":
            result = w.end_write(state, run, args.attempt_id, w.read_json(args.input))
        elif command == "import-collection":
            import real_collector
            raw_payload = w.read_json(args.input)
            result = real_collector.ingest_collection_result(
                run,
                state,
                raw_payload,
                platform=args.platform,
                plan_id=args.plan_id,
                persist=False,
            )
            code = 0 if result.get("ok") else 2
        elif command == "reconcile":
            result = w.reconcile(state, run, args.platform)
            w.write_json(run / "audit" / (args.platform + "-reconcile.json"), result)
            code = 0 if result["ok"] else 2
        elif command == "index":
            result = w.export_index(state, run, args.catalog)
        elif command == "finish":
            result = w.finish(state, run)
            result["index"] = w.export_index(state, run, args.catalog)
            w.write_json(run / "audit" / "delivery.json", result)
            code = 0 if result["status"] in {"complete", "audit_passed"} else 2
        else:
            result = {"run_id": state["run_id"], "issue": state["issue"], "status": state["status"], "mode": state["mode"], "counts": w.audit(state, run)["counts"], "permissions": state["permissions"], "pending_writes": [a for a in state["attempts"] if a["outcome"] in {"in_flight", "uncertain", "failed"} and not a.get("observed_in_snapshot")], "next": "audit -> recover evidence -> render -> publish/readback -> fresh snapshot -> plan/diff -> serial writes -> two snapshots/reconcile -> finish/index"}
        if old_hash != w.digest(state):
            state["updated_at"] = w.now()
            # A recoverable one-command backup, plus append-only command hashes (no secrets).
            w.atomic_text(run / "run.previous.json", (run / "run.json").read_text(encoding="utf-8"))
            w.write_json(run / "run.json", state)
            with (run / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(w.compact({"at": state["updated_at"], "command": command, "before_hash": old_hash, "after_hash": w.digest(state)}) + "\n")
        output(result)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (w.GateError, OSError, KeyError, TypeError, ValueError, w.ET.ParseError) as e:
        output({"ok": False, "error": str(e), "external_writes": 0, "note": "This helper does not call remote write APIs. Preserve checkpoint and repair evidence."})
        raise SystemExit(2)
