#!/usr/bin/env python3
"""Automated video finder and link recovery engine for weekly reports.
Resolves truncated Douyin IDs, missing URLs, and validates original works.
"""
from __future__ import annotations
import argparse
import hashlib
import subprocess
import json
import re
import urllib.parse
from pathlib import Path
import weekly_core as w

DOUYIN_TRUNCATED_RE = re.compile(r"https?://(?:www\.)?douyin\.com/(?:video|note)/([0-9]{11,18})/?")
DOUYIN_FULL_RE = re.compile(r"https?://(?:www\.)?douyin\.com/(?:video|note)/([0-9]{19})/?")
BVID_RE = re.compile(r"BV[A-Za-z0-9]{10}")

_OCR_ENGINE = None


def get_ocr_engine():
    """Initializes and returns cached RapidOCR instance when available."""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR_ENGINE = RapidOCR()
        except Exception:
            _OCR_ENGINE = False
    return _OCR_ENGINE if _OCR_ENGINE is not False else None


def extract_candidate_urls_from_ocr_lines(lines: list[str]) -> list[dict]:
    """Extract candidate video URLs and IDs from raw OCR lines, cleaning whitespace and normalizing patterns."""
    candidates = []
    seen = set()
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        compacted = re.sub(r"https?\s*:\s*/\s*/\s*", "https://", raw)
        compacted = re.sub(r"\s+", "", compacted)

        # 1. Full Douyin 19 digits
        for m in re.finditer(r"https?://(?:www\.)?douyin\.com/(?:video|note)/([0-9]{19})", compacted):
            vid = m.group(1)
            url = f"https://www.douyin.com/video/{vid}"
            if url not in seen:
                seen.add(url)
                candidates.append({"platform": "douyin", "id": vid, "url": url, "raw": raw, "type": "full_douyin"})

        # 2. Truncated Douyin 11-18 digits (only if prefix not already covered by full 19-digit)
        for m in re.finditer(r"https?://(?:www\.)?douyin\.com/(?:video|note)/([0-9]{11,18})", compacted):
            vid = m.group(1)
            if not any(c["platform"] == "douyin" and c["id"].startswith(vid) for c in candidates):
                url = f"https://www.douyin.com/video/{vid}"
                if url not in seen:
                    seen.add(url)
                    candidates.append({"platform": "douyin", "id": vid, "url": url, "raw": raw, "type": "truncated_douyin"})

        # 3. Bilibili BV URL
        for m in re.finditer(r"https?://(?:www\.)?bilibili\.com/video/(BV[A-Za-z0-9]{10})", compacted):
            bvid = m.group(1)
            url = f"https://www.bilibili.com/video/{bvid}"
            if bvid not in seen:
                seen.add(bvid)
                candidates.append({"platform": "bilibili", "id": bvid, "url": url, "raw": raw, "type": "full_bilibili"})

        # 4. Standalone BV code
        for m in re.finditer(r"\b(BV[A-Za-z0-9]{10})\b", raw):
            bvid = m.group(1)
            url = f"https://www.bilibili.com/video/{bvid}"
            if bvid not in seen:
                seen.add(bvid)
                candidates.append({"platform": "bilibili", "id": bvid, "url": url, "raw": raw, "type": "bvid_text"})

        # 5. Standalone Douyin 19-digit ID
        for m in re.finditer(r"\b([0-9]{19})\b", compacted):
            vid = m.group(1)
            url = f"https://www.douyin.com/video/{vid}"
            if url not in seen:
                seen.add(url)
                candidates.append({"platform": "douyin", "id": vid, "url": url, "raw": raw, "type": "douyin_id_text"})
    return candidates


def scan_screenshot_for_urls(image_path: Path | str) -> list[dict]:
    """Scans an image using RapidOCR and extracts candidate video URLs and IDs."""
    img_p = Path(image_path)
    if not img_p.is_file():
        return []
    engine = get_ocr_engine()
    if not engine:
        return []
    try:
        result, _ = engine(str(img_p))
        if not result:
            return []
        lines = [x[1] for x in result if len(x) >= 2 and isinstance(x[1], str)]
        return extract_candidate_urls_from_ocr_lines(lines)
    except Exception:
        return []

