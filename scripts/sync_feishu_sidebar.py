# coding: utf-8
import argparse
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

DEFAULT_SUBSECTIONS = [
    # 游戏人生，说地谈天
    ("热点/认知", "3. 知行合一"),
    ("关系/情感", "15. 人是社会关系的总和"),
    ("生命/告别", "20. 当我们开始告别时"),
    ("健身/励志", "开源健身神器"),
    ("轻松/娱乐", "逃？往哪里逃"),
    ("轻松/日常", "21. 依旧小品卡万物"),
    # 自媒体与搞钱
    ("视频制作", "1. 【牧】AI全自动剪辑"),
    ("人设", "15. 你不是能力差"),
    ("搞钱", "17. 一个很简单又很适合美术生的副业"),
    # AI视野
    ("AI周报", "1. AI周报"),
    ("AI优秀文章", "2. 【Grok中配】"),
    ("AI改变", "7. 刚刚，Karpathy"),
    ("AI热点", "10. Inclusion · 外滩大会"),
    ("AI基础", "13. 不会用Github")
]


def normalize_doc_id(doc_ref: str) -> str:
    """从飞书文档 URL 或裸 token 中提取纯净 token，避免 Windows 批处理拆分 & 参数。"""
    raw = (doc_ref or "").strip()
    m = re.search(r"/(?:docx|doc)/([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    if raw.startswith(("http://", "https://")):
        return raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return raw


def run_lark(args: list[str], cwd: Path | None = None):
    """以参数数组安全执行 lark-cli（禁用 shell=True），统一使用 --as user。"""
    lark_bin = shutil.which("lark-cli") or "lark-cli"
    cmd = [lark_bin, *args]
    if "--as" not in cmd:
        cmd.extend(["--as", "user"])
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\nOutput: {res.stdout}\nError: {res.stderr}")
    try:
        return json.loads(res.stdout)
    except Exception:
        return res.stdout


def get_current_state(doc_id: str):
    clean_id = normalize_doc_id(doc_id)
    data = run_lark(["docs", "+fetch", "--doc", clean_id, "--detail", "with-ids"])
    xml = data["data"]["document"]["content"]
    rev = data["data"]["document"]["revision_id"]
    return xml, rev


def sync_document_sidebar(doc_id: str, subsections_config: list = None, temp_dir: Path = None) -> dict:
    """Rebuilds Feishu document outline sidebar by:
    1. Converting item H3s under target major sections to H4
    2. Inserting authoritative H3 subsection headings before specific anchor items
    3. Verifying the resulting outline hierarchy
    """
    clean_id = normalize_doc_id(doc_id)
    print(f"=== Step 1: Analyze Document Headings for {clean_id} ===")
    xml, rev = get_current_state(clean_id)
    headings = re.findall(r"<(h[23]) id=\"([^\"]+)\"(?:[^>]*)>(.*?)</\1>", xml, re.S)

    # Check existing subsections
    existing_subtitles = {re.sub(r"<[^>]+>", "", c).strip() for lvl, _, c in headings if lvl == "h3"}

    items_to_convert = []
    in_target = False
    cur_sec = ""
    sub_list = subsections_config or DEFAULT_SUBSECTIONS
    sub_names = {s[0] for s in sub_list}

    for lvl, bid, content in headings:
        if lvl == "h2":
            clean_sec = re.sub(r"<[^>]+>", "", content).strip()
            if any(k in clean_sec for k in ["游戏人生", "自媒体与搞钱", "AI视野"]):
                in_target = True
                cur_sec = clean_sec
            else:
                in_target = False
        elif lvl == "h3" and in_target:
            clean_c = re.sub(r"<[^>]+>", "", content).strip()
            # Don't convert if it's already a subsection header
            if clean_c not in sub_names:
                items_to_convert.append((cur_sec, bid, content))

    print(f"Found {len(items_to_convert)} items to convert from H3 to H4 under target sections.")

    print("\n=== Step 2: Converting Items to H4 (in-place replacement) ===")
    failed_count = 0
    with tempfile.TemporaryDirectory(prefix="feishu_sidebar_") as tdir:
        tdir_p = Path(tdir)
        wrap_file = tdir_p / "wrap.xml"
        converted_count = 0
        for i, (sec, bid, content) in enumerate(items_to_convert, 1):
            h4_content = f"<h4>{content}</h4>"
            wrap_file.write_text(h4_content, encoding="utf-8")
            try:
                # 必须切入临时目录并以相对路径 @wrap.xml 传参，避免 lark-cli 拒收绝对路径
                res = run_lark(
                    ["docs", "+update", "--doc", clean_id, "--command", "block_replace", "--block-id", bid, "--content", "@wrap.xml"],
                    cwd=tdir_p,
                )
                if isinstance(res, dict) and res.get("ok"):
                    converted_count += 1
                    clean_title = re.sub(r"<[^>]+>", "", content)[:30]
                    print(f"  [{i:02d}/{len(items_to_convert)}] Converted -> H4: {clean_title}")
                else:
                    failed_count += 1
                    print(f"  [FAIL] {i}/{len(items_to_convert)} {bid}: {res}")
            except Exception as ex:
                failed_count += 1
                print(f"  [ERROR] {bid}: {ex}")
            time.sleep(0.08)

    print("\n=== Step 3: Fetch Fresh Children and XML to Insert Subsections ===")
    root_data = run_lark(["api", "GET", f"/open-apis/docx/v1/documents/{clean_id}/blocks/{clean_id}"])
    children = root_data["data"]["block"]["children"]

    xml_fresh, _ = get_current_state(clean_id)
    fresh_h4 = re.findall(r"<(h[2-4]) id=\"([^\"]+)\"(?:[^>]*)>(.*?)</\1>", xml_fresh, re.S)

    subsections_to_insert = sub_list

    insert_actions = []
    for sub_title, anchor_kw in subsections_to_insert:
        if sub_title in existing_subtitles:
            print(f"  [SKIP] Subsection '{sub_title}' already exists in document.")
            continue
        target_bid = None
        for lvl, bid, content in fresh_h4:
            clean_text = re.sub(r"<[^>]+>", "", content)
            if anchor_kw in clean_text:
                target_bid = bid
                break
        if not target_bid:
            print(f"  [WARN] Anchor item for subsection '{sub_title}' (keyword: '{anchor_kw}') not found, skipping insertion.")
            continue
        try:
            idx = children.index(target_bid)
            insert_actions.append((idx, sub_title, target_bid, anchor_kw))
        except ValueError:
            pass

    insert_actions.sort(key=lambda x: x[0], reverse=True)

    print(f"\n=== Step 4: Inserting {len(insert_actions)} Subsections (Bottom-to-Top) ===")
    inserted_count = 0
    with tempfile.TemporaryDirectory(prefix="feishu_insert_") as idir:
        idir_p = Path(idir)
        for idx, sub_title, target_bid, anchor_kw in insert_actions:
            payload = {
                "index": idx,
                "children": [
                    {
                        "block_type": 5,
                        "heading3": {
                            "elements": [
                                {
                                    "text_run": {
                                        "content": sub_title
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
            json_name = f"insert_{idx}.json"
            json_file = idir_p / json_name
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)

            try:
                res = run_lark(
                    ["api", "POST", f"/open-apis/docx/v1/documents/{clean_id}/blocks/{clean_id}/children", "--data", f"@{json_name}"],
                    cwd=idir_p,
                )
                if isinstance(res, dict) and res.get("ok"):
                    inserted_count += 1
                    print(f"  [SUCCESS] Inserted H3 '{sub_title}' at index {idx} (before '{anchor_kw}')")
                else:
                    failed_count += 1
                    print(f"  [FAIL] Failed inserting '{sub_title}': {res}")
            except Exception as ex:
                failed_count += 1
                print(f"  [ERROR] {ex}")
            time.sleep(0.12)

    print("\n=== Step 5: Verification & Outline Fetch ===")
    outline_res = run_lark(["docs", "+fetch", "--doc", clean_id, "--scope", "outline"])
    outline_content = outline_res["data"]["document"]["content"]

    out_headings = re.findall(r"<(h[1-6]) id=\"([^\"]+)\"(?:[^>]*)>(.*?)</\1>", outline_content, re.S)
    print(f"\nFinal Outline Tree ({len(out_headings)} headings):")
    parsed_outline = []
    for lvl, hid, raw_title in out_headings:
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        indent = "  " * (int(lvl[1]) - 1)
        print(f"{indent}{lvl.upper()}: {title}")
        parsed_outline.append({"level": lvl, "id": hid, "title": title})

    return {
        "ok": failed_count == 0,
        "doc_id": clean_id,
        "converted_h4_count": converted_count,
        "inserted_h3_count": inserted_count,
        "failed_count": failed_count,
        "total_headings": len(parsed_outline),
        "outline": parsed_outline
    }

def main():
    parser = argparse.ArgumentParser(description="Synchronize and rebuild Feishu doc sidebar outline with H3 subsections.")
    parser.add_argument("--doc", required=True, help="Feishu Document ID/Token")
    parser.add_argument("--rules", help="Optional JSON file defining [(sub_title, anchor_keyword), ...]")
    args = parser.parse_args()
    
    rules = None
    if args.rules and Path(args.rules).is_file():
        rules = json.loads(Path(args.rules).read_text(encoding="utf-8"))
        
    res = sync_document_sidebar(args.doc, subsections_config=rules)
    print("\nResult Summary:")
    print(json.dumps({k: v for k, v in res.items() if k != "outline"}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
