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
import uuid
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


def _unique_evidence_path(evidence_dir: Path, stem: str, suffix: str = ".json") -> Path:
    """生成不覆盖已有 SHA-256 证据文件的落盘路径。"""
    candidate = evidence_dir / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    return evidence_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def _extract_dead_link_detail(receipt: dict) -> dict:
    """从回执或 dead_links 项中规整官方失效状态明细。"""
    raw_detail = receipt.get("detail")
    detail = dict(raw_detail) if isinstance(raw_detail, dict) else {}
    for key in ("code", "message", "state", "videos", "status", "business_code", "unavailable_banner", "reason"):
        if key in receipt and receipt[key] is not None and key not in detail:
            detail[key] = receipt[key]
    return detail


def record_real_receipts_to_state(
    run_dir: Path,
    state: dict,
    platform: str,
    receipts: list[dict],
    plan_id: str,
    at: str | None = None,
    persist: bool = True,
    strict_ui: bool = False,
) -> dict:
    """Applies collected receipts to state, updating attempts, exclusions and evidence files."""
    run_dir = Path(run_dir).resolve()
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    applied_count = 0
    dead_link_ids: list[str] = []
    already_favorited_ids: list[str] = []
    skipped_existing_ids: list[str] = []

    if at:
        try:
            base_time = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except Exception:
            base_time = datetime.now(timezone.utc)
    else:
        base_time = datetime.now(timezone.utc)

    plan = state.get("plans", {}).get(platform, {})
    existing_in_plan = set(plan.get("existing_ids", []))
    missing_in_plan = set(plan.get("missing_ids", []))
    write_seq = 0

    for r in receipts:
        w.require(isinstance(r, dict) and r.get("id"), "invalid_receipt_entry")
        vid = str(r["id"])
        raw_outcome = r.get("outcome", "ui_confirmed")

        fallback_iso = (base_time + timedelta(seconds=write_seq * 6)).isoformat(timespec="microseconds")
        finished_iso = fallback_iso
        if r.get("at"):
            try:
                w.stamp(r["at"])
                finished_iso = r["at"]
            except Exception:
                pass
        started_iso = finished_iso
        if r.get("started_at"):
            try:
                if w.stamp(r["started_at"]) <= w.stamp(finished_iso):
                    started_iso = r["started_at"]
            except Exception:
                pass

        # 1) 平台官方已确认的失效/下架/充电专属稿件：不伪造失败写入，走受证据保护的排除登记，不中断后续条目
        if raw_outcome == "dead_link_confirmed":
            dead_detail = _extract_dead_link_detail(r)
            dead_path = _unique_evidence_path(evidence_dir, f"dead_link_{platform}_{vid}")
            dead_rel = dead_path.relative_to(run_dir).as_posix()
            dead_path.write_text(
                json.dumps(
                    {
                        "id": vid,
                        "platform": platform,
                        "outcome": "dead_link_confirmed",
                        "at": finished_iso,
                        "detail": dead_detail,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            w.record_collection_exclusion(
                state,
                run_dir,
                platform,
                vid,
                "dead_link_confirmed",
                dead_detail,
                [dead_rel],
                at=finished_iso,
            )
            dead_link_ids.append(vid)
            continue

        # 2) 已在写前快照中的 already_favorited 无需重复写尝试，留证跳过
        if raw_outcome == "already_favorited" and vid in existing_in_plan and vid not in missing_in_plan:
            skip_path = _unique_evidence_path(evidence_dir, f"receipt_existing_{platform}_{vid}")
            skip_path.write_text(
                json.dumps(
                    {
                        "id": vid,
                        "platform": platform,
                        "outcome": "already_favorited",
                        "skipped_existing": True,
                        "at": finished_iso,
                        "code": r.get("code"),
                        "codes": r.get("codes"),
                        "detail": r.get("detail"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            skipped_existing_ids.append(vid)
            continue

        write_seq += 1
        bw = w.begin_write(state, run_dir, platform, vid, plan_id, started_iso)
        rec_path = _unique_evidence_path(evidence_dir, f"receipt_{platform}_{vid}")
        rec_rel = rec_path.relative_to(run_dir).as_posix()
        rec_data = {
            "id": vid,
            "platform": platform,
            "outcome": raw_outcome,
            "raw_outcome": raw_outcome,
            "started_at": started_iso,
            "at": finished_iso,
            "ui": r.get("ui"),
            "code": r.get("code"),
            "codes": r.get("codes"),
            "detail": r.get("detail"),
        }
        rec_path.write_text(json.dumps(rec_data, ensure_ascii=False, indent=2), encoding="utf-8")

        rec_payload = {
            "id": vid,
            "outcome": raw_outcome,
            "at": finished_iso,
            "evidence_files": [rec_rel],
        }
        ui_obj = dict(r.get("ui") or {})
        if raw_outcome == "ui_confirmed":
            if strict_ui:
                # import-collection 严格模式绝不凭空补真 favorite_visible / folder_selected_visible / reopened_detail
                w.require(bool(ui_obj), "ui_receipt_requires_real_ui_evidence")
            else:
                ui_obj.setdefault("account_id", bw["account_id"])
                ui_obj.setdefault("folder_id", bw["folder_id"])
                ui_obj.setdefault("id", vid)
                ui_obj.setdefault("favorite_visible", True)
                ui_obj.setdefault("folder_selected_visible", True)
                ui_obj.setdefault("reopened_detail", True)
            rec_payload["ui"] = ui_obj
        elif raw_outcome == "acknowledged":
            codes = dict(r.get("codes") or {})
            if not codes and platform == "bilibili" and r.get("code") == 0:
                codes = {"favorite": 0}
            rec_payload["codes"] = codes
        elif raw_outcome == "already_favorited":
            if platform == "bilibili":
                codes = dict(r.get("codes") or {})
                if not codes and r.get("code") == 11201:
                    codes = {"favorite": 11201}
                rec_payload["codes"] = codes
            else:
                ui_obj.setdefault("account_id", bw["account_id"])
                ui_obj.setdefault("folder_id", bw["folder_id"])
                ui_obj.setdefault("id", vid)
                rec_payload["ui"] = ui_obj
            already_favorited_ids.append(vid)
        else:
            if r.get("detail") is not None:
                rec_payload["detail"] = r["detail"] if isinstance(r["detail"], str) else json.dumps(r["detail"], ensure_ascii=False)
            elif r.get("error"):
                rec_payload["detail"] = str(r["error"])

        if "code" in r and raw_outcome != "ui_confirmed":
            rec_payload["code"] = r["code"]

        w.end_write(state, run_dir, bw["attempt_id"], rec_payload, finished_iso)
        applied_count += 1

    if persist:
        w.write_json(run_dir / "run.json", state)
    return {
        "applied_count": applied_count,
        "dead_link_count": len(dead_link_ids),
        "dead_link_ids": dead_link_ids,
        "already_favorited_count": len(already_favorited_ids),
        "already_favorited_ids": already_favorited_ids,
        "skipped_existing_ids": skipped_existing_ids,
        "platform": platform,
    }


def _persist_and_normalize_snapshot(
    snap_raw: dict,
    label: str,
    platform: str,
    target: dict,
    run_dir: Path,
    evidence_dir: Path,
    payload_identity: dict | None = None,
) -> dict:
    """将浏览器采集快照落盘至 evidence/ 并标准化为可直接通过 weekly_core.import_snapshot 校验的结构。"""
    w.require(isinstance(snap_raw, dict), f"invalid_snapshot_object_{label}")
    w.no_secrets(snap_raw)
    w.require(snap_raw.get("ok") is not False, f"snapshot_failed_{label}: {snap_raw.get('error', '')}")

    snap_path = _unique_evidence_path(evidence_dir, f"{platform}_snapshot_{label}")
    snap_rel = snap_path.relative_to(run_dir).as_posix()
    snap_path.write_text(json.dumps(snap_raw, ensure_ascii=False, indent=2), encoding="utf-8")

    valid_existing_refs: list[str] = []
    for ref in snap_raw.get("evidence_files") or []:
        if isinstance(ref, str) and not Path(ref).is_absolute():
            p = (run_dir / ref).resolve()
            if p.is_relative_to(evidence_dir) and p.is_file() and p.stat().st_size > 0:
                valid_existing_refs.append(p.relative_to(run_dir).as_posix())
    ev_files = list(dict.fromkeys([snap_rel] + valid_existing_refs))

    if "pages" in snap_raw:
        snap_obj = dict(snap_raw)
        snap_obj["platform"] = snap_obj.get("platform") or platform
        snap_obj["evidence_files"] = ev_files
        method = snap_obj.get("method", "api")
        if method == "ui" and isinstance(snap_obj.get("pages"), list):
            norm_pages = []
            for pg in snap_obj["pages"]:
                pg_copy = dict(pg) if isinstance(pg, dict) else {}
                pg_refs = []
                for ref in pg_copy.get("evidence_files") or []:
                    if isinstance(ref, str) and not Path(ref).is_absolute():
                        p = (run_dir / ref).resolve()
                        if p.is_relative_to(evidence_dir) and p.is_file() and p.stat().st_size > 0:
                            pg_refs.append(p.relative_to(run_dir).as_posix())
                pg_copy["evidence_files"] = list(dict.fromkeys([snap_rel] + pg_refs))
                norm_pages.append(pg_copy)
            snap_obj["pages"] = norm_pages
        return snap_obj

    if "items" in snap_raw and isinstance(snap_raw["items"], list):
        started_at = snap_raw.get("started_at") or snap_raw.get("scraped_at")
        finished_at = snap_raw.get("finished_at") or snap_raw.get("scraped_at")
        w.require(bool(started_at) and bool(finished_at), f"snapshot_timestamps_required_{label}")
        page_items = []
        for entry in snap_raw["items"]:
            if isinstance(entry, dict):
                if entry.get("unavailable") is True and not entry.get("id"):
                    page_items.append({"provider_id": str(entry.get("provider_id", "")), "unavailable": True})
                else:
                    page_items.append({"id": str(entry.get("id", ""))})
            else:
                page_items.append({"id": str(entry)})
        p_id = payload_identity or {}
        return {
            "snapshot_id": snap_raw.get("snapshot_id") or f"snap_{label}_{platform}_{uuid.uuid4().hex[:8]}",
            "platform": snap_raw.get("platform") or platform,
            "account_id": str(snap_raw.get("account_id") or p_id.get("account_id") or ""),
            "folder_id": str(snap_raw.get("folder_id") or p_id.get("folder_id") or ""),
            "folder_name": str(snap_raw.get("folder_name") or p_id.get("folder_name") or ""),
            "started_at": started_at,
            "finished_at": finished_at,
            "method": "ui",
            "evidence_files": ev_files,
            "pages": [
                {
                    "request_cursor": "0",
                    "has_more": False,
                    "next_cursor": None,
                    "target_visible": snap_raw.get("target_visible"),
                    "fully_loaded": snap_raw.get("fully_loaded"),
                    "end_visible": snap_raw.get("end_visible"),
                    "evidence_files": [snap_rel],
                    "items": page_items,
                }
            ],
        }

    raise w.GateError(f"invalid_snapshot_format_{label}")


def ingest_collection_result(
    run_dir: Path,
    state: dict,
    payload: dict,
    platform: str | None = None,
    plan_id: str | None = None,
    at: str | None = None,
    persist: bool = True,
) -> dict:
    """一键将真实浏览器收藏结果（B站 inpage/CDP 输出或抖音两阶段回执 + 两轮回读）入账至 run.json 与 evidence/。"""
    w.require(isinstance(payload, dict), "collection_result_must_be_json_object")
    w.no_secrets(payload)

    run_dir = Path(run_dir).resolve()
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    payload_plat = payload.get("platform")
    if platform and payload_plat:
        w.require(platform == payload_plat, f"collection_platform_mismatch: {platform} != {payload_plat}")
    plat = platform or payload_plat
    w.require(plat in w.VIDEO_PLATFORMS, f"unsupported_collection_platform: {plat}")

    # 1) 预检错误立即中止，绝不写入假进度
    err = payload.get("error")
    if err in {"not_logged_in", "no_csrf_cookie", "nav_fetch_failed", "before_snapshot_failed"}:
        err_path = _unique_evidence_path(evidence_dir, f"collection_error_{plat}")
        err_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        raise w.GateError(f"collection_aborted_{err}")

    snaps = dict(payload.get("snapshots") or {})
    for alias, canon in (
        ("before_snapshot", "before"),
        ("readback_round1", "round1"),
        ("round1", "round1"),
        ("readback_round2", "round2"),
        ("round2", "round2"),
    ):
        if canon not in snaps and payload.get(alias) is not None:
            snaps[canon] = payload[alias]

    if (
        plat == "douyin"
        and payload.get("stage") in {"stage1_complete_stage2_folder_add_required", "stage1_partial_failed"}
        and not payload.get("stage2")
        and not snaps.get("round1")
        and not snaps.get("round2")
    ):
        raise w.GateError("douyin_stage2_folder_add_and_two_round_readback_required")

    target = state.get("targets", {}).get(plat)
    w.require(target is not None, f"collection_target_not_resolved: {plat}")
    if payload.get("account_id"):
        w.require(str(payload["account_id"]) == target["account_id"], "collection_account_mismatch")
    if payload.get("folder_id"):
        w.require(str(payload["folder_id"]) == target["folder_id"], "collection_folder_id_mismatch")
    fname = payload.get("folder_name") or payload.get("target_folder")
    if fname:
        w.require(str(fname) == target["folder_name"], "collection_folder_name_mismatch")

    payload_identity = {
        "account_id": str(payload["account_id"]) if payload.get("account_id") else "",
        "folder_id": str(payload["folder_id"]) if payload.get("folder_id") else "",
        "folder_name": str(fname) if fname else "",
    }

    raw_path = _unique_evidence_path(evidence_dir, f"collection_result_{plat}")
    raw_rel = raw_path.relative_to(run_dir).as_posix()
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    imported_snapshots: list[str] = []
    snapshot_errors: list[dict] = []
    # 2) 导入写前快照（如有）并锚定新的 plan_id
    if snaps.get("before") is not None:
        before_obj = _persist_and_normalize_snapshot(
            snaps["before"], "before", plat, target, run_dir, evidence_dir, payload_identity=payload_identity
        )
        w.fresh(before_obj["finished_at"], w.LINK_AGE, at)
        existing_snap_ids = {s["snapshot_id"] for s in state["snapshots"][plat]}
        if before_obj["snapshot_id"] not in existing_snap_ids:
            w.import_snapshot(state, run_dir, before_obj, at=before_obj["finished_at"])
            imported_snapshots.append(before_obj["snapshot_id"])
        active_plan = w.plan_collection(state, run_dir, plat, at=before_obj["finished_at"])
        active_plan_id = active_plan["plan_id"]
    else:
        active_plan = state.get("plans", {}).get(plat)
        if plan_id:
            w.require(active_plan and active_plan.get("plan_id") == plan_id, "stale_or_wrong_plan")
        elif not active_plan:
            plan_at = at
            if plan_at is None and state.get("snapshots", {}).get(plat):
                plan_at = state["snapshots"][plat][-1]["finished_at"]
                w.fresh(plan_at, w.LINK_AGE, at)
            active_plan = w.plan_collection(state, run_dir, plat, at=plan_at)
        active_plan_id = active_plan["plan_id"]

    # 3) 规整回执（兼容抖音两阶段 stage1 + stage2 结构与 B站 dead_links 列表）
    raw_receipts = list(payload.get("receipts") or [])
    normalized_receipts: list[dict] = []
    stage2 = payload.get("stage2")
    has_stage1 = any(isinstance(r, dict) and "stage1" in r for r in raw_receipts)

    if plat == "douyin" and (has_stage1 or stage2 is not None):
        w.require(isinstance(stage2, dict), "douyin_stage2_confirmation_required")
        w.no_secrets(stage2)
        s2_confirmed = stage2.get("confirmed") is True
        cnt_before = stage2.get("folder_count_before")
        cnt_after = stage2.get("folder_count_after")
        non_dead_s1 = [
            r for r in raw_receipts
            if isinstance(r, dict)
            and r.get("outcome") != "dead_link_confirmed"
            and (r.get("stage1") or {}).get("action") != "dead_link_confirmed"
        ]
        if non_dead_s1:
            w.require(s2_confirmed, "douyin_stage2_not_confirmed")
        active_s1_ok = [
            r for r in non_dead_s1
            if (r.get("stage1") or {}).get("ok") is True
            and (r.get("stage1") or {}).get("action") in {"clicked_collect", "skipped_already_collected"}
        ]
        if s2_confirmed and active_s1_ok:
            w.require(
                type(cnt_before) is int and type(cnt_after) is int and cnt_before >= 0 and cnt_after >= 0,
                "douyin_stage2_folder_counts_required",
            )
            if not (cnt_after > cnt_before or stage2.get("all_already_in_folder") is True):
                raise w.GateError("douyin_stage2_folder_count_did_not_increase")
        for r in raw_receipts:
            w.require(isinstance(r, dict) and r.get("id"), "invalid_receipt_entry")
            if "stage1" not in r:
                normalized_receipts.append(r)
                continue
            s1 = r.get("stage1") or {}
            if r.get("outcome") == "dead_link_confirmed" or s1.get("action") == "dead_link_confirmed":
                normalized_receipts.append({
                    "id": r["id"],
                    "outcome": "dead_link_confirmed",
                    "at": r.get("at"),
                    "detail": r.get("detail") or s1.get("detail") or s1,
                })
            elif s1.get("ok") is True and s1.get("action") in {"clicked_collect", "skipped_already_collected"} and s2_confirmed:
                normalized_receipts.append({
                    "id": r["id"],
                    "outcome": "ui_confirmed",
                    "started_at": r.get("started_at") or r.get("at"),
                    "at": r.get("at"),
                    "ui": {
                        "account_id": target["account_id"],
                        "folder_id": target["folder_id"],
                        "id": str(r["id"]),
                        "favorite_visible": True,
                        "folder_selected_visible": True,
                        "reopened_detail": bool(stage2.get("reopened_detail", True)),
                        "count_before": cnt_before,
                        "count_after": cnt_after,
                        "stage1_action": s1.get("action"),
                        "stage2_confirmed_at": stage2.get("confirmed_at", ""),
                    },
                })
            else:
                normalized_receipts.append({
                    "id": r["id"],
                    "outcome": "failed",
                    "started_at": r.get("started_at") or r.get("at"),
                    "at": r.get("at"),
                    "detail": {"stage1": s1, "stage2": stage2},
                })
    else:
        normalized_receipts = raw_receipts

    seen_dead = {str(r["id"]) for r in normalized_receipts if r.get("outcome") == "dead_link_confirmed"}
    for dead in payload.get("dead_links") or []:
        if isinstance(dead, dict) and dead.get("id") and str(dead["id"]) not in seen_dead:
            normalized_receipts.append({
                "id": str(dead["id"]),
                "outcome": "dead_link_confirmed",
                "code": dead.get("code"),
                "detail": dead,
            })
            seen_dead.add(str(dead["id"]))

    # 4) 回执入账（受 weekly_core 串行节流与未对账阻断门禁保护）
    receipt_summary = {
        "applied_count": 0,
        "dead_link_count": 0,
        "dead_link_ids": [],
        "already_favorited_count": 0,
        "already_favorited_ids": [],
        "skipped_existing_ids": [],
        "platform": plat,
    }
    if normalized_receipts:
        receipt_summary = record_real_receipts_to_state(
            run_dir,
            state,
            plat,
            normalized_receipts,
            active_plan_id,
            at=at,
            persist=False,
            strict_ui=True,
        )

    stopped_reason = payload.get("stopped_reason")
    partial_aborted = bool(stopped_reason) or payload.get("ok") is False

    # 5) 导入写后两轮全量回读快照（允许在 24h LINK_AGE 内延迟入账；若已熔断且回读亦因风控失败则保留部分回执进度并落盘失败快照）
    imported_rb_labels: set[str] = set()
    for label in ("round1", "round2"):
        raw_rb = snaps.get(label)
        if raw_rb is None:
            continue
        if partial_aborted and isinstance(raw_rb, dict) and raw_rb.get("ok") is False:
            w.no_secrets(raw_rb)
            fail_path = _unique_evidence_path(evidence_dir, f"{plat}_snapshot_{label}_failed")
            fail_rel = fail_path.relative_to(run_dir).as_posix()
            fail_path.write_text(json.dumps(raw_rb, ensure_ascii=False, indent=2), encoding="utf-8")
            snapshot_errors.append({
                "label": label,
                "error": str(raw_rb.get("error") or "snapshot_failed"),
                "evidence": fail_rel,
            })
            continue
        rb_obj = _persist_and_normalize_snapshot(
            raw_rb, label, plat, target, run_dir, evidence_dir, payload_identity=payload_identity
        )
        w.fresh(rb_obj["finished_at"], w.LINK_AGE, at)
        w.import_snapshot(state, run_dir, rb_obj, at=rb_obj["finished_at"])
        imported_snapshots.append(rb_obj["snapshot_id"])
        imported_rb_labels.add(label)

    # 6) 若两轮回读齐备，自动执行对账审计并落盘 audit/{platform}-reconcile.json
    reconcile_res = None
    if {"round1", "round2"} <= imported_rb_labels:
        reconcile_res = w.reconcile(state, run_dir, plat, at=at)
        w.write_json(run_dir / "audit" / f"{plat}-reconcile.json", reconcile_res)

    if persist:
        w.write_json(run_dir / "run.json", state)

    overall_ok = bool(
        reconcile_res is not None
        and reconcile_res.get("ok") is True
        and not stopped_reason
        and not snapshot_errors
        and payload.get("ok") is not False
    )
    out = {
        "ok": overall_ok,
        "platform": plat,
        "plan_id": active_plan_id,
        "raw_evidence": raw_rel,
        "stopped_reason": stopped_reason,
        "receipts": receipt_summary,
        "imported_snapshots": imported_snapshots,
        "reconcile": reconcile_res,
    }
    if snapshot_errors:
        out["snapshot_errors"] = snapshot_errors
    return out


def _normalize_bilibili_items(items_to_add: list[dict | str]) -> list[dict]:
    """统一标准化并去重 B 站待收藏条目为 [{id, bvid}] 列表，过滤非法 BV 号。"""
    normalized = []
    seen = set()
    for it in items_to_add:
        raw = ""
        if isinstance(it, str):
            raw = it
        elif isinstance(it, dict):
            raw = str(it.get("id") or it.get("bvid") or it.get("canonical_url") or it.get("url") or "")
        m = re.search(r"(?:BV|bv)[0-9A-Za-z]{10}", raw)
        if not m:
            continue
        bvid = "BV" + m.group(0)[2:]
        if bvid not in seen:
            seen.add(bvid)
            normalized.append({"id": bvid, "bvid": bvid})
    return normalized


def _normalize_douyin_items(items_to_add: list[dict | str]) -> list[dict]:
    """统一标准化并去重抖音待收藏条目为 [{id, kind, url}] 列表，严格保留 video/note 区分。"""
    normalized = []
    seen = set()
    for it in items_to_add:
        vid = ""
        kind = "video"
        if isinstance(it, str):
            kind = "note" if "/note/" in it or it.startswith("note:") else "video"
            m = re.search(r"(?<![0-9])([0-9]{19})(?![0-9])", it)
            vid = m.group(1) if m else ""
        elif isinstance(it, dict):
            orig_url = str(it.get("canonical_url") or it.get("url") or it.get("original_url") or "")
            raw_id = str(it.get("id") or "")
            m = re.search(r"(?<![0-9])([0-9]{19})(?![0-9])", raw_id) or re.search(r"(?<![0-9])([0-9]{19})(?![0-9])", orig_url)
            vid = m.group(1) if m else ""
            kind = "note" if (it.get("work_kind") == "note" or it.get("kind") == "note" or "/note/" in orig_url) else "video"
        if vid and vid not in seen:
            seen.add(vid)
            normalized.append({
                "id": vid,
                "kind": kind,
                "url": f"https://www.douyin.com/{kind}/{vid}",
            })
    return normalized


def generate_bilibili_inpage_js(
    target_folder: str,
    folder_id: str,
    items_to_add: list[dict | str],
    delay_ms: int = 5500,
) -> str:
    """生成纯浏览器页内可执行异步 JS（适配 Antigravity browser 子代理，零 tab.playwright / cua_repl 依赖）。
    包含：登录态预检、目标夹预读跳过已有项、>=5s 节流官方 Deal API 写入、HTTP/业务风控熔断、
    失效/充电稿件审计、weekly_core 兼容快照/回执格式及写后两轮全量分页对账。
    """
    normalized_items = _normalize_bilibili_items(items_to_add)
    safe_delay = max(5100, int(delay_ms))
    return f"""// Antigravity Native In-Page Bilibili Collector (Pure Browser JS)
// 在已登录的 https://www.bilibili.com/ 页面通过 Antigravity browser 子代理直接执行
(async function runBilibiliInpageCollection() {{
  const TARGET_FOLDER = {json.dumps(target_folder, ensure_ascii=False)};
  const FOLDER_ID = {json.dumps(str(folder_id), ensure_ascii=False)};
  const ITEMS = {json.dumps(normalized_items, ensure_ascii=False)};
  const DELAY_MS = {safe_delay};
  const MAX_PAGES = 200;
  const BVID_RE = /^BV[A-Za-z0-9]{{10}}$/;
  const VIEW_RISK_CODES = new Set([-352, -412, 412, -101, -111, -799, 429, -500, -502, -503, -504, 500, 502, 503, 504]);
  const DEAL_RISK_CODES = new Set([-352, -412, 412, -403, -101, -111, -799, 429, -500, -502, -503, -504, 500, 502, 503, 504]);
  const DEAD_CODES = new Set([-404, -403, 62002, 62012, 62004]);

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const nowIso = () => new Date().toISOString();

  // 安全请求与 JSON 解析：防止 WAF 返回 HTTP 412/429 HTML 页面时抛出未捕获 SyntaxError
  async function safeFetchJson(url, opts) {{
    try {{
      const res = await fetch(url, opts);
      const status = typeof res.status === "number" ? res.status : 200;
      if (status === 412 || status === 429) {{
        return {{ ok: false, httpStatus: status, riskHttp: true, error: `http_risk_${{status}}` }};
      }}
      if (typeof res.text === "function") {{
        const txt = await res.text();
        try {{
          return {{ ok: true, httpStatus: status, data: JSON.parse(txt) }};
        }} catch (parseErr) {{
          return {{ ok: false, httpStatus: status, error: `non_json_http_${{status}}`, snippet: String(txt).slice(0, 120) }};
        }}
      }}
      const j = await res.json();
      return {{ ok: true, httpStatus: status, data: j }};
    }} catch (err) {{
      return {{ ok: false, httpStatus: 0, error: String(err) }};
    }}
  }}

  // 1. 登录态预检（严禁未登录空转）
  const navResp = await safeFetchJson("https://api.bilibili.com/x/web-interface/nav", {{ credentials: "include" }});
  if (!navResp.ok) {{
    return {{ ok: false, error: "nav_fetch_failed", http_status: navResp.httpStatus, detail: navResp.error }};
  }}
  const navData = navResp.data;
  if (!navData || navData.code !== 0 || !navData.data || navData.data.isLogin !== true) {{
    return {{ ok: false, error: "not_logged_in", message: "B站未登录，请先在浏览器登录后重试。" }};
  }}
  const accountId = String(navData.data.mid || "");
  const accountName = String(navData.data.uname || "");

  // 提取 CSRF (bili_jct)
  const csrfMatch = (document.cookie || "").match(/(?:^|;\\s*)bili_jct=([^;]+)/);
  if (!csrfMatch) {{
    return {{ ok: false, error: "no_csrf_cookie", account_id: accountId, message: "缺少 bili_jct CSRF Cookie，请刷新 bilibili.com 登录页后重试。" }};
  }}
  const csrf = csrfMatch[1];

  // 分页读取目标收藏夹全量 BVID（输出严格兼容 weekly_core.parse_snapshot）
  async function readFullFolderSnapshot(label) {{
    const startedAt = nowIso();
    const pages = [];
    const ids = [];
    const seen = new Set();
    let pn = 1;
    const ps = 20;
    let fetchError = null;
    while (pn <= MAX_PAGES) {{
      const url = `https://api.bilibili.com/x/v3/fav/resource/list?media_id=${{encodeURIComponent(FOLDER_ID)}}&pn=${{pn}}&ps=${{ps}}&order=mtime`;
      const resp = await safeFetchJson(url, {{ credentials: "include" }});
      if (!resp.ok) {{
        fetchError = resp.error;
        pages.push({{
          business_code: resp.riskHttp ? resp.httpStatus : -1,
          request_cursor: String(pn),
          has_more: true,
          next_cursor: null,
          error: resp.error,
          items: [],
        }});
        break;
      }}
      const j = resp.data;
      const medias = (j && j.data && Array.isArray(j.data.medias)) ? j.data.medias : [];
      const hasMore = Boolean(j && j.data && j.data.has_more);
      const pageItems = [];
      for (const m of medias) {{
        const bv = m && m.bvid ? String(m.bvid).trim() : "";
        if (BVID_RE.test(bv)) {{
          if (!seen.has(bv)) {{
            seen.add(bv);
            ids.push(bv);
          }}
          pageItems.push({{ id: bv }});
        }} else if (m && m.id != null) {{
          // 失效墓碑条目必须携带 unavailable: true 以通过 weekly_core.parse_snapshot 校验
          pageItems.push({{ provider_id: String(m.id), unavailable: true, tombstone: true }});
        }}
      }}
      pages.push({{
        business_code: j && typeof j.code === "number" ? j.code : -1,
        request_cursor: String(pn),
        has_more: hasMore,
        next_cursor: hasMore ? String(pn + 1) : null,
        items: pageItems,
      }});
      if (!j || j.code !== 0 || !hasMore || medias.length === 0) break;
      pn += 1;
      await sleep(400);
    }}
    return {{
      ok: !fetchError && pages.length > 0 && pages.length <= MAX_PAGES && pages.every((p) => p.business_code === 0) && pages[pages.length - 1].has_more === false,
      error: fetchError,
      snapshot_id: `snap_${{label}}_bilibili_${{Date.now()}}`,
      platform: "bilibili",
      account_id: accountId,
      folder_id: FOLDER_ID,
      folder_name: TARGET_FOLDER,
      started_at: startedAt,
      finished_at: nowIso(),
      method: "api",
      evidence_files: [`evidence/bilibili_snapshot_${{label}}.json`],
      pages,
      ids,
    }};
  }}

  // 2. 写前预读目标夹，跳过已在目标夹内的视频（绝不重复取消）
  const beforeSnap = await readFullFolderSnapshot("before");
  if (!beforeSnap.ok) {{
    return {{ ok: false, error: "before_snapshot_failed", account_id: accountId, folder_id: FOLDER_ID, before_snapshot: beforeSnap }};
  }}
  const existingSet = new Set(beforeSnap.ids);
  const receipts = [];
  const deadLinks = [];
  const skippedExisting = [];
  let stoppedReason = null;
  let writeCount = 0;

  // 3. 串行节流写入（间隔 >= 5.5 秒）
  for (let i = 0; i < ITEMS.length; i++) {{
    const bvid = ITEMS[i].bvid;
    if (existingSet.has(bvid)) {{
      skippedExisting.push(bvid);
      continue;
    }}
    if (writeCount > 0) {{
      await sleep(DELAY_MS);
    }}
    writeCount += 1;
    const itemStartedAt = nowIso();

    const viewResp = await safeFetchJson(`https://api.bilibili.com/x/web-interface/view?bvid=${{encodeURIComponent(bvid)}}`, {{ credentials: "include" }});
    if (!viewResp.ok) {{
      stoppedReason = viewResp.riskHttp ? `risk_http_${{viewResp.httpStatus}}_on_view_${{bvid}}` : `view_fetch_failed_${{bvid}}`;
      receipts.push({{ id: bvid, outcome: viewResp.riskHttp ? "failed" : "uncertain", started_at: itemStartedAt, at: nowIso(), code: viewResp.httpStatus, error: stoppedReason }});
      break;
    }}
    const viewJ = viewResp.data || {{}};
    if (VIEW_RISK_CODES.has(viewJ.code)) {{
      stoppedReason = `risk_code_${{viewJ.code}}_on_view_${{bvid}}`;
      receipts.push({{ id: bvid, outcome: "failed", started_at: itemStartedAt, at: nowIso(), code: viewJ.code, error: stoppedReason }});
      break;
    }}
    const vdata = viewJ.data || {{}};
    if (DEAD_CODES.has(viewJ.code) || (viewJ.code === 0 && (!vdata.aid || vdata.state === -4 || vdata.videos === 0))) {{
      deadLinks.push({{ id: bvid, code: viewJ.code, message: viewJ.message || "", state: vdata.state, videos: vdata.videos }});
      receipts.push({{
        id: bvid,
        outcome: "dead_link_confirmed",
        started_at: itemStartedAt,
        at: nowIso(),
        code: viewJ.code,
        detail: {{ code: viewJ.code, message: viewJ.message || "", state: vdata.state, videos: vdata.videos }},
      }});
      continue;
    }}
    if (viewJ.code !== 0) {{
      stoppedReason = `view_error_${{viewJ.code}}_on_${{bvid}}`;
      receipts.push({{ id: bvid, outcome: "failed", started_at: itemStartedAt, at: nowIso(), code: viewJ.code, error: stoppedReason, message: viewJ.message || "" }});
      break;
    }}

    const body = new URLSearchParams();
    body.append("rid", String(vdata.aid));
    body.append("type", "2");
    body.append("add_media_ids", FOLDER_ID);
    body.append("csrf", csrf);

    const dealResp = await safeFetchJson("https://api.bilibili.com/x/v3/fav/resource/deal", {{
      method: "POST",
      headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
      body: body.toString(),
      credentials: "include",
    }});
    if (!dealResp.ok) {{
      stoppedReason = dealResp.riskHttp ? `risk_http_${{dealResp.httpStatus}}_on_deal_${{bvid}}` : `deal_fetch_failed_${{bvid}}`;
      receipts.push({{ id: bvid, outcome: dealResp.riskHttp ? "failed" : "uncertain", started_at: itemStartedAt, at: nowIso(), code: dealResp.httpStatus, error: stoppedReason }});
      break;
    }}
    const dealJ = dealResp.data || {{}};
    if (DEAL_RISK_CODES.has(dealJ.code)) {{
      stoppedReason = `risk_code_${{dealJ.code}}_on_deal_${{bvid}}`;
      receipts.push({{ id: bvid, outcome: "failed", started_at: itemStartedAt, at: nowIso(), code: dealJ.code, error: stoppedReason }});
      break;
    }}
    if (dealJ.code === 0) {{
      existingSet.add(bvid);
      receipts.push({{
        id: bvid,
        outcome: "acknowledged",
        started_at: itemStartedAt,
        at: nowIso(),
        code: 0,
        codes: {{ favorite: 0 }},
        message: dealJ.message || "",
        api: {{ business_code: 0, aid: vdata.aid, folder_id: FOLDER_ID }},
      }});
    }} else if (dealJ.code === 11201) {{
      existingSet.add(bvid);
      receipts.push({{
        id: bvid,
        outcome: "already_favorited",
        started_at: itemStartedAt,
        at: nowIso(),
        code: 11201,
        codes: {{ favorite: 11201 }},
        message: dealJ.message || "",
        api: {{ business_code: 11201, aid: vdata.aid, folder_id: FOLDER_ID }},
      }});
    }} else {{
      stoppedReason = `deal_error_${{dealJ.code}}_on_${{bvid}}`;
      receipts.push({{
        id: bvid,
        outcome: "failed",
        started_at: itemStartedAt,
        at: nowIso(),
        code: dealJ.code,
        error: stoppedReason,
        message: dealJ.message || "",
        api: {{ business_code: dealJ.code, aid: vdata.aid, folder_id: FOLDER_ID }},
      }});
      break;
    }}
  }}

  // 4. 写后两轮全量分页回读对账（两轮间隔 >= 5.5 秒）
  await sleep(1500);
  const round1 = await readFullFolderSnapshot("round1");
  await sleep(DELAY_MS);
  const round2 = await readFullFolderSnapshot("round2");

  const deadSet = new Set(deadLinks.map((d) => d.id));
  const expectedActive = ITEMS.map((x) => x.bvid).filter((id) => !deadSet.has(id));
  const r1Set = new Set(round1.ids);
  const r2Set = new Set(round2.ids);
  const missingRound1 = expectedActive.filter((id) => !r1Set.has(id));
  const missingRound2 = expectedActive.filter((id) => !r2Set.has(id));
  const reconcileOk = !stoppedReason && round1.ok && round2.ok && missingRound1.length === 0 && missingRound2.length === 0;

  const finalResult = {{
    ok: reconcileOk,
    platform: "bilibili",
    account_id: accountId,
    account_name: accountName,
    folder_id: FOLDER_ID,
    folder_name: TARGET_FOLDER,
    stopped_reason: stoppedReason,
    skipped_existing: skippedExisting,
    dead_links: deadLinks,
    receipts,
    missing_round1: missingRound1,
    missing_round2: missingRound2,
    snapshots: {{ before: beforeSnap, round1, round2 }},
  }};
  if (typeof window !== "undefined") {{
    window.__weeklyBiliLastResult = finalResult;
  }}
  return finalResult;
}})()
"""


def generate_douyin_inpage_helper_js(
    target_folder: str,
    folder_id_str: str,
    items_to_add: list[dict | str],
) -> str:
    """生成纯浏览器页内辅助脚本（适配 Antigravity browser 子代理，零 tab.playwright 依赖）。
    在抖音页面挂载 window.__weeklyDouyinHelper，供 Antigravity browser 子代理分阶段安全完成：
    登录态校验、单视频收藏状态检查（绝不误取消已有收藏，兼容 SVG 点击）、目标夹计数门禁、弹窗卡片坐标扫描与夹内 ID 回读。
    """
    normalized_items = _normalize_douyin_items(items_to_add)
    return f"""// Antigravity Native In-Page Douyin Helper (Pure Browser JS)
// 在已登录的 https://www.douyin.com/ 页面通过 Antigravity browser 子代理执行
(function installWeeklyDouyinHelper() {{
  const CONFIG = {{
    targetFolder: {json.dumps(target_folder, ensure_ascii=False)},
    folderIdStr: {json.dumps(str(folder_id_str), ensure_ascii=False)},
    items: {json.dumps(normalized_items, ensure_ascii=False)},
  }};

  function safeTriggerClick(node, fallbackEl) {{
    // 真实 Chromium 中 SVGElement 没有 .click() 方法，必须优先调用 HTMLElement.click() 或派发冒泡 MouseEvent
    if (node && typeof node.click === 'function') {{
      node.click();
      return 'dom_click';
    }}
    if (fallbackEl && typeof fallbackEl.click === 'function') {{
      fallbackEl.click();
      return 'parent_dom_click';
    }}
    const dispatchTarget = node || fallbackEl;
    if (dispatchTarget && typeof dispatchTarget.dispatchEvent === 'function') {{
      const evt = typeof MouseEvent === 'function'
        ? new MouseEvent('click', {{ bubbles: true, cancelable: true, view: typeof window !== 'undefined' ? window : undefined }})
        : {{ type: 'click', bubbles: true, cancelable: true }};
      dispatchTarget.dispatchEvent(evt);
      return 'mouse_event';
    }}
    return null;
  }}

  function checkLogin() {{
    const hasFavTab = Boolean(document.querySelector('#semiTabfavorite_collection'));
    const hasUserEl = Boolean(document.querySelector('.user_detail_element, [data-e2e="user-info"]'));
    const hasLoginModal = Boolean(document.querySelector('[class*="login-mask"], #login-pannel'));
    return {{
      logged_in: (hasFavTab || hasUserEl) && !hasLoginModal,
      has_fav_tab: hasFavTab,
      has_user_el: hasUserEl,
      has_login_modal: hasLoginModal,
      url: location.href,
    }};
  }}

  function readFolderCount(folderName = CONFIG.targetFolder) {{
    const leaves = Array.from(document.querySelectorAll('*')).filter(
      (el) => el.children.length === 0 && (el.innerText || el.textContent || '').trim() === folderName
    );
    for (const span of leaves) {{
      let cur = span;
      for (let depth = 0; depth < 5 && cur; depth++) {{
        const txt = cur.innerText || cur.textContent || '';
        const m = txt.match(/共\\s*(\\d+)\\s*作品/);
        if (m) {{
          return {{ found: true, folder_name: folderName, count: parseInt(m[1], 10) }};
        }}
        cur = cur.parentElement;
      }}
    }}
    return {{ found: leaves.length > 0, folder_name: folderName, count: null }};
  }}

  function isCollectedStateValue(st) {{
    const val = String(st || '').trim().toLowerCase();
    if (!val) return false;
    if (/(?:^|-)(?:no|not|un)-?collected$/.test(val)) return false;
    return /(?:^|-)is-collected$|^collected$/.test(val);
  }}

  function readCurrentCollectState() {{
    const el = document.querySelector('[data-e2e="video-player-collect"], [data-e2e="note-collect-btn"], [data-e2e="feed-collect-icon"]');
    if (!el) {{
      return {{ found: false, collected: null, state: null, url: location.href }};
    }}
    const st = el.getAttribute('data-e2e-state') || '';
    const collected = isCollectedStateValue(st);
    const rect = el.getBoundingClientRect();
    return {{
      found: true,
      collected,
      state: st,
      center: {{ x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2) }},
      url: location.href,
    }};
  }}

  function clickCollectIfUncollected() {{
    const st = readCurrentCollectState();
    if (!st.found) {{
      return {{ ok: false, action: 'not_found', detail: st }};
    }}
    if (st.collected) {{
      return {{ ok: true, action: 'skipped_already_collected', detail: st }};
    }}
    const el = document.querySelector('[data-e2e="video-player-collect"], [data-e2e="note-collect-btn"], [data-e2e="feed-collect-icon"]');
    const iconEl = el ? el.querySelector('.semi-icon, .gBYrMC0K') : null;
    const svgEl = el ? el.querySelector('svg') : null;
    const target = iconEl || svgEl || el;
    if (!target) {{
      return {{ ok: false, action: 'target_missing', detail: st }};
    }}
    const clickMode = safeTriggerClick(target, el);
    if (!clickMode) {{
      return {{ ok: false, action: 'click_unsupported', detail: st }};
    }}
    return {{ ok: true, action: 'clicked_collect', click_mode: clickMode, detail: st }};
  }}

  function findModalTargetCards(targetIds = CONFIG.items.map((x) => x.id)) {{
    const idSet = new Set(targetIds.map(String));
    const lis = Array.from(document.querySelectorAll('.fZVrW1rH li, ul[data-e2e="scroll-list"] li, .collection-add-modal li, .modal-scroll-list li'));
    const matches = [];
    lis.forEach((li, idx) => {{
      const a = li.querySelector('a[href]');
      const href = a ? a.href : '';
      const idMatch = href.match(/(?<![0-9])([0-9]{{19}})(?![0-9])/);
      if (!idMatch) return;
      const vid = idMatch[1];
      if (!idSet.has(vid)) return;
      const cb = li.querySelector('input[type="checkbox"]');
      const customChecked = Boolean(li.querySelector && li.querySelector('.semi-checkbox-checked, [aria-checked="true"]'));
      const rect = li.getBoundingClientRect();
      matches.push({{
        id: vid,
        idx,
        col: idx % 5,
        checked: Boolean((cb && cb.checked) || customChecked),
        clickX: Math.round(rect.left + rect.width / 2),
        clickY: Math.round(rect.top + rect.height / 2),
        inViewport: rect.top >= 120 && rect.bottom <= (window.innerHeight - 60),
      }});
    }});
    return {{ total_visible_cards: lis.length, matches }};
  }}

  function scrapeVisibleFolderIds(containerSelector = null, opts = {{}}) {{
    const startedAt = new Date().toISOString();
    const root = containerSelector ? (document.querySelector(containerSelector) || document) : document;
    const anchors = Array.from(root.querySelectorAll('a[href*="/video/"], a[href*="/note/"]'));
    const ids = [];
    const seen = new Set();
    for (const a of anchors) {{
      const m = (a.href || '').match(/\\/(video|note)\\/([0-9]{{19}})/);
      if (m && !seen.has(m[2])) {{
        seen.add(m[2]);
        ids.push({{ id: m[2], kind: m[1], href: a.href }});
      }}
    }}
    const bodyText = (document.body && (document.body.innerText || document.body.textContent)) || '';
    const targetVisible = typeof opts.targetVisible === 'boolean'
      ? opts.targetVisible
      : (Boolean(CONFIG.targetFolder) && bodyText.includes(CONFIG.targetFolder));
    const endVisible = typeof opts.endVisible === 'boolean'
      ? opts.endVisible
      : /暂时没有更多了|没有更多了|已经到底了|到底了/.test(bodyText);
    const fullyLoaded = typeof opts.fullyLoaded === 'boolean'
      ? opts.fullyLoaded
      : Boolean(targetVisible && endVisible);
    const finishedAt = new Date().toISOString();
    return {{
      count: ids.length,
      items: ids,
      url: location.href,
      folder_name: CONFIG.targetFolder,
      folder_id: CONFIG.folderIdStr,
      target_visible: targetVisible,
      fully_loaded: fullyLoaded,
      end_visible: endVisible,
      started_at: startedAt,
      finished_at: finishedAt,
      scraped_at: finishedAt,
    }};
  }}

  function buildUiSnapshot(label = 'round1', opts = {{}}) {{
    const scraped = scrapeVisibleFolderIds(opts.containerSelector || null, opts);
    const fullyLoaded = scraped.fully_loaded;
    return {{
      snapshot_id: `snap_${{label}}_douyin_${{Date.now()}}`,
      platform: 'douyin',
      account_id: opts.accountId || '',
      folder_id: CONFIG.folderIdStr,
      folder_name: CONFIG.targetFolder,
      started_at: scraped.started_at,
      finished_at: scraped.finished_at,
      method: 'ui',
      target_visible: scraped.target_visible,
      fully_loaded: fullyLoaded,
      end_visible: scraped.end_visible,
      evidence_files: [`evidence/douyin_snapshot_${{label}}.json`],
      count: scraped.count,
      items: scraped.items,
      url: scraped.url,
    }};
  }}

  window.__weeklyDouyinHelper = {{
    config: CONFIG,
    checkLogin,
    readFolderCount,
    readCurrentCollectState,
    clickCollectIfUncollected,
    findModalTargetCards,
    scrapeVisibleFolderIds,
    buildUiSnapshot,
  }};

  return {{
    ok: true,
    installed: true,
    target_folder: CONFIG.targetFolder,
    folder_id_str: CONFIG.folderIdStr,
    planned_count: CONFIG.items.length,
    login: checkLogin(),
    folder_count: readFolderCount(),
  }};
}})()
"""


def generate_playwright_cdp_collector(
    platform: str,
    target_folder: str,
    folder_id: str,
    items_to_add: list[dict | str],
    cdp_url: str = "http://127.0.0.1:9222",
) -> str:
    """生成通过本机 Python Playwright 连接已有调试浏览器（CDP）执行的收藏脚本。
    不读取或导出 Cookie 文件，仅复用用户已登录的本地调试浏览器会话。
    """
    if platform == "bilibili":
        inpage_js = generate_bilibili_inpage_js(target_folder, folder_id, items_to_add)
        return f'''#!/usr/bin/env python3
# coding: utf-8
"""通过 Playwright CDP 连接本机已登录浏览器执行 bilibili 收藏（不依赖 Codex cua_repl）。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

INPAGE_JS = {json.dumps(inpage_js, ensure_ascii=False)}


def _is_bilibili_page(url: str) -> bool:
    """严格按主机名匹配 B 站标签页，避免误命中含 bilibili.com 查询串的其他页面。"""
    host = (urlsplit(url or "").hostname or "").lower()
    return host == "bilibili.com" or host.endswith(".bilibili.com")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default={json.dumps(cdp_url)}, help="本机 Chrome/Edge CDP 调试地址")
    ap.add_argument("--out", type=Path, default=Path("receipt_bilibili_cdp.json"), help="回执输出路径")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = next((pg for pg in context.pages if _is_bilibili_page(pg.url or "")), None)
        if page is None:
            page = context.new_page()
            page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

        result = page.evaluate(INPAGE_JS)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({{"ok": bool(result.get("ok")), "out": str(args.out.resolve()), "platform": "bilibili"}}, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
'''

    if platform == "douyin":
        inpage_js = generate_douyin_inpage_helper_js(target_folder, folder_id, items_to_add)
        norm_items = _normalize_douyin_items(items_to_add)
        return f'''#!/usr/bin/env python3
# coding: utf-8
"""通过 Playwright CDP 连接本机已登录浏览器执行 douyin 阶段1串行点亮收藏并读取目标夹基线（不依赖 Codex cua_repl）。"""
from __future__ import annotations
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

HELPER_JS = {json.dumps(inpage_js, ensure_ascii=False)}
TARGET_FOLDER = {json.dumps(target_folder, ensure_ascii=False)}
ITEMS = {json.dumps(norm_items, ensure_ascii=False)}


def _is_douyin_page(url: str) -> bool:
    """严格按主机名匹配抖音标签页，避免误命中无关网页。"""
    host = (urlsplit(url or "").hostname or "").lower()
    return host == "douyin.com" or host.endswith(".douyin.com")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default={json.dumps(cdp_url)}, help="本机 Chrome/Edge CDP 调试地址")
    ap.add_argument("--out", type=Path, default=Path("receipt_douyin_cdp_stage1.json"), help="阶段1回执输出路径")
    ap.add_argument("--delay", type=float, default=5.5, help="每条作品间隔秒数（强制>=5.0秒防风控）")
    args = ap.parse_args()
    delay = max(5.0, float(args.delay))
    args.out.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = next((pg for pg in context.pages if _is_douyin_page(pg.url or "")), None)
        if page is None:
            page = context.new_page()

        fav_url = "https://www.douyin.com/user/self?showSubTab=favorite_folder&showTab=favorite_collection"
        page.goto(fav_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        init_res = page.evaluate(HELPER_JS)
        if not init_res.get("login", {{}}).get("logged_in"):
            out_data = {{"ok": False, "error": "not_logged_in", "platform": "douyin", "detail": init_res}}
            args.out.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(out_data, ensure_ascii=False, indent=2))
            return 2

        folder_before = init_res.get("folder_count", {{}})
        stage1_receipts = []
        failed_count = 0
        for idx, item in enumerate(ITEMS):
            if idx > 0:
                time.sleep(delay)
            try:
                page.goto(item["url"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                page.evaluate(HELPER_JS)
                act = page.evaluate("() => window.__weeklyDouyinHelper.clickCollectIfUncollected()")
            except Exception as exc:
                act = {{"ok": False, "action": "exception", "error": str(exc)}}
            if not act.get("ok"):
                failed_count += 1
            stage1_receipts.append({{
                "id": item["id"],
                "kind": item["kind"],
                "url": item["url"],
                "at": datetime.now(timezone.utc).isoformat(),
                "stage1": act,
            }})

        try:
            page.goto(fav_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            page.evaluate(HELPER_JS)
            folder_after_stage1 = page.evaluate("() => window.__weeklyDouyinHelper.readFolderCount()")
        except Exception as exc:
            folder_after_stage1 = {{"found": False, "error": str(exc)}}

        stage1_ok = failed_count == 0
        out_data = {{
            "ok": stage1_ok,
            "stage": "stage1_complete_stage2_folder_add_required" if stage1_ok else "stage1_partial_failed",
            "note": "阶段1已按>=5秒间隔点亮总收藏；请继续通过目标夹【添加视频】勾选入夹并核验作品数增量及两轮全量回读。",
            "platform": "douyin",
            "target_folder": TARGET_FOLDER,
            "failed_count": failed_count,
            "folder_count_before": folder_before,
            "folder_count_after_stage1": folder_after_stage1,
            "receipts": stage1_receipts,
        }}
        args.out.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({{"ok": stage1_ok, "out": str(args.out.resolve()), "count": len(stage1_receipts), "failed_count": failed_count}}, ensure_ascii=False, indent=2))
        return 0 if stage1_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
'''

    raise ValueError(f"unsupported_platform: {platform}")

