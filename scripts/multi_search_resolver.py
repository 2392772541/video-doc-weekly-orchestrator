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


def generate_search_queries_for_item(item: dict) -> list[str]:
    """Generates a ranked list of search queries for Bilibili and general web search,
    stripping episode prefixes, brackets, splitting compound titles on delimiters,
    and combining with author clues.
    """
    title = item.get("title", "")
    author = item.get("author", "")
    if not title:
        return []

    clean = re.sub(r"^\d+[\.、\s\-]+", "", title).strip()
    clean = re.sub(r"^第\d+[集期话篇][\|｜:：\s]*", "", clean).strip()

    queries = []
    generic_authors = {"且曼", "水球泡", "未知", "且曼内刊"}
    clean_author = author.strip() if author and author.strip() not in generic_authors else ""

    no_brackets = re.sub(r"【.*?】|\[.*?\]|（.*?）|\(.*?\)", " ", clean).strip()
    no_brackets = re.sub(r"\s+", " ", no_brackets)

    if clean_author and no_brackets:
        queries.append(f"{clean_author} {no_brackets}")
    if no_brackets:
        queries.append(no_brackets)

    # Delimiter splits: | ｜ —— - : ：
    parts = re.split(r"[\|｜——:：\-]+", clean)
    for p in parts:
        sub_clean = re.sub(r"【.*?】|\[.*?\]|（.*?）|\(.*?\)", " ", p).strip()
        sub_clean = re.sub(r"\s+", " ", sub_clean)
        if len(sub_clean) >= 4 and sub_clean not in queries:
            if clean_author:
                queries.append(f"{clean_author} {sub_clean}")
            queries.append(sub_clean)

    # Bracket contents
    bracket_contents = re.findall(r"[（\(【\[](.*?)[）\)】\]]", clean)
    for bc in bracket_contents:
        bc_clean = bc.strip()
        if len(bc_clean) >= 4 and bc_clean not in queries:
            if clean_author:
                queries.append(f"{clean_author} {bc_clean}")
            queries.append(bc_clean)

    # First clause before punctuation
    comma_parts = re.split(r"[，,。！!？?]", clean)
    if comma_parts and len(comma_parts[0].strip()) >= 5:
        first_clause = comma_parts[0].strip()
        if first_clause not in queries:
            if clean_author:
                queries.append(f"{clean_author} {first_clause}")
            queries.append(first_clause)

    seen = set()
    result = []
    for q in queries:
        q_norm = q.strip()
        if q_norm and q_norm not in seen:
            seen.add(q_norm)
            result.append(q_norm)
    return result



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


def evaluate_candidate_match(item_title: str, item_author: str, candidate_title: str, candidate_author: str, item_body: str = "") -> dict:
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

    # 2. Continuous shared core token of length >= 4 + overlap
    has_core_token = any(c_orig[i:i+4] in c_cand for i in range(len(c_orig) - 3))
    common_chars = sum(1 for ch in c_orig if ch in c_cand)
    ratio = common_chars / max(len(c_orig), 1)

    # Check author correlation
    author_bonus = False
    clean_orig_auth = extract_title_core_tokens(item_author)
    clean_cand_auth = extract_title_core_tokens(candidate_author)
    generic_authors = {"且曼", "水球泡", "未知", "且曼内刊"}

    if clean_orig_auth and clean_cand_auth:
        if clean_orig_auth in generic_authors:
            pass
        elif clean_orig_auth in clean_cand_auth or clean_cand_auth in clean_orig_auth:
            author_bonus = True

    # Check body text corroboration
    body_bonus = False
    if item_body:
        b_tokens = extract_title_core_tokens(item_body)
        if clean_cand_auth and len(clean_cand_auth) >= 2 and clean_cand_auth in b_tokens:
            body_bonus = True
            author_bonus = True
        if c_cand and any(c_cand[i:i+6] in b_tokens for i in range(len(c_cand) - 5)):
            body_bonus = True

    if has_core_token and (ratio >= 0.4 or body_bonus):
        score = round(ratio + (0.2 if author_bonus else 0.1), 2)
        return {"matched": True, "score": min(score, 1.0), "reason": "core_phrase_and_context_matched"}

    if ratio >= 0.75 or (ratio >= 0.6 and (author_bonus or body_bonus)):
        return {"matched": True, "score": round(ratio + (0.1 if author_bonus else 0), 2), "reason": "high_overlap_and_author_correlation"}

    return {"matched": False, "score": round(ratio, 2), "reason": f"insufficient_similarity: {ratio:.2f}"}


