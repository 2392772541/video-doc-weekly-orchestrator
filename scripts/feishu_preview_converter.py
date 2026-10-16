# coding: utf-8
"""feishu_preview_converter.py
Converts Feishu Docx Bookmark Blocks (blue card links) into interactive Preview Blocks (Iframe players).
Preserves document hierarchy, block ordering, and ensures no text/images/headings are compromised.
Timing: Strictly executed at the final stage of the weekly report pipeline, after all videos are collected.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path


def get_doc_xml_and_blocks(doc_id: str) -> dict:
    """Fetches full XML content and block tree for a Feishu docx."""
    fetch_cmd = f"lark-cli docs +fetch --doc {doc_id} --detail with-ids"
    res = subprocess.run(fetch_cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        raise RuntimeError(f"lark-cli fetch failed: {res.stderr or res.stdout}")
    
    data = json.loads(res.stdout)
    if not data.get("ok"):
        raise RuntimeError(f"lark-cli returned non-ok: {data}")
        
    xml = data.get("data", {}).get("document", {}).get("content", "")
    rev_id = data.get("data", {}).get("document", {}).get("revision_id")
    
    # Also fetch root block children structure
    root_cmd = f"lark-cli api GET /open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}"
    root_res = subprocess.run(root_cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
    root_data = json.loads(root_res.stdout) if root_res.returncode == 0 else {}
    children = root_data.get("data", {}).get("block", {}).get("children", [])
    
    return {
        "doc_id": doc_id,
        "revision_id": rev_id,
        "xml": xml,
        "children": children
    }


def audit_doc_content(xml: str) -> dict:
    """Audits structural elements in document XML content."""
    bookmarks = re.findall(r'<bookmark id="([^"]+)" name="([^"]*)" href="([^"]+)"></bookmark>', xml)
    iframes = re.findall(r'<readonly-block id="([^"]+)" href="([^"]+)" type="iframe"></readonly-block>', xml)
    headings = re.findall(r'<h[1-6] id="([^"]+)">([^<]*)</h[1-6]>', xml)
    paragraphs = re.findall(r'<p id="([^"]+)">([^<]*)</p>', xml)
    images = re.findall(r'<img id="([^"]+)"', xml)
    
    return {
        "bookmark_count": len(bookmarks),
        "bookmarks": [{"id": b[0], "name": b[1], "href": b[2]} for b in bookmarks],
        "iframe_count": len(iframes),
        "heading_count": len(headings),
        "paragraph_count": len(paragraphs),
        "image_count": len(images)
    }


def convert_single_bookmark(doc_id: str, block_id: str, url: str, temp_dir: Path | None = None) -> dict:
    """Converts a single bookmark block into an iframe preview block in-place."""
    temp_dir = temp_dir or Path(os.environ.get("TEMP", "."))
    
    # 1. Fetch fresh root children
    root_cmd = f"lark-cli api GET /open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}"
    root_res = subprocess.run(root_cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
    root_data = json.loads(root_res.stdout)
    children = root_data.get("data", {}).get("block", {}).get("children", [])
    
    if block_id not in children:
        return {"ok": False, "error": f"block_id {block_id} not in top-level children"}
        
    idx = children.index(block_id)
    iframe_type = 1 if "bilibili.com" in url else 99
    encoded_url = urllib.parse.quote(url, safe="")
    
    payload = {
        "index": idx,
        "children": [
            {
                "block_type": 26,
                "iframe": {
                    "component": {
                        "iframe_type": iframe_type,
                        "url": encoded_url
                    }
                }
            }
        ]
    }
    
    temp_file = temp_dir / f"feishu_payload_{block_id}.json"
    temp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    
    try:
        # 2. Insert iframe block at idx
        insert_cmd = f'lark-cli api POST /open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children --data @"{temp_file}"'
        res_insert = subprocess.run(insert_cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
        insert_data = json.loads(res_insert.stdout) if res_insert.returncode == 0 else {}
        if not insert_data.get("ok"):
            return {"ok": False, "error": f"insert failed: {res_insert.stdout}"}
            
        new_children = insert_data.get("data", {}).get("children", [])
        new_iframe_id = new_children[0].get("children", [None])[0] if new_children else None
        
        # 3. Delete old bookmark block
        del_cmd = f"lark-cli docs +update --doc {doc_id} --command block_delete --block-id {block_id}"
        res_del = subprocess.run(del_cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
        del_data = json.loads(res_del.stdout) if res_del.returncode == 0 else {}
        if not del_data.get("ok"):
            return {"ok": False, "error": f"delete failed: {res_del.stdout}", "inserted_iframe_id": new_iframe_id}
            
        return {
            "ok": True,
            "old_block_id": block_id,
            "new_iframe_id": new_iframe_id,
            "url": url,
            "iframe_type": iframe_type,
            "index": idx
        }
    finally:
        temp_file.unlink(missing_ok=True)


def convert_all_bookmarks_to_previews(doc_id: str, delay: float = 0.3) -> dict:
    """Full batch conversion of all bookmark blocks in a Feishu doc to iframe preview blocks.
    Executes pre-audit, item-by-item atomic insertion & deletion, and post-audit validation.
    """
    initial_info = get_doc_xml_and_blocks(doc_id)
    pre_audit = audit_doc_content(initial_info["xml"])
    
    bookmarks = pre_audit["bookmarks"]
    total = len(bookmarks)
    
    if total == 0:
        return {
            "ok": True,
            "message": "文档中无待转换的蓝色书签卡片（已全部为预览视图或无链接）",
            "pre_audit": pre_audit,
            "converted_count": 0,
            "converted": [],
            "post_audit": pre_audit
        }
        
    converted = []
    failed = []
    
    for i, b in enumerate(bookmarks, 1):
        b_id = b["id"]
        href = b["href"]
        res = convert_single_bookmark(doc_id, b_id, href)
        if res.get("ok"):
            converted.append(res)
        else:
            failed.append({"id": b_id, "href": href, "error": res.get("error")})
        time.sleep(delay)
        
    # Post-audit check
    post_info = get_doc_xml_and_blocks(doc_id)
    post_audit = audit_doc_content(post_info["xml"])
    
    # Integrity check: Ensure headings, paragraphs, and images were not dropped
    headings_preserved = post_audit["heading_count"] == pre_audit["heading_count"]
    paragraphs_preserved = post_audit["paragraph_count"] == pre_audit["paragraph_count"]
    images_preserved = post_audit["image_count"] == pre_audit["image_count"]
    
    integrity_ok = headings_preserved and paragraphs_preserved and images_preserved
    
    return {
        "ok": len(failed) == 0 and integrity_ok,
        "doc_id": doc_id,
        "initial_revision_id": initial_info["revision_id"],
        "final_revision_id": post_info["revision_id"],
        "pre_audit": pre_audit,
        "converted_count": len(converted),
        "failed_count": len(failed),
        "converted": converted,
        "failed": failed,
        "post_audit": post_audit,
        "integrity_check": {
            "all_preserved": integrity_ok,
            "headings_preserved": headings_preserved,
            "paragraphs_preserved": paragraphs_preserved,
            "images_preserved": images_preserved
        }
    }


if __name__ == "__main__":
    import sys
    doc_id = sys.argv[1] if len(sys.argv) > 1 else "ImVld2fwgoOB5MxzzSvc5q9Qnqh"
    res = convert_all_bookmarks_to_previews(doc_id)
    print(json.dumps(res, ensure_ascii=False, indent=2))
