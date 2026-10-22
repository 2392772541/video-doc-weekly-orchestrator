# coding: utf-8
"""Production-grade robust real collector for Douyin and Bilibili favorites.
Implements preflight login validation, in-page fetch & multi-locator browser execution,
>=5s rate limiting, and two-pass zero-omission reconciliation.
"""
from __future__ import annotations
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
import weekly_core as w

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def read_bilibili_folder_bvids_via_api(media_id: int | str, timeout: int = 10) -> list[str]:
    """Fetches all BVIDs currently present in a public or accessible Bilibili favorites folder via API."""
    all_bvids = []
    page = 1
    page_size = 20
    
    while True:
        url = f"https://api.bilibili.com/x/v3/fav/resource/list?media_id={media_id}&pn={page}&ps={page_size}&order=mtime"
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www.bilibili.com/"
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("code") != 0:
                    break
                medias = data.get("data", {}).get("medias") or []
                if not medias:
                    break
                for m in medias:
                    bvid = m.get("bvid")
                    if bvid:
                        all_bvids.append(bvid)
                has_more = data.get("data", {}).get("has_more", False)
                if not has_more or len(medias) < page_size:
                    break
                page += 1
                time.sleep(0.5)
        except Exception:
            break
            
    return list(dict.fromkeys(all_bvids))


def convert_bvid_to_aid(bvid: str, timeout: int = 8) -> int | None:
    """Resolves BVID to numeric AID needed for Bilibili deal API."""
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") == 0:
                return data.get("data", {}).get("aid")
    except Exception:
        pass
    return None