def inspect_item_url_health(item: dict) -> dict:
    url = (item.get("url") or item.get("original_url") or "").strip()
    title = (item.get("title") or "").strip()
    kind = item.get("kind") or "video"
    
    if kind == "quick_news":
        if not url:
            return {"healthy": True, "issue": None, "platform": "plain", "note": "quick_news_text"}
    elif kind == "plain":
        if not url:
            return {"healthy": True, "issue": None, "platform": "plain", "note": "plain_text_entry"}
        else:
            return {"healthy": False, "issue": "plain_item_has_link", "platform": "unknown"}
        
    if not url:
        return {"healthy": False, "issue": "missing_url", "platform": "unknown"}
        
    m_trunc = DOUYIN_TRUNCATED_RE.search(url)
    if m_trunc and not DOUYIN_FULL_RE.search(url):
        return {"healthy": False, "issue": "douyin_truncated_id", "digits": m_trunc.group(1), "platform": "douyin"}
        
    m_full_dy = DOUYIN_FULL_RE.search(url)
    if m_full_dy:
        return {"healthy": True, "issue": None, "platform": "douyin", "id": m_full_dy.group(1)}
        
    m_bv = BVID_RE.search(url)
    if m_bv:
        return {"healthy": True, "issue": None, "platform": "bilibili", "id": m_bv.group(0)}
        
    if url.startswith("http://") or url.startswith("https://"):
        return {"healthy": True, "issue": None, "platform": "web"}
        
    return {"healthy": False, "issue": "invalid_url_format", "platform": "unknown"}

def build_search_queries(item: dict) -> list[str]:
    title = item.get("title", "").strip()
    clean_title = re.sub(r"^\d+[\.、\s]", "", title).strip()
    author = item.get("author") or ""
    queries = []
    
    if clean_title:
        queries.append(f'"{clean_title}"')
        queries.append(f"{clean_title} 抖音")
        queries.append(f"{clean_title} bilibili")
        if author:
            queries.append(f"{author} {clean_title}")
            
    return queries

def verify_resolution(item: dict, candidate_url: str, candidate_title: str = "", candidate_author: str = "") -> dict:
    health = inspect_item_url_health({"url": candidate_url})
    if not health["healthy"]:
        return {"verified": False, "reason": "candidate_url_invalid"}
        
    title = re.sub(r"^\d+[\.、\s]", "", item.get("title", "")).strip()
    orig_author = item.get("author", "").strip()
    
    clean_c_title = re.sub(r"[^\w\u4e00-\u9fff]", "", candidate_title)
    clean_orig_title = re.sub(r"[^\w\u4e00-\u9fff]", "", title)
    
    title_match = False
    if clean_orig_title and clean_c_title:
        if clean_orig_title in clean_c_title or clean_c_title in clean_orig_title:
            title_match = True
        else:
            common = sum(1 for c in clean_orig_title if c in clean_c_title)
            has_core_token = any(clean_orig_title[i:i+4] in clean_c_title for i in range(len(clean_orig_title) - 3))
            if has_core_token and common >= max(5, int(len(clean_orig_title) * 0.4)):
                title_match = True
            elif common >= max(6, int(len(clean_orig_title) * 0.6)):
                title_match = True

    # Check body text corroboration
    body_text = re.sub(r"[^\w\u4e00-\u9fff]", "", item.get("body", "") or "").lower()
    if body_text and clean_c_title:
        if clean_c_title in body_text or (len(clean_c_title) >= 6 and sum(1 for c in clean_c_title if c in body_text) >= len(clean_c_title) * 0.6):
            title_match = True

    author_match = True
    generic_authors = {"且曼", "水球泡", "未知", "且曼内刊"}
    if orig_author and candidate_author:
        if orig_author in generic_authors:
            author_match = True
        elif (orig_author in candidate_author) or (candidate_author in orig_author):
            author_match = True
        elif body_text and candidate_author.lower() in body_text:
            author_match = True
        else:
            author_match = False
        
    prefix_match = False
    orig_url = (item.get("url") or item.get("original_url") or "").strip()
    m_trunc = DOUYIN_TRUNCATED_RE.search(orig_url)
    if m_trunc:
        trunc_digits = m_trunc.group(1)
        m_cand = DOUYIN_FULL_RE.search(candidate_url)
        if m_cand and m_cand.group(1).startswith(trunc_digits):
            prefix_match = True

    if (title_match and author_match) or prefix_match:
        method = "truncated_id_prefix_match" if prefix_match else "title_author_visual"
        return {
            "verified": True,
            "platform": health["platform"],
            "resolved_url": candidate_url,
            "method": method,
            "reason": f"Title and author match: '{candidate_title}' by '{candidate_author}'" if not prefix_match else f"ID prefix matched truncated digits {trunc_digits}"
        }
    return {"verified": False, "reason": f"Mismatch: '{candidate_title}' does not match original '{title}'"}


