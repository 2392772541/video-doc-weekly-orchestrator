# coding: utf-8
import subprocess
import json
import re
import time
from pathlib import Path

import argparse
import tempfile

DEFAULT_SUBSECTIONS = [
    # 游戏人生，说地谈天
    ("热点/认知", "3. 知行合一"),
    ("关系/情感", "15. 人是社会关系的总和"),
    ("生命/告别", "20. 当我们开始告别时"),
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

def run_cmd(cmd: str):
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\nOutput: {res.stdout}\nError: {res.stderr}")
    try:
        return json.loads(res.stdout)
    except Exception:
        return res.stdout

def get_current_state(doc_id: str):
    cmd = f"lark-cli docs +fetch --doc {doc_id} --detail with-ids"
    data = run_cmd(cmd)
    xml = data["data"]["document"]["content"]
    rev = data["data"]["document"]["revision_id"]
    return xml, rev

def sync_document_sidebar(doc_id: str, subsections_config: list = None, temp_dir: Path = None) -> dict:
    """Rebuilds Feishu document outline sidebar by:
    1. Converting item H3s under target major sections to H4
    2. Inserting authoritative H3 subsection headings before specific anchor items
    3. Verifying the resulting outline hierarchy
    """
    print(f"=== Step 1: Analyze Document Headings for {doc_id} ===")
    xml, rev = get_current_state(doc_id)
    headings = re.findall(r"<(h[23]) id=\"([^\"]+)\"(?:[^>]*)>(.*?)</\1>", xml)
    
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
    with tempfile.TemporaryDirectory(prefix="feishu_sidebar_") as tdir:
        tdir_p = Path(tdir)
        wrap_file = tdir_p / "wrap.xml"
        converted_count = 0
        for i, (sec, bid, content) in enumerate(items_to_convert, 1):
            h4_content = f"<h4>{content}</h4>"
            wrap_file.write_text(h4_content, encoding="utf-8")
            update_cmd = f"lark-cli docs +update --doc {doc_id} --command block_replace --block-id {bid} --content @\"{wrap_file}\""
            try:
                res = run_cmd(update_cmd)
                if res.get("ok"):
                    converted_count += 1
                    clean_title = re.sub(r"<[^>]+>", "", content)[:30]
                    print(f"  [{i:02d}/{len(items_to_convert)}] Converted -> H4: {clean_title}")
                else:
                    print(f"  [FAIL] {i}/{len(items_to_convert)} {bid}: {res}")
            except Exception as ex:
                print(f"  [ERROR] {bid}: {ex}")
            time.sleep(0.08)
    
    print("\n=== Step 3: Fetch Fresh Children and XML to Insert Subsections ===")
    root_cmd = f"lark-cli api GET /open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}"
    root_data = run_cmd(root_cmd)
    children = root_data["data"]["block"]["children"]
    
    xml_fresh, _ = get_current_state(doc_id)
    fresh_h4 = re.findall(r"<(h[2-4]) id=\"([^\"]+)\"(?:[^>]*)>(.*?)</\1>", xml_fresh)
    
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
            json_file = idir_p / f"insert_{idx}.json"
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                
            insert_cmd = f'lark-cli api POST /open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children --data @"{json_file}"'
            try:
                res = run_cmd(insert_cmd)
                if res.get("ok"):
                    inserted_count += 1
                    print(f"  [SUCCESS] Inserted H3 '{sub_title}' at index {idx} (before '{anchor_kw}')")
                else:
                    print(f"  [FAIL] Failed inserting '{sub_title}': {res}")
            except Exception as ex:
                print(f"  [ERROR] {ex}")
            time.sleep(0.12)
        
    print("\n=== Step 5: Verification & Outline Fetch ===")
    outline_cmd = f"lark-cli docs +fetch --doc {doc_id} --scope outline"
    outline_res = run_cmd(outline_cmd)
    outline_content = outline_res["data"]["document"]["content"]
    
    out_headings = re.findall(r"<(h[1-6]) id=\"([^\"]+)\">([^<]*)</\1>", outline_content)
    print(f"\nFinal Outline Tree ({len(out_headings)} headings):")
    for lvl, hid, title in out_headings:
        indent = "  " * (int(lvl[1]) - 1)
        print(f"{indent}{lvl.upper()}: {title}")
        
    return {
        "ok": True,
        "doc_id": doc_id,
        "converted_h4_count": converted_count,
        "inserted_h3_count": inserted_count,
        "total_headings": len(out_headings),
        "outline": [{"level": lvl, "id": hid, "title": title} for lvl, hid, title in out_headings]
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
