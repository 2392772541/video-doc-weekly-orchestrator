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
            if common >= max(6, len(clean_orig_title) * 0.6):
                title_match = True
                
    author_match = True
    if orig_author and candidate_author:
        author_match = (orig_author in candidate_author) or (candidate_author in orig_author)
        
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

def capture_screenshots_for_run(run_dir: Path, state: dict, keys: list[str] | None = None) -> dict:
    captured = []
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
                
        rel = capture_item_screenshot(run_dir, it, sources)
        if rel:
            captured.append({"key": k, "title": it.get("title"), "screenshot": rel})
            
    return {"total_captured": len(captured), "captured": captured}



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
    args = parser.parse_args()
    
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