def capture_item_screenshot(run_dir: Path, item: dict, sources: dict | None = None, timestamp: float | None = None) -> str | None:
    """Extract a high-quality frame from source video at timestamp_seconds using ffmpeg.
    Returns relative path within run_dir, e.g., 'evidence/screenshots/life-1_145s.jpg'.
    """
    run_dir = Path(run_dir).resolve()
    src = item.get("source") or {}
    source_id = src.get("source_id")
    if not source_id:
        return None
        
    ts = timestamp if timestamp is not None else src.get("timestamp_seconds")
    if ts is None or not (isinstance(ts, (int, float)) and ts >= 0):
        return None
        
    video_path = None
    if sources and source_id in sources:
        video_path = sources[source_id].get("path")
    if not video_path or not Path(video_path).is_file():
        run_file = run_dir / "run.json"
        if run_file.is_file():
            try:
                run_data = json.loads(run_file.read_text(encoding="utf-8"))
                video_path = run_data.get("sources", {}).get(source_id, {}).get("path")
            except Exception:
                pass
                
    if not video_path or not Path(video_path).is_file():
        return None
        
    screen_dir = run_dir / "evidence" / "screenshots"
    screen_dir.mkdir(parents=True, exist_ok=True)
    
    key = item.get("key") or "item"
    safe_key = re.sub(r"[^\w\-]", "_", key)
    img_name = f"{safe_key}_{int(ts)}s.jpg"
    img_path = screen_dir / img_name
    
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(ts),
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(img_path)
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if proc.returncode == 0 and img_path.is_file() and img_path.stat().st_size > 0:
            rel_path = f"evidence/screenshots/{img_name}"
            meta = {
                "item_key": key,
                "source_id": source_id,
                "timestamp_seconds": ts,
                "screenshot_file": rel_path,
                "sha256": hashlib.sha256(img_path.read_bytes()).hexdigest(),
                "created_at": w.now()
            }
            meta_path = screen_dir / f"{safe_key}_{int(ts)}s.json"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            item["screenshot"] = rel_path
            return rel_path
    except Exception:
        pass
    return None


def smart_capture_item_screenshot(run_dir: Path, item: dict, sources: dict | None = None) -> dict:
    """Accurately captures an item screenshot verifying that the frame actually contains
    the item's number (e.g. 25) and/or title keywords. Also auto-heals if an on-screen URL is seen.
    """
    run_dir = Path(run_dir).resolve()
    # Mutual exclusion: items with canonical_url must NOT have screenshots
    if item.get("canonical_url"):
        item.pop("screenshot", None)
        return {"healed": False, "screenshot": None, "key": item.get("key"), "reason": "item_already_has_url"}

    src = item.get("source") or {}
    source_id = src.get("source_id")
    ts = src.get("timestamp_seconds", 0)

    video_path = None
    if sources and source_id in sources:
        video_path = sources[source_id].get("path")
    if not video_path or not Path(video_path).is_file():
        run_file = run_dir / "run.json"
        if run_file.is_file():
            try:
                run_data = json.loads(run_file.read_text(encoding="utf-8"))
                video_path = run_data.get("sources", {}).get(source_id, {}).get("path")
            except Exception:
                pass

    best_sec = ts
    if video_path and Path(video_path).is_file():
        try:
            import multi_search_resolver
            loc = multi_search_resolver.find_item_in_video_timeline(video_path, item.get("number"), item.get("title", ""), ts)
            if loc.get("found_url"):
                cand = loc["found_url"]
                ver = verify_candidate_for_item(item, cand)
                if ver.get("matched"):
                    curl = ver["resolved_url"]
                    cplat = ver["platform"]
                    item["canonical_url"] = curl
                    item["url"] = curl
                    item["id"] = ver["id"]
                    item["platform"] = cplat
                    item["kind"] = "video" if cplat in ("douyin", "bilibili") else "link"
                    item["resolved_by"] = "video_frame_ocr_verified"
                    item.pop("exception", None)
                    item.pop("screenshot", None)
                    item.pop("candidates", None)
                    item.pop("candidate_url", None)
                    return {"healed": True, "resolved_url": curl, "platform": cplat, "key": item.get("key")}
            best_sec = loc.get("best_frame_sec", ts)
        except Exception:
            best_sec = ts

    rel_path = capture_item_screenshot(run_dir, item, sources, timestamp=best_sec)
    return {"healed": False, "screenshot": rel_path, "key": item.get("key"), "timestamp_seconds": best_sec}


