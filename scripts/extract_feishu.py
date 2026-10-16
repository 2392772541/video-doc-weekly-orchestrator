"""Extract work occurrences from a full Feishu XML readback. Never verifies identity.
Handles numbered h3/h4 titles, inline links, paragraphs and list items; retains order.
"""
import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit
import weekly_core as w

NUMBERED = re.compile(r"^\s*(\d+)[.、．)]\s*(.*)", re.S)
KNOWN_SUBSECTIONS = {
    "热点/认知", "关系/情感", "生命/告别", "轻松/日常",
    "视频制作", "人设", "搞钱",
    "AI周报", "AI优秀文章", "AI改变", "AI热点", "AI基础"
}


def resolve_embedded_player_url(url: str) -> str:
    """Converts Feishu embedded player URLs (readonly-block/iframe) back to canonical video URLs."""
    if not url:
        return ""
    if "player.bilibili.com" in url:
        m = re.search(r"bvid=([A-Za-z0-9]+)", url)
        if m:
            bvid = m.group(1)
            if not bvid.startswith("BV"):
                bvid = "BV" + bvid
            return f"https://www.bilibili.com/video/{bvid}"
    elif "open.douyin.com" in url:
        m = re.search(r"vid=(\d+)", url)
        if m:
            return f"https://www.douyin.com/video/{m.group(1)}"
    return url


def extract(payload, platform=None):
    w.require(payload.get("ok") is True and payload.get("identity") == "user", "full_user_feishu_read_required")
    doc = payload["data"]["document"]
    w.require(doc.get("document_id") and doc.get("revision_id") is not None, "source_revision_required")
    root = ET.fromstring("<root>" + doc["content"] + "</root>")
    title = next(("".join(e.itertext()) for e in root if e.tag == "title"), "未命名周报")
    items, section, subsection, owner = [], "未分组", "", None
    for e in root.iter():
        heading = re.fullmatch(r"h([1-6])", e.tag)
        if not heading and e.tag not in {"p", "li", "readonly-block", "iframe"}:
            continue
        if e.tag == "li" and any(c.tag in {"p", "li"} for c in e.iter() if c is not e):
            continue  # Visit nested blocks, not the aggregate parent's duplicate text.
        text = "".join(e.itertext()).strip().lstrip("# ")
        numbered = NUMBERED.match(text)
        if numbered:
            owner = {"number": int(numbered[1]), "title": numbered[2], "block_id": e.get("block-id", e.get("id", ""))}
        elif heading:
            lvl = int(heading[1])
            if lvl <= 2:
                section, subsection = text, ""
                owner = None
            elif lvl == 3:
                subsection = text
                owner = {"number": None, "title": text, "block_id": e.get("block-id", e.get("id", ""))}
            else:
                owner = {"number": None, "title": text, "block_id": e.get("block-id", e.get("id", ""))}
        urls = [a.get("href", "") for a in e.iter("a")]
        if e.get("href"):
            urls.append(e.get("href"))
        urls += re.findall(r'https?://[^\s<>"，）]+', text)
        resolved_urls = []
        for u in urls:
            u_clean = u.rstrip(".,;，。；）】")
            resolved_urls.append(resolve_embedded_player_url(u_clean))
        urls = list(dict.fromkeys(resolved_urls))
        for url in urls:
            u = urlsplit(url)
            kind = None
            if u.hostname in {"douyin.com", "www.douyin.com", "m.douyin.com", "v.douyin.com", "open.douyin.com"} and (re.match(r"/(video|note|player)/", u.path) or "modal_id=" in u.query or "vid=" in u.query or u.hostname == "v.douyin.com"):
                kind = "douyin"
            elif u.hostname in {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv", "player.bilibili.com"} and (u.path.startswith("/video/") or u.path.startswith("/player") or u.hostname == "b23.tv" or "bvid=" in u.query):
                kind = "bilibili"
            if not kind or platform and platform != kind:
                continue
            n = len(items) + 1
            label = owner["title"] if owner else "待定位标题 " + str(n)
            # An inline URL is not part of a numbered title.
            label = label.replace(url, "").strip() if numbered else label
            sub = "" if subsection == label else subsection
            items.append({
                "key": f"i{n:04d}",
                "section": section,
                "subsection": sub,
                "number": owner["number"] if owner and owner["number"] is not None else n,
                "title": label or "待定位标题 " + str(n),
                "kind": "video",
                "platform": kind,
                "url": url,
                "source_block_id": (owner or {}).get("block_id", ""),
                "extraction_note": "Proposed owning heading only; inspect same-item source and original before verification."
            })
    w.require(items, "no_work_urls_found_not_assumed_empty_success")
    return {"title": title, "source_week": "", "numbered_item_count": len(items), "quick_news_count": 0, "scope": "collection_inventory_only_not_full_report", "source_document": {"document_id": doc["document_id"], "revision_id": doc["revision_id"]}, "url_occurrences": len(items), "unique_raw_urls": len({i["url"] for i in items}), "items": items}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--platform", choices=sorted(w.VIDEO_PLATFORMS), help="Only for an explicitly single-platform collection request")
    args = p.parse_args()
    result = extract(w.read_json(args.input), args.platform)
    w.write_json(args.out, result)
    print(json.dumps({"out": str(args.out), "items": len(result["items"]), "unique_raw_urls": result["unique_raw_urls"], "source_document": result["source_document"], "verified": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
