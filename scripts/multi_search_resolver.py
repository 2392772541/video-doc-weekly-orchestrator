# coding: utf-8
"""Multi-channel search and link resolver for weekly reports.
Supports Bilibili API search, Douyin short-ID resolution, neighboring video frame OCR,
and ChatGPT deep-search query generation and result ingestion.
"""
from __future__ import annotations
import html
import json
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
import weekly_core as w
import resolve_links

BVID_PATTERN = re.compile(r"BV[A-Za-z0-9]{10}")
DOUYIN_19_PATTERN = re.compile(r"[0-9]{19}")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def clean_html_tags(text: str) -> str:
    """Removes HTML markup (like Bilibili search highlight tags) and unescapes entities."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", "", text)
    return html.unescape(clean).strip()


def extract_title_core_tokens(title: str) -> str:
    """Extracts alphanumeric and Chinese characters for strict semantic matching."""
    clean = re.sub(r"^\d+[\.、\s]", "", title or "")
    clean = re.sub(r"【.*?】|\[.*?\]|（.*?）|\(.*?\)", "", clean)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", clean).lower()


def search_bilibili_web_api(keyword: str, max_results: int = 5, timeout: int = 8) -> list[dict]:
    """Queries Bilibili public web search API for video candidates."""
    if not keyword or not keyword.strip():
        return []
    encoded_kw = urllib.parse.quote(keyword.strip())
    url = f"https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword={encoded_kw}&page=1"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": "https://search.bilibili.com/"
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") != 0:
                return []
            result_list = data.get("data", {}).get("result", [])
            if not isinstance(result_list, list):
                return []
            candidates = []
            for it in result_list[:max_results]:
                bvid = it.get("bvid")
                raw_title = it.get("title", "")
                author = it.get("author", "")
                if bvid:
                    candidates.append({
                        "platform": "bilibili",
                        "id": bvid,
                        "url": f"https://www.bilibili.com/video/{bvid}",
                        "title": clean_html_tags(raw_title),
                        "author": clean_html_tags(author),
                        "play": it.get("play", 0),
                        "description": clean_html_tags(it.get("description", "")),
                        "source": "bilibili_search_api"
                    })
            return candidates
    except Exception:
        return []


def evaluate_candidate_match(item_title: str, item_author: str, candidate_title: str, candidate_author: str) -> dict:
    """Strictly evaluates if a search candidate represents the exact same original work."""
    c_orig = extract_title_core_tokens(item_title)
    c_cand = extract_title_core_tokens(candidate_title)
    if not c_orig:
        return {"matched": False, "score": 0.0, "reason": "empty_original_title"}

    # 1. Exact or substring match
    if c_orig == c_cand:
        return {"matched": True, "score": 1.0, "reason": "exact_core_title_match"}
    if len(c_orig) >= 6 and (c_orig in c_cand or c_cand in c_orig):
        return {"matched": True, "score": 0.95, "reason": "title_substring_inclusion"}

    # 2. Overlap ratio
    common_chars = sum(1 for ch in c_orig if ch in c_cand)
    ratio = common_chars / max(len(c_orig), 1)

    # Check author correlation
    author_bonus = False
    clean_orig_auth = extract_title_core_tokens(item_author)
    clean_cand_auth = extract_title_core_tokens(candidate_author)
    if clean_orig_auth and clean_cand_auth:
        if clean_orig_auth in clean_cand_auth or clean_cand_auth in clean_orig_auth:
            author_bonus = True

    if ratio >= 0.75 or (ratio >= 0.6 and author_bonus):
        return {"matched": True, "score": round(ratio + (0.1 if author_bonus else 0), 2), "reason": "high_overlap_and_author_correlation"}

    return {"matched": False, "score": round(ratio, 2), "reason": f"insufficient_similarity: {ratio:.2f}"}


def find_best_bilibili_candidate(item: dict) -> dict | None:
    """Attempts to find and strictly verify a Bilibili video for an unresolved item."""
    title = item.get("title", "")
    author = item.get("author", "")
    clean_t = re.sub(r"^\d+[\.、\s]", "", title).strip()
    clean_t = re.sub(r"【.*?】|\[.*?\]", "", clean_t).strip()
    if not clean_t:
        return None

    queries = [clean_t]
    if author:
        queries.insert(0, f"{author} {clean_t}")

    for q in queries:
        cands = search_bilibili_web_api(q, max_results=5)
        for c in cands:
            ev = evaluate_candidate_match(title, author, c["title"], c["author"])
            if ev["matched"]:
                return {
                    "platform": "bilibili",
                    "id": c["id"],
                    "url": c["url"],
                    "title": c["title"],
                    "author": c["author"],
                    "confidence": ev["score"],
                    "reason": ev["reason"]
                }
    return None


def scan_neighboring_video_frames(video_path: str | Path, timestamp_sec: float, window_sec: float = 4.0, step_sec: float = 0.5) -> list[dict]:
    """Extracts multiple neighboring frames around timestamp_sec and runs RapidOCR
    to catch video URLs/IDs that appear only briefly on screen.
    """
    v_path = Path(video_path)
    if not v_path.is_file():
        return []
    ocr = resolve_links.get_ocr_engine()
    if not ocr:
        return []

    ffmpeg = "C:/Users/win10/scoop/shims/ffmpeg.EXE"
    if not Path(ffmpeg).is_file():
        ffmpeg = "ffmpeg"

    start_sec = max(0.0, timestamp_sec - (window_sec / 2.0))
    end_sec = timestamp_sec + (window_sec / 2.0)
    current_sec = start_sec

    discovered = []
    seen_urls = set()

    with tempfile.TemporaryDirectory(prefix="weekly_scan_") as tmpdir:
        tmp_p = Path(tmpdir)
        idx = 0
        while current_sec <= end_sec:
            idx += 1
            frame_file = tmp_p / f"frame_{idx:03d}_{int(current_sec)}s.jpg"
            cmd = [
                ffmpeg, "-y",
                "-ss", f"{current_sec:.2f}",
                "-i", str(v_path),
                "-frames:v", "1",
                "-q:v", "2",
                str(frame_file)
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=15)
                if proc.returncode == 0 and frame_file.is_file():
                    res, _ = ocr(str(frame_file))
                    if res:
                        lines = [x[1] for x in res if len(x) >= 2 and isinstance(x[1], str)]
                        cands = resolve_links.extract_candidate_urls_from_ocr_lines(lines)
                        for c in cands:
                            if c["url"] not in seen_urls:
                                seen_urls.add(c["url"])
                                c["timestamp_seconds"] = current_sec
                                discovered.append(c)
            except Exception:
                pass
            current_sec += step_sec

    return discovered


def generate_chatgpt_search_sheet(unresolved_items: list[dict]) -> dict:
    """Generates an organized, prompt-ready query sheet for ChatGPT to deep-search
    video origins using titles, quotes, and author clues.
    """
    if not unresolved_items:
        return {"total": 0, "prompt": "所有条目均已具备明确外链，无需调用 ChatGPT 检索。"}

    prompt_lines = [
        "你是一个精通全网视频与设计/自媒体出处的专业信息研究员。我在制作《且曼周报/水球泡周报》时，以下条目在录像中被讲解或提及，但缺少明确的独立视频外链（或外链被截断）。",
        "请利用联网搜索与知识库，帮我精准找到这些条目在【B站】或【抖音】上的原始视频链接。要求：",
        "1. 必须是原作者的真实原作视频，拒绝同主题其他人的二创或仿作；",
        "2. 优先提供 B站 (bilibili.com/video/BV...) 或 抖音 (douyin.com/video/19位数字) 原作链接；",
        "3. 输出格式请严格遵守以下 JSON 数组：",
        "```json",
        "[",
        '  {"key": "条目key", "title": "原标题", "url": "找到的权威原视频链接", "author": "UP主/创作者姓名", "confidence": "high/medium"}',
        "]",
        "```",
        "",
        "待检索条目清单如下："
    ]

    for idx, it in enumerate(unresolved_items, 1):
        k = it.get("key", f"item_{idx}")
        t = it.get("title", "")
        author = it.get("author", "未知")
        sec = it.get("source", {}).get("timestamp_seconds", 0)
        orig_u = it.get("url") or it.get("original_url") or ""
        body_snippet = (it.get("body") or "")[:80].replace("\n", " ")
        prompt_lines.append(f"{idx}. [Key: {k}] 标题：《{t}》 | 作者线索: {author} | 录像时间戳: {int(sec)}s | 原有线索/截断链接: {orig_u}")
        if body_snippet:
            prompt_lines.append(f"   正文关键句: {body_snippet}")

    return {
        "total": len(unresolved_items),
        "keys": [it.get("key") for it in unresolved_items],
        "prompt": "\n".join(prompt_lines)
    }


def apply_search_resolutions_to_state(run_dir: Path, state: dict, resolutions: list[dict]) -> dict:
    """Applies verified search resolutions (e.g. from Bilibili search, ChatGPT, or web search)
    to run state, upgrading items to videos and removing redundant screenshot placeholders.
    """
    run_dir = Path(run_dir).resolve()
    applied = []
    rejected = []
    items = state.get("items", [])
    layout = state.get("layout", [])

    res_map = {r.get("key"): r for r in resolutions if r.get("key")}
    title_map = {r.get("title"): r for r in resolutions if r.get("title")}

    for it in items:
        k = it.get("key")
        t = it.get("title")
        r = res_map.get(k) or title_map.get(t)
        if not r:
            continue

        cand_url = (r.get("url") or "").strip()
        cand_title = r.get("title", "")
        cand_author = r.get("author", "")

        ver = resolve_links.verify_resolution(it, cand_url, cand_title, cand_author)
        if ver["verified"]:
            it["canonical_url"] = ver["resolved_url"]
            it["url"] = ver["resolved_url"]
            it["kind"] = "video" if ver["platform"] in ("douyin", "bilibili") else "link"
            it["resolved_by"] = ver["method"]
            it.pop("exception", None)
            it.pop("screenshot", None)
            it.pop("candidates", None)
            it.pop("candidate_url", None)

            # Strictly enforce mutual exclusion: remove screenshot image blocks for newly linked items
            layout = [b for b in layout if not (b.get("type") == "image" and b.get("owner_key") == k)]

            applied.append({
                "key": k,
                "title": t,
                "url": ver["resolved_url"],
                "platform": ver["platform"],
                "method": ver["method"]
            })
        else:
            rejected.append({
                "key": k,
                "title": t,
                "attempted_url": cand_url,
                "reason": ver.get("reason", "verification_failed")
            })

    state["layout"] = layout
    inv = state.get("inventory", {})
    if "review" in inv and inv["review"]:
        inv_keys = [i["key"] for i in items]
        inv["review"]["binding"] = w.digest({"layout": state["layout"], "inventory": inv_keys})

    w.write_json(run_dir / "run.json", state)

    return {
        "applied_count": len(applied),
        "applied": applied,
        "rejected_count": len(rejected),
        "rejected": rejected
    }