def attach_search_candidate(item: dict, candidate_url: str, candidate_title: str = "", candidate_author: str = "", screenshot_path: str = None, reason: str = "") -> dict:
    """Attaches a candidate found via text search along with its source video screenshot for manual user verification."""
    if "candidates" not in item:
        item["candidates"] = []
    candidate_record = {
        "url": candidate_url,
        "title": candidate_title,
        "author": candidate_author,
        "match_type": "text_search_candidate",
        "reason": reason or "Candidate found via text search for manual verification",
        "screenshot": screenshot_path,
        "recorded_at": w.now()
    }
    item["candidates"].append(candidate_record)
    item["candidate_url"] = candidate_url
    if screenshot_path:
        item["screenshot"] = screenshot_path
    return candidate_record

def capture_screenshots_for_run(run_dir: Path, state: dict, keys: list[str] | None = None, smart: bool = True) -> dict:
    run_dir = Path(run_dir).resolve()
    captured = []
    healed = []
    items = state.get("items", [])
    sources = state.get("sources", {})
    target_keys = set(keys) if keys else None
    
    for it in items:
        k = it.get("key")
        if target_keys and k not in target_keys:
            continue
        if not target_keys:
            is_unresolved = it.get("exception") or not it.get("canonical_url") or it.get("candidates") or it.get("candidate_url")
            if not is_unresolved:
                continue
                
        if smart:
            res = smart_capture_item_screenshot(run_dir, it, sources)
            if res.get("healed"):
                healed.append(res)
                state["layout"] = [b for b in state.get("layout", []) if not (b.get("type") == "image" and b.get("owner_key") == k)]
            elif res.get("screenshot"):
                captured.append({"key": k, "title": it.get("title"), "screenshot": res["screenshot"], "timestamp": res.get("timestamp_seconds")})
        else:
            rel = capture_item_screenshot(run_dir, it, sources)
            if rel:
                captured.append({"key": k, "title": it.get("title"), "screenshot": rel})

    if healed:
        inv = state.get("inventory", {})
        if "review" in inv and inv["review"]:
            inv_keys = [i["key"] for i in items]
            inv["review"]["binding"] = w.digest({"layout": state["layout"], "inventory": inv_keys})

    w.write_json(run_dir / "run.json", state)
    return {"total_captured": len(captured), "captured": captured, "healed_count": len(healed), "healed": healed}



def embed_captured_screenshots_into_layout(run_dir: Path, state: dict, receipts: dict) -> dict:
    """Embeds uploaded screenshots into state layout as first-class image blocks."""
    run_dir = Path(run_dir).resolve()
    items = state.get("items", [])
    old_layout = [b for b in state.get("layout", []) if b.get("type") != "image"]
    new_layout = []
    embedded_count = 0

    for b in old_layout:
        new_layout.append(b)
        if b.get("type") == "item":
            k = b.get("key")
            if k in receipts:
                rec = receipts[k]
                token = rec.get("file_token") or rec.get("token") or rec.get("src")
                rel_file = rec.get("file") or rec.get("screenshot")
                if token and rel_file:
                    file_p = run_dir / rel_file
                    sha = hashlib.sha256(file_p.read_bytes()).hexdigest() if file_p.is_file() else ""
                    img_block = {
                        "type": "image",
                        "owner_key": k,
                        "src": token,
                        "caption": rec.get("caption") or "【原周报视频录像截图对照】",
                        "evidence": [{"path": rel_file, "sha256": sha}] if sha else []
                    }
                    new_layout.append(img_block)
                    embedded_count += 1
                    it = w.get_item(state, k)
                    if it:
                        it["screenshot"] = rel_file

    state["layout"] = new_layout
    inv = state.get("inventory", {})
    if "review" in inv and inv["review"]:
        inv_keys = [i["key"] for i in items]
        inv["review"]["binding"] = w.digest({"layout": state["layout"], "inventory": inv_keys})

    return {"embedded_count": embedded_count, "layout_blocks": len(new_layout)}