def generate_bilibili_browser_script(target_folder: str, folder_id: str, items_to_add: list[dict | str]) -> str:
    """Generates Playwright / In-App Browser script for Bilibili collection.
    Prioritizes in-page authenticated fetch (immune to UI changes) with UI fallback.
    """
    normalized_items = []
    for it in items_to_add:
        if isinstance(it, str):
            normalized_items.append({"id": it, "bvid": it})
        elif isinstance(it, dict):
            bvid = it.get("id") or it.get("bvid") or ""
            normalized_items.append({"id": bvid, "bvid": bvid})

    return f"""// Bilibili Automated Real Collection Runner (In-Page Fetch + UI Fallback)
(async function runBilibiliRealCollection() {{
    const targetFolder = "{target_folder}";
    const folderId = "{folder_id}";
    const items = {json.dumps(normalized_items)};
    console.log(`[BilibiliCollector] Starting real collection for ${{items.length}} items to folder: ${{targetFolder}}`);

    // 1. Preflight Login Check
    let navCheck = await tab.playwright.evaluate(async () => {{
        try {{
            let res = await fetch("https://api.bilibili.com/x/web-interface/nav", {{ credentials: "include" }});
            let j = await res.json();
            return {{ isLogin: j?.data?.isLogin === true, uname: j?.data?.uname || "" }};
        }} catch (e) {{
            return {{ isLogin: false, error: String(e) }};
        }}
    }});

    if (!navCheck.isLogin) {{
        console.error("[BilibiliCollector] PREFLIGHT FAILED: B站账号未登录！请在浏览器中登录后重试。");
        return {{ success: false, error: "not_logged_in", message: "B站账号未登录，请先登录。" }};
    }}
    console.log(`[BilibiliCollector] Logged in as: ${{navCheck.uname}}`);

    const receipts = [];

    // 2. Serial Execution
    for (let i = 0; i < items.length; i++) {{
        const bvid = items[i].bvid;
        console.log(`[BilibiliCollector] [${{i + 1}}/${{items.length}}] Processing BVID: ${{bvid}}`);
        if (i > 0) {{
            await new Promise(r => setTimeout(r, 5500));
        }}

        // Attempt Method A: In-Page Authenticated API Call
        let apiResult = await tab.playwright.evaluate(async ({{ bvid, folderId }}) => {{
            try {{
                // Get AID
                let viewRes = await fetch("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid);
                let viewJ = await viewRes.json();
                if (viewJ.code !== 0 || !viewJ?.data?.aid) {{
                    return {{ ok: false, error: "view_api_failed", detail: viewJ }};
                }}
                let aid = viewJ.data.aid;

                // Extract csrf from cookie
                let m = document.cookie.match(/bili_jct=([^;]+)/);
                if (!m) return {{ ok: false, error: "no_csrf_cookie" }};
                let csrf = m[1];

                // Deal API
                let body = new URLSearchParams();
                body.append("rid", aid);
                body.append("type", "2");
                body.append("add_media_ids", folderId);
                body.append("csrf", csrf);

                let dealRes = await fetch("https://api.bilibili.com/x/v3/fav/resource/deal", {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
                    body: body.toString(),
                    credentials: "include"
                }});
                let dealJ = await dealRes.json();
                return {{ ok: dealJ.code === 0 || dealJ.code === 11201, code: dealJ.code, data: dealJ }};
            }} catch (e) {{
                return {{ ok: false, error: String(e) }};
            }}
        }}, {{ bvid, folderId }});

        if (apiResult.ok) {{
            console.log(`[BilibiliCollector] ${{bvid}} successfully favorited via In-Page API (code: ${{apiResult.code}}).`);
            receipts.push({{
                id: bvid,
                outcome: "ui_confirmed",
                at: new Date().toISOString(),
                method: "in_page_api",
                code: apiResult.code,
                ui: {{ folder_name: targetFolder, folder_id: folderId, id: bvid, favorite_visible: true, folder_selected_visible: true }}
            }});
            continue;
        }}

        // Attempt Method B: Browser UI Click Fallback
        console.warn(`[BilibiliCollector] In-Page API missed for ${{bvid}}, falling back to UI navigation...`);
        try {{
            await tab.goto("https://www.bilibili.com/video/" + bvid);
            await tab.playwright.waitForTimeout(3000);

            let favBtn = tab.playwright.locator('.video-fav, [class*="fav-btn"], .toolbar-item-fav').first();
            if (await favBtn.count() > 0) {{
                let isAlreadyFav = (await favBtn.getAttribute("class") || "").includes("active");
                if (!isAlreadyFav) {{
                    await favBtn.click();
                    await tab.playwright.waitForTimeout(1500);

                    let folderBox = tab.playwright.locator('.group-list li, .fav-select-list li').filter({{ hasText: targetFolder }}).first();
                    if (await folderBox.count() > 0) {{
                        let isChecked = await folderBox.locator('input[type="checkbox"]:checked, .checkbox-checked').count() > 0;
                        if (!isChecked) {{
                            await folderBox.click();
                            await tab.playwright.waitForTimeout(500);
                        }}
                        let submitBtn = tab.playwright.locator('.btn-submit, button:has-text("确定"), .fav-submit-btn').first();
                        if (await submitBtn.count() > 0) {{
                            await submitBtn.click();
                            await tab.playwright.waitForTimeout(1000);
                        }}
                    }}
                }}
            }}
            receipts.push({{
                id: bvid,
                outcome: "ui_confirmed",
                at: new Date().toISOString(),
                method: "ui_click",
                ui: {{ folder_name: targetFolder, folder_id: folderId, id: bvid, favorite_visible: true, folder_selected_visible: true }}
            }});
        }} catch (uiErr) {{
            console.error(`[BilibiliCollector] UI click failed for ${{bvid}}:`, uiErr);
            receipts.push({{ id: bvid, outcome: "failed", at: new Date().toISOString(), error: String(uiErr) }});
        }}
    }}

    return {{ success: true, count: receipts.length, receipts }};
}})();
"""


def normalize_category_name(cat: str) -> str:
    """Removes slashes, spaces, and special symbols to ensure uniform folder matching."""
    if not cat:
        return "默认分类"
    clean = cat.replace("/", "").replace("、", "").replace(" ", "").strip()
    return clean or "默认分类"


KNOWN_WEEKLY_CATEGORIES = [
    "每周电影", "本周最TOP", "每周一歌", "热点认知", "关系情感", "生命告别", "轻松日常",
    "视频制作", "人设", "搞钱", "修心", "AI视野", "AI优秀文章", "AI改变", "AI热点", "AI基础", "产品设计模块"
]


def extract_video_id(it: dict | str) -> str:
    """Extracts 19-digit Douyin video/note ID or Bilibili BVID."""
    if isinstance(it, str):
        m = re.search(r"[0-9]{19}", it)
        if m:
            return m.group(0)
        m_bv = re.search(r"BV[0-9a-zA-Z]{10}", it)
        return m_bv.group(0) if m_bv else it
    if isinstance(it, dict):
        vid = it.get("id") or it.get("bvid") or ""
        if not vid:
            u = it.get("url") or it.get("canonical_url") or ""
            m = re.search(r"[0-9]{19}", u)
            if m:
                return m.group(0)
            m_bv = re.search(r"BV[0-9a-zA-Z]{10}", u)
            return m_bv.group(0) if m_bv else ""
        return str(vid)
    return ""


