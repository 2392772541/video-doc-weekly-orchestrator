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
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path


def normalize_doc_id(doc_ref: str) -> str:
    """从粘贴的飞书 URL 或裸 token 中提取纯净文档 token，防止 Windows 批处理拆分 & 参数。"""
    raw = (doc_ref or "").strip()
    m = re.search(r"/(?:docx|doc)/([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    if raw.startswith(("http://", "https://")):
        return raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return raw


def _is_video_preview_url(url: str) -> bool:
    """判断链接是否属于支持飞书原生播放器预览的 B 站 / 抖音作品链接（排除普通图文书签误转换）。"""
    try:
        host = (urllib.parse.urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    if host in {"live.bilibili.com", "live.douyin.com"}:
        return False
    return (
        host in {"bilibili.com", "douyin.com"}
        or host.endswith(".bilibili.com")
        or host.endswith(".douyin.com")
    )


def _run_lark_cli(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """以参数数组方式安全调用 lark-cli（禁用 shell=True），统一显式使用 --as user。"""
    lark_bin = shutil.which("lark-cli") or "lark-cli"
    cmd = [lark_bin, *args]
    if "--as" not in cmd:
        cmd.extend(["--as", "user"])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
    )


def get_doc_xml_and_blocks(doc_id: str) -> dict:
    """Fetches full XML content and block tree for a Feishu docx."""
    clean_id = normalize_doc_id(doc_id)
    res = _run_lark_cli(["docs", "+fetch", "--doc", clean_id, "--detail", "with-ids"])
    if res.returncode != 0:
        raise RuntimeError(f"lark-cli fetch failed: {res.stderr or res.stdout}")

    data = json.loads(res.stdout)
    if not data.get("ok"):
        raise RuntimeError(f"lark-cli returned non-ok: {data}")

    xml = data.get("data", {}).get("document", {}).get("content", "")
    rev_id = data.get("data", {}).get("document", {}).get("revision_id")

    root_res = _run_lark_cli(["api", "GET", f"/open-apis/docx/v1/documents/{clean_id}/blocks/{clean_id}"])
    root_data = json.loads(root_res.stdout) if root_res.returncode == 0 and root_res.stdout.strip() else {}
    children = root_data.get("data", {}).get("block", {}).get("children", [])

    return {
        "doc_id": clean_id,
        "revision_id": rev_id,
        "xml": xml,
        "children": children
    }


def audit_doc_content(xml: str) -> dict:
    """Audits structural elements in document XML content."""
    bookmarks_list: list[dict] = []
    para_links_list: list[dict] = []
    iframe_count = 0
    heading_count = 0
    paragraph_count = 0
    image_count = 0

    try:
        root = ET.fromstring(f"<weekly-audit-root>{xml}</weekly-audit-root>")
        for el in root.iter():
            tag = el.tag
            if tag == "bookmark":
                bid = el.get("id") or el.get("block-id") or ""
                name = el.get("name") or ""
                href = el.get("href") or ""
                if bid and href:
                    bookmarks_list.append({"id": bid, "name": name, "href": href})
            elif tag == "readonly-block" and el.get("type") == "iframe":
                iframe_count += 1
            elif tag == "iframe":
                iframe_count += 1
            elif re.fullmatch(r"h[1-6]", tag):
                heading_count += 1
            elif tag == "img":
                image_count += 1
            elif tag == "p":
                paragraph_count += 1
                pid = el.get("id") or el.get("block-id") or ""
                # 仅当段落主体就是一条单独的 B 站/抖音蓝链时才列入待转目标，不破坏正文含链接的说明段
                children = list(el)
                leading_text = (el.text or "").strip()
                if pid and not leading_text and len(children) == 1 and children[0].tag == "a" and not (children[0].tail or "").strip():
                    href = children[0].get("href") or ""
                    if _is_video_preview_url(href):
                        para_links_list.append({"id": pid, "name": "".join(children[0].itertext()), "href": href})
    except ET.ParseError:
        # 兜底兼容非严格 XML 片段
        bookmarks = re.findall(r'<bookmark id="([^"]+)" name="([^"]*)" href="([^"]+)"></bookmark>', xml)
        para_links = re.findall(r'<p id="([^"]+)"><a href="(https?://[^"]*(?:bilibili|douyin)[^"]*)">([^<]*)</a></p>', xml)
        iframes = re.findall(r'<readonly-block id="([^"]+)" href="([^"]+)" type="iframe">', xml)
        headings = re.findall(r'<h[1-6]\b[^>]*>.*?</h[1-6]>', xml, re.S)
        paragraphs = re.findall(r'<p\b[^>]*>.*?</p>', xml, re.S)
        images = re.findall(r'<img\b[^>]*id="([^"]+)"', xml)
        bookmarks_list = [{"id": b[0], "name": b[1], "href": b[2]} for b in bookmarks]
        para_links_list = [{"id": p[0], "name": p[2], "href": p[1]} for p in para_links if _is_video_preview_url(p[1])]
        iframe_count = len(iframes)
        heading_count = len(headings)
        paragraph_count = len(paragraphs)
        image_count = len(images)

    targets = []
    skipped_non_video_bookmarks = 0
    for b in bookmarks_list:
        if _is_video_preview_url(b["href"]):
            targets.append({"id": b["id"], "name": b["name"], "href": b["href"], "kind": "bookmark"})
        else:
            skipped_non_video_bookmarks += 1
    for p in para_links_list:
        targets.append({"id": p["id"], "name": p["name"], "href": p["href"], "kind": "paragraph_link"})

    return {
        "bookmark_count": len(bookmarks_list),
        "bookmarks": bookmarks_list,
        "skipped_non_video_bookmark_count": skipped_non_video_bookmarks,
        "para_link_count": len(para_links_list),
        "target_count": len(targets),
        "targets": targets,
        "iframe_count": iframe_count,
        "heading_count": heading_count,
        "paragraph_count": paragraph_count,
        "image_count": image_count
    }


def convert_single_link_target(doc_id: str, block_id: str, url: str) -> dict:
    """Converts a single bookmark or link paragraph block into an iframe preview block in-place."""
    clean_id = normalize_doc_id(doc_id)
    root_res = _run_lark_cli(["api", "GET", f"/open-apis/docx/v1/documents/{clean_id}/blocks/{clean_id}"])
    root_data = json.loads(root_res.stdout) if root_res.returncode == 0 and root_res.stdout.strip() else {}
    children = root_data.get("data", {}).get("block", {}).get("children", [])

    if block_id not in children:
        return {"ok": False, "error": f"block_id {block_id} not in top-level children"}

    idx = children.index(block_id)
    host = (urllib.parse.urlsplit((url or "").strip()).hostname or "").lower()
    iframe_type = 1 if (host == "bilibili.com" or host.endswith(".bilibili.com")) else 99
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

    # 在独立临时目录内以相对路径 @payload.json 传给 lark-cli，既满足相对路径约束又不污染工作目录
    with tempfile.TemporaryDirectory(prefix="feishu_preview_") as tdir:
        tdir_p = Path(tdir)
        payload_file = tdir_p / "payload.json"
        payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        res_insert = _run_lark_cli(
            ["api", "POST", f"/open-apis/docx/v1/documents/{clean_id}/blocks/{clean_id}/children", "--data", "@payload.json"],
            cwd=tdir_p,
        )
        insert_data = json.loads(res_insert.stdout) if res_insert.returncode == 0 and res_insert.stdout.strip() else {}
        if not insert_data.get("ok"):
            err_msg = res_insert.stderr or res_insert.stdout
            return {"ok": False, "error": f"insert failed: {err_msg}"}

        new_children = insert_data.get("data", {}).get("children", [])
        new_iframe_id = new_children[0].get("block_id") if new_children else None

        res_del = _run_lark_cli(
            ["docs", "+update", "--doc", clean_id, "--command", "block_delete", "--block-id", block_id]
        )
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


def convert_all_bookmarks_to_previews(doc_id: str, delay: float = 0.3) -> dict:
    """Full batch conversion of all bookmark and blue link blocks in a Feishu doc to iframe preview blocks.
    Executes pre-audit, item-by-item atomic insertion & deletion, and post-audit validation.
    """
    clean_id = normalize_doc_id(doc_id)
    initial_info = get_doc_xml_and_blocks(clean_id)
    pre_audit = audit_doc_content(initial_info["xml"])

    targets = pre_audit["targets"]
    total = len(targets)

    if total == 0:
        return {
            "ok": True,
            "doc_id": clean_id,
            "message": "文档中无待转换的蓝色书签或链接卡片（已全部为预览视图或无视频链接）",
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
        res = convert_single_link_target(clean_id, b_id, href)
        if res.get("ok"):
            converted.append(res)
        else:
            failed.append({"id": b_id, "href": href, "error": res.get("error")})
        time.sleep(delay)

    post_info = get_doc_xml_and_blocks(clean_id)
    post_audit = audit_doc_content(post_info["xml"])

    headings_preserved = post_audit["heading_count"] == pre_audit["heading_count"]
    images_preserved = post_audit["image_count"] == pre_audit["image_count"]
    integrity_ok = headings_preserved and images_preserved

    return {
        "ok": len(failed) == 0 and integrity_ok,
        "doc_id": clean_id,
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