def verify_candidate_for_item(item: dict, candidate: dict, existing_ids: set) -> dict:
    """Checks if an OCR candidate URL belongs to this specific item using prefix matching or title correlation."""
    cid = candidate.get("id")
    plat = candidate.get("platform")
    if not cid or not plat:
        return {"matched": False, "reason": "missing_id_or_platform"}
    if cid in existing_ids:
        return {"matched": False, "reason": f"id_already_used_by_another_item: {cid}"}

    orig_url = (item.get("url") or item.get("original_url") or "").strip()
    m_trunc = re.search(r"douyin\.com/(?:video|note)/([0-9]{11,18})", orig_url)
    if m_trunc and plat == "douyin":
        trunc_digits = m_trunc.group(1)
        if cid.startswith(trunc_digits):
            return {
                "matched": True,
                "method": "screenshot_ocr_prefix_match",
                "resolved_url": candidate["url"],
                "platform": plat,
                "id": cid,
                "confidence": 1.0,
                "reason": f"Screenshot OCR 19-digit ID {cid} matched 11-digit prefix {trunc_digits}"
            }

    item_title = item.get("title", "")
    clean_item_title = re.sub(r"^\d+[\.、\s]", "", item_title).strip()
    clean_item_title_core = re.sub(r"[^\w\u4e00-\u9fff]", "", clean_item_title)
    raw = candidate.get("raw", "")
    clean_raw = re.sub(r"[^\w\u4e00-\u9fff]", "", raw)
    if clean_item_title_core and len(clean_item_title_core) >= 4:
        if clean_item_title_core in clean_raw or clean_raw in clean_item_title_core:
            return {
                "matched": True,
                "method": "screenshot_ocr_title_match",
                "resolved_url": candidate["url"],
                "platform": plat,
                "id": cid,
                "confidence": 0.9,
                "reason": f"Screenshot OCR URL near matching title: {clean_item_title}"
            }

    return {"matched": False, "reason": "no_prefix_or_title_match"}


def auto_heal_from_screenshots(run_dir: Path, state: dict, keys: list[str] | None = None) -> dict:
    """Scans screenshots of unresolved items via OCR, extracts genuine URLs, upgrades items and removes redundant image blocks."""
    run_dir = Path(run_dir).resolve()
    items = state.get("items", [])
    target_keys = set(keys) if keys else None

    existing_ids = {it.get("id") for it in items if it.get("id") and not it.get("exception")}
    frame_urls_file = run_dir / "frame_urls.json"
    timeline_cands = []
    if frame_urls_file.is_file():
        try:
            f_data = json.loads(frame_urls_file.read_text(encoding="utf-8"))
            if isinstance(f_data, dict):
                for _, c_list in f_data.items():
                    if isinstance(c_list, list):
                        timeline_cands.extend(c_list)
            elif isinstance(f_data, list):
                timeline_cands.extend(f_data)
        except Exception:
            pass


    healed = []
    skipped = []
    sc_dir = run_dir / "evidence" / "screenshots"

    for it in items:
        k = it.get("key")
        if target_keys and k not in target_keys:
            continue
        needs_healing = bool(it.get("exception")) or not bool(it.get("canonical_url")) or bool(it.get("candidate_url")) or bool(it.get("candidates")) or inspect_item_url_health(it)["issue"] in ("douyin_truncated_id", "missing_url")
        if not needs_healing:
            continue

        possible_scs = []
        if it.get("screenshot"):
            sc_p = run_dir / it["screenshot"]
            if sc_p.is_file():
                possible_scs.append(sc_p)

        safe_key = re.sub(r"[^\w\-]", "_", k)
        if sc_dir.is_dir():
            for f in sc_dir.glob(f"{safe_key}_*.jpg"):
                if f not in possible_scs:
                    possible_scs.append(f)

        if not possible_scs:
            cap_rel = capture_item_screenshot(run_dir, it, state.get("sources"))
            if cap_rel:
                possible_scs.append(run_dir / cap_rel)

        item_healed = False
        for sc_file in possible_scs:
            cands = scan_screenshot_for_urls(sc_file)
            for cand in cands:
                ver = verify_candidate_for_item(it, cand, existing_ids)
                if ver.get("matched"):
                    cid = ver["id"]
                    cplat = ver["platform"]
                    curl = ver["resolved_url"]

                    it["canonical_url"] = curl
                    it["url"] = curl
                    it["id"] = cid
                    it["platform"] = cplat
                    it["kind"] = "video" if cplat in ("douyin", "bilibili") else "link"
                    it["resolved_by"] = ver["method"]
                    it.pop("exception", None)
                    it.pop("screenshot", None)
                    it.pop("candidates", None)
                    it.pop("candidate_url", None)
                    existing_ids.add(cid)

                    # Strictly enforce mutual exclusion: remove screenshot image blocks for items with links
                    state["layout"] = [b for b in state.get("layout", []) if not (b.get("type") == "image" and b.get("owner_key") == k)]

                    healed.append({
                        "key": k,
                        "title": it.get("title"),
                        "resolved_url": curl,
                        "platform": cplat,
                        "method": ver["method"],
                        "screenshot": str(sc_file.relative_to(run_dir))
                    })
                    item_healed = True
                    break
            if item_healed:
                break
        
        if not item_healed and timeline_cands:
            for tc in timeline_cands:
                ver = verify_candidate_for_item(it, tc, existing_ids)
                if ver.get("matched"):
                    cid = ver["id"]
                    cplat = ver["platform"]
                    curl = ver["resolved_url"]
                    it["canonical_url"] = curl
                    it["url"] = curl
                    it["id"] = cid
                    it["platform"] = cplat
                    it["kind"] = "video" if cplat in ("douyin", "bilibili") else "link"
                    it["resolved_by"] = "timeline_frame_ocr"
                    it.pop("exception", None)
                    it.pop("screenshot", None)
                    it.pop("candidates", None)
                    it.pop("candidate_url", None)
                    existing_ids.add(cid)
                    state["layout"] = [b for b in state.get("layout", []) if not (b.get("type") == "image" and b.get("owner_key") == k)]
                    healed.append({
                        "key": k,
                        "title": it.get("title"),
                        "resolved_url": curl,
                        "platform": cplat,
                        "method": "timeline_frame_ocr"
                    })
                    item_healed = True
                    break

        if not item_healed:
            skipped.append(k)

    if healed:
        inv = state.get("inventory", {})
        if "review" in inv and inv["review"]:
            inv_keys = [i["key"] for i in items]
            inv["review"]["binding"] = w.digest({"layout": state["layout"], "inventory": inv_keys})

    return {
        "healed_count": len(healed),
        "healed_items": healed,
        "unresolved_remaining": len(skipped),
        "skipped_keys": skipped
    }