def find_best_bilibili_candidate(item: dict) -> dict | None:
    """Attempts to find and strictly verify a Bilibili video for an unresolved item
    using multi-query decomposition and strict semantic matching.
    """
    title = item.get("title", "")
    author = item.get("author", "")
    body = item.get("body", "")
    queries = generate_search_queries_for_item(item)
    if not queries:
        return None

    best_match = None
    best_score = 0.0

    for q in queries:
        cands = search_bilibili_web_api(q, max_results=5)
        for c in cands:
            ev = evaluate_candidate_match(title, author, c["title"], c["author"], item_body=body)
            if ev["matched"] and ev["score"] > best_score:
                best_score = ev["score"]
                best_match = {
                    "platform": "bilibili",
                    "id": c["id"],
                    "url": c["url"],
                    "title": c["title"],
                    "author": c["author"],
                    "confidence": ev["score"],
                    "reason": ev["reason"]
                }
                if best_score >= 0.95:
                    return best_match
    return best_match

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


def get_video_duration(video_path: str | Path) -> float | None:
    """Gets exact video duration in seconds via ffprobe or ffmpeg."""
    v_p = Path(video_path).resolve()
    if not v_p.is_file():
        return None
    ffprobe = "C:/Users/win10/scoop/shims/ffprobe.EXE"
    if not Path(ffprobe).is_file():
        ffprobe = "ffprobe"
    try:
        cmd = [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(v_p)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass
    try:
        ffmpeg = "C:/Users/win10/scoop/shims/ffmpeg.EXE"
        if not Path(ffmpeg).is_file():
            ffmpeg = "ffmpeg"
        cmd = [ffmpeg, "-i", str(v_p)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
        if m:
            h, m_min, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600 + m_min * 60 + s
    except Exception:
        pass
    return None


def scan_video_timeline_for_links(
    video_path: str | Path,
    start_sec: float = 0,
    end_sec: float | None = None,
    step_sec: float = 12.0,
    max_frames: int | None = None
) -> list[dict]:
    """Pass 1: Scans video timeline with single-thread memory-safe ffmpeg and RapidOCR
    to discover all on-screen URLs, truncated IDs, BVIDs, and slide titles across 100% duration.
    """
    v_path = Path(video_path).resolve()
    if not v_path.is_file():
        return []
    ocr = resolve_links.get_ocr_engine()
    if not ocr:
        return []

    ffmpeg = "C:/Users/win10/scoop/shims/ffmpeg.EXE"
    if not Path(ffmpeg).is_file():
        ffmpeg = "ffmpeg"

    if end_sec is None:
        end_sec = get_video_duration(v_path)

    discovered = []
    seen_urls = set()

    with tempfile.TemporaryDirectory(prefix="weekly_timeline_") as tmpdir:
        tmp_p = Path(tmpdir)
        curr_sec = max(0.0, float(start_sec))
        frame_count = 0

        while True:
            if end_sec is not None and curr_sec > end_sec:
                break
            if max_frames is not None and frame_count >= max_frames:
                break

            frame_file = tmp_p / f"frame_{int(curr_sec)}s.jpg"
            cmd = [
                ffmpeg, "-y",
                "-ss", f"{curr_sec:.2f}",
                "-i", str(v_path),
                "-frames:v", "1",
                "-q:v", "2",
                "-threads", "1",
                str(frame_file)
            ]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                if proc.returncode == 0 and frame_file.is_file():
                    res, _ = ocr(str(frame_file))
                    if res:
                        lines = [x[1] for x in res if len(x) >= 2 and isinstance(x[1], str) and x[1].strip()]
                        cands = resolve_links.extract_candidate_urls_from_ocr_lines(lines)
                        for c in cands:
                            if c["url"] not in seen_urls:
                                seen_urls.add(c["url"])
                                c["timestamp_seconds"] = curr_sec
                                c["ocr_lines"] = lines[:8]
                                c["source_pass"] = "pass1_coarse"
                                discovered.append(c)
                    try:
                        frame_file.unlink()
                    except Exception:
                        pass
                else:
                    break
            except Exception:
                break

            curr_sec += step_sec
            frame_count += 1

    return discovered


def targeted_dense_gap_scan(
    video_path: str | Path,
    unresolved_items: list[dict],
    all_items: list[dict] | None = None,
    window_sec: float = 60.0,
    dense_step: float = 1.0
) -> list[dict]:
    """Pass 2: Targeted dense frame extraction (1.0s step / 1 fps) focused strictly around
    unresolved items, unconfirmed exceptions, or numbering sequence gaps.
    """
    v_path = Path(video_path).resolve()
    if not v_path.is_file() or not unresolved_items:
        return []
    ocr = resolve_links.get_ocr_engine()
    if not ocr:
        return []

    ffmpeg = "C:/Users/win10/scoop/shims/ffmpeg.EXE"
    if not Path(ffmpeg).is_file():
        ffmpeg = "ffmpeg"

    duration = get_video_duration(v_path) or 7200.0
    intervals = []
    all_list = all_items or unresolved_items
    key_to_idx = {it.get("key"): idx for idx, it in enumerate(all_list)}

    for it in unresolved_items:
        ts = it.get("timestamp_seconds")
        if ts is None and it.get("source"):
            ts = it["source"].get("timestamp_seconds")

        if ts is not None and isinstance(ts, (int, float)) and ts > 0:
            intervals.append((max(0.0, float(ts) - window_sec), min(duration, float(ts) + window_sec)))
        else:
            k = it.get("key")
            idx = key_to_idx.get(k)
            prev_ts = None
            next_ts = None
            if idx is not None:
                for p in range(idx - 1, -1, -1):
                    pts = all_list[p].get("timestamp_seconds") or (all_list[p].get("source") or {}).get("timestamp_seconds")
                    if pts and isinstance(pts, (int, float)) and pts > 0:
                        prev_ts = float(pts)
                        break
                for n in range(idx + 1, len(all_list)):
                    nts = all_list[n].get("timestamp_seconds") or (all_list[n].get("source") or {}).get("timestamp_seconds")
                    if nts and isinstance(nts, (int, float)) and nts > 0:
                        next_ts = float(nts)
                        break

            if prev_ts is not None and next_ts is not None:
                intervals.append((max(0.0, prev_ts - 5.0), min(duration, next_ts + 10.0)))
            elif prev_ts is not None:
                intervals.append((max(0.0, prev_ts - 5.0), min(duration, prev_ts + 90.0)))
            elif next_ts is not None:
                intervals.append((max(0.0, next_ts - 90.0), min(duration, next_ts + 5.0)))

    if not intervals:
        return []

    # Merge overlapping or close intervals (within 10s)
    intervals.sort(key=lambda x: x[0])
    merged = []
    curr_start, curr_end = intervals[0]
    for s, e in intervals[1:]:
        if s <= curr_end + 10.0:
            curr_end = max(curr_end, e)
        else:
            merged.append((curr_start, curr_end))
            curr_start, curr_end = s, e
    merged.append((curr_start, curr_end))

    discovered = []
    seen_urls = set()

    with tempfile.TemporaryDirectory(prefix="weekly_dense_gap_") as tmpdir:
        tmp_p = Path(tmpdir)
        frame_idx = 0
        for w_start, w_end in merged:
            c_sec = w_start
            while c_sec <= w_end:
                frame_idx += 1
                frame_file = tmp_p / f"dense_{frame_idx}_{int(c_sec)}s.jpg"
                cmd = [
                    ffmpeg, "-y",
                    "-ss", f"{c_sec:.2f}",
                    "-i", str(v_path),
                    "-frames:v", "1",
                    "-q:v", "2",
                    "-threads", "1",
                    str(frame_file)
                ]
                try:
                    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                    if proc.returncode == 0 and frame_file.is_file():
                        res, _ = ocr(str(frame_file))
                        if res:
                            lines = [x[1] for x in res if len(x) >= 2 and isinstance(x[1], str) and x[1].strip()]
                            cands = resolve_links.extract_candidate_urls_from_ocr_lines(lines)
                            for c in cands:
                                if c["url"] not in seen_urls:
                                    seen_urls.add(c["url"])
                                    c["timestamp_seconds"] = c_sec
                                    c["ocr_lines"] = lines[:8]
                                    c["source_pass"] = "pass2_dense_gap"
                                    discovered.append(c)
                        try:
                            frame_file.unlink()
                        except Exception:
                            pass
                    else:
                        break
                except Exception:
                    break
                c_sec += dense_step

    return discovered


def tail_section_dense_scan(
    video_path: str | Path,
    tail_ratio: float = 0.20,
    dense_step: float = 2.0,
    min_start_sec: float | None = None
) -> list[dict]:
    """Pass 3: Dense sweep over final 15-20% of video duration where product design,
    tools & technology, and briefing sections are frequently flipped through quickly.
    """
    v_path = Path(video_path).resolve()
    if not v_path.is_file():
        return []
    duration = get_video_duration(v_path)
    if not duration or duration < 60.0:
        return []

    start_sec = min_start_sec if min_start_sec is not None else max(0.0, duration * (1.0 - tail_ratio))
    cands = scan_video_timeline_for_links(
        v_path,
        start_sec=start_sec,
        end_sec=duration,
        step_sec=dense_step,
        max_frames=None
    )
    for c in cands:
        c["source_pass"] = "pass3_tail_sweep"
    return cands


def _auto_heal_state_with_candidates(run_dir: Path, state: dict, candidates: list[dict], method_tag: str) -> list[dict]:
    """Helper to safely apply verified candidate URLs to unresolved state items with 1:1 uniqueness check."""
    if not candidates:
        return []
    items = state.get("items", [])
    layout = state.get("layout", [])
    run_dir = Path(run_dir).resolve()
    screen_dir = run_dir / "evidence" / "screenshots"

    assigned_ids = {it.get("id") for it in items if it.get("id") and not it.get("exception")}
    assigned_urls = {it.get("canonical_url") or it.get("url") for it in items if (it.get("canonical_url") or it.get("url")) and not it.get("exception")}

    healed = []
    for it in items:
        needs_healing = bool(it.get("exception")) or not bool(it.get("canonical_url"))
        if not needs_healing:
            continue

        for cand in candidates:
            curl = cand.get("url", "")
            cid = cand.get("id", "")
            if curl in assigned_urls or cid in assigned_ids:
                continue

            ver = resolve_links.verify_candidate_for_item(it, cand, assigned_ids)
            if ver.get("matched"):
                assigned_ids.add(ver["id"])
                assigned_urls.add(ver["resolved_url"])

                it["canonical_url"] = ver["resolved_url"]
                it["url"] = ver["resolved_url"]
                it["id"] = ver["id"]
                it["platform"] = ver["platform"]
                it["kind"] = "video" if ver["platform"] in ("douyin", "bilibili") else "link"
                it["resolved_by"] = f"{method_tag}:{ver.get('method', 'ocr_matched')}"
                it.pop("exception", None)
                it.pop("screenshot", None)
                it.pop("candidates", None)
                it.pop("candidate_url", None)

                # Strict mutual exclusion: remove screenshot image blocks from layout for newly linked items
                k = it.get("key")
                layout = [b for b in layout if not (b.get("type") == "image" and b.get("owner_key") == k)]
                if screen_dir.is_dir() and k:
                    safe_k = re.sub(r"[^\w\-]", "_", k)
                    for sf in screen_dir.glob(f"{safe_k}*"):
                        try:
                            sf.unlink()
                        except Exception:
                            pass

                healed.append({
                    "key": k,
                    "title": it.get("title"),
                    "resolved_url": ver["resolved_url"],
                    "platform": ver["platform"],
                    "method": it["resolved_by"]
                })
                break

    state["layout"] = layout
    return healed


def run_three_pass_video_extraction(
    video_sources: dict[str, str | Path],
    state: dict,
    run_dir: Path | str,
    coarse_step: float = 12.0,
    dense_step: float = 1.0,
    tail_step: float = 2.0
) -> dict:
    """Executes the full 3-Pass Progressive Multi-Stage Frame Extraction:
    Pass 1: Global Baseline Timeline & TOC Coarse Pass (10-15s step across 100% duration)
    Pass 2: Targeted Dense Gap & Unresolved Pass (1.0s step around unresolved/broken items)
    Pass 3: Tail Section Sweep & High-Speed Zone Pass (2.0s step over final 20% duration)
    Strictly observes single-thread memory safety and immediate frame unlinking.
    """
    run_p = Path(run_dir).resolve()
    evidence_dir = run_p / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    all_candidates = []
    seen_cands = set()

    # 1. Pass 1: Global Baseline Timeline Pass across all registered videos
    pass1_candidates = []
    for sid, v_raw in video_sources.items():
        vp = Path(v_raw).resolve()
        if vp.is_file():
            cands = scan_video_timeline_for_links(vp, step_sec=coarse_step, max_frames=None)
            for c in cands:
                if c["url"] not in seen_cands:
                    seen_cands.add(c["url"])
                    c["source_id"] = sid
                    c["source_pass"] = "pass1_coarse"
                    pass1_candidates.append(c)
                    all_candidates.append(c)

    pass1_healed = _auto_heal_state_with_candidates(run_p, state, pass1_candidates, "pass1_coarse")

    # 2. Pass 2: Targeted Dense Gap & Unresolved Pass (Immediately executed if items remain unresolved)
    unresolved_after_p1 = [it for it in state.get("items", []) if it.get("exception") or not it.get("canonical_url")]
    pass2_candidates = []
    pass2_healed = []
    if unresolved_after_p1:
        for sid, v_raw in video_sources.items():
            vp = Path(v_raw).resolve()
            if vp.is_file():
                gap_cands = targeted_dense_gap_scan(vp, unresolved_after_p1, all_items=state.get("items", []), dense_step=dense_step)
                for c in gap_cands:
                    if c["url"] not in seen_cands:
                        seen_cands.add(c["url"])
                        c["source_id"] = sid
                        pass2_candidates.append(c)
                        all_candidates.append(c)

        pass2_healed = _auto_heal_state_with_candidates(run_p, state, pass2_candidates, "pass2_dense_gap")

    # 3. Pass 3: Tail Section Sweep & High-Speed Zone Pass
    pass3_candidates = []
    pass3_healed = []
    for sid, v_raw in video_sources.items():
        vp = Path(v_raw).resolve()
        if vp.is_file():
            tail_cands = tail_section_dense_scan(vp, tail_ratio=0.20, dense_step=tail_step)
            for c in tail_cands:
                if c["url"] not in seen_cands:
                    seen_cands.add(c["url"])
                    c["source_id"] = sid
                    pass3_candidates.append(c)
                    all_candidates.append(c)

    pass3_healed = _auto_heal_state_with_candidates(run_p, state, pass3_candidates, "pass3_tail_sweep")

    # Save persistent frame_urls.json for cross-tool reuse
    (run_p / "frame_urls.json").write_text(
        json.dumps(all_candidates, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = {
        "pass1_coarse": {
            "candidates_found": len(pass1_candidates),
            "items_healed": len(pass1_healed),
            "healed_keys": [x["key"] for x in pass1_healed]
        },
        "pass2_dense_gap": {
            "candidates_found": len(pass2_candidates),
            "items_healed": len(pass2_healed),
            "healed_keys": [x["key"] for x in pass2_healed]
        },
        "pass3_tail_sweep": {
            "candidates_found": len(pass3_candidates),
            "items_healed": len(pass3_healed),
            "healed_keys": [x["key"] for x in pass3_healed]
        },
        "total_candidates": len(all_candidates),
        "total_healed": len(pass1_healed) + len(pass2_healed) + len(pass3_healed),
        "unresolved_remaining": len([it for it in state.get("items", []) if it.get("exception") or not it.get("canonical_url")])
    }

    (evidence_dir / "three_pass_extraction_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return summary


def find_item_in_video_timeline(
    video_path: str | Path,
    item_num: int | str | None,
    item_title: str,
    approx_sec: float,
    window_sec: float = 90.0,
    step_sec: float = 5.0,
    used_timestamps: set[float] | None = None,
    card_output_path: Path | str | None = None
) -> dict:
    """Searches around approx_sec for the exact video frame where item_num and/or item_title
    is visibly presented on screen with strict number prefix verification and collision avoidance.
    Also extracts screen-visible video URLs (auto-healing) and optionally crops a focused ROI card.
    """
    v_path = Path(video_path).resolve()
    default_res = {
        "best_frame_sec": approx_sec,
        "found_url": None,
        "confidence": 0.0,
        "reason": "fallback_approx_sec",
        "ocr_lines": [],
        "card_path": None
    }
    if not v_path.is_file():
        return default_res

    ocr = resolve_links.get_ocr_engine()
    if not ocr:
        return default_res

    ffmpeg = "C:/Users/win10/scoop/shims/ffmpeg.EXE"
    if not Path(ffmpeg).is_file():
        ffmpeg = "ffmpeg"

    start_sec = max(0.0, approx_sec - window_sec)
    end_sec = approx_sec + window_sec
    curr_sec = start_sec

    best_sec = approx_sec
    best_score = -1.0
    best_lines = []
    best_box = None
    found_url = None

    clean_t = extract_title_core_tokens(item_title)
    num_str = str(item_num).strip() if item_num is not None else ""
    strict_num_re = re.compile(r"(?:^|\s)" + re.escape(num_str) + r"(?:[\.、\s]|$)") if num_str else None

    with tempfile.TemporaryDirectory(prefix="weekly_find_") as tmpdir:
        tmp_p = Path(tmpdir)
        idx = 0
        while curr_sec <= end_sec:
            idx += 1
            frame_file = tmp_p / f"check_{idx}.jpg"
            cmd = [
                ffmpeg, "-y",
                "-ss", f"{curr_sec:.2f}",
                "-i", str(v_path),
                "-frames:v", "1",
                "-q:v", "2",
                "-threads", "1",
                str(frame_file)
            ]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                if proc.returncode == 0 and frame_file.is_file():
                    res, _ = ocr(str(frame_file))
                    if res:
                        lines = [x[1] for x in res if len(x) >= 2 and isinstance(x[1], str)]
                        full_text = " ".join(lines)
                        tokens = extract_title_core_tokens(full_text)

                        num_matched = False
                        current_box = None
                        if strict_num_re:
                            for item in res:
                                box, text = item[0], item[1]
                                # Filter out 4-digit years like 2024, 2025, 2026 to prevent false match on 25
                                cleaned_text = re.sub(r"202[0-9]", "", text)
                                if strict_num_re.search(cleaned_text):
                                    # Exclude drama episode numbers (e.g. 第5集) when item is not that number
                                    if re.search(r"第" + re.escape(num_str) + r"[集话期]", text) and not num_str.startswith("第") and not strict_num_re.search(re.sub(r"第\d+[集话期]", "", text)):
                                        continue
                                    num_matched = True
                                    current_box = box
                                    break

                        score = 2.0 if num_matched else 0.0
                        if clean_t:
                            if clean_t in tokens:
                                score += 2.0
                            else:
                                common = sum(1 for ch in clean_t if ch in tokens)
                                score += (common / max(len(clean_t), 1)) * 1.8

                        # Avoid reusing an identical timestamp already taken by another item
                        if used_timestamps and any(abs(curr_sec - ut) < 15.0 for ut in used_timestamps):
                            if not num_matched:
                                score -= 2.0

                        cands = resolve_links.extract_candidate_urls_from_ocr_lines(lines)
                        for cand in cands:
                            ev = evaluate_candidate_match(item_title, "", cand.get("title", ""), "")
                            if score >= 1.5 or ev["matched"]:
                                found_url = cand
                                found_url["timestamp_seconds"] = curr_sec
                                break

                        if score > best_score:
                            best_score = score
                            best_sec = curr_sec
                            best_lines = lines[:6]
                            best_box = current_box

                        if (best_score >= 3.5 and num_matched) or found_url:
                            break
                    try:
                        frame_file.unlink()
                    except Exception:
                        pass
            except Exception:
                pass
            curr_sec += step_sec

    # Optional ROI Card Cropping with PIL
    saved_card = None
    if card_output_path and best_box and best_sec is not None:
        temp_frame_path = None
        try:
            from PIL import Image
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
                temp_frame_path = tf.name
            cmd = [
                ffmpeg, "-y",
                "-ss", f"{best_sec:.2f}",
                "-i", str(v_path),
                "-frames:v", "1",
                "-q:v", "2",
                temp_frame_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if Path(temp_frame_path).is_file():
                img = Image.open(temp_frame_path)
                w, h = img.size
                xs = [p[0] for p in best_box]
                ys = [p[1] for p in best_box]
                x1 = max(0, int(min(xs)) - 35)
                y1 = max(0, int(min(ys)) - 25)
                if min(xs) < 650 and max(xs) <= 650:
                    x2 = min(645, max(int(max(xs)) + 15, x1 + 100))
                else:
                    x2 = min(w, int(max(xs)) + 450)
                y2 = min(h, int(max(ys)) + 45)
                crop_img = img.crop((x1, y1, x2, y2))
                card_out = Path(card_output_path)
                card_out.parent.mkdir(parents=True, exist_ok=True)
                crop_img.save(str(card_out))
                saved_card = str(card_out)
        except Exception:
            pass
        finally:
            if temp_frame_path and Path(temp_frame_path).is_file():
                try:
                    Path(temp_frame_path).unlink(missing_ok=True)
                except Exception:
                    pass

    return {
        "best_frame_sec": best_sec,
        "found_url": found_url,
        "confidence": round(best_score, 2),
        "reason": f"matched_frame_at_{int(best_sec)}s_score_{round(best_score, 2)}",
        "ocr_lines": best_lines,
        "card_path": saved_card
    }


def parse_chatgpt_response(text: str) -> list[dict]:
    """Robustly parses ChatGPT search responses, handling:
    - Raw JSON arrays or objects with 'results' / 'items'
    - Markdown code fences (```json ... ``` or ``` ...)
    - Embedded JSON objects
    """
    if not text or not text.strip():
        return []
    raw = text.strip()

    # 1. Try direct JSON load
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("results", "items", "data", "resolutions"):
                if k in data and isinstance(data[k], list):
                    return data[k]
    except Exception:
        pass

    # 2. Extract from markdown code fences: ```json ... ``` or ``` ... ```
    fences = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    for f in fences:
        try:
            parsed = json.loads(f.strip())
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for k in ("results", "items", "data", "resolutions"):
                    if k in parsed and isinstance(parsed[k], list):
                        return parsed[k]
        except Exception:
            pass

    # 3. Fallback: line-by-line / regex pattern matching for JSON objects
    objects = []
    for m in re.finditer(r"\{[^{}]*\"url\"[^{}]*\}", raw):
        try:
            obj = json.loads(m.group(0))
            if "url" in obj:
                objects.append(obj)
        except Exception:
            pass
    return objects



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
        sec_val = it.get("source", {}).get("timestamp_seconds")
        if sec_val is None:
            sec_val = it.get("timestamp_seconds")
        sec_str = f"{int(sec_val)}s" if (sec_val is not None and isinstance(sec_val, (int, float)) and sec_val >= 30.0) else "待定位"
        orig_u = it.get("url") or it.get("original_url") or ""
        body_snippet = (it.get("body") or "")[:80].replace("\n", " ")
        prompt_lines.append(f"{idx}. [Key: {k}] 标题：《{t}》 | 作者线索: {author} | 录像时间戳: {sec_str} | 原有线索/截断链接: {orig_u}")
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

    # Global URL uniqueness tracking to prevent duplicate link assignments
    assigned_urls = set()
    for it in items:
        existing_u = it.get("canonical_url") or it.get("url")
        if existing_u:
            assigned_urls.add(existing_u)

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
            resolved_u = ver["resolved_url"]
            # Strict 1-to-1 uniqueness check: reject if another item already holds this URL
            if resolved_u in assigned_urls and (it.get("canonical_url") != resolved_u and it.get("url") != resolved_u):
                rejected.append({
                    "key": k,
                    "title": t,
                    "attempted_url": resolved_u,
                    "reason": "duplicate_url_already_assigned_to_another_item"
                })
                continue

            assigned_urls.add(resolved_u)
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
            screen_dir = run_dir / "evidence" / "screenshots"
            if screen_dir.is_dir():
                safe_k = re.sub(r"[^\w\-]", "_", k)
                for sf in screen_dir.glob(f"{safe_k}*"):
                    try:
                        sf.unlink()
                    except Exception:
                        pass

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
