# coding: utf-8
"""阶段 7：周报全期视频提音、通义听悟转写（额度耗尽自动降级本机 FunASR）、伴读文档合成与云端转录库整合入库。

设计原则：
1. 直接复用本机 `通译转录` (`zhuanlu_pipeline`) 的免费音频提取、云端查重、通义听悟持久化浏览器与本机 RTX 4070 FunASR。
2. 全程逐条串行、即时落盘断点（`transcripts/manifest.json` 与 `transcripts/items/*.txt`），任意时刻中断均可无损续跑。
3. 严禁伪造或编造逐字稿；已失效视频、抖音纯图文笔记（`/note/`）、无语音纯音乐与提音受限项如实标注跳过。
4. 临时 MP3 音频转完即删，不留冗余大文件；通义听悟原记录完整保留，绝不删除。
5. 最终将整期伴读 Markdown 作为单独 1 条整合记录写入云端 `https://gz.grzdt.com/zhuanlu/`（分类：`直播回放与周刊`）并做 `verify-item` 回读校验。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import weekly_core as w

# 默认本机通译转录项目根目录（可通过 ZHUANLU_WORKSPACE_ROOT / ZHUANLU_PROJECT_ROOT 覆盖）
DEFAULT_ZHUANLU_WORKSPACE = Path(r"e:\工程项目\antigravity项目\通译转录")
DEFAULT_ZHUANLU_CATEGORY = "直播回放与周刊"
DEFAULT_ZHUANLU_STATUS = "未研究"
DEFAULT_ZHUANLU_PLATFORM = "且曼周报"

# 听悟页面额度/时长/空间受限判定词（命中即触发自动无缝降级到本机 RTX 4070 FunASR）
QUOTA_EXHAUSTED_RE = re.compile(r"时长不足|空间不足|超出限制|余额不足|额度不足|免费时长|上传受限")
# 疑似无语音/空正文提示词
NO_SPEECH_RE = re.compile(r"结果为空|正文为空|非空正文|无有效语音|静音|未识别到语音")
# 不可见格式控制字符（与通义听悟/zhuanlu 清洗规则对齐）
INVISIBLE_FORMAT_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
# 句末标点切分，用于给本机 FunASR 连续文本补自然段
SENTENCE_END_RE = re.compile(r"(?<=[。！？!?])\s*")

# 可直接跳过、无需重复执行的已完成状态集合
DONE_STATUSES = frozenset({
    "ok",
    "reused_cloud",
    "skipped_dead_link",
    "skipped_photo_note",
    "skipped_no_speech",
})


def _utc_now() -> str:
    """返回当前 UTC ISO 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(text: str) -> str:
    """将条目键转为安全 ASCII 文件名片段。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text or "").strip()).strip("_.")
    return (cleaned or "item")[:80]


def _format_duration(seconds: float | int | None) -> str:
    """将秒数格式化为 MM:SS 或 HH:MM:SS。"""
    if seconds is None:
        return "未知"
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "未知"
    if total <= 0:
        return "未知"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def clean_and_paragraph_text(raw_text: str, *, auto_paragraph: bool = False) -> str:
    """清除零宽水印与多余空白，并在需要时按句末标点合理分段。"""
    text = INVISIBLE_FORMAT_RE.sub("", str(raw_text or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    # 规范已有空行
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    paragraphs: list[str] = []
    buf: list[str] = []
    for ln in lines:
        if not ln:
            if buf:
                paragraphs.append(" ".join(buf).strip())
                buf = []
        else:
            buf.append(ln)
    if buf:
        paragraphs.append(" ".join(buf).strip())

    if auto_paragraph and len(paragraphs) == 1 and len(paragraphs[0]) > 260:
        sentences = [s.strip() for s in SENTENCE_END_RE.split(paragraphs[0]) if s.strip()]
        if len(sentences) > 1:
            rebuilt: list[str] = []
            cur = ""
            for sent in sentences:
                if cur and len(cur) + len(sent) > 220:
                    rebuilt.append(cur.strip())
                    cur = sent
                else:
                    cur = (cur + sent) if cur else sent
            if cur.strip():
                rebuilt.append(cur.strip())
            paragraphs = rebuilt

    return "\n\n".join(p for p in paragraphs if p)


def _ensure_zhuanlu_importable(workspace_root: Path | str | None = None) -> Path:
    """定位通译转录工作区并将其 src 目录加入 sys.path。"""
    raw = (
        str(workspace_root).strip()
        if workspace_root is not None and str(workspace_root).strip()
        else (
            os.environ.get("ZHUANLU_WORKSPACE_ROOT")
            or os.environ.get("ZHUANLU_PROJECT_ROOT")
            or str(DEFAULT_ZHUANLU_WORKSPACE)
        ).strip()
    )
    root = Path(raw).expanduser().resolve()
    src_dir = root / "src"
    if not src_dir.is_dir():
        raise FileNotFoundError(f"未找到通译转录源码目录：{src_dir}")
    src_str = str(src_dir)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)
    os.environ.setdefault("ZHUANLU_WORKSPACE_ROOT", str(root))
    os.environ.setdefault("ZHUANLU_DATA_DIR", str(root / "data"))
    return root


def _is_dead_video_item(state: dict[str, Any], item: dict[str, Any]) -> tuple[bool, str]:
    """判断该视频条目在当前周报状态中是否已确认失效或无原链接。"""
    if item.get("exception"):
        reason = str((item.get("exception") or {}).get("reason") or "原作品未确认")
        return True, f"原作品未确认（{reason}）"
    url = str(item.get("canonical_url") or "").strip()
    if not url:
        return True, "缺少已核验原作品链接"
    platform = str(item.get("platform") or "")
    vid = str(item.get("id") or "")
    exc = (
        state.get("collection_exclusions", {})
        .get(platform, {})
        .get(vid)
    )
    if isinstance(exc, dict) and exc.get("reason") == "dead_link_confirmed":
        detail = exc.get("detail") or {}
        msg = str(detail.get("message") or detail.get("status") or "平台显示视频已下架或不存在")
        return True, f"原视频已下架/不可访问（{msg}）"
    return False, ""


def _is_douyin_photo_note(item: dict[str, Any]) -> bool:
    """判断是否为抖音纯图文笔记（/note/），默认无口播音轨。"""
    platform = str(item.get("platform") or "")
    url = str(item.get("canonical_url") or item.get("original_url") or "")
    return platform == "douyin" and "/note/" in url.lower()


def _extract_core_id_from_url(url: str) -> tuple[str, str]:
    """从链接提取 (平台标识, 核心ID)，用于与云端 zhuanlu 列表做高速内存查重。"""
    u = str(url or "").strip()
    bv = re.search(r"(?:BV|bv)[0-9A-Za-z]{10}", u)
    if bv:
        return "bilibili", "BV" + bv.group(0)[2:]
    dy = re.search(r"douyin\.com/(?:video|note)/([0-9]{19})", u)
    if dy:
        return "douyin", dy.group(1)
    dy_modal = re.search(r"modal_id=([0-9]{19})", u)
    if dy_modal:
        return "douyin", dy_modal.group(1)
    return "", ""


def _build_cloud_index(cloud_items: list[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """把云端可见条目列表建为 {(platform, core_id): meta} 内存索引。"""
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in cloud_items or []:
        if isinstance(entry, dict):
            item_id = entry.get("id")
            src_url = str(entry.get("source_url") or "")
            title = str(entry.get("title") or "")
            chars = int(entry.get("chars") or 0)
        else:
            item_id = getattr(entry, "id", None)
            src_url = str(getattr(entry, "source_url", "") or "")
            title = str(getattr(entry, "title", "") or "")
            chars = int(getattr(entry, "chars", 0) or 0)
        if not item_id or chars <= 0:
            continue
        plat, cid = _extract_core_id_from_url(src_url)
        if plat and cid and (plat, cid) not in idx:
            idx[(plat, cid)] = {
                "id": int(item_id),
                "title": title,
                "source_url": src_url,
                "chars": chars,
            }
    return idx


def _load_or_init_manifest(manifest_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    """加载已有断点清册，或按当期周报初始化空白清册。"""
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    return {
        "schema_version": 1,
        "run_id": state.get("run_id", ""),
        "issue_title": state.get("issue", {}).get("title", ""),
        "week": state.get("issue", {}).get("week", ""),
        "document_url": state.get("publication", {}).get("document_url", ""),
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "quota_exhausted_at": None,
        "entries": {},
    }


def _save_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    """原子落盘断点清册，防止中断损坏。"""
    manifest["updated_at"] = _utc_now()
    w.write_json(manifest_path, manifest)


def _compute_stats(state: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """统计整期视频与图文条目的转写覆盖情况。"""
    entries = manifest.get("entries", {})
    video_items = [it for it in state.get("items", []) if it.get("kind") == "video"]
    non_video_items = [it for it in state.get("items", []) if it.get("kind") != "video"]

    counts = {
        "total_items": len(state.get("items", [])),
        "video_total": len(video_items),
        "non_video_total": len(non_video_items),
        "transcribed_ok": 0,
        "reused_cloud": 0,
        "engine_tingwu": 0,
        "engine_funasr_fallback": 0,
        "skipped_dead_link": 0,
        "skipped_photo_note": 0,
        "skipped_no_speech": 0,
        "extract_failed": 0,
        "transcribe_failed": 0,
        "pending": 0,
        "total_transcript_chars": 0,
    }

    for it in video_items:
        rec = entries.get(it["key"])
        if not isinstance(rec, dict):
            counts["pending"] += 1
            continue
        st = rec.get("status", "pending")
        if st == "reused_cloud":
            counts["reused_cloud"] += 1
            counts["transcribed_ok"] += 1
            counts["total_transcript_chars"] += int(rec.get("chars") or 0)
        elif st == "ok":
            counts["transcribed_ok"] += 1
            counts["total_transcript_chars"] += int(rec.get("chars") or 0)
            eng = str(rec.get("engine") or "")
            if "funasr" in eng:
                counts["engine_funasr_fallback"] += 1
            else:
                counts["engine_tingwu"] += 1
        elif st in {
            "skipped_dead_link",
            "skipped_photo_note",
            "skipped_no_speech",
            "extract_failed",
            "transcribe_failed",
        }:
            counts[st] += 1
        else:
            counts["pending"] += 1

    return counts


def _platform_label(platform: str) -> str:
    """将内部平台码转换为中文显示名。"""
    return {
        "douyin": "抖音",
        "bilibili": "B站",
        "web": "网页",
        "note": "图文/随笔",
    }.get(platform, platform or "其他")


def _engine_label(rec: dict[str, Any]) -> str:
    """返回单条记录可读的来源引擎标签。"""
    st = rec.get("status", "")
    if st == "reused_cloud":
        cid = rec.get("zhuanlu_source_id")
        return f"云端转录库复用（#{cid}）" if cid else "云端转录库复用"
    eng = str(rec.get("engine") or "")
    if "funasr" in eng:
        return "本机离线转写（FunASR）"
    if "tingwu" in eng:
        return "通义听悟"
    return eng or "未知"


def render_companion_markdown(
    state: dict[str, Any],
    run_dir: Path,
    manifest: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """严格按周报原板块与编号结构，合成整期 AI 伴读 Markdown 文档。"""
    issue_title = str(state.get("issue", {}).get("title") or "周报").strip()
    week = str(state.get("issue", {}).get("week") or "").strip()
    doc_url = str(state.get("publication", {}).get("document_url") or "").strip()
    entries = manifest.get("entries", {})
    stats = _compute_stats(state, manifest)

    lines: list[str] = [
        f"# {issue_title} · 全期视频伴读逐字稿",
        "",
        "> **给 AI 的伴读说明**：本文档按当期周报原始板块与编号 1:1 排布，整合了全部有效抖音/B站视频的去时间轴完整逐字稿以及网页文章/工具简介。用户在看视频时可直接按「板块 + 序号」或标题关键词向你提问、延展原理或对比观点。",
        "",
        "## 本期伴读总览",
        "",
        f"- **期号**：{issue_title}" + (f"（{week}）" if week else ""),
    ]
    if doc_url:
        lines.append(f"- **飞书周报**：{doc_url}")
    lines.extend([
        f"- **生成时间**：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        (
            f"- **内容统计**：共 {stats['total_items']} 条"
            f"（视频 {stats['video_total']} 条，网页/工具/快讯 {stats['non_video_total']} 条）；"
            f"视频已出稿 **{stats['transcribed_ok']}** 条"
            f"（通义听悟 {stats['engine_tingwu']} 条 / 云端复用 {stats['reused_cloud']} 条 / 本机离线兜底 {stats['engine_funasr_fallback']} 条），"
            f"跳过 {stats['skipped_dead_link'] + stats['skipped_photo_note'] + stats['skipped_no_speech']} 条"
            f"（失效 {stats['skipped_dead_link']} / 图文笔记 {stats['skipped_photo_note']} / 无口播 {stats['skipped_no_speech']}），"
            f"失败或待补 {stats['extract_failed'] + stats['transcribe_failed'] + stats['pending']} 条；"
            f"逐字稿总计约 **{stats['total_transcript_chars']:,}** 字。"
        ),
        "",
        "## 速查编号目录",
        "",
    ])

    # 生成速查目录（严格按 layout 中的条目顺序）
    items_by_key = {it["key"]: it for it in state.get("items", [])}
    for block in state.get("layout", []):
        if block.get("type") != "item":
            continue
        it = items_by_key.get(block.get("key", ""))
        if not it:
            continue
        num_prefix = f"{it['number']}. " if it.get("number") is not None else ""
        sec_tag = str(it.get("section") or "").lstrip("# ").strip()
        if it.get("subsection"):
            sec_tag = f"{sec_tag} / {str(it['subsection']).lstrip('# ').strip()}"
        if it.get("kind") == "video":
            rec = entries.get(it["key"], {})
            st = rec.get("status", "pending")
            if st in {"ok", "reused_cloud"}:
                badge = f"{_platform_label(it.get('platform', ''))} · {_format_duration(rec.get('duration_sec'))} · {int(rec.get('chars') or 0)}字"
            elif st == "skipped_dead_link":
                badge = f"{_platform_label(it.get('platform', ''))} · 已跳过（原视频下架）"
            elif st == "skipped_photo_note":
                badge = f"{_platform_label(it.get('platform', ''))} · 已跳过（图文笔记）"
            elif st == "skipped_no_speech":
                badge = f"{_platform_label(it.get('platform', ''))} · 已跳过（无口播/纯音乐）"
            elif st in {"extract_failed", "transcribe_failed"}:
                short_reason = " ".join(str(rec.get("reason") or "异常").split())[:60]
                badge = f"{_platform_label(it.get('platform', ''))} · 未成功（{short_reason}）"
            else:
                badge = f"{_platform_label(it.get('platform', ''))} · 待转写"
        else:
            badge = "网页/工具/快讯"
        lines.append(f"- [{sec_tag}] {num_prefix}{it['title']}（{badge}）")

    lines.extend(["", "---", ""])

    # 按周报原始布局输出完整正文
    current_level = 2
    for block in state.get("layout", []):
        btype = block.get("type")
        if btype == "heading":
            current_level = int(block.get("level") or 2)
            heading_text = str(block.get("text") or "").lstrip("# ").strip()
            lines.extend(["#" * current_level + " " + heading_text, ""])
        elif btype == "paragraph":
            if "runs" in block and isinstance(block["runs"], list):
                p_text = "".join(str(r.get("text") or "") for r in block["runs"]).strip()
            else:
                p_text = str(block.get("text") or "").strip()
            if p_text:
                lines.extend([p_text, ""])
        elif btype == "item":
            it = items_by_key.get(block.get("key", ""))
            if not it:
                continue
            item_level = min(current_level + 1, 6)
            num_prefix = f"{it['number']}. " if it.get("number") is not None else ""
            lines.append("#" * item_level + f" {num_prefix}{it['title']}")
            lines.append("")

            url = str(it.get("canonical_url") or it.get("original_url") or "").strip()
            if it.get("kind") == "video":
                rec = entries.get(it["key"], {})
                st = rec.get("status", "pending")
                plat_name = _platform_label(it.get("platform", ""))
                meta_parts = [f"平台：{plat_name}"]
                if rec.get("duration_sec"):
                    meta_parts.append(f"时长：{_format_duration(rec.get('duration_sec'))}")
                if st in {"ok", "reused_cloud"}:
                    meta_parts.append(f"来源：{_engine_label(rec)}")
                    meta_parts.append(f"字数：{int(rec.get('chars') or 0):,}")
                lines.append("- " + " | ".join(meta_parts))
                if url:
                    lines.append(f"- 原链接：{url}")
                if it.get("body"):
                    lines.append(f"- 附注：{it['body']}")
                lines.append("")

                if st in {"ok", "reused_cloud"}:
                    txt_rel = str(rec.get("text_file") or "")
                    txt_content = ""
                    if txt_rel:
                        txt_p = (run_dir / txt_rel).resolve()
                        if txt_p.is_file():
                            txt_content = txt_p.read_text(encoding="utf-8").strip()
                    if txt_content:
                        lines.extend([txt_content, ""])
                    else:
                        lines.extend(["> 【异常：逐字稿文件缺失，请重新运行补全】", ""])
                elif st == "skipped_dead_link":
                    lines.extend([f"> 【已跳过：{rec.get('reason') or '原视频已下架或不可访问'}】", ""])
                elif st == "skipped_photo_note":
                    lines.extend(["> 【已跳过：抖音图文笔记，无口播音轨】", ""])
                elif st == "skipped_no_speech":
                    lines.extend([f"> 【已跳过：{rec.get('reason') or '未识别到有效口播人声（纯音乐或静音）'}】", ""])
                elif st in {"extract_failed", "transcribe_failed"}:
                    lines.extend([f"> 【转写未完成：{rec.get('reason') or '音频提取或转写受限'}】", ""])
                else:
                    lines.extend(["> 【待转写：尚未执行到该条】", ""])
            else:
                if url:
                    lines.append(f"- 类型：网页/工具 | 原链接：{url}")
                else:
                    lines.append("- 类型：周报快讯")
                if it.get("body"):
                    lines.append(f"- 简介：{it['body']}")
                lines.append("")

    md_text = "\n".join(lines).rstrip() + "\n"
    return md_text, stats


def build_zhuanlu_summary(state: dict[str, Any], stats: dict[str, Any]) -> str:
    """生成符合 zhuanlu 规范（60~140 字）的整期伴读中文摘要。"""
    issue_title = str(state.get("issue", {}).get("title") or "且曼内刊").strip()
    sections = []
    for it in state.get("items", []):
        sec = str(it.get("section") or "").lstrip("# ").strip()
        if sec and sec not in sections and sec != "星球快讯":
            sections.append(sec)
    sec_preview = "、".join(sections[:5])
    skip_cnt = stats["skipped_dead_link"] + stats["skipped_photo_note"] + stats["skipped_no_speech"]
    summary = (
        f"{issue_title} 全期视频伴读逐字稿合集，按周报原编号整合「{sec_preview}」等板块。"
        f"共收录 {stats['total_items']} 条内容，其中 {stats['transcribed_ok']}/{stats['video_total']} 条视频已完成无时间轴逐字稿"
        f"（约 {stats['total_transcript_chars']} 字，跳过失效/图文/无口播 {skip_cnt} 条），便于看视频时直接交给 AI 深度问答。"
    )
    return summary[:195]


def run_build_transcripts(
    run_dir: Path | str,
    *,
    engine: str = "tingwu",
    headless: bool = False,
    upload_to_zhuanlu: bool = True,
    upload_partial: bool = False,
    zhuanlu_category: str = DEFAULT_ZHUANLU_CATEGORY,
    zhuanlu_status: str = DEFAULT_ZHUANLU_STATUS,
    zhuanlu_workspace: Path | str | None = None,
    limit: int | None = None,
    force: bool = False,
    keep_audio: bool = False,
    progress_cb: Callable[[str], None] | None = None,
    # 依赖注入钩子（便于单元测试零外网全分支覆盖）
    cloud_list_fn: Callable[[], list[Any]] | None = None,
    cloud_get_fn: Callable[[int], Any] | None = None,
    cloud_save_fn: Callable[..., Any] | None = None,
    cloud_verify_fn: Callable[[int], Any] | None = None,
    extract_audio_fn: Callable[..., Any] | None = None,
    tingwu_fn: Callable[..., Any] | None = None,
    funasr_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行阶段 7：周报全期视频提音转写、伴读 Markdown 合成与云端 zhuanlu 整合入库。"""
    run_p = Path(run_dir).resolve()
    run_json_path = run_p / "run.json"
    w.require(run_json_path.is_file(), "run_missing_initialize_first")
    state = w.read_json(run_json_path)

    if engine not in {"tingwu", "local"}:
        raise ValueError(f"unsupported_transcript_engine: {engine}")

    # 懒加载本机 zhuanlu_pipeline 模块（若全部已注入测试替身则跳过外部导入）
    need_real_zhuanlu = any(
        fn is None
        for fn in (cloud_list_fn, cloud_get_fn, cloud_save_fn, cloud_verify_fn, extract_audio_fn, tingwu_fn, funasr_fn)
    )
    zh_root: Path | None = None
    if need_real_zhuanlu:
        zh_root = _ensure_zhuanlu_importable(zhuanlu_workspace)
        from zhuanlu_pipeline import audio_extractor, local_asr, tingwu_browser, url_parser, zhuanlu_client

        client = zhuanlu_client.ZhuanluClient()
        if cloud_list_fn is None:
            cloud_list_fn = client.list_visible_metadata
        if cloud_get_fn is None:
            cloud_get_fn = client.get_item
        if cloud_save_fn is None:
            def _real_save(
                *,
                title: str,
                text: str,
                summary: str,
                category: str,
                source_url: str,
                platform: str,
                status: str,
                overwrite: bool,
            ) -> dict[str, Any]:
                saved_item, act = client.save_item(
                    title,
                    text,
                    source_url=source_url,
                    platform=platform,
                    summary=summary,
                    category=category,
                    status=status,
                    overwrite=overwrite,
                )
                return {
                    "item_id": saved_item.id,
                    "detail_url": saved_item.detail_url,
                    "action": act,
                    "chars": saved_item.chars,
                }
            cloud_save_fn = _real_save
        if cloud_verify_fn is None:
            def _real_verify(item_id: int) -> dict[str, Any]:
                ver_item = client.verify_item(int(item_id), require_nonempty_text=True)
                return {
                    "ok": True,
                    "fts_ok": True,
                    "item_id": ver_item.id,
                    "chars": ver_item.chars,
                    "detail_url": ver_item.detail_url,
                }
            cloud_verify_fn = _real_verify
        if extract_audio_fn is None:
            def _real_extract(url: str, out_dir: Path, title_hint: str) -> Any:
                parsed_list = url_parser.parse_inputs(url)
                if not parsed_list:
                    raise RuntimeError(f"无法解析视频链接: {url}")
                src_obj = parsed_list[0]
                if title_hint and not src_obj.title_hint:
                    from dataclasses import replace
                    src_obj = replace(src_obj, title_hint=title_hint)
                return audio_extractor.extract_audio(src_obj, out_dir=out_dir)
            extract_audio_fn = _real_extract
        if tingwu_fn is None:
            def _real_tingwu(audio_path: Path, tag: str, out_txt: Path, logs_dir: Path) -> Any:
                return tingwu_browser.transcribe_via_tingwu_persistent(
                    audio_path,
                    tag=tag,
                    headless=headless,
                    output_txt=out_txt,
                    logs_dir=logs_dir,
                )
            tingwu_fn = _real_tingwu
        if funasr_fn is None:
            def _real_funasr(audio_path: Path, out_txt: Path) -> Any:
                try:
                    return local_asr.transcribe_with_funasr(
                        audio_path,
                        output_txt=out_txt,
                        workspace_root=zh_root,
                    )
                except Exception as first_exc:  # noqa: BLE001
                    if NO_SPEECH_RE.search(str(first_exc)):
                        raise
                    # 长音频单次推理爆显存/异常时，自动按 20 分钟切段并降低 batch_size_s 逐段转写拼接
                    ffmpeg_bin = (
                        str(audio_extractor.DEFAULT_FFMPEG_PATH)
                        if audio_extractor.DEFAULT_FFMPEG_PATH.is_file()
                        else (shutil.which("ffmpeg") or "")
                    )
                    if not ffmpeg_bin:
                        raise
                    with tempfile.TemporaryDirectory(prefix="weekly_asr_seg_") as seg_dir_str:
                        seg_dir = Path(seg_dir_str)
                        seg_pat = seg_dir / "part_%03d.mp3"
                        proc = subprocess.run(
                            [
                                ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
                                "-i", str(audio_path),
                                "-f", "segment", "-segment_time", "1200", "-c", "copy",
                                str(seg_pat),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=180,
                            check=False,
                        )
                        parts = sorted(seg_dir.glob("part_*.mp3"))
                        if proc.returncode != 0 or len(parts) <= 1:
                            raise first_exc
                        seg_texts: list[str] = []
                        for idx_p, part_file in enumerate(parts, 1):
                            _log(f"[Stage7]   ↳ 长音频分段转写 [{idx_p}/{len(parts)}]：{part_file.name}")
                            try:
                                seg_res = local_asr.transcribe_with_funasr(
                                    part_file,
                                    batch_size_s=120,
                                    workspace_root=zh_root,
                                )
                                seg_txt = str(getattr(seg_res, "text", "") or "").strip()
                                if seg_txt:
                                    seg_texts.append(seg_txt)
                            except Exception as seg_exc:  # noqa: BLE001
                                if NO_SPEECH_RE.search(str(seg_exc)):
                                    continue
                                raise
                        merged = "\n\n".join(seg_texts).strip()
                        if not merged:
                            raise RuntimeError("FunASR 分段转写结果为空") from first_exc
                        out_txt.parent.mkdir(parents=True, exist_ok=True)
                        out_txt.write_text(merged, encoding="utf-8")
                        return {"text": merged}
            funasr_fn = _real_funasr

    def _log(msg: str) -> None:
        if progress_cb is not None:
            progress_cb(msg)
        else:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()

    transcripts_dir = run_p / "transcripts"
    items_dir = transcripts_dir / "items"
    tmp_audio_dir = transcripts_dir / "_tmp_audio"
    logs_dir = transcripts_dir / "logs"
    for d in (transcripts_dir, items_dir, tmp_audio_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    manifest_path = transcripts_dir / "manifest.json"
    manifest = _load_or_init_manifest(manifest_path, state)
    manifest["issue_title"] = state.get("issue", {}).get("title", "")
    manifest["document_url"] = state.get("publication", {}).get("document_url", "")
    entries = manifest["entries"]

    # 1. 一次性拉取云端 zhuanlu 已有条目索引，命中直接秒复用（拉取失败不阻断，只记告警）
    cloud_index: dict[tuple[str, str], dict[str, Any]] = {}
    cloud_list_error = ""
    try:
        cloud_items = cloud_list_fn() if cloud_list_fn is not None else []
        cloud_index = _build_cloud_index(cloud_items)
        _log(f"[Stage7] 已加载云端转录库查重索引：{len(cloud_index)} 条视频可复用")
    except Exception as exc:  # noqa: BLE001
        cloud_list_error = str(exc)
        _log(f"[Stage7] 云端查重列表暂时不可达（将直接提取转写）：{cloud_list_error}")

    video_items = [it for it in state.get("items", []) if it.get("kind") == "video"]
    quota_exhausted = bool(manifest.get("quota_exhausted_at")) and engine == "tingwu"
    processed_this_call = 0
    need_login_info: dict[str, Any] | None = None

    # 2. 逐条串行处理视频条目（实时写断点，临时 MP3 用完即删）
    for seq_idx, it in enumerate(video_items, 1):
        key = it["key"]
        existing = entries.get(key)
        if not force and isinstance(existing, dict) and existing.get("status") in DONE_STATUSES:
            # 校验文本文件确实还在才跳过
            if existing.get("status") in {"ok", "reused_cloud"}:
                rel = str(existing.get("text_file") or "")
                if rel and (run_p / rel).is_file() and (run_p / rel).stat().st_size > 0:
                    continue
            else:
                continue

        if limit is not None and processed_this_call >= int(limit):
            break

        processed_this_call += 1
        title = str(it.get("title") or key)
        platform = str(it.get("platform") or "")
        vid = str(it.get("id") or "")
        url = str(it.get("canonical_url") or it.get("original_url") or "").strip()
        per_item_rel = f"transcripts/items/{seq_idx:03d}_{_safe_filename(key)}.txt"
        per_item_abs = run_p / per_item_rel

        # 2.1 已确认失效视频直接跳过
        is_dead, dead_reason = _is_dead_video_item(state, it)
        if is_dead:
            entries[key] = {
                "key": key,
                "seq": seq_idx,
                "title": title,
                "platform": platform,
                "id": vid,
                "url": url,
                "status": "skipped_dead_link",
                "reason": dead_reason,
                "updated_at": _utc_now(),
            }
            _save_manifest(manifest_path, manifest)
            _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 跳过失效视频：{title}")
            continue

        # 2.2 抖音纯图文笔记跳过（无口播音轨）
        if _is_douyin_photo_note(it):
            entries[key] = {
                "key": key,
                "seq": seq_idx,
                "title": title,
                "platform": platform,
                "id": vid,
                "url": url,
                "status": "skipped_photo_note",
                "reason": "抖音图文笔记无口播音轨",
                "updated_at": _utc_now(),
            }
            _save_manifest(manifest_path, manifest)
            _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 跳过抖音图文笔记：{title}")
            continue

        # 2.3 云端 zhuanlu 命中直接复用（零提音、零听悟额度）
        plat_k, cid_k = _extract_core_id_from_url(url)
        cloud_hit = cloud_index.get((plat_k or platform, cid_k or vid))
        if cloud_hit and not force and cloud_get_fn is not None:
            try:
                cloud_detail = cloud_get_fn(int(cloud_hit["id"]))
                raw_cloud_text = (
                    cloud_detail.get("text", "")
                    if isinstance(cloud_detail, dict)
                    else getattr(cloud_detail, "text", "")
                )
                cleaned = clean_and_paragraph_text(raw_cloud_text, auto_paragraph=False)
                if cleaned:
                    per_item_abs.write_text(cleaned, encoding="utf-8")
                    entries[key] = {
                        "key": key,
                        "seq": seq_idx,
                        "title": title,
                        "platform": platform,
                        "id": vid,
                        "url": url,
                        "status": "reused_cloud",
                        "engine": "zhuanlu_cloud_cache",
                        "zhuanlu_source_id": int(cloud_hit["id"]),
                        "chars": len(cleaned),
                        "text_file": per_item_rel,
                        "updated_at": _utc_now(),
                    }
                    _save_manifest(manifest_path, manifest)
                    _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 复用云端转录 #{cloud_hit['id']}：{title}（{len(cleaned)}字）")
                    continue
            except Exception as exc:  # noqa: BLE001
                _log(f"[Stage7] 云端拉取 #{cloud_hit['id']} 失败，改走提取转写：{exc}")

        # 2.4 免费提取轻量化 16kHz 单声道 MP3
        _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 提取音频：{title}")
        audio_path_obj: Path | None = None
        duration_sec = 0.0
        try:
            ext_res = extract_audio_fn(url, tmp_audio_dir, title)
        except Exception as exc:  # noqa: BLE001
            entries[key] = {
                "key": key,
                "seq": seq_idx,
                "title": title,
                "platform": platform,
                "id": vid,
                "url": url,
                "status": "extract_failed",
                "reason": f"提音异常：{exc}",
                "updated_at": _utc_now(),
            }
            _save_manifest(manifest_path, manifest)
            _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 提音异常跳过：{exc}")
            continue

        ext_ok = bool(ext_res.get("success") if isinstance(ext_res, dict) else getattr(ext_res, "success", False))
        if not ext_ok:
            ext_status = str(ext_res.get("status") if isinstance(ext_res, dict) else getattr(ext_res, "status", "error"))
            ext_msg = str(ext_res.get("message") if isinstance(ext_res, dict) else getattr(ext_res, "message", "提音失败"))
            # 如果平台明确无音轨，直接归类为无口播跳过
            final_status = "skipped_no_speech" if "无音轨" in ext_msg or "不包含音频流" in ext_msg else "extract_failed"
            entries[key] = {
                "key": key,
                "seq": seq_idx,
                "title": title,
                "platform": platform,
                "id": vid,
                "url": url,
                "status": final_status,
                "extract_status": ext_status,
                "reason": ext_msg,
                "updated_at": _utc_now(),
            }
            _save_manifest(manifest_path, manifest)
            _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 提音未通过（{ext_status}）：{ext_msg}")
            continue

        audio_str = str(ext_res.get("audio_path") if isinstance(ext_res, dict) else getattr(ext_res, "audio_path", ""))
        duration_sec = float(ext_res.get("duration_sec") if isinstance(ext_res, dict) else getattr(ext_res, "duration_sec", 0.0) or 0.0)
        audio_path_obj = Path(audio_str).resolve()

        # 2.5 转文字：优先通义听悟，额度耗尽自动无缝切本机 RTX 4070 FunASR
        try:
            use_local_now = (engine == "local") or quota_exhausted
            transcript_text = ""
            used_engine = ""
            tingwu_url = ""
            fallback_reason = ""

            if not use_local_now:
                _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 通义听悟转写中（{_format_duration(duration_sec)}）：{title}")
                try:
                    tw_res = tingwu_fn(audio_path_obj, f"{platform}_{vid or key}", per_item_abs, logs_dir)
                    tw_ok = bool(tw_res.get("ok") if isinstance(tw_res, dict) else getattr(tw_res, "ok", False))
                    tw_status = str(tw_res.get("status") if isinstance(tw_res, dict) else getattr(tw_res, "status", ""))
                    tw_msg = str(tw_res.get("message") if isinstance(tw_res, dict) else getattr(tw_res, "message", ""))
                    tingwu_url = str(tw_res.get("detail_url") if isinstance(tw_res, dict) else getattr(tw_res, "detail_url", ""))
                except Exception as tw_exc:  # noqa: BLE001
                    tw_res = None
                    tw_ok = False
                    tw_status = "error"
                    tw_msg = f"通义听悟调用异常：{tw_exc}"
                    tingwu_url = ""

                if tw_status == "need_login":
                    need_login_info = {
                        "key": key,
                        "seq": seq_idx,
                        "title": title,
                        "message": tw_msg or "通义听悟专用浏览器尚未登录或登录已过期，请先运行 tingwu-login 扫码登录。",
                    }
                    _log(f"[Stage7] 检测到通义听悟需要登录，已保存断点并立即暂停：{need_login_info['message']}")
                    break

                if tw_ok:
                    raw_tw = str(tw_res.get("text") if isinstance(tw_res, dict) else getattr(tw_res, "text", ""))
                    transcript_text = clean_and_paragraph_text(raw_tw, auto_paragraph=False)
                    used_engine = "tingwu"
                else:
                    if QUOTA_EXHAUSTED_RE.search(tw_msg):
                        quota_exhausted = True
                        manifest["quota_exhausted_at"] = _utc_now()
                        _save_manifest(manifest_path, manifest)
                        fallback_reason = f"通义听悟额度受限（{tw_msg}），自动降级本机 FunASR"
                        _log(f"[Stage7] ⚠️ {fallback_reason}")
                    else:
                        fallback_reason = f"通义听悟单条异常（{tw_msg}），尝试本机 FunASR 复核"
                        _log(f"[Stage7] {fallback_reason}")
                    use_local_now = True

            if use_local_now and not transcript_text:
                _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 本机 RTX 4070 FunASR 离线转写中：{title}")
                try:
                    fa_res = funasr_fn(audio_path_obj, per_item_abs)
                    raw_fa = str(fa_res.get("text") if isinstance(fa_res, dict) else getattr(fa_res, "text", ""))
                    transcript_text = clean_and_paragraph_text(raw_fa, auto_paragraph=True)
                    used_engine = "funasr_local_fallback" if engine == "tingwu" else "funasr_local"
                except Exception as fa_exc:  # noqa: BLE001
                    fa_msg = str(fa_exc)
                    if NO_SPEECH_RE.search(fa_msg):
                        entries[key] = {
                            "key": key,
                            "seq": seq_idx,
                            "title": title,
                            "platform": platform,
                            "id": vid,
                            "url": url,
                            "duration_sec": round(duration_sec, 2),
                            "status": "skipped_no_speech",
                            "reason": "未识别到有效口播人声（可能为纯音乐或静音）",
                            "updated_at": _utc_now(),
                        }
                    else:
                        entries[key] = {
                            "key": key,
                            "seq": seq_idx,
                            "title": title,
                            "platform": platform,
                            "id": vid,
                            "url": url,
                            "duration_sec": round(duration_sec, 2),
                            "status": "transcribe_failed",
                            "reason": f"{fallback_reason + '；' if fallback_reason else ''}FunASR 失败：{fa_msg}",
                            "updated_at": _utc_now(),
                        }
                    _save_manifest(manifest_path, manifest)
                    _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 离线转写未出稿：{fa_msg}")
                    continue

            if not transcript_text:
                entries[key] = {
                    "key": key,
                    "seq": seq_idx,
                    "title": title,
                    "platform": platform,
                    "id": vid,
                    "url": url,
                    "duration_sec": round(duration_sec, 2),
                    "status": "skipped_no_speech",
                    "reason": "转写结果为空（无有效口播人声）",
                    "updated_at": _utc_now(),
                }
                _save_manifest(manifest_path, manifest)
                continue

            per_item_abs.write_text(transcript_text, encoding="utf-8")
            entries[key] = {
                "key": key,
                "seq": seq_idx,
                "title": title,
                "platform": platform,
                "id": vid,
                "url": url,
                "duration_sec": round(duration_sec, 2),
                "status": "ok",
                "engine": used_engine,
                "tingwu_detail_url": tingwu_url,
                "fallback_reason": fallback_reason,
                "chars": len(transcript_text),
                "text_file": per_item_rel,
                "updated_at": _utc_now(),
            }
            _save_manifest(manifest_path, manifest)
            _log(f"[Stage7] [{seq_idx}/{len(video_items)}] 完成（{used_engine}，{len(transcript_text)}字）：{title}")
        finally:
            # 转写结束后自动清理临时 MP3，避免堆积磁盘
            if not keep_audio and audio_path_obj is not None and audio_path_obj.is_file():
                try:
                    audio_path_obj.unlink(missing_ok=True)
                except OSError:
                    pass

    # 2.9 清理残留的临时提取子目录（如提音中途失败留下的 .tmp_extract_*）
    if not keep_audio and tmp_audio_dir.is_dir():
        for leftover in tmp_audio_dir.iterdir():
            try:
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink(missing_ok=True)
            except OSError:
                pass

    # 3. 合成整期伴读 Markdown 文档
    md_text, stats = render_companion_markdown(state, run_p, manifest)
    md_rel = "report/companion_transcript.md"
    md_abs = run_p / md_rel
    w.atomic_text(md_abs, md_text)

    # 若因通义听悟掉登录暂停，先保留已合成的草稿并立即返回 need_login（不上传残缺草稿）
    if need_login_info is not None:
        return {
            "ok": False,
            "status": "need_login",
            "need_login": need_login_info,
            "markdown_file": str(md_abs),
            "manifest_file": str(manifest_path),
            "stats": stats,
            "zhuanlu": None,
        }

    # 4. 作为单独 1 条整合记录写入云端 zhuanlu 并强制 verify-item 回读核验
    zhuanlu_result: dict[str, Any] | None = None
    zhuanlu_error = ""
    should_upload = upload_to_zhuanlu and stats["transcribed_ok"] > 0 and (stats["pending"] == 0 or upload_partial)
    if should_upload:
        issue_title = str(state.get("issue", {}).get("title") or "周报").strip()
        comp_title = f"{issue_title} 全期视频伴读逐字稿"
        doc_url = str(state.get("publication", {}).get("document_url") or "").strip()
        source_url = doc_url or f"https://weekly.local/run/{state.get('run_id', run_p.name)}"
        summary_text = build_zhuanlu_summary(state, stats)
        try:
            saved = cloud_save_fn(
                title=comp_title,
                text=md_text,
                summary=summary_text,
                category=zhuanlu_category,
                source_url=source_url,
                platform=DEFAULT_ZHUANLU_PLATFORM,
                status=zhuanlu_status,
                overwrite=True,
            )
            if isinstance(saved, tuple) and len(saved) == 2:
                saved_obj, saved_action = saved
                saved_id = int(getattr(saved_obj, "id"))
                saved_url = str(getattr(saved_obj, "detail_url", ""))
            else:
                saved_id = int(saved.get("item_id") if isinstance(saved, dict) else getattr(saved, "item_id", getattr(saved, "id")))
                saved_url = str(saved.get("detail_url") if isinstance(saved, dict) else getattr(saved, "detail_url", ""))
                saved_action = str(saved.get("action") if isinstance(saved, dict) else getattr(saved, "action", ""))

            ver = cloud_verify_fn(saved_id)
            if isinstance(ver, dict):
                ver_ok = bool(ver.get("ok", True))
                ver_fts = bool(ver.get("fts_ok", True))
            else:
                ver_ok = int(getattr(ver, "chars", 0)) > 0 and int(getattr(ver, "hidden", 0)) == 0
                ver_fts = True
            if not ver_ok or not ver_fts:
                raise RuntimeError(f"云端入库回读校验未通过：ok={ver_ok}, fts_ok={ver_fts}")

            zhuanlu_result = {
                "ok": True,
                "item_id": saved_id,
                "detail_url": saved_url or f"https://gz.grzdt.com/zhuanlu/item/{saved_id}",
                "action": saved_action,
                "category": zhuanlu_category,
                "title": comp_title,
                "chars": len(md_text),
                "verified": True,
                "fts_ok": True,
                "saved_at": _utc_now(),
            }
            _log(f"[Stage7] 整期伴读合集已入库并回读通过：{zhuanlu_result['detail_url']}")
        except Exception as exc:  # noqa: BLE001
            zhuanlu_error = str(exc)
            zhuanlu_result = {"ok": False, "error": zhuanlu_error}
            _log(f"[Stage7] 云端入库失败：{zhuanlu_error}")

    # 5. 回写 state["companion_transcript"] 到 run.json（绝不改动已审计的 items/layout/content_hash）
    has_unfinished = (stats["pending"] + stats["extract_failed"] + stats["transcribe_failed"]) > 0
    overall_status = "complete"
    if zhuanlu_error:
        overall_status = "partial_upload_failed"
    elif has_unfinished:
        overall_status = "partial"

    companion_record = {
        "ok": (not zhuanlu_error) and (stats["transcribed_ok"] > 0 or stats["video_total"] == 0),
        "status": overall_status,
        "markdown_file": md_rel,
        "manifest_file": "transcripts/manifest.json",
        "zhuanlu_item_id": zhuanlu_result.get("item_id") if isinstance(zhuanlu_result, dict) and zhuanlu_result.get("ok") else None,
        "zhuanlu_url": zhuanlu_result.get("detail_url", "") if isinstance(zhuanlu_result, dict) and zhuanlu_result.get("ok") else "",
        "zhuanlu_verified": bool(zhuanlu_result and zhuanlu_result.get("verified")),
        "category": zhuanlu_category,
        "chars": len(md_text),
        "stats": stats,
        "quota_exhausted_fallback_used": bool(stats["engine_funasr_fallback"] > 0),
        "updated_at": _utc_now(),
    }

    latest_state = w.read_json(run_json_path)
    latest_state["companion_transcript"] = companion_record
    latest_state["updated_at"] = _utc_now()
    w.write_json(run_json_path, latest_state)

    return {
        "ok": companion_record["ok"] and not has_unfinished,
        "status": overall_status,
        "markdown_file": str(md_abs),
        "manifest_file": str(manifest_path),
        "items_dir": str(items_dir),
        "stats": stats,
        "cloud_list_error": cloud_list_error,
        "zhuanlu": zhuanlu_result,
        "companion_transcript": companion_record,
    }


__all__ = [
    "DEFAULT_ZHUANLU_CATEGORY",
    "DEFAULT_ZHUANLU_PLATFORM",
    "DEFAULT_ZHUANLU_STATUS",
    "DEFAULT_ZHUANLU_WORKSPACE",
    "build_zhuanlu_summary",
    "clean_and_paragraph_text",
    "render_companion_markdown",
    "run_build_transcripts",
]