def scan_and_report_issues(manifest_data: dict) -> dict:
    items = manifest_data.get("items", [])
    unresolved = []
    healthy = []
    
    for idx, item in enumerate(items, 1):
        h = inspect_item_url_health(item)
        if not h["healthy"]:
            unresolved.append({
                "index": idx,
                "key": item.get("key") or f"item_{idx}",
                "title": item.get("title"),
                "url": item.get("url"),
                "issue": h["issue"],
                "suggested_queries": build_search_queries(item)
            })
        else:
            healthy.append(item)
            
    return {
        "total_items": len(items),
        "healthy_count": len(healthy),
        "unresolved_count": len(unresolved),
        "unresolved_items": unresolved
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Path to manifest or seed json")
    parser.add_argument("--output", type=Path, help="Path to output report json")
    parser.add_argument("--apply-resolutions", type=Path, help="Path to json mapping key/title to resolved URL")
    parser.add_argument("--auto-heal", type=Path, help="Path to run directory to perform screenshot OCR auto-heal")
    parser.add_argument("--scan-screenshots", nargs="*", help="Image paths to scan for embedded video URLs")
    args = parser.parse_args()

    if args.scan_screenshots:
        all_sc_cands = {}
        for img in args.scan_screenshots:
            all_sc_cands[img] = scan_screenshot_for_urls(img)
        print(json.dumps(all_sc_cands, ensure_ascii=False, indent=2))
        return

    if args.auto_heal:
        run_d = args.auto_heal.resolve()
        st = w.read_json(run_d / "run.json")
        h_res = auto_heal_from_screenshots(run_d, st)
        w.write_json(run_d / "run.json", st)
        print(json.dumps(h_res, ensure_ascii=False, indent=2))
        return
    
    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = scan_and_report_issues(data)
    
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
                v = verify_resolution(item, c_url, c_title, c_author)
                if v["verified"]:
                    item["url"] = v["resolved_url"]
                    item["resolved_by"] = v["method"]
                    applied += 1
        report["applied_resolutions"] = applied
        args.manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