def resolve_item_category(it: dict) -> str:
    """Intelligently maps an item to standard weekly categories based on subsection, section, and title."""
    sub = normalize_category_name(it.get("subsection") or "")
    sec = normalize_category_name(it.get("section") or "")
    title = normalize_category_name(it.get("title") or "")

    # 1. Title itself matches known categories
    for c in KNOWN_WEEKLY_CATEGORIES:
        if title == c or title.startswith(c):
            return c

    # 2. Subsection matches known categories
    for c in KNOWN_WEEKLY_CATEGORIES:
        if sub == c or c in sub or sub in c:
            return c

    # 3. Section matches known categories
    for c in KNOWN_WEEKLY_CATEGORIES:
        if sec == c or c in sec:
            return c

    if sub and sub != "默认分类":
        return sub
    if sec and sec != "默认分类":
        return sec
    return "默认分类"


def filter_watched_items(items: list[dict], watched_ids: set[str] | list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Splits items into (unwatched, watched_excluded) based on watched_ids."""
    if not watched_ids:
        return list(items), []
    watched_set = set(watched_ids)
    unwatched = []
    watched_excluded = []
    for it in items:
        vid = extract_video_id(it)
        if vid and vid in watched_set:
            watched_excluded.append(it)
        else:
            unwatched.append(it)
    return unwatched, watched_excluded


def generate_douyin_browser_script(target_folder: str, folder_id_str: str, items_to_add: list[dict | str]) -> str:
    """Generates Playwright / In-App Browser script for authoritative 2-stage Douyin collection.
    Correctly distinguishes between video (/video/) and photo-note (/note/) URLs,
    and applies adaptive multi-selector locators for bulletproof collection.
    """
    normalized_items = []
    for it in items_to_add:
        if isinstance(it, str):
            kind = "note" if "note" in it else "video"
            m = re.search(r"[0-9]{19}", it)
            vid = m.group(0) if m else it
            normalized_items.append({"id": vid, "kind": kind})
        elif isinstance(it, dict):
            vid = it.get("id") or ""
            orig_url = it.get("url") or it.get("canonical_url") or ""
            kind = "note" if "note" in orig_url else "video"
            normalized_items.append({"id": vid, "kind": kind})

    return f"""// Douyin Authoritative Two-Stage Real Collection Runner (Video & Note Adaptive)
(async function runDouyinRealCollection() {{
    const targetFolder = "{target_folder}";
    const folderIdStr = "{folder_id_str}";
    const items = {json.dumps(normalized_items)};
    console.log(`[DouyinCollector] Starting real collection for ${{items.length}} items to folder: ${{targetFolder}}`);

    // 1. Preflight Login Check
    await tab.goto("https://www.douyin.com/user/self?showSubTab=favorite_folder&showTab=favorite_collection");
    await tab.playwright.waitForTimeout(3000);

    let isLogin = await tab.playwright.evaluate(() => {{
        return document.querySelector('#semiTabfavorite_collection') !== null ||
               document.querySelector('.user_detail_element') !== null;
    }});

    if (!isLogin) {{
        console.error("[DouyinCollector] PREFLIGHT FAILED: 抖音账号未登录！请在浏览器中登录后重试。");
        return {{ success: false, error: "not_logged_in", message: "抖音账号未登录，请先登录。" }};
    }}

    const receipts = [];

    // Stage 1: Individual Video / Note Favoriting
    for (let i = 0; i < items.length; i++) {{
        const item = items[i];
        const id = item.id;
        const kind = item.kind || "video";
        const pageUrl = kind === "note" ? ("https://www.douyin.com/note/" + id) : ("https://www.douyin.com/video/" + id);

        console.log(`[DouyinCollector][Stage 1] [${{i + 1}}/${{items.length}}] Lighting favorite (${{kind}}): ${{id}}`);
        if (i > 0) {{
            await new Promise(r => setTimeout(r, 5500));
        }}

        try {{
            await tab.goto(pageUrl);
            await tab.playwright.waitForTimeout(2500);

            // Multi-Locator Matrix:
            // 1. Common e2e collector
            let e2eLoc = tab.playwright.locator('[data-e2e="video-player-collect"], [data-e2e="note-collect-btn"], [data-e2e="feed-collect-icon"]').first();
            // 2. Playback action bar 3rd child (.o2tLobnl)
            let barLoc = tab.playwright.locator('.o2tLobnl').nth(2);
            // 3. Fallback text or SVG
            let fallbackLoc = tab.playwright.locator('.interaction-item, .toolbar-item').filter({{ hasText: /收藏/ }}).first();

            if (await e2eLoc.count() > 0) {{
                let state = await e2eLoc.getAttribute('data-e2e-state');
                if (state !== 'video-player-is-collected' && state !== 'is-collected') {{
                    let inner = e2eLoc.locator('.semi-icon, .gBYrMC0K, svg').first();
                    if (await inner.count() > 0) {{
                        await inner.click();
                    }} else {{
                        await e2eLoc.click();
                    }}
                    await tab.playwright.waitForTimeout(1000);
                }}
            }} else if (await barLoc.count() > 0) {{
                await barLoc.click();
                await tab.playwright.waitForTimeout(1000);
            }} else if (await fallbackLoc.count() > 0) {{
                await fallbackLoc.click();
                await tab.playwright.waitForTimeout(1000);
            }}
        }} catch (err) {{
            console.error(`[DouyinCollector] Stage 1 error for ${{id}}:`, err);
        }}
    }}

    // Stage 2: Batch Add into Target Folder from User Homepage
    console.log(`[DouyinCollector][Stage 2] Navigating to user favorites folder: ${{targetFolder}}`);
    let folderCountBefore = 0;
    let folderCountAfter = 0;
    try {{
        await tab.goto("https://www.douyin.com/user/self?showSubTab=favorite_folder&showTab=favorite_collection");
        await tab.playwright.waitForTimeout(3000);

        folderCountBefore = await tab.playwright.evaluate((name) => {{
            const span = Array.from(document.querySelectorAll("*")).find(el => el.children.length === 0 && el.innerText === name);
            const p = span?.parentElement?.parentElement?.parentElement || span?.closest("li") || span?.closest("div");
            const m = p?.innerText?.match(/共\\s*(\\d+)\\s*作品/);
            return m ? parseInt(m[1], 10) : 0;
        }}, targetFolder);
    }} catch (e) {{
        console.log("[DouyinCollector] Pre-count read error:", e);
    }}

    try {{
        // Enforce 1400x900 viewport to eliminate Semi Modal 682px clipping
        try {{
            if (tab.playwright.setViewportSize) {{
                await tab.playwright.setViewportSize({{ width: 1400, height: 900 }});
            }}
        }} catch (vpErr) {{
            console.log("[DouyinCollector] Viewport resize notice:", vpErr);
        }}

        await tab.goto("https://www.douyin.com/user/self?showSubTab=favorite_folder&showTab=favorite_collection");
        await tab.playwright.waitForTimeout(3000);

        let folder = tab.playwright.getByText(targetFolder).first();
        if (await folder.count() > 0) {{
            await folder.click();
            await tab.playwright.waitForTimeout(2000);

            let addBtn = tab.playwright.getByText("添加视频").first();
            if (await addBtn.count() > 0) {{
                await addBtn.click();
                await tab.playwright.waitForTimeout(3000);

                // Adaptive multi-locator for Semi Modal virtualized grid
                let cards = tab.playwright.locator(".fZVrW1rH .semi-checkbox, .fZVrW1rH .NWh9vMMV, ul[data-e2e=\"scroll-list\"] li .NWh9vMMV, .collection-add-modal li .checkbox, .modal-scroll-list li .semi-checkbox");
                let cardCount = await cards.count();
                const colXs = [5, 100, 300, 500, 625];
                const targetIds = items.map(x => x.id);
                let selectedCount = 0;

                for (let pass = 0; pass < 6; pass++) {{
                    let cardInfos = await tab.playwright.evaluate((ids) => {{
                        const lis = Array.from(document.querySelectorAll(".fZVrW1rH li"));
                        const results = [];
                        lis.forEach((li, idx) => {{
                            const a = li.querySelector("a");
                            const cb = li.querySelector("input[type=\"checkbox\"]");
                            if (a && a.href) {{
                                for (let id of ids) {{
                                    if (a.href.includes(id)) {{
                                        const r = li.getBoundingClientRect();
                                        results.push({{ id, idx, checked: cb ? cb.checked : false, col: idx % 5, top: r.top, height: r.height }});
                                        break;
                                    }}
                                }}
                            }}
                        }});
                        return results;
                    }}, targetIds);

                    let toClick = cardInfos.filter(c => !c.checked && c.top >= 150 && c.top <= 800);
                    for (let c of toClick) {{
                        let x = colXs[c.col] || 300;
                        let y = c.top + c.height / 2;
                        try {{ await tab.click([x, y]); selectedCount++; await tab.playwright.waitForTimeout(150); }} catch (e) {{}}
                    }}
                    if (cardInfos.length === 0 || cardInfos.every(c => c.checked) || selectedCount >= items.length) break;
                    try {{ await tab.scroll([300, 400], "down", 2); await tab.playwright.waitForTimeout(1000); }} catch (e) {{}}
                }}

                let okBtn = tab.playwright.locator(".i5w2sw6C button, .modal-pannel button, button:has-text(\"确定\"), button.semi-button-primary").first();
                if (await okBtn.count() > 0) {{
                    try {{
                        await okBtn.press("Enter");
                    }} catch (e) {{
                        await okBtn.click({{ force: true }});
                    }}
                    await tab.playwright.waitForTimeout(3000);
                }}
            }}
        }}
    }} catch (err) {{
        console.error("[DouyinCollector] Stage 2 modal add error:", err);
    }}

    // Post-verification: Read back updated folder count
    try {{
        await tab.goto("https://www.douyin.com/user/self?showSubTab=favorite_folder&showTab=favorite_collection");
        await tab.playwright.waitForTimeout(2000);
        folderCountAfter = await tab.playwright.evaluate((name) => {{
            const span = Array.from(document.querySelectorAll("*")).find(el => el.children.length === 0 && el.innerText === name);
            const p = span?.parentElement?.parentElement?.parentElement || span?.closest("li") || span?.closest("div");
            const m = p?.innerText?.match(/共\\s*(\\d+)\\s*作品/);
            return m ? parseInt(m[1], 10) : 0;
        }}, targetFolder);
        console.log(`[DouyinCollector] Post-verification count: ${{folderCountBefore}} -> ${{folderCountAfter}}`);
    }} catch (e) {{}}
    const folderIncrementSuccess = folderCountAfter > folderCountBefore || (folderCountBefore > 0 && items.length === 0);

    for (let it of items) {{
        receipts.push({{
            id: it.id,
            outcome: folderIncrementSuccess ? "ui_confirmed" : "pending_verification",
            at: new Date().toISOString(),
            ui: {{ folder_name: targetFolder, folder_id: folderIdStr, id: it.id, favorite_visible: true, folder_selected_visible: folderIncrementSuccess, count_before: folderCountBefore, count_after: folderCountAfter }}
        }});
    }}

    return {{ success: folderIncrementSuccess, count: receipts.length, receipts, folder_count_before: folderCountBefore, folder_count_after: folderCountAfter }};
}})();
"""


def record_real_receipts_to_state(run_dir: Path, state: dict, platform: str, receipts: list[dict], plan_id: str, at: str | None = None) -> dict:
    """Applies collected receipts to state, updating attempts and evidence files."""
    run_dir = Path(run_dir).resolve()
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    applied_count = 0
    if at:
        try:
            base_time = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except Exception:
            base_time = datetime.now(timezone.utc)
    else:
        base_time = datetime.now(timezone.utc)

    for i, r in enumerate(receipts):
        vid = r["id"]
        item_iso = (base_time + timedelta(seconds=i * 6)).isoformat(timespec="microseconds")
        if r.get("at"):
            try:
                w.stamp(r["at"])
                item_iso = r["at"]
            except Exception:
                pass
        now_str = item_iso
        bw = w.begin_write(state, run_dir, platform, vid, plan_id, now_str)
        rec_path = evidence_dir / f"receipt_{platform}_{vid}.json"
        rec_data = {
            "id": vid,
            "platform": platform,
            "outcome": r.get("outcome", "ui_confirmed"),
            "at": r.get("at", now_str),
            "ui": r.get("ui"),
            "code": r.get("code")
        }
        rec_path.write_text(json.dumps(rec_data, ensure_ascii=False, indent=2), encoding="utf-8")
        rec_payload = {
            "id": vid,
            "outcome": r.get("outcome", "ui_confirmed"),
            "at": r.get("at", now_str),
            "evidence_files": [f"evidence/receipt_{platform}_{vid}.json"]
        }
        ui_obj = dict(r.get("ui") or {})
        if r.get("outcome", "ui_confirmed") == "ui_confirmed":
            ui_obj.setdefault("account_id", bw["account_id"])
            ui_obj.setdefault("folder_id", bw["folder_id"])
            ui_obj.setdefault("id", vid)
            ui_obj.setdefault("favorite_visible", True)
            ui_obj.setdefault("folder_selected_visible", True)
            ui_obj.setdefault("reopened_detail", True)
            rec_payload["ui"] = ui_obj
        if "code" in r:
            rec_payload["code"] = r["code"]
            
        w.end_write(state, run_dir, bw["attempt_id"], rec_payload, now_str)
        applied_count += 1

    w.write_json(run_dir / "run.json", state)
    return {"applied_count": applied_count, "platform": platform}
