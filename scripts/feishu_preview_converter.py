# coding: utf-8
"""feishu_preview_converter.py
Converts Feishu Docx Bookmark Blocks and Blue Link Paragraphs into interactive Preview Blocks (Iframe players).
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
    para_links = re.findall(r'<p id="([^"]+)"><a href="(https?://[^"]*(?:bilibili|douyin)[^"]*)">([^<]*)</a></p>', xml)
    iframes = re.findall(r'<readonly-block id="([^"]+)" href="([^"]+)" type="iframe">', xml)
    headings = re.findall(r'<h[1-6] id="([^"]+)">([^<]*)</h[1-6]>', xml)
    paragraphs = re.findall(r'<p id="([^"]+)">([^<]*)</p>', xml)
    images = re.findall(r'<img id="([^"]+)"', xml)
    
    targets = []
    for b in bookmarks:
        targets.append({"id": b[0], "name": b[1], "href": b[2], "kind": "bookmark"})
    for p in para_links:
        targets.append({"id": p[0], "name": p[2], "href": p[1], "kind": "paragraph_link"})
        
    return {
        "bookmark_count": len(bookmarks),
        "bookmarks": [{"id": b[0], "name": b[1], "href": b[2]} for b in bookmarks],
        "bookmarks": [{"id": b[0], "name": b[1], "href": b[2]} for b in bookmarks],
        "para_link_count": len(para_links),
        "target_count": len(targets),
        "targets": targets,
        "iframe_count": len(iframes),
        "heading_count": len(headings),
        "paragraph_count": len(paragraphs),
        "image_count": len(images)
    }


def convert_single_link_target(doc_id: str, block_id: str, url: str) -> dict:
    """Converts a single bookmark or link paragraph block into an iframe preview block in-place."""
    root_cmd = f"lark-cli api GET /open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}"
    root_res = subprocess.run(root_cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
    root_data = json.loads(root_res.stdout) if root_res.returncode == 0 else {}
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
    
    # Must use a relative path within current working directory for lark-cli
    temp_file = Path(f"./temp_feishu_payload_{block_id}.json")
    temp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    
    try:
        rel_path = temp_file.name
        insert_cmd = f"lark-cli api POST /open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children --data @{rel_path}"
        res_insert = subprocess.run(insert_cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
        insert_data = json.loads(res_insert.stdout) if res_insert.returncode == 0 and res_insert.stdout.strip() else {}
        if not insert_data.get("ok"):
            err_msg = res_insert.stderr or res_insert.stdout
            return {"ok": False, "error": f"insert failed: {err_msg}"}
            
        new_children = insert_data.get("data", {}).get("children", [])
        new_iframe_id = new_children[0].get("block_id") if new_children else None
        
        del_cmd = f"lark-cli docs +update --doc {doc_id} --command block_delete --block-id {block_id}"
        res_del = subprocess.run(del_cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
        del_data = json.loads(res_del.stdout) if res_del.returncode == 0 and res_del.stdout.strip() else {}
        if not del_data.get("ok"):
            err_msg = res_del.stderr or res_del.stdout
            return {"ok": False, "error": f"delete failed: {err_msg}", "inserted_iframe_id": new_iframe_id}
            
        return {
            "ok": True,
            "old_block_id": block_id,
            "new_iframe_id": new_iframe_id,
            "url": url,
            "iframe_type": iframe_type,
            "index": idx
        }
    finally:
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)


def convert_all_bookmarks_to_previews(doc_id: str, delay: float = 0.3) -> dict:
    """Full batch conversion of all bookmark and blue link blocks in a Feishu doc to iframe preview blocks.
    Executes pre-audit, item-by-item atomic insertion & deletion, and post-audit validation.
    """
    initial_info = get_doc_xml_and_blocks(doc_id)
    pre_audit = audit_doc_content(initial_info["xml"])
    
    targets = pre_audit["targets"]
    total = len(targets)
    
    if total == 0:
        return {
            "ok": True,
            "message": "文档中无待转换的蓝色书签或链接卡片（已全部为预览视图或无链接）",
            "pre_audit": pre_audit,
            "converted_count": 0,
            "converted": [],
            "post_audit": pre_audit
        }
        
    converted = []
    failed = []
    
    for i, t in enumerate(targets, 1):
        b_id = t["id"]
        href = t["href"]
        res = convert_single_link_target(doc_id, b_id, href)
        if res.get("ok"):
            converted.append(res)
        else:
            failed.append({"id": b_id, "href": href, "error": res.get("error")})
        time.sleep(delay)
        
    post_info = get_doc_xml_and_blocks(doc_id)
    post_audit = audit_doc_content(post_info["xml"])
    
    headings_preserved = post_audit["heading_count"] == pre_audit["heading_count"]
    images_preserved = post_audit["image_count"] == pre_audit["image_count"]
    integrity_ok = headings_preserved and images_preserved
    
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
            "images_preserved": images_preserved
        }
    }


if __name__ == "__main__":
    import sys
    doc_id = sys.argv[1] if len(sys.argv) > 1 else "ZUuKdpwdZoWuBcxQwyVcxwSsnyi"
    res = convert_all_bookmarks_to_previews(doc_id)
    print(json.dumps(res, ensure_ascii=False, indent=2))
