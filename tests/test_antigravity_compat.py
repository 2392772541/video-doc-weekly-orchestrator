# coding: utf-8
"""Antigravity 兼容性自动化测试：
覆盖纯页内 JS 语法校验、真实 Node.js 模拟执行（登录态/风控/失效稿件/去重/两轮对账/防误取消）、
Playwright CDP 脚本编译校验、weekly.py collect-plan/doctor/extract-feishu 边界测试。
全程离线执行，零远程写操作。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import real_collector as rc
import weekly
import weekly_core as w


def _run_node_eval(js_code: str) -> dict:
    """在真实 Node.js 子进程中运行测试桩并返回 JSON 结果。"""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not installed")
    res = subprocess.run(
        [node, "-e", js_code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if res.returncode != 0:
        raise AssertionError(f"node failed (rc={res.returncode})\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
    return json.loads(res.stdout.strip())


class AntigravityCollectorTests(unittest.TestCase):
    def test_inpage_js_has_no_codex_cua_dependency_and_valid_syntax(self):
        b_js = rc.generate_bilibili_inpage_js("水球泡周报视频", "4103329683", ["BV1QgY86XEXs"])
        d_js = rc.generate_douyin_inpage_helper_js("水球泡视频周报", "7672986870204225339", ["7689404171926108874"])

        for script in (b_js, d_js):
            self.assertNotIn("tab.playwright", script)
            self.assertNotIn("cua_repl", script)

        node = shutil.which("node")
        if node:
            with tempfile.TemporaryDirectory() as td:
                for fname, content in (("b.js", b_js), ("d.js", d_js)):
                    p = Path(td) / fname
                    p.write_text(content, encoding="utf-8")
                    chk = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, timeout=10)
                    self.assertEqual(chk.returncode, 0, f"JS syntax error in {fname}: {chk.stderr}")

    def test_bilibili_inpage_js_not_logged_in_and_missing_csrf(self):
        b_js = rc.generate_bilibili_inpage_js("水球泡周报视频", "4103329683", ["BV1QgY86XEXs"], delay_ms=5000)
        # 1) 未登录必须立刻中断，零写操作
        harness_no_login = f"""
        global.setTimeout = (fn) => {{ fn(); return 0; }};
        let postCalls = 0;
        global.document = {{ cookie: "bili_jct=fake_csrf" }};
        global.fetch = async (url, opts) => {{
          if (opts && opts.method === "POST") postCalls++;
          if (url.includes("/x/web-interface/nav")) return {{ json: async () => ({{ code: 0, data: {{ isLogin: false }} }}) }};
          throw new Error("unexpected url: " + url);
        }};
        (async () => {{
          const r = await ({b_js});
          console.log(JSON.stringify({{ result: r, postCalls }}));
        }})();
        """
        out1 = _run_node_eval(harness_no_login)
        self.assertFalse(out1["result"]["ok"])
        self.assertEqual(out1["result"]["error"], "not_logged_in")
        self.assertEqual(out1["postCalls"], 0)

        # 2) 缺少 bili_jct 必须中断
        harness_no_csrf = f"""
        global.setTimeout = (fn) => {{ fn(); return 0; }};
        global.document = {{ cookie: "SESSDATA=abc" }};
        global.fetch = async (url) => {{
          if (url.includes("/x/web-interface/nav")) return {{ json: async () => ({{ code: 0, data: {{ isLogin: true, mid: 123, uname: "tester" }} }}) }};
          throw new Error("unexpected url: " + url);
        }};
        (async () => {{
          const r = await ({b_js});
          console.log(JSON.stringify(r));
        }})();
        """
        out2 = _run_node_eval(harness_no_csrf)
        self.assertFalse(out2["ok"])
        self.assertEqual(out2["error"], "no_csrf_cookie")

    def test_bilibili_inpage_js_full_flow_skip_dead_and_reconcile(self):
        items = ["BV1EXIST0001", "BV1NEW000002", "BV1DEAD00003"]
        b_js = rc.generate_bilibili_inpage_js("水球泡周报视频", "4103329683", items, delay_ms=5000)
        harness = f"""
        global.setTimeout = (fn) => {{ fn(); return 0; }};
        global.document = {{ cookie: "foo=1; bili_jct=csrf_ok_token" }};
        const folderIds = ["BV1EXIST0001"];
        const dealt = [];
        global.fetch = async (url, opts) => {{
          if (url.includes("/x/web-interface/nav")) {{
            return {{ json: async () => ({{ code: 0, data: {{ isLogin: true, mid: 3546569697659483, uname: "马" }} }}) }};
          }}
          if (url.includes("/x/v3/fav/resource/list")) {{
            return {{ json: async () => ({{ code: 0, data: {{ has_more: false, medias: folderIds.map(b => ({{ bvid: b }})) }} }}) }};
          }}
          if (url.includes("bvid=BV1NEW000002")) {{
            return {{ json: async () => ({{ code: 0, data: {{ aid: 999888, state: 0, videos: 1 }} }}) }};
          }}
          if (url.includes("bvid=BV1DEAD00003")) {{
            return {{ json: async () => ({{ code: -404, message: "啥都木有" }}) }};
          }}
          if (url.includes("/x/v3/fav/resource/deal")) {{
            dealt.push(opts.body);
            folderIds.push("BV1NEW000002");
            return {{ json: async () => ({{ code: 0, message: "0" }}) }};
          }}
          throw new Error("unexpected url: " + url);
        }};
        (async () => {{
          const r = await ({b_js});
          console.log(JSON.stringify({{ r, dealt }}));
        }})();
        """
        out = _run_node_eval(harness)
        res = out["r"]
        self.assertTrue(res["ok"])
        self.assertEqual(res["skipped_existing"], ["BV1EXIST0001"])
        self.assertEqual(len(res["dead_links"]), 1)
        self.assertEqual(res["dead_links"][0]["id"], "BV1DEAD00003")
        self.assertEqual(res["missing_round1"], [])
        self.assertEqual(res["missing_round2"], [])
        self.assertEqual(len(out["dealt"]), 1)
        self.assertIn("rid=999888", out["dealt"][0])
        self.assertIn("add_media_ids=4103329683", out["dealt"][0])

    def test_bilibili_inpage_js_risk_circuit_breaker_and_before_snap_failure(self):
        b_js = rc.generate_bilibili_inpage_js("水球泡周报视频", "4103329683", ["BV1RISK00001", "BV1NEXT00002"], delay_ms=5000)
        # 1) 风控码 -412 必须立即熔断，不继续写第 2 条
        harness_risk = f"""
        global.setTimeout = (fn) => {{ fn(); return 0; }};
        global.document = {{ cookie: "bili_jct=csrf_ok" }};
        const viewSeen = [];
        global.fetch = async (url) => {{
          if (url.includes("/x/web-interface/nav")) return {{ json: async () => ({{ code: 0, data: {{ isLogin: true, mid: 1, uname: "u" }} }}) }};
          if (url.includes("/x/v3/fav/resource/list")) return {{ json: async () => ({{ code: 0, data: {{ has_more: false, medias: [] }} }}) }};
          if (url.includes("/x/web-interface/view")) {{
            viewSeen.push(url);
            return {{ json: async () => ({{ code: 0, data: {{ aid: 111, state: 0, videos: 1 }} }}) }};
          }}
          if (url.includes("/x/v3/fav/resource/deal")) return {{ json: async () => ({{ code: -412, message: "风控拦截" }}) }};
          throw new Error("unexpected " + url);
        }};
        (async () => {{
          const r = await ({b_js});
          console.log(JSON.stringify({{ r, viewSeen }}));
        }})();
        """
        out = _run_node_eval(harness_risk)
        self.assertFalse(out["r"]["ok"])
        self.assertIn("-412", out["r"]["stopped_reason"])
        self.assertEqual(len(out["viewSeen"]), 1)

        # 2) 写前快照 API 失败必须中止，不能当成空夹硬写
        harness_snap_err = f"""
        global.setTimeout = (fn) => {{ fn(); return 0; }};
        global.document = {{ cookie: "bili_jct=csrf_ok" }};
        global.fetch = async (url) => {{
          if (url.includes("/x/web-interface/nav")) return {{ json: async () => ({{ code: 0, data: {{ isLogin: true, mid: 1, uname: "u" }} }}) }};
          if (url.includes("/x/v3/fav/resource/list")) return {{ json: async () => ({{ code: -400, data: null }}) }};
          throw new Error("should not continue after failed before snapshot");
        }};
        (async () => {{
          const r = await ({b_js});
          console.log(JSON.stringify(r));
        }})();
        """
        out2 = _run_node_eval(harness_snap_err)
        self.assertFalse(out2["ok"])
        self.assertEqual(out2["error"], "before_snapshot_failed")

    def test_douyin_inpage_helper_prevents_unfavoriting_and_scans_cards(self):
        vid1 = "7689404171926108874"
        vid2 = "7695302751908478458"
        d_js = rc.generate_douyin_inpage_helper_js("水球泡视频周报", "7672986870204225339", [vid1, f"https://www.douyin.com/note/{vid2}"])
        harness = f"""
        let clickCount = 0;
        let collectState = "video-player-is-collected";
        const fakeCollectEl = {{
          getAttribute: (k) => k === "data-e2e-state" ? collectState : null,
          getBoundingClientRect: () => ({{ left: 100, top: 200, width: 40, height: 40 }}),
          querySelector: () => ({{ click: () => {{ clickCount++; }} }}),
          click: () => {{ clickCount++; }},
        }};
        const folderLeaf = {{
          children: [],
          innerText: "水球泡视频周报",
          parentElement: {{ innerText: "水球泡视频周报\\n共 195 作品", parentElement: null }},
        }};
        const modalLis = [
          {{
            querySelector: (s) => s.startsWith("a") ? {{ href: "https://www.douyin.com/video/{vid1}" }} : {{ checked: false }},
            getBoundingClientRect: () => ({{ left: 10, top: 180, width: 100, height: 120, bottom: 300 }}),
          }},
          {{
            querySelector: (s) => s.startsWith("a") ? {{ href: "https://www.douyin.com/video/1111111111111111111" }} : {{ checked: false }},
            getBoundingClientRect: () => ({{ left: 120, top: 180, width: 100, height: 120, bottom: 300 }}),
          }},
        ];
        global.window = {{ innerHeight: 900 }};
        global.location = {{ href: "https://www.douyin.com/video/{vid1}" }};
        global.document = {{
          querySelector: (sel) => {{
            if (sel.includes("semiTabfavorite_collection")) return {{}};
            if (sel.includes("video-player-collect")) return fakeCollectEl;
            return null;
          }},
          querySelectorAll: (sel) => {{
            if (sel === "*") return [folderLeaf];
            if (sel.includes(".fZVrW1rH")) return modalLis;
            if (sel.includes("/video/")) return [
              {{ href: "https://www.douyin.com/video/{vid1}" }},
              {{ href: "https://www.douyin.com/note/{vid2}" }},
              {{ href: "https://www.douyin.com/video/{vid1}" }},
            ];
            return [];
          }},
        }};
        const initRes = ({d_js});
        const h = window.__weeklyDouyinHelper;
        const alreadyRes = h.clickCollectIfUncollected();
        collectState = "video-player-no-collected";
        const newClickRes = h.clickCollectIfUncollected();
        const modalRes = h.findModalTargetCards();
        const scraped = h.scrapeVisibleFolderIds();
        console.log(JSON.stringify({{ initRes, alreadyRes, newClickRes, clickCount, modalRes, scraped }}));
        """
        out = _run_node_eval(harness)
        self.assertTrue(out["initRes"]["ok"])
        self.assertEqual(out["initRes"]["folder_count"]["count"], 195)
        self.assertEqual(out["alreadyRes"]["action"], "skipped_already_collected")
        self.assertEqual(out["newClickRes"]["action"], "clicked_collect")
        self.assertEqual(out["clickCount"], 1)  # 已收藏时不点击，未收藏时才点击 1 次
        self.assertEqual(len(out["modalRes"]["matches"]), 1)
        self.assertEqual(out["modalRes"]["matches"][0]["id"], vid1)
        self.assertEqual(out["scraped"]["count"], 2)

    def test_playwright_cdp_collector_compiles_for_both_platforms(self):
        for plat, fid, ids in [
            ("bilibili", "4103329683", ["BV1QgY86XEXs"]),
            ("douyin", "7672986870204225339", ["7689404171926108874"]),
        ]:
            code = rc.generate_playwright_cdp_collector(plat, "测试夹", fid, ids)
            compile(code, f"cdp_{plat}.py", "exec")
        with self.assertRaises(ValueError):
            rc.generate_playwright_cdp_collector("unknown", "x", "1", [])

    def test_weekly_cli_collect_plan_antigravity_and_watched_env(self):
        with tempfile.TemporaryDirectory(prefix="weekly-ag-test-") as td:
            run_dir = Path(td) / "run"
            run_dir.mkdir()
            ev = run_dir / "evidence"
            ev.mkdir()
            for name in ("source.json", "detail.json", "discovery.json", "pages.json", "style.json"):
                w.write_json(ev / name, {"synthetic": True, "name": name})

            dy_keep = "7689404171926108874"
            dy_watched = "7695302751908478458"
            bv_keep = "BV1QgY86XEXs"
            seed = {
                "title": "Synthetic AG Test Vol.1",
                "source_week": "20261122",
                "numbered_item_count": 3,
                "quick_news_count": 0,
                "items": [
                    {"key": "d1", "title": "抖音未看", "section": "视频", "number": 1, "url": f"https://www.douyin.com/video/{dy_keep}"},
                    {"key": "d2", "title": "抖音已看", "section": "视频", "number": 2, "url": f"https://www.douyin.com/video/{dy_watched}"},
                    {"key": "b1", "title": "B站视频", "section": "视频", "number": 3, "url": f"https://www.bilibili.com/video/{bv_keep}"},
                ],
            }
            state = w.initialize(seed, mode="collect")
            base = datetime.now(timezone.utc) - timedelta(seconds=30)
            t0 = base.isoformat()
            t1 = (base + timedelta(seconds=5)).isoformat()
            t2 = (base + timedelta(seconds=10)).isoformat()
            events = [
                {"type": "permissions", "publish": False, "collect": ["douyin", "bilibili"], "user_request": "test"},
                {"type": "register_source", "source_id": "src", "kind": "feishu", "document_id": "doc_1", "revision_id": 1, "checked_at": t0, "evidence_files": ["evidence/source.json"]},
                {"type": "target", "platform": "douyin", "authenticated": True, "account_id": "acc_dy", "complete": True, "checked_at": t0, "desired_name": "水球泡视频周报", "folders": [{"name": "水球泡视频周报", "collects_id_str": "7672986870204225339"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
                {"type": "target", "platform": "bilibili", "authenticated": True, "account_id": "acc_bv", "complete": True, "checked_at": t0, "desired_name": "水球泡周报视频", "folders": [{"name": "水球泡周报视频", "id": "4103329683"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
            ]
            for it in state["items"]:
                plat = w.normalize_url(it["original_url"])["platform"]
                oid = w.normalize_url(it["original_url"])["id"]
                events.append({"type": "source", "key": it["key"], "source_id": "src", "item_title": it["title"], "ownership": "same_item", "revision_id": 1, "observed_url": it["original_url"], "author": "author", "evidence_files": ["evidence/source.json"]})
                events.append({
                    "type": "verify", "key": it["key"], "url": it["original_url"], "observed_title": it["title"], "observed_author": "author",
                    "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "ok"},
                    "availability": {"status": "available", "checked_at": t0, "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": oid},
                    "evidence_files": ["evidence/detail.json"],
                })
            events.append({
                "type": "inventory",
                "expected_numbered": 3,
                "expected_quick_news": 0,
                "all_source_parts_reviewed": True,
                "layout_reviewed": True,
                "evidence_files": ["evidence/source.json"],
            })
            state = w.apply_events(state, run_dir, events, t0)
            for plat, acc, fid, fname in [
                ("douyin", "acc_dy", "7672986870204225339", "水球泡视频周报"),
                ("bilibili", "acc_bv", "4103329683", "水球泡周报视频"),
            ]:
                w.import_snapshot(state, run_dir, {
                    "snapshot_id": f"init_{plat}", "platform": plat, "account_id": acc, "folder_id": fid, "folder_name": fname,
                    "started_at": t1, "finished_at": t2, "method": "api", "evidence_files": ["evidence/pages.json"],
                    "pages": [{"business_code": 0, "request_cursor": "0", "has_more": False, "next_cursor": None, "items": []}],
                }, t2)
            w.write_json(run_dir / "run.json", state)

            watched_file = Path(td) / "watched.json"
            watched_file.write_text(json.dumps([dy_watched]), encoding="utf-8")

            with mock.patch.dict(os.environ, {"WEEKLY_WATCHED_FILE": str(watched_file)}):
                rc_code = weekly.main(["collect-plan", "--run", str(run_dir), "--runtime", "antigravity"])
            self.assertEqual(rc_code, 0)
            self.assertTrue((ev / "collect_bilibili_inpage.js").is_file())
            self.assertTrue((ev / "collect_bilibili_cdp.py").is_file())
            self.assertTrue((ev / "collect_douyin_inpage_helper.js").is_file())
            self.assertTrue((ev / "collect_douyin_cdp.py").is_file())
            self.assertFalse((ev / "collect_bilibili.js").exists())  # --runtime antigravity 不额外写旧 Codex 文件
            dy_js_txt = (ev / "collect_douyin_inpage_helper.js").read_text(encoding="utf-8")
            self.assertIn(dy_keep, dy_js_txt)
            self.assertNotIn(dy_watched, dy_js_txt)

    def test_extract_feishu_uses_argv_list_for_urls_with_ampersand(self):
        fake_payload = {
            "ok": True,
            "identity": "user",
            "data": {
                "document": {
                    "document_id": "doc_amp",
                    "revision_id": 2,
                    "content": '<title>测试周报</title><h2># 测试</h2><h3>1. 条目A</h3><p><a href="https://www.bilibili.com/video/BV1QgY86XEXs">https://www.bilibili.com/video/BV1QgY86XEXs</a></p>',
                }
            },
        }
        url_with_amp = "https://my.feishu.cn/docx/doc_amp?from=space&tab=1"
        self.assertEqual(weekly.normalize_feishu_doc_token(url_with_amp), "doc_amp")
        self.assertEqual(weekly.normalize_feishu_doc_token("QBIhd5Xm7otjmRx3jZhcE71Znde"), "QBIhd5Xm7otjmRx3jZhcE71Znde")
        with tempfile.TemporaryDirectory() as td:
            out_p = Path(td) / "out.json"
            with mock.patch("subprocess.check_output", return_value=json.dumps(fake_payload)) as m_co:
                rc_code = weekly.main(["extract-feishu", "--doc", url_with_amp, "--out", str(out_p)])
            self.assertEqual(rc_code, 0)
            called_args = m_co.call_args[0][0]
            self.assertIsInstance(called_args, list)
            self.assertIn("doc_amp", called_args)
            self.assertNotIn("&", " ".join(called_args))
            self.assertIn("--as", called_args)
            self.assertTrue(out_p.is_file())

    def test_extract_feishu_includes_video_bookmarks_and_skips_non_video_bookmarks(self):
        import extract_feishu

        xml = (
            '<title>Vol.302 回归测试</title>'
            '<h2>游戏人生</h2>'
            '<h3>1. 抖音书签条目</h3>'
            '<bookmark id="bm_dy" name="抖音｜测试视频" href="https://www.douyin.com/video/7588083083560242482"></bookmark>'
            '<h3>2. 微信图文书签</h3>'
            '<bookmark id="bm_wx" name="mp.weixin.qq.com" href="https://mp.weixin.qq.com/s/abc123"></bookmark>'
            '<h3>3. 小写bv内嵌播放器</h3>'
            '<readonly-block id="rb_bv" href="https://player.bilibili.com/player.html?bvid=bv1dKeB6HEvJ" type="iframe"></readonly-block>'
        )
        payload = {"ok": True, "identity": "user", "data": {"document": {"document_id": "doc_bm", "revision_id": 9, "content": xml}}}
        res = extract_feishu.extract(payload)
        self.assertEqual(res["numbered_item_count"], 2)
        self.assertEqual(res["items"][0]["platform"], "douyin")
        self.assertEqual(res["items"][0]["url"], "https://www.douyin.com/video/7588083083560242482")
        self.assertEqual(res["items"][0]["title"], "抖音书签条目")
        self.assertEqual(res["items"][1]["platform"], "bilibili")
        self.assertEqual(res["items"][1]["url"], "https://www.bilibili.com/video/BV1dKeB6HEvJ")

    def test_feishu_preview_converter_safe_argv_and_skips_non_video_bookmarks(self):
        import feishu_preview_converter as fpc

        sample_xml = (
            '<h2 id="h2_1">板块</h2>'
            '<h4 id="h4_red"><b><span text-color="red">1. 红色图文条目</span></b></h4>'
            '<bookmark id="bm_wx" name="mp.weixin.qq.com" href="https://mp.weixin.qq.com/s/3609l8zUcjLSrGuNslCHpA"></bookmark>'
            '<bookmark id="bm_dy" name="抖音视频" href="https://www.douyin.com/video/7588083083560242482"></bookmark>'
            '<p id="p_bv"><a href="https://www.bilibili.com/video/BV1QgY86XEXs">https://www.bilibili.com/video/BV1QgY86XEXs</a></p>'
        )
        audit = fpc.audit_doc_content(sample_xml)
        self.assertEqual(audit["heading_count"], 2)  # 包含嵌套 <b><span> 的红色标题
        self.assertEqual(audit["bookmark_count"], 2)
        self.assertEqual(audit["skipped_non_video_bookmark_count"], 1)
        self.assertEqual(audit["target_count"], 2)  # 仅转换抖音书签与 B 站蓝链，不破坏微信书签
        target_ids = [t["id"] for t in audit["targets"]]
        self.assertIn("bm_dy", target_ids)
        self.assertIn("p_bv", target_ids)
        self.assertNotIn("bm_wx", target_ids)

        # 验证直接传入带 & 的 URL 时禁用 shell=True、使用 --as user 且清洗为纯 token
        fake_fetch = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"ok": True, "data": {"document": {"content": sample_xml, "revision_id": 3}}}),
            stderr="",
        )
        fake_root = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"ok": True, "data": {"block": {"children": ["bm_dy"]}}}),
            stderr="",
        )
        with mock.patch("subprocess.run", side_effect=[fake_fetch, fake_root]) as m_run:
            info = fpc.get_doc_xml_and_blocks("https://my.feishu.cn/docx/doc_clean123?from=space&tab=1")
        self.assertEqual(info["doc_id"], "doc_clean123")
        for call in m_run.call_args_list:
            cmd = call[0][0]
            self.assertIsInstance(cmd, list)
            self.assertFalse(call[1].get("shell", False))
            self.assertIn("--as", cmd)
            self.assertNotIn("&", " ".join(cmd))

    def test_sync_feishu_sidebar_uses_relative_at_files_and_no_shell(self):
        import sync_feishu_sidebar as sfs

        xml = (
            '<h2 id="h2_1">游戏人生，说地谈天</h2>'
            '<h3 id="h3_item">3. 知行合一</h3>'
        )
        responses = [
            subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {"document": {"content": xml, "revision_id": 1}}}), ""),
            subprocess.CompletedProcess([], 0, json.dumps({"ok": True}), ""),
            subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {"block": {"children": ["h2_1", "h3_item"]}}}), ""),
            subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {"document": {"content": '<h2 id="h2_1">游戏人生，说地谈天</h2><h4 id="h3_item">3. 知行合一</h4>', "revision_id": 2}}}), ""),
            subprocess.CompletedProcess([], 0, json.dumps({"ok": True}), ""),
            subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "data": {"document": {"content": '<h2 id="h2_1">游戏人生，说地谈天</h2><h3 id="h3_sub">热点/认知</h3><h4 id="h3_item"><b><span>3. 知行合一</span></b></h4>'}}}), ""),
        ]
        with mock.patch("subprocess.run", side_effect=responses) as m_run, mock.patch("time.sleep"):
            res = sfs.sync_document_sidebar(
                "https://my.feishu.cn/docx/doc_sb999?from=space&tab=1",
                subsections_config=[("热点/认知", "3. 知行合一")],
            )
        self.assertTrue(res["ok"])
        self.assertEqual(res["doc_id"], "doc_sb999")
        self.assertEqual(res["converted_h4_count"], 1)
        self.assertEqual(res["inserted_h3_count"], 1)
        self.assertEqual(res["total_headings"], 3)
        for call in m_run.call_args_list:
            cmd = call[0][0]
            self.assertIsInstance(cmd, list)
            self.assertFalse(call[1].get("shell", False))
            self.assertIn("--as", cmd)
            for arg in cmd:
                if isinstance(arg, str) and arg.startswith("@"):
                    # 必须为相对文件名，绝不能是绝对路径（lark-cli 会拒收）
                    self.assertFalse(Path(arg[1:]).is_absolute(), f"Absolute @file path rejected by lark-cli: {arg}")
                    self.assertIsNotNone(call[1].get("cwd"))

    def test_bilibili_inpage_js_tombstone_receipt_compat_and_http_412_resilience(self):
        # 1) 验证 HTTP 412 HTML WAF 返回不会抛出 SyntaxError
        b_js_412 = rc.generate_bilibili_inpage_js("水球泡周报视频", "4103329683", ["BV1NEW000002"], delay_ms=5000)
        harness_412 = f"""
        global.setTimeout = (fn) => {{ fn(); return 0; }};
        global.document = {{ cookie: "bili_jct=csrf_ok" }};
        global.fetch = async (url) => {{
          if (url.includes("/x/web-interface/nav")) {{
            return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ isLogin: true, mid: 1, uname: "u" }} }}) }};
          }}
          if (url.includes("/x/v3/fav/resource/list")) {{
            return {{ status: 412, text: async () => "<html><body>412 WAF</body></html>" }};
          }}
          throw new Error("unexpected " + url);
        }};
        (async () => {{
          const r = await ({b_js_412});
          console.log(JSON.stringify(r));
        }})();
        """
        out_412 = _run_node_eval(harness_412)
        self.assertFalse(out_412["ok"])
        self.assertEqual(out_412["error"], "before_snapshot_failed")
        self.assertIn("412", out_412["before_snapshot"]["error"])

        # 2) 验证重复 BVID 去重、单条 -403 私密视频不熔断全批、墓碑快照与回执直接通过 weekly_core 状态机校验
        b_js = rc.generate_bilibili_inpage_js(
            "水球泡周报视频", "4103329683",
            ["BV1PRIV00001", "BV1NEW000002", "BV1NEW000002"],
            delay_ms=5000,
        )
        harness_compat = f"""
        global.setTimeout = (fn) => {{ fn(); return 0; }};
        global.document = {{ cookie: "bili_jct=csrf_ok" }};
        const folderMedias = [{{ id: 88888, bvid: "", attr: 9 }}];
        global.fetch = async (url) => {{
          if (url.includes("/x/web-interface/nav")) {{
            return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ isLogin: true, mid: 3546569697659483, uname: "马" }} }}) }};
          }}
          if (url.includes("/x/v3/fav/resource/list")) {{
            return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ has_more: false, medias: folderMedias }} }}) }};
          }}
          if (url.includes("bvid=BV1PRIV00001")) {{
            return {{ status: 200, text: async () => JSON.stringify({{ code: -403, message: "访问权限不足" }}) }};
          }}
          if (url.includes("bvid=BV1NEW000002")) {{
            return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ aid: 666777, state: 0, videos: 1 }} }}) }};
          }}
          if (url.includes("/x/v3/fav/resource/deal")) {{
            folderMedias.push({{ id: 666777, bvid: "BV1NEW000002" }});
            return {{ status: 200, text: async () => JSON.stringify({{ code: 0, message: "0" }}) }};
          }}
          throw new Error("unexpected " + url);
        }};
        (async () => {{
          const r = await ({b_js});
          console.log(JSON.stringify(r));
        }})();
        """
        out = _run_node_eval(harness_compat)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["dead_links"]), 1)
        self.assertEqual(out["dead_links"][0]["id"], "BV1PRIV00001")
        ack_receipts = [r for r in out["receipts"] if r["id"] == "BV1NEW000002"]
        self.assertEqual(len(ack_receipts), 1)
        self.assertEqual(ack_receipts[0]["codes"], {"favorite": 0})

        # 将真实输出喂给 weekly_core.parse_snapshot 与 record_real_receipts_to_state
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            ev = run_dir / "evidence"
            ev.mkdir(parents=True)
            for nm in ("source.json", "detail.json", "discovery.json", "pages.json"):
                w.write_json(ev / nm, {"ok": True})
            t0 = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            t1 = (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat()
            t2 = (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat()
            seed = {
                "title": "Compat Bili Test", "source_week": "20261122",
                "numbered_item_count": 1, "quick_news_count": 0,
                "items": [{"key": "b1", "title": "新视频", "section": "视频", "number": 1, "url": "https://www.bilibili.com/video/BV1NEW000002"}],
            }
            st = w.initialize(seed, mode="collect")
            st = w.apply_events(st, run_dir, [
                {"type": "permissions", "publish": False, "collect": ["bilibili"], "user_request": "t"},
                {"type": "register_source", "source_id": "src", "kind": "feishu", "document_id": "d1", "revision_id": 1, "checked_at": t0, "evidence_files": ["evidence/source.json"]},
                {"type": "target", "platform": "bilibili", "authenticated": True, "account_id": "3546569697659483", "complete": True, "checked_at": t0, "desired_name": "水球泡周报视频", "folders": [{"name": "水球泡周报视频", "id": "4103329683"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
                {"type": "source", "key": "b1", "source_id": "src", "item_title": "新视频", "ownership": "same_item", "revision_id": 1, "observed_url": "https://www.bilibili.com/video/BV1NEW000002", "author": "a", "evidence_files": ["evidence/source.json"]},
                {"type": "verify", "key": "b1", "url": "https://www.bilibili.com/video/BV1NEW000002", "observed_title": "新视频", "observed_author": "a",
                 "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "ok"},
                 "availability": {"status": "available", "checked_at": t0, "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": "BV1NEW000002"},
                 "evidence_files": ["evidence/detail.json"]},
                {"type": "inventory", "expected_numbered": 1, "expected_quick_news": 0, "all_source_parts_reviewed": True, "layout_reviewed": True, "evidence_files": ["evidence/source.json"]},
            ], t0)
            before_snap = dict(out["snapshots"]["before"], evidence_files=["evidence/pages.json"], started_at=t1, finished_at=t2)
            parsed = w.import_snapshot(st, run_dir, before_snap, t2)
            self.assertEqual(parsed["ids"], [])  # 墓碑条目不报错且不混入有效 ID
            plan = w.plan_collection(st, run_dir, "bilibili", t2)
            rc_apply = rc.record_real_receipts_to_state(run_dir, st, "bilibili", ack_receipts, plan["plan_id"], at=(datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat())
            self.assertEqual(rc_apply["applied_count"], 1)

    def test_douyin_svg_without_click_method_and_note_preservation_in_collect_plan(self):
        vid_note = "7695302751908478458"
        d_js = rc.generate_douyin_inpage_helper_js("水球泡视频周报", "7672986870204225339", [f"https://www.douyin.com/note/{vid_note}"])
        harness = f"""
        let parentClicked = 0;
        let collectState = "note-is-collected";
        const svgNoClick = {{}};  // 模拟真实 Chromium 中无 .click() 方法的 SVGElement
        const fakeCollectEl = {{
          getAttribute: (k) => k === "data-e2e-state" ? collectState : null,
          getBoundingClientRect: () => ({{ left: 10, top: 20, width: 30, height: 30 }}),
          querySelector: (sel) => sel === "svg" ? svgNoClick : null,
          click: () => {{ parentClicked++; }},
        }};
        global.window = {{ innerHeight: 900 }};
        global.location = {{ href: "https://www.douyin.com/note/{vid_note}" }};
        global.document = {{
          querySelector: (sel) => sel.includes("video-player-collect") ? fakeCollectEl : null,
          querySelectorAll: () => [],
        }};
        ({d_js});
        const h = window.__weeklyDouyinHelper;
        const r1 = h.clickCollectIfUncollected();
        collectState = "video-player-no-collected";
        const r2 = h.clickCollectIfUncollected();
        console.log(JSON.stringify({{ r1, r2, parentClicked }}));
        """
        out = _run_node_eval(harness)
        self.assertEqual(out["r1"]["action"], "skipped_already_collected")
        self.assertTrue(out["r2"]["ok"])
        self.assertEqual(out["r2"]["action"], "clicked_collect")
        self.assertEqual(out["r2"]["click_mode"], "parent_dom_click")
        self.assertEqual(out["parentClicked"], 1)

        # 验证 weekly.py collect-plan 保留 /note/ URL 与单平台支持
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            ev = run_dir / "evidence"
            ev.mkdir(parents=True)
            for nm in ("source.json", "detail.json", "discovery.json", "pages.json"):
                w.write_json(ev / nm, {"ok": True})
            t0 = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
            t1 = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
            t2 = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
            note_url = f"https://www.douyin.com/note/{vid_note}"
            seed = {
                "title": "Douyin Note Preserve Test", "source_week": "20261122",
                "numbered_item_count": 1, "quick_news_count": 0,
                "items": [{"key": "n1", "title": "图文笔记", "section": "视频", "number": 1, "url": note_url}],
            }
            st = w.initialize(seed, mode="collect")
            st = w.apply_events(st, run_dir, [
                {"type": "permissions", "publish": False, "collect": ["douyin"], "user_request": "t"},
                {"type": "register_source", "source_id": "src", "kind": "feishu", "document_id": "d1", "revision_id": 1, "checked_at": t0, "evidence_files": ["evidence/source.json"]},
                {"type": "target", "platform": "douyin", "authenticated": True, "account_id": "acc_dy", "complete": True, "checked_at": t0, "desired_name": "水球泡视频周报", "folders": [{"name": "水球泡视频周报", "collects_id_str": "7672986870204225339"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
                {"type": "source", "key": "n1", "source_id": "src", "item_title": "图文笔记", "ownership": "same_item", "revision_id": 1, "observed_url": note_url, "author": "a", "evidence_files": ["evidence/source.json"]},
                {"type": "verify", "key": "n1", "url": note_url, "observed_title": "图文笔记", "observed_author": "a",
                 "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "ok"},
                 "availability": {"status": "available", "checked_at": t0, "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": vid_note},
                 "evidence_files": ["evidence/detail.json"]},
                {"type": "inventory", "expected_numbered": 1, "expected_quick_news": 0, "all_source_parts_reviewed": True, "layout_reviewed": True, "evidence_files": ["evidence/source.json"]},
            ], t0)
            w.import_snapshot(st, run_dir, {
                "snapshot_id": "init_dy", "platform": "douyin", "account_id": "acc_dy",
                "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                "started_at": t1, "finished_at": t2, "method": "api", "evidence_files": ["evidence/pages.json"],
                "pages": [{"business_code": 0, "request_cursor": "0", "has_more": False, "next_cursor": None, "items": []}],
            }, t2)
            w.write_json(run_dir / "run.json", st)

            rc_code = weekly.main(["collect-plan", "--run", str(run_dir), "--runtime", "antigravity", "--no-default-watched"])
            self.assertEqual(rc_code, 0)
            dy_js_txt = (ev / "collect_douyin_inpage_helper.js").read_text(encoding="utf-8")
            dy_cdp_txt = (ev / "collect_douyin_cdp.py").read_text(encoding="utf-8")
            self.assertIn(f"/note/{vid_note}", dy_js_txt)
            self.assertNotIn(f"/video/{vid_note}", dy_js_txt)
            self.assertIn(f"/note/{vid_note}", dy_cdp_txt)
            # 验证 plan_id 已持久化进 run.json
            saved_st = w.read_json(run_dir / "run.json")
            self.assertIn("douyin", saved_st["plans"])

    def test_cdp_scripts_strict_hostname_matching(self):
        b_code = rc.generate_playwright_cdp_collector("bilibili", "夹", "4103329683", ["BV1QgY86XEXs"])
        d_code = rc.generate_playwright_cdp_collector("douyin", "夹", "7672986870204225339", ["7689404171926108874"])
        ns_b: dict = {}
        ns_d: dict = {}
        exec(compile(b_code, "b_cdp.py", "exec"), ns_b)
        exec(compile(d_code, "d_cdp.py", "exec"), ns_d)
        self.assertTrue(ns_b["_is_bilibili_page"]("https://space.bilibili.com/3546569697659483"))
        self.assertFalse(ns_b["_is_bilibili_page"]("https://www.google.com/search?q=bilibili.com"))
        self.assertTrue(ns_d["_is_douyin_page"]("https://www.douyin.com/user/self"))
        self.assertFalse(ns_d["_is_douyin_page"]("https://evil.example.com/douyin.com"))

    def test_bilibili_inpage_snapshot_has_evidence_files_for_direct_import(self):
        b_js = rc.generate_bilibili_inpage_js("水球泡周报视频", "4103329683", ["BV1NEW000002"], delay_ms=5000)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 20000
        harness = f"""
        let fakeMs = {now_ms};
        const RealDate = Date;
        class FakeDate extends RealDate {{
          constructor(...args) {{ super(...(args.length ? args : [fakeMs])); }}
          static now() {{ return fakeMs; }}
        }}
        global.Date = FakeDate;
        global.setTimeout = (fn, ms) => {{ fakeMs += Math.max(ms || 0, 100); fn(); return 0; }};
        global.document = {{ cookie: "bili_jct=csrf_ok" }};
        const folderMedias = [];
        global.fetch = async (url) => {{
          fakeMs += 100;
          if (url.includes("/x/web-interface/nav")) return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ isLogin: true, mid: 3546569697659483, uname: "马" }} }}) }};
          if (url.includes("/x/v3/fav/resource/list")) return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ has_more: false, medias: folderMedias }} }}) }};
          if (url.includes("bvid=BV1NEW000002")) return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ aid: 123456, state: 0, videos: 1 }} }}) }};
          if (url.includes("/x/v3/fav/resource/deal")) {{
            folderMedias.push({{ id: 123456, bvid: "BV1NEW000002" }});
            return {{ status: 200, text: async () => JSON.stringify({{ code: 0, message: "0" }}) }};
          }}
          throw new Error("unexpected " + url);
        }};
        (async () => {{
          const r = await ({b_js});
          console.log(JSON.stringify(r));
        }})();
        """
        out = _run_node_eval(harness)
        self.assertTrue(out["ok"])
        for label in ("before", "round1", "round2"):
            self.assertEqual(out["snapshots"][label]["evidence_files"], [f"evidence/bilibili_snapshot_{label}.json"])

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            ev = run_dir / "evidence"
            ev.mkdir(parents=True)
            for nm in ("source.json", "detail.json", "discovery.json"):
                w.write_json(ev / nm, {"ok": True})
            t0 = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            seed = {
                "title": "Direct Snap Import Test", "source_week": "20261122",
                "numbered_item_count": 1, "quick_news_count": 0,
                "items": [{"key": "b1", "title": "视频1", "section": "视频", "number": 1, "url": "https://www.bilibili.com/video/BV1NEW000002"}],
            }
            st = w.initialize(seed, mode="collect")
            st = w.apply_events(st, run_dir, [
                {"type": "permissions", "publish": False, "collect": ["bilibili"], "user_request": "t"},
                {"type": "register_source", "source_id": "src", "kind": "feishu", "document_id": "d1", "revision_id": 1, "checked_at": t0, "evidence_files": ["evidence/source.json"]},
                {"type": "target", "platform": "bilibili", "authenticated": True, "account_id": "3546569697659483", "complete": True, "checked_at": t0, "desired_name": "水球泡周报视频", "folders": [{"name": "水球泡周报视频", "id": "4103329683"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
                {"type": "source", "key": "b1", "source_id": "src", "item_title": "视频1", "ownership": "same_item", "revision_id": 1, "observed_url": "https://www.bilibili.com/video/BV1NEW000002", "author": "a", "evidence_files": ["evidence/source.json"]},
                {"type": "verify", "key": "b1", "url": "https://www.bilibili.com/video/BV1NEW000002", "observed_title": "视频1", "observed_author": "a",
                 "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "ok"},
                 "availability": {"status": "available", "checked_at": t0, "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": "BV1NEW000002"},
                 "evidence_files": ["evidence/detail.json"]},
                {"type": "inventory", "expected_numbered": 1, "expected_quick_news": 0, "all_source_parts_reviewed": True, "layout_reviewed": True, "evidence_files": ["evidence/source.json"]},
            ], t0)
            # 直接把 JS 输出的 snapshots.before 写入其自带 evidence_files 指向的路径，不改写任何字段即可通过 weekly_core.import_snapshot
            before_snap = out["snapshots"]["before"]
            w.write_json(run_dir / before_snap["evidence_files"][0], before_snap)
            parsed = w.import_snapshot(st, run_dir, before_snap)
            self.assertEqual(parsed["snapshot_id"], before_snap["snapshot_id"])

    def test_end_to_end_collect_mode_with_dead_already_and_watched_exclusion(self):
        """全链路集成测试：extract-feishu -> init --mode collect -> apply -> collect-plan --runtime antigravity
        -> Node.js 真实执行双平台脚本 -> import-collection（B站含失效/已收藏/新收藏，抖音含已观看排除与/note/）
        -> reconcile -> finish（仅写入临时目录 catalog.sqlite，绝不污染真实总库）。
        """
        bv_dead = "BV1DEAD00001"
        bv_already = "BV1ALRD00002"
        bv_new = "BV1NEW000003"
        dy_video = "7689404171926108874"
        dy_note = "7695302751908478458"
        dy_watched = "7651922649496593073"

        feishu_xml = (
            "<title>水球泡周报 Vol.303 集成回归</title>"
            "<h2>游戏人生，说地谈天</h2>"
            "<h3>热点/认知</h3>"
            f"<h4>1. B站失效稿件</h4><bookmark id='bm1' name='B站失效' href='https://www.bilibili.com/video/{bv_dead}'></bookmark>"
            f"<h4>2. B站幂等已收藏</h4><p><a href='https://www.bilibili.com/video/{bv_already}'>https://www.bilibili.com/video/{bv_already}</a></p>"
            f"<h4>3. B站新增收藏</h4><readonly-block id='rb1' href='https://player.bilibili.com/player.html?bvid=bv{bv_new[2:]}' type='iframe'></readonly-block>"
            "<h4>4. 微信图文不纳入视频收藏</h4><bookmark id='bm_wx' name='mp.weixin.qq.com' href='https://mp.weixin.qq.com/s/test123'></bookmark>"
            "<h3>轻松/日常</h3>"
            f"<h4>5. 抖音未看视频</h4><bookmark id='bm2' name='抖音视频' href='https://www.douyin.com/video/{dy_video}'></bookmark>"
            f"<h4>6. 抖音未看图文</h4><p><a href='https://www.douyin.com/note/{dy_note}'>https://www.douyin.com/note/{dy_note}</a></p>"
            f"<h4>7. 抖音已看黑名单</h4><bookmark id='bm3' name='抖音已看' href='https://www.douyin.com/video/{dy_watched}'></bookmark>"
        )
        fake_feishu_payload = {
            "ok": True,
            "identity": "user",
            "data": {"document": {"document_id": "doc_e2e_303", "revision_id": 5, "content": feishu_xml}},
        }

        with tempfile.TemporaryDirectory(prefix="weekly-e2e-collect-") as td:
            td_p = Path(td)
            seed_path = td_p / "video-seed.json"
            run_dir = td_p / "run"
            ev_dir = run_dir / "evidence"
            watched_file = td_p / "douyin_watched_vids.json"
            watched_file.write_text(json.dumps([dy_watched]), encoding="utf-8")
            temp_catalog = td_p / "isolated-library" / "catalog.sqlite"

            # 1) extract-feishu（使用带 & 查询参数的 URL 验证清洗逻辑）
            with mock.patch("subprocess.check_output", return_value=json.dumps(fake_feishu_payload, ensure_ascii=False)):
                self.assertEqual(
                    weekly.main(["extract-feishu", "--doc", "https://my.feishu.cn/docx/doc_e2e_303?from=space&tab=1", "--out", str(seed_path)]),
                    0,
                )
            seed_data = w.read_json(seed_path)
            self.assertEqual(seed_data["numbered_item_count"], 6)

            # 2) init --mode collect
            self.assertEqual(weekly.main(["init", "--run", str(run_dir), "--manifest", str(seed_path), "--mode", "collect"]), 0)

            # 3) 写入完整权限 / 飞书来源 / 双平台目标 / 核验 / 清单复核事件
            base_dt = datetime.now(timezone.utc) - timedelta(seconds=120)
            t0 = base_dt.isoformat()
            t_init_start = (base_dt + timedelta(seconds=2)).isoformat()
            t_init_end = (base_dt + timedelta(seconds=4)).isoformat()

            w.write_json(ev_dir / "feishu_source.json", fake_feishu_payload)
            w.write_json(ev_dir / "detail_check.json", {"checked_at": t0, "ok": True})
            w.write_json(ev_dir / "target_discovery.json", {"checked_at": t0, "ok": True})
            w.write_json(ev_dir / "init_pages.json", {"checked_at": t_init_end, "ok": True})

            events = [
                {"type": "permissions", "publish": False, "collect": ["bilibili", "douyin"], "user_request": "端到端回归测试收藏"},
                {"type": "register_source", "source_id": "src_feishu_303", "kind": "feishu", "document_id": "doc_e2e_303", "revision_id": 5, "checked_at": t0, "evidence_files": ["evidence/feishu_source.json"]},
                {"type": "target", "platform": "bilibili", "authenticated": True, "account_id": "3546569697659483", "complete": True, "checked_at": t0, "desired_name": "水球泡周报视频", "folders": [{"name": "水球泡周报视频", "id": "4103329683"}], "allow_empty_initial": True, "evidence_files": ["evidence/target_discovery.json"]},
                {"type": "target", "platform": "douyin", "authenticated": True, "account_id": "acc_dy_self", "complete": True, "checked_at": t0, "desired_name": "水球泡视频周报", "folders": [{"name": "水球泡视频周报", "collects_id_str": "7672986870204225339"}], "allow_empty_initial": True, "evidence_files": ["evidence/target_discovery.json"]},
            ]
            for it in seed_data["items"]:
                norm = w.normalize_url(it["url"])
                events.append({
                    "type": "source", "key": it["key"], "source_id": "src_feishu_303", "item_title": it["title"],
                    "ownership": "same_item", "revision_id": 5, "observed_url": norm["url"], "author": "原作者",
                    "evidence_files": ["evidence/feishu_source.json"],
                })
                events.append({
                    "type": "verify", "key": it["key"], "url": norm["url"], "observed_title": it["title"], "observed_author": "原作者",
                    "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "feishu_doc_verified"},
                    "availability": {"status": "available", "checked_at": t0, "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": norm["id"]},
                    "evidence_files": ["evidence/detail_check.json"],
                })
            events.append({
                "type": "inventory", "expected_numbered": 6, "expected_quick_news": 0,
                "all_source_parts_reviewed": True, "layout_reviewed": True,
                "evidence_files": ["evidence/feishu_source.json"],
            })
            events_path = ev_dir / "collect_events.json"
            w.write_json(events_path, events)
            self.assertEqual(weekly.main(["apply", "--run", str(run_dir), "--input", str(events_path)]), 0)

            # 4) 导入双平台写前初始快照
            for plat, acc, fid, fname in [
                ("bilibili", "3546569697659483", "4103329683", "水球泡周报视频"),
                ("douyin", "acc_dy_self", "7672986870204225339", "水球泡视频周报"),
            ]:
                init_snap_path = ev_dir / f"init_snap_{plat}.json"
                w.write_json(init_snap_path, {
                    "snapshot_id": f"init_{plat}_001", "platform": plat, "account_id": acc,
                    "folder_id": fid, "folder_name": fname, "started_at": t_init_start, "finished_at": t_init_end,
                    "method": "api", "evidence_files": ["evidence/init_pages.json"],
                    "pages": [{"business_code": 0, "request_cursor": "0", "has_more": False, "next_cursor": None, "items": []}],
                })
                self.assertEqual(weekly.main(["snapshot", "--run", str(run_dir), "--input", str(init_snap_path)]), 0)

            # 5) collect-plan --runtime antigravity --watched-file（模拟在浏览器执行前 4 秒生成计划）
            t_plan = (base_dt + timedelta(seconds=6)).isoformat()
            with mock.patch.object(w, "now", return_value=t_plan):
                self.assertEqual(
                    weekly.main(["collect-plan", "--run", str(run_dir), "--runtime", "antigravity", "--watched-file", str(watched_file)]),
                    0,
                )
            bili_js_path = ev_dir / "collect_bilibili_inpage.js"
            dy_js_path = ev_dir / "collect_douyin_inpage_helper.js"
            self.assertTrue(bili_js_path.is_file())
            self.assertTrue(dy_js_path.is_file())
            self.assertTrue((ev_dir / "douyin_watched_filtering.json").is_file())
            dy_js_txt = dy_js_path.read_text(encoding="utf-8")
            self.assertIn(f"/video/{dy_video}", dy_js_txt)
            self.assertIn(f"/note/{dy_note}", dy_js_txt)
            self.assertNotIn(dy_watched, dy_js_txt)

            # 6) Node.js 真实执行生成的 collect_bilibili_inpage.js（含失效 -404 + 已收藏 11201 + 新增 0）
            bili_start_ms = int((base_dt + timedelta(seconds=10)).timestamp() * 1000)
            bili_harness = f"""
            let fakeMs = {bili_start_ms};
            const RealDate = Date;
            class FakeDate extends RealDate {{
              constructor(...args) {{ super(...(args.length ? args : [fakeMs])); }}
              static now() {{ return fakeMs; }}
            }}
            global.Date = FakeDate;
            global.setTimeout = (fn, ms) => {{ fakeMs += Math.max(ms || 0, 100); fn(); return 0; }};
            global.document = {{ cookie: "bili_jct=csrf_e2e_token" }};
            const folderMedias = [];
            let listCall = 0;
            global.fetch = async (url, opts) => {{
              fakeMs += 100;
              if (url.includes("/x/web-interface/nav")) {{
                return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ isLogin: true, mid: 3546569697659483, uname: "马" }} }}) }};
              }}
              if (url.includes("/x/v3/fav/resource/list")) {{
                listCall += 1;
                return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ has_more: false, medias: folderMedias }} }}) }};
              }}
              if (url.includes("bvid={bv_dead}")) {{
                return {{ status: 200, text: async () => JSON.stringify({{ code: -404, message: "啥都木有" }}) }};
              }}
              if (url.includes("bvid={bv_already}")) {{
                return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ aid: 222002, state: 0, videos: 1 }} }}) }};
              }}
              if (url.includes("bvid={bv_new}")) {{
                return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ aid: 333003, state: 0, videos: 1 }} }}) }};
              }}
              if (url.includes("/x/v3/fav/resource/deal")) {{
                const body = String(opts.body || "");
                if (body.includes("rid=222002")) {{
                  folderMedias.push({{ id: 222002, bvid: "{bv_already}" }});
                  return {{ status: 200, text: async () => JSON.stringify({{ code: 11201, message: "已经收藏过了" }}) }};
                }}
                if (body.includes("rid=333003")) {{
                  folderMedias.push({{ id: 333003, bvid: "{bv_new}" }});
                  return {{ status: 200, text: async () => JSON.stringify({{ code: 0, message: "0" }}) }};
                }}
              }}
              throw new Error("unexpected url: " + url);
            }};
            (async () => {{
              const r = await ({bili_js_path.read_text(encoding="utf-8")});
              console.log(JSON.stringify(r));
            }})();
            """
            bili_res = _run_node_eval(bili_harness)
            self.assertTrue(bili_res["ok"])
            self.assertEqual([d["id"] for d in bili_res["dead_links"]], [bv_dead])
            bili_res_path = ev_dir / "bilibili_collection_result.json"
            w.write_json(bili_res_path, bili_res)
            self.assertEqual(
                weekly.main(["import-collection", "--run", str(run_dir), "--platform", "bilibili", "--input", str(bili_res_path)]),
                0,
            )

            # 7) Node.js 真实执行生成的 collect_douyin_inpage_helper.js（两阶段 + buildUiSnapshot 两轮回读）
            dy_start_ms = int((base_dt + timedelta(seconds=10)).timestamp() * 1000)
            dy_harness = f"""
            let fakeMs = {dy_start_ms};
            const RealDate = Date;
            class FakeDate extends RealDate {{
              constructor(...args) {{ super(...(args.length ? args : [fakeMs])); }}
              static now() {{ return fakeMs; }}
            }}
            global.Date = FakeDate;
            let collectState = "video-player-no-collected";
            const fakeBtn = {{
              getAttribute: (k) => k === "data-e2e-state" ? collectState : null,
              getBoundingClientRect: () => ({{ left: 10, top: 20, width: 30, height: 30 }}),
              querySelector: () => ({{ click: () => {{}} }}),
              click: () => {{}},
            }};
            global.window = {{ innerHeight: 900 }};
            global.location = {{ href: "https://www.douyin.com/user/self?showSubTab=favorite_folder&showTab=favorite_collection" }};
            global.document = {{
              body: {{ innerText: "水球泡视频周报\\n共 197 作品\\n暂时没有更多了" }},
              querySelector: (sel) => sel.includes("video-player-collect") ? fakeBtn : {{}},
              querySelectorAll: (sel) => {{
                if (sel.includes("/video/")) {{
                  return [
                    {{ href: "https://www.douyin.com/video/{dy_video}" }},
                    {{ href: "https://www.douyin.com/note/{dy_note}" }},
                  ];
                }}
                return [];
              }},
            }};
            ({dy_js_txt});
            const h = window.__weeklyDouyinHelper;
            const receipts = [];
            for (const item of h.config.items) {{
              fakeMs += 6000;
              collectState = "video-player-no-collected";
              const act = h.clickCollectIfUncollected();
              receipts.push({{ id: item.id, kind: item.kind, url: item.url, at: new Date().toISOString(), stage1: act }});
            }}
            fakeMs += 4000;
            const stage2 = {{
              confirmed: true,
              confirmed_at: new Date().toISOString(),
              folder_count_before: 195,
              folder_count_after: 197,
              reopened_detail: true,
            }};
            fakeMs += 3000;
            const round1 = h.buildUiSnapshot("round1", {{ accountId: "acc_dy_self" }});
            fakeMs += 6000;
            const round2 = h.buildUiSnapshot("round2", {{ accountId: "acc_dy_self" }});
            console.log(JSON.stringify({{
              ok: true,
              platform: "douyin",
              account_id: "acc_dy_self",
              folder_id: "7672986870204225339",
              folder_name: "水球泡视频周报",
              receipts,
              stage2,
              snapshots: {{ round1, round2 }},
            }}));
            """
            dy_res = _run_node_eval(dy_harness)
            dy_res_path = ev_dir / "douyin_collection_result.json"
            w.write_json(dy_res_path, dy_res)
            self.assertEqual(
                weekly.main(["import-collection", "--run", str(run_dir), "--platform", "douyin", "--input", str(dy_res_path)]),
                0,
            )

            # 8) 显式执行双平台 reconcile 门禁
            self.assertEqual(weekly.main(["reconcile", "--run", str(run_dir), "--platform", "bilibili"]), 0)
            self.assertEqual(weekly.main(["reconcile", "--run", str(run_dir), "--platform", "douyin"]), 0)
            bili_rec = w.read_json(run_dir / "audit" / "bilibili-reconcile.json")
            dy_rec = w.read_json(run_dir / "audit" / "douyin-reconcile.json")
            self.assertTrue(bili_rec["ok"])
            self.assertEqual(bili_rec["target_count"], 2)
            self.assertEqual(bili_rec["missing"], {"round1": [], "round2": []})
            self.assertEqual(bili_rec["excluded"]["dead_link_confirmed"], [bv_dead])
            self.assertTrue(dy_rec["ok"])
            self.assertEqual(dy_rec["target_count"], 2)
            self.assertEqual(dy_rec["missing"], {"round1": [], "round2": []})
            self.assertEqual(dy_rec["excluded"]["watched_excluded"], [dy_watched])

            # 9) finish 并归档到临时 catalog.sqlite，核验绝不把排除项伪造为已收藏
            self.assertEqual(weekly.main(["finish", "--run", str(run_dir), "--catalog", str(temp_catalog)]), 0)
            delivery = w.read_json(run_dir / "audit" / "delivery.json")
            self.assertEqual(delivery["status"], "complete")
            self.assertTrue(temp_catalog.is_file())
            lib_rows = {r["id"]: r for r in w.read_json(run_dir / "library.json")}
            self.assertEqual(lib_rows[bv_new]["collection"], "confirmed")
            self.assertEqual(lib_rows[bv_already]["collection"], "confirmed")
            self.assertEqual(lib_rows[bv_dead]["collection"], "dead_link_confirmed")
            self.assertEqual(lib_rows[dy_video]["collection"], "confirmed")
            self.assertEqual(lib_rows[dy_note]["collection"], "confirmed")
            self.assertEqual(lib_rows[dy_watched]["collection"], "excluded_watched")
            index_html = (run_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("失效稿件（已存证未收藏）", index_html)
            self.assertIn("已观看排除（未收藏）", index_html)
            self.assertEqual(weekly.main(["search", "--catalog", str(temp_catalog), "--query", bv_new]), 0)

    def test_batch_receipt_dead_already_vs_real_failure_and_exclusion_gates(self):
        """验证：dead_link_confirmed 与 already_favorited 不误中断批次，但真实失败/风控/伪造排除/篡改证据仍被严格拦截。"""
        bv_dead = "BV1DEAD00001"
        bv_already = "BV1ALRD00002"
        bv_ok = "BV1GOOD00003"
        dy_1 = "7689404171926108874"
        dy_2 = "7695302751908478458"

        def make_run(td_path: Path):
            run_dir = td_path / "run"
            ev = run_dir / "evidence"
            ev.mkdir(parents=True)
            for nm in ("source.json", "detail.json", "discovery.json", "pages.json"):
                w.write_json(ev / nm, {"ok": True})
            base = datetime.now(timezone.utc) - timedelta(seconds=90)
            t0 = base.isoformat()
            t1 = (base + timedelta(seconds=2)).isoformat()
            t2 = (base + timedelta(seconds=4)).isoformat()
            seed = {
                "title": "Gate Test", "source_week": "20261122",
                "numbered_item_count": 5, "quick_news_count": 0,
                "items": [
                    {"key": "b1", "title": "B死链", "section": "视频", "number": 1, "url": f"https://www.bilibili.com/video/{bv_dead}"},
                    {"key": "b2", "title": "B已藏", "section": "视频", "number": 2, "url": f"https://www.bilibili.com/video/{bv_already}"},
                    {"key": "b3", "title": "B新藏", "section": "视频", "number": 3, "url": f"https://www.bilibili.com/video/{bv_ok}"},
                    {"key": "d1", "title": "D一", "section": "视频", "number": 4, "url": f"https://www.douyin.com/video/{dy_1}"},
                    {"key": "d2", "title": "D二", "section": "视频", "number": 5, "url": f"https://www.douyin.com/video/{dy_2}"},
                ],
            }
            st = w.initialize(seed, mode="collect")
            events = [
                {"type": "permissions", "publish": False, "collect": ["bilibili", "douyin"], "user_request": "t"},
                {"type": "register_source", "source_id": "src", "kind": "feishu", "document_id": "d1", "revision_id": 1, "checked_at": t0, "evidence_files": ["evidence/source.json"]},
                {"type": "target", "platform": "bilibili", "authenticated": True, "account_id": "acc_bv", "complete": True, "checked_at": t0, "desired_name": "水球泡周报视频", "folders": [{"name": "水球泡周报视频", "id": "4103329683"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
                {"type": "target", "platform": "douyin", "authenticated": True, "account_id": "acc_dy", "complete": True, "checked_at": t0, "desired_name": "水球泡视频周报", "folders": [{"name": "水球泡视频周报", "collects_id_str": "7672986870204225339"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
            ]
            for it in st["items"]:
                norm = w.normalize_url(it["original_url"])
                events.append({"type": "source", "key": it["key"], "source_id": "src", "item_title": it["title"], "ownership": "same_item", "revision_id": 1, "observed_url": norm["url"], "author": "a", "evidence_files": ["evidence/source.json"]})
                events.append({
                    "type": "verify", "key": it["key"], "url": norm["url"], "observed_title": it["title"], "observed_author": "a",
                    "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "ok"},
                    "availability": {"status": "available", "checked_at": t0, "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": norm["id"]},
                    "evidence_files": ["evidence/detail.json"],
                })
            events.append({"type": "inventory", "expected_numbered": 5, "expected_quick_news": 0, "all_source_parts_reviewed": True, "layout_reviewed": True, "evidence_files": ["evidence/source.json"]})
            st = w.apply_events(st, run_dir, events, t0)
            for plat, acc, fid, fname in [
                ("bilibili", "acc_bv", "4103329683", "水球泡周报视频"),
                ("douyin", "acc_dy", "7672986870204225339", "水球泡视频周报"),
            ]:
                w.import_snapshot(st, run_dir, {
                    "snapshot_id": f"init_{plat}", "platform": plat, "account_id": acc, "folder_id": fid, "folder_name": fname,
                    "started_at": t1, "finished_at": t2, "method": "api", "evidence_files": ["evidence/pages.json"],
                    "pages": [{"business_code": 0, "request_cursor": "0", "has_more": False, "next_cursor": None, "items": []}],
                }, t2)
            return run_dir, st, base

        # 1) 混合批次 dead_link_confirmed + already_favorited + acknowledged 正常完成，且 already_favorited 若未出现在回读里仍判失败
        with tempfile.TemporaryDirectory() as td:
            run_dir, st, base = make_run(Path(td))
            plan = w.plan_collection(st, run_dir, "bilibili", (base + timedelta(seconds=6)).isoformat())
            res = rc.record_real_receipts_to_state(
                run_dir, st, "bilibili",
                [
                    {"id": bv_dead, "outcome": "dead_link_confirmed", "code": -404, "detail": {"message": "啥都木有"}, "at": (base + timedelta(seconds=10)).isoformat()},
                    {"id": bv_already, "outcome": "already_favorited", "code": 11201, "at": (base + timedelta(seconds=16)).isoformat()},
                    {"id": bv_ok, "outcome": "acknowledged", "code": 0, "codes": {"favorite": 0}, "at": (base + timedelta(seconds=22)).isoformat()},
                ],
                plan["plan_id"],
            )
            self.assertEqual(res["dead_link_count"], 1)
            self.assertEqual(res["already_favorited_count"], 1)
            self.assertEqual(res["applied_count"], 2)
            # 若回读中缺少 already_favorited 条目，reconcile 必须诚实返回 ok=False
            for idx, off in ((1, 30), (2, 40)):
                w.import_snapshot(st, run_dir, {
                    "snapshot_id": f"rb_{idx}", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频",
                    "started_at": (base + timedelta(seconds=off)).isoformat(), "finished_at": (base + timedelta(seconds=off + 2)).isoformat(),
                    "method": "api", "evidence_files": ["evidence/pages.json"],
                    "pages": [{"business_code": 0, "request_cursor": "0", "has_more": False, "next_cursor": None, "items": [{"id": bv_ok}]}],
                })
            rec = w.reconcile(st, run_dir, "bilibili")
            self.assertFalse(rec["ok"])
            self.assertEqual(rec["missing"]["round1"], [bv_already])

        # 2) 真实失败（如 -412）后面若继续写下一条，仍必须触发 unreconciled_write_must_stop_batch
        with tempfile.TemporaryDirectory() as td:
            run_dir, st, base = make_run(Path(td))
            plan = w.plan_collection(st, run_dir, "bilibili", (base + timedelta(seconds=6)).isoformat())
            with self.assertRaisesRegex(w.GateError, "unreconciled_write_must_stop_batch"):
                rc.record_real_receipts_to_state(
                    run_dir, st, "bilibili",
                    [
                        {"id": bv_dead, "outcome": "failed", "code": -412, "at": (base + timedelta(seconds=10)).isoformat()},
                        {"id": bv_ok, "outcome": "acknowledged", "code": 0, "codes": {"favorite": 0}, "at": (base + timedelta(seconds=16)).isoformat()},
                    ],
                    plan["plan_id"],
                )

        # 3) 防伪造排除门禁：正常状态/风控码冒充死链、证据不含 ID 或篡改证据必须拒绝
        with tempfile.TemporaryDirectory() as td:
            run_dir, st, base = make_run(Path(td))
            ev = run_dir / "evidence"
            w.write_json(ev / "fake_dead.json", {"code": 0})
            with self.assertRaisesRegex(w.GateError, "bilibili_dead_link_not_confirmed_by_official_status"):
                w.record_collection_exclusion(st, run_dir, "bilibili", bv_ok, "dead_link_confirmed", {"code": 0, "state": 0, "videos": 1}, ["evidence/fake_dead.json"])
            with self.assertRaisesRegex(w.GateError, "risk_control_is_not_dead_link"):
                w.record_collection_exclusion(st, run_dir, "bilibili", bv_ok, "dead_link_confirmed", {"code": -412, "message": "风控"}, ["evidence/fake_dead.json"])
            with self.assertRaisesRegex(w.GateError, "watched_exclusion_only_supported_for_douyin"):
                w.record_collection_exclusion(st, run_dir, "bilibili", bv_ok, "watched_excluded", {"source": "x"}, ["evidence/fake_dead.json"])
            w.write_json(ev / "empty_watched.json", {"watched_excluded_ids": []})
            with self.assertRaisesRegex(w.GateError, "watched_exclusion_id_not_present_in_evidence"):
                w.record_collection_exclusion(st, run_dir, "douyin", dy_1, "watched_excluded", {"source": "empty"}, ["evidence/empty_watched.json"])

            # 合法登记后若篡改证据文件，reconcile 必须报错 evidence_changed
            w.write_json(ev / "real_watched.json", {"watched_excluded_ids": [dy_1]})
            w.record_collection_exclusion(st, run_dir, "douyin", dy_1, "watched_excluded", {"source": "real_watched.json"}, ["evidence/real_watched.json"])
            for idx, off in ((1, 20), (2, 30)):
                w.import_snapshot(st, run_dir, {
                    "snapshot_id": f"dy_rb_{idx}", "platform": "douyin", "account_id": "acc_dy", "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                    "started_at": (base + timedelta(seconds=off)).isoformat(), "finished_at": (base + timedelta(seconds=off + 2)).isoformat(),
                    "method": "api", "evidence_files": ["evidence/pages.json"],
                    "pages": [{"business_code": 0, "request_cursor": "0", "has_more": False, "next_cursor": None, "items": [{"id": dy_2}]}],
                })
            self.assertTrue(w.reconcile(st, run_dir, "douyin")["ok"])
            w.write_json(ev / "real_watched.json", {"watched_excluded_ids": [dy_1], "tampered": True})
            with self.assertRaisesRegex(w.GateError, "evidence_changed"):
                w.reconcile(st, run_dir, "douyin")

        # 4) 抖音阶段 1 单独入账 / 阶段 2 无计数增量 / UI 快照无终态标记 / B站未登录或熔断均被严格拦截
        with tempfile.TemporaryDirectory() as td:
            run_dir, st, base = make_run(Path(td))
            w.plan_collection(st, run_dir, "douyin", (base + timedelta(seconds=6)).isoformat())
            w.plan_collection(st, run_dir, "bilibili", (base + timedelta(seconds=6)).isoformat())
            with self.assertRaisesRegex(w.GateError, "collection_aborted_not_logged_in"):
                rc.ingest_collection_result(run_dir, st, {"ok": False, "platform": "bilibili", "error": "not_logged_in"})
            with self.assertRaisesRegex(w.GateError, "douyin_stage2_folder_add_and_two_round_readback_required"):
                rc.ingest_collection_result(run_dir, st, {"ok": True, "platform": "douyin", "stage": "stage1_complete_stage2_folder_add_required", "receipts": []})
            with self.assertRaisesRegex(w.GateError, "douyin_stage2_folder_count_did_not_increase"):
                rc.ingest_collection_result(run_dir, st, {
                    "ok": True, "platform": "douyin", "account_id": "acc_dy", "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                    "receipts": [{"id": dy_1, "at": (base + timedelta(seconds=12)).isoformat(), "stage1": {"ok": True, "action": "clicked_collect"}}],
                    "stage2": {"confirmed": True, "folder_count_before": 195, "folder_count_after": 195},
                })
            with self.assertRaisesRegex(w.GateError, "douyin_stage2_folder_counts_required"):
                rc.ingest_collection_result(run_dir, st, {
                    "ok": True, "platform": "douyin", "account_id": "acc_dy", "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                    "receipts": [{"id": dy_1, "at": (base + timedelta(seconds=12)).isoformat(), "stage1": {"ok": True, "action": "clicked_collect"}}],
                    "stage2": {"confirmed": True},
                })
            with self.assertRaisesRegex(w.GateError, "ui_needs_visible_terminal_state_not_scroll_guess"):
                rc.ingest_collection_result(run_dir, st, {
                    "ok": True, "platform": "douyin", "account_id": "acc_dy", "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                    "receipts": [],
                    "snapshots": {
                        "round1": {
                            "started_at": (base + timedelta(seconds=20)).isoformat(),
                            "finished_at": (base + timedelta(seconds=22)).isoformat(),
                            "target_visible": True, "fully_loaded": True, "end_visible": False,
                            "items": [{"id": dy_1}, {"id": dy_2}],
                        },
                        "round2": {
                            "started_at": (base + timedelta(seconds=30)).isoformat(),
                            "finished_at": (base + timedelta(seconds=32)).isoformat(),
                            "target_visible": True, "fully_loaded": True, "end_visible": True,
                            "items": [{"id": dy_1}, {"id": dy_2}],
                        },
                    },
                })

        # 5) B站风控熔断部分结果：CLI import-collection 返回退出码 2 且保留真实部分进度
        with tempfile.TemporaryDirectory() as td:
            run_dir, st, base = make_run(Path(td))
            w.write_json(run_dir / "run.json", st)
            risk_payload = {
                "ok": False, "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频",
                "stopped_reason": "risk_code_-412_on_deal_" + bv_already,
                "receipts": [
                    {"id": bv_ok, "outcome": "acknowledged", "started_at": (base + timedelta(seconds=10)).isoformat(), "at": (base + timedelta(seconds=11)).isoformat(), "code": 0, "codes": {"favorite": 0}},
                    {"id": bv_already, "outcome": "failed", "started_at": (base + timedelta(seconds=17)).isoformat(), "at": (base + timedelta(seconds=18)).isoformat(), "code": -412, "error": "risk_code_-412"},
                ],
                "snapshots": {
                    "round1": {
                        "snapshot_id": "risk_r1", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频",
                        "started_at": (base + timedelta(seconds=25)).isoformat(), "finished_at": (base + timedelta(seconds=26)).isoformat(), "method": "api",
                        "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": [{"id": bv_ok}]}],
                    },
                    "round2": {
                        "snapshot_id": "risk_r2", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频",
                        "started_at": (base + timedelta(seconds=33)).isoformat(), "finished_at": (base + timedelta(seconds=34)).isoformat(), "method": "api",
                        "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": [{"id": bv_ok}]}],
                    },
                },
            }
            risk_path = run_dir / "evidence" / "bili_risk.json"
            w.write_json(risk_path, risk_payload)
            rc_code = weekly.main(["import-collection", "--run", str(run_dir), "--platform", "bilibili", "--input", str(risk_path)])
            self.assertEqual(rc_code, 2)
            saved_st = w.read_json(run_dir / "run.json")
            self.assertEqual(len(saved_st["attempts"]), 2)
            self.assertEqual(saved_st["attempts"][1]["outcome"], "failed")

    def test_pipeline_cli_blocked_in_production_and_offline_simulation_forbids_feishu(self):
        """验证 weekly.py pipeline 默认强阻断，仅 --offline-simulation 允许纯本地演练且绝不触碰线上飞书文档。"""
        import feishu_preview_converter
        import sync_feishu_sidebar

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            run_dir = td_p / "run"
            dummy_video = td_p / "dummy.mp4"
            dummy_video.write_bytes(b"dummy")
            manifest = td_p / "seed.json"
            w.write_json(manifest, {
                "issue": "SIM_GUARD",
                "title": "Pipeline Guardrail Test",
                "feishu_doc_id": "doc_should_never_be_touched",
                "sources": [{"source_id": "src-1", "path": str(dummy_video), "kind": "video", "duration": 60}],
                "items": [{
                    "key": "it_1", "title": "视频1", "section": "Sec 1", "kind": "video",
                    "url": "https://www.douyin.com/video/7689404171926108874", "source_id": "src-1", "timestamp_seconds": 10,
                }],
            })

            # 1) 默认调用必须强阻断且不创建 run.json
            self.assertEqual(weekly.main(["pipeline", "--run", str(run_dir), "--manifest", str(manifest)]), 2)
            self.assertFalse((run_dir / "run.json").exists())

            # 2) 带 --feishu-doc 无论是否加 --offline-simulation 均必须阻断
            self.assertEqual(
                weekly.main(["pipeline", "--run", str(run_dir), "--manifest", str(manifest), "--feishu-doc", "doc_live_999"]),
                2,
            )
            self.assertEqual(
                weekly.main(["pipeline", "--run", str(run_dir), "--manifest", str(manifest), "--offline-simulation", "--feishu-doc", "doc_live_999"]),
                2,
            )
            self.assertFalse((run_dir / "run.json").exists())

            # 3) 显式 --offline-simulation（不带 --feishu-doc）：即使 manifest 含 feishu_doc_id 也绝不调飞书写接口
            with (
                mock.patch.object(sync_feishu_sidebar, "sync_document_sidebar") as m_sb,
                mock.patch.object(feishu_preview_converter, "convert_all_bookmarks_to_previews") as m_pv,
            ):
                rc_code = weekly.main(["pipeline", "--run", str(run_dir), "--manifest", str(manifest), "--offline-simulation"])
            self.assertEqual(rc_code, 0)
            m_sb.assert_not_called()
            m_pv.assert_not_called()
            self.assertTrue((run_dir / "run.json").is_file())

    def test_scrape_visible_folder_ids_direct_ingest_and_strict_ui_gates(self):
        """验证 scrapeVisibleFolderIds() 输出可直接通过 import-collection，且严格拦截伪造 UI 回执、未确认阶段2与无身份快照。"""
        dy_1 = "7689404171926108874"
        dy_2 = "7695302751908478458"

        def init_dy_run(td_p: Path, offset_sec: int = 90):
            run_dir = td_p / "run"
            ev = run_dir / "evidence"
            ev.mkdir(parents=True)
            for nm in ("source.json", "detail.json", "discovery.json", "pages.json"):
                w.write_json(ev / nm, {"ok": True})
            base = datetime.now(timezone.utc) - timedelta(seconds=offset_sec)
            t0 = base.isoformat()
            t1 = (base + timedelta(seconds=2)).isoformat()
            t2 = (base + timedelta(seconds=4)).isoformat()
            seed = {
                "title": "Douyin Scrape Direct Test", "source_week": "20261122",
                "numbered_item_count": 2, "quick_news_count": 0,
                "items": [
                    {"key": "d1", "title": "D1", "section": "视频", "number": 1, "url": f"https://www.douyin.com/video/{dy_1}"},
                    {"key": "d2", "title": "D2", "section": "视频", "number": 2, "url": f"https://www.douyin.com/video/{dy_2}"},
                ],
            }
            st = w.initialize(seed, mode="collect")
            events = [
                {"type": "permissions", "publish": False, "collect": ["douyin"], "user_request": "t"},
                {"type": "register_source", "source_id": "src", "kind": "feishu", "document_id": "d1", "revision_id": 1, "checked_at": t0, "evidence_files": ["evidence/source.json"]},
                {"type": "target", "platform": "douyin", "authenticated": True, "account_id": "acc_dy", "complete": True, "checked_at": t0, "desired_name": "水球泡视频周报", "folders": [{"name": "水球泡视频周报", "collects_id_str": "7672986870204225339"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
            ]
            for it in st["items"]:
                norm = w.normalize_url(it["original_url"])
                events.append({"type": "source", "key": it["key"], "source_id": "src", "item_title": it["title"], "ownership": "same_item", "revision_id": 1, "observed_url": norm["url"], "author": "a", "evidence_files": ["evidence/source.json"]})
                events.append({
                    "type": "verify", "key": it["key"], "url": norm["url"], "observed_title": it["title"], "observed_author": "a",
                    "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "ok"},
                    "availability": {"status": "available", "checked_at": t0, "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": norm["id"]},
                    "evidence_files": ["evidence/detail.json"],
                })
            events.append({"type": "inventory", "expected_numbered": 2, "expected_quick_news": 0, "all_source_parts_reviewed": True, "layout_reviewed": True, "evidence_files": ["evidence/source.json"]})
            st = w.apply_events(st, run_dir, events, t0)
            w.import_snapshot(st, run_dir, {
                "snapshot_id": "init_dy", "platform": "douyin", "account_id": "acc_dy",
                "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                "started_at": t1, "finished_at": t2, "method": "api", "evidence_files": ["evidence/pages.json"],
                "pages": [{"business_code": 0, "request_cursor": "0", "has_more": False, "next_cursor": None, "items": []}],
            }, t2)
            w.plan_collection(st, run_dir, "douyin", (base + timedelta(seconds=6)).isoformat())
            w.write_json(run_dir / "run.json", st)
            return run_dir, st, base

        # 1) scrapeVisibleFolderIds() 真实 Node.js 输出直接送入 CLI import-collection 并通过 reconcile
        with tempfile.TemporaryDirectory() as td:
            run_dir, _, base = init_dy_run(Path(td))
            d_js = rc.generate_douyin_inpage_helper_js("水球泡视频周报", "7672986870204225339", [dy_1, dy_2])
            ms = int((base + timedelta(seconds=25)).timestamp() * 1000)
            dy_out = _run_node_eval(f"""
            let fakeMs = {ms};
            const RealDate = Date;
            class FakeDate extends RealDate {{ constructor(...a){{super(...(a.length?a:[fakeMs]));}} static now(){{return fakeMs;}} }}
            global.Date = FakeDate;
            global.window = {{ innerHeight: 900 }};
            global.location = {{ href: "https://www.douyin.com/user/self?showSubTab=favorite_folder&showTab=favorite_collection" }};
            let bodyTxt = "水球泡视频周报\\n共 197 作品\\n暂时没有更多了";
            global.document = {{
              get body() {{ return {{ innerText: bodyTxt }}; }},
              querySelector: () => null,
              querySelectorAll: (s) => s.includes("/video/") ? [
                {{ href: "https://www.douyin.com/video/{dy_1}" }},
                {{ href: "https://www.douyin.com/video/{dy_2}" }},
              ] : [],
            }};
            ({d_js});
            const h = window.__weeklyDouyinHelper;
            const r1 = h.scrapeVisibleFolderIds();
            fakeMs += 6000;
            const r2 = h.scrapeVisibleFolderIds();
            bodyTxt = "水球泡视频周报\\n加载中...";
            const notEnd = h.scrapeVisibleFolderIds();
            console.log(JSON.stringify({{ r1, r2, notEnd }}));
            """)
            self.assertTrue(dy_out["r1"]["fully_loaded"])
            self.assertFalse(dy_out["notEnd"]["fully_loaded"])
            in_path = run_dir / "evidence" / "dy_scrape_direct.json"
            w.write_json(in_path, {
                "ok": True, "platform": "douyin", "account_id": "acc_dy",
                "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                "receipts": [
                    {"id": dy_1, "at": (base + timedelta(seconds=10)).isoformat(), "stage1": {"ok": True, "action": "clicked_collect"}},
                    {"id": dy_2, "at": (base + timedelta(seconds=16)).isoformat(), "stage1": {"ok": True, "action": "clicked_collect"}},
                ],
                "stage2": {"confirmed": True, "folder_count_before": 195, "folder_count_after": 197},
                "snapshots": {"round1": dy_out["r1"], "round2": dy_out["r2"]},
            })
            self.assertEqual(weekly.main(["import-collection", "--run", str(run_dir), "--platform", "douyin", "--input", str(in_path)]), 0)
            self.assertEqual(weekly.main(["reconcile", "--run", str(run_dir), "--platform", "douyin"]), 0)

        # 2) 严格拦截：缺少真实 ui 字段的裸 ui_confirmed、stage2.confirmed=False、以及缺少身份信息的快照
        with tempfile.TemporaryDirectory() as td:
            run_dir, st, base = init_dy_run(Path(td))
            with self.assertRaisesRegex(w.GateError, "ui_receipt_requires_real_ui_evidence"):
                rc.ingest_collection_result(run_dir, st, {
                    "ok": True, "platform": "douyin", "account_id": "acc_dy", "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                    "receipts": [{"id": dy_1, "outcome": "ui_confirmed", "at": (base + timedelta(seconds=12)).isoformat()}],
                }, persist=False)
        with tempfile.TemporaryDirectory() as td:
            run_dir, st, base = init_dy_run(Path(td))
            with self.assertRaisesRegex(w.GateError, "douyin_stage2_not_confirmed"):
                rc.ingest_collection_result(run_dir, st, {
                    "ok": False, "platform": "douyin", "account_id": "acc_dy", "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                    "receipts": [
                        {"id": dy_1, "at": (base + timedelta(seconds=12)).isoformat(), "stage1": {"ok": True, "action": "clicked_collect"}},
                        {"id": dy_2, "at": (base + timedelta(seconds=18)).isoformat(), "stage1": {"ok": True, "action": "clicked_collect"}},
                    ],
                    "stage2": {"confirmed": False, "folder_count_before": 195, "folder_count_after": 195},
                }, persist=False)
        with tempfile.TemporaryDirectory() as td:
            run_dir, st, base = init_dy_run(Path(td))
            with self.assertRaisesRegex(w.GateError, "snapshot_target_mismatch"):
                rc.ingest_collection_result(run_dir, st, {
                    "ok": True, "platform": "douyin", "receipts": [],
                    "snapshots": {
                        "round1": {"started_at": (base + timedelta(seconds=20)).isoformat(), "finished_at": (base + timedelta(seconds=22)).isoformat(), "target_visible": True, "fully_loaded": True, "end_visible": True, "items": [{"id": dy_1}, {"id": dy_2}]},
                        "round2": {"started_at": (base + timedelta(seconds=30)).isoformat(), "finished_at": (base + timedelta(seconds=32)).isoformat(), "target_visible": True, "fully_loaded": True, "end_visible": True, "items": [{"id": dy_1}, {"id": dy_2}]},
                    },
                }, persist=False)

    def test_bilibili_view_rate_limit_deal_error_and_strict_dead_link_evidence(self):
        """验证 B 站 /view 限流(-799)/未登录(-101)/超时(-504) 与 /deal 异常(11007) 必须立即熔断，且排除门禁拒绝非死链码与无关假证据。"""
        b_js = rc.generate_bilibili_inpage_js("水球泡周报视频", "4103329683", ["BV1GOOD00001", "BV1GOOD00002"], delay_ms=5000)

        # 1) /view 返回 -799 限流：必须立即熔断，绝不能记为 dead_link_confirmed 或继续写第 2 条
        out_799 = _run_node_eval(f"""
        global.setTimeout = (fn) => {{ fn(); return 0; }};
        global.document = {{ cookie: "bili_jct=csrf" }};
        let secondViewCalled = false;
        global.fetch = async (url) => {{
          if (url.includes("/nav")) return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ isLogin: true, mid: 1, uname: "u" }} }}) }};
          if (url.includes("/list")) return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ has_more: false, medias: [] }} }}) }};
          if (url.includes("BV1GOOD00001")) return {{ status: 200, text: async () => JSON.stringify({{ code: -799, message: "请求过于频繁" }}) }};
          if (url.includes("BV1GOOD00002")) {{ secondViewCalled = true; return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ aid: 2, state: 0, videos: 1 }} }}) }}; }}
          throw new Error("unexpected " + url);
        }};
        (async () => {{ const r = await ({b_js}); console.log(JSON.stringify({{ r, secondViewCalled }})); }})();
        """)
        self.assertFalse(out_799["r"]["ok"])
        self.assertIn("-799", out_799["r"]["stopped_reason"])
        self.assertEqual(out_799["r"]["dead_links"], [])
        self.assertFalse(out_799["secondViewCalled"])

        # 2) /deal 返回 11007 收藏夹已满：必须立即熔断停止批次，不再写第 2 条
        out_11007 = _run_node_eval(f"""
        global.setTimeout = (fn) => {{ fn(); return 0; }};
        global.document = {{ cookie: "bili_jct=csrf" }};
        let secondDealCalled = false;
        global.fetch = async (url, opts) => {{
          if (url.includes("/nav")) return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ isLogin: true, mid: 1, uname: "u" }} }}) }};
          if (url.includes("/list")) return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ has_more: false, medias: [] }} }}) }};
          if (url.includes("BV1GOOD00001")) return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ aid: 111, state: 0, videos: 1 }} }}) }};
          if (url.includes("BV1GOOD00002")) {{ secondDealCalled = true; return {{ status: 200, text: async () => JSON.stringify({{ code: 0, data: {{ aid: 222, state: 0, videos: 1 }} }}) }}; }}
          if (url.includes("/deal")) return {{ status: 200, text: async () => JSON.stringify({{ code: 11007, message: "收藏夹已满" }}) }};
          throw new Error("unexpected " + url);
        }};
        (async () => {{ const r = await ({b_js}); console.log(JSON.stringify({{ r, secondDealCalled }})); }})();
        """)
        self.assertFalse(out_11007["r"]["ok"])
        self.assertIn("11007", out_11007["r"]["stopped_reason"])
        self.assertEqual(len(out_11007["r"]["receipts"]), 1)
        self.assertFalse(out_11007["secondDealCalled"])

        # 3) weekly_core 排除门禁：拒绝 -799、11007、抖音 business_code=429 以及不含该视频 ID 的无关证据文件
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            ev = run_dir / "evidence"
            ev.mkdir(parents=True)
            w.write_json(ev / "dummy.json", {"irrelevant": True})
            w.write_json(ev / "real_dead.json", {"id": "BV1GOOD00001", "code": -404})
            st = w.initialize({
                "title": "Dead Gate Test", "source_week": "20261122", "numbered_item_count": 2, "quick_news_count": 0,
                "items": [
                    {"key": "b1", "title": "B1", "section": "V", "number": 1, "url": "https://www.bilibili.com/video/BV1GOOD00001"},
                    {"key": "d1", "title": "D1", "section": "V", "number": 2, "url": "https://www.douyin.com/video/7689404171926108874"},
                ],
            }, mode="collect")
            st["items"][0]["id"] = "BV1GOOD00001"
            st["items"][1]["id"] = "7689404171926108874"
            with self.assertRaisesRegex(w.GateError, "risk_control_is_not_dead_link"):
                w.record_collection_exclusion(st, run_dir, "bilibili", "BV1GOOD00001", "dead_link_confirmed", {"code": -799}, ["evidence/real_dead.json"])
            with self.assertRaisesRegex(w.GateError, "bilibili_dead_link_not_confirmed_by_official_status"):
                w.record_collection_exclusion(st, run_dir, "bilibili", "BV1GOOD00001", "dead_link_confirmed", {"code": 11007}, ["evidence/real_dead.json"])
            with self.assertRaisesRegex(w.GateError, "douyin_dead_link_not_confirmed_by_status"):
                w.record_collection_exclusion(st, run_dir, "douyin", "7689404171926108874", "dead_link_confirmed", {"business_code": 429}, ["evidence/dummy.json"])
            with self.assertRaisesRegex(w.GateError, "dead_link_evidence_missing_identity"):
                w.record_collection_exclusion(st, run_dir, "bilibili", "BV1GOOD00001", "dead_link_confirmed", {"code": -404}, ["evidence/dummy.json"])
            # 含真实 ID 的官方 -404 证据正常通过
            exc = w.record_collection_exclusion(st, run_dir, "bilibili", "BV1GOOD00001", "dead_link_confirmed", {"code": -404}, ["evidence/real_dead.json"])
            self.assertEqual(exc["reason"], "dead_link_confirmed")

    def test_circuit_breaker_failed_readback_preservation_resume_and_delayed_long_batch(self):
        """验证：-412 熔断且回读失败时保留部分进度并可二次续跑闭环；20 分钟延迟入账与跨 15 分钟连续批次正常通过，但超时停顿仍拒绝。"""
        def make_bili_run(td_p: Path, bvids: list[str], base_offset_sec: int = 180):
            run_dir = td_p / "run"
            ev = run_dir / "evidence"
            ev.mkdir(parents=True)
            for nm in ("source.json", "detail.json", "discovery.json", "pages.json"):
                w.write_json(ev / nm, {"ok": True})
            base = datetime.now(timezone.utc) - timedelta(seconds=base_offset_sec)
            t0 = base.isoformat()
            t1 = (base + timedelta(seconds=2)).isoformat()
            t2 = (base + timedelta(seconds=4)).isoformat()
            seed = {
                "title": "Resume Test", "source_week": "20261122",
                "numbered_item_count": len(bvids), "quick_news_count": 0,
                "items": [{"key": f"b{i}", "title": f"视频{i}", "section": "V", "number": i, "url": f"https://www.bilibili.com/video/{bv}"} for i, bv in enumerate(bvids, 1)],
            }
            st = w.initialize(seed, mode="collect")
            events = [
                {"type": "permissions", "publish": False, "collect": ["bilibili"], "user_request": "resume"},
                {"type": "register_source", "source_id": "src", "kind": "feishu", "document_id": "d1", "revision_id": 1, "checked_at": t0, "evidence_files": ["evidence/source.json"]},
                {"type": "target", "platform": "bilibili", "authenticated": True, "account_id": "acc_bv", "complete": True, "checked_at": t0, "desired_name": "水球泡周报视频", "folders": [{"name": "水球泡周报视频", "id": "4103329683"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
            ]
            for it in st["items"]:
                norm = w.normalize_url(it["original_url"])
                events.append({"type": "source", "key": it["key"], "source_id": "src", "item_title": it["title"], "ownership": "same_item", "revision_id": 1, "observed_url": norm["url"], "author": "a", "evidence_files": ["evidence/source.json"]})
                events.append({
                    "type": "verify", "key": it["key"], "url": norm["url"], "observed_title": it["title"], "observed_author": "a",
                    "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "ok"},
                    "availability": {"status": "available", "checked_at": t0, "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": norm["id"]},
                    "evidence_files": ["evidence/detail.json"],
                })
            events.append({"type": "inventory", "expected_numbered": len(bvids), "expected_quick_news": 0, "all_source_parts_reviewed": True, "layout_reviewed": True, "evidence_files": ["evidence/source.json"]})
            st = w.apply_events(st, run_dir, events, t0)
            w.import_snapshot(st, run_dir, {
                "snapshot_id": "init_bili", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频",
                "started_at": t1, "finished_at": t2, "method": "api", "evidence_files": ["evidence/pages.json"],
                "pages": [{"business_code": 0, "request_cursor": "0", "has_more": False, "next_cursor": None, "items": []}],
            }, t2)
            w.write_json(run_dir / "run.json", st)
            return run_dir, base

        # 1) Run 1 命中 -412 且 round1/round2 也 412 -> 保留部分回执 -> Run 2 带新 beforeSnap 续跑闭环
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            bvids = ["BV1RESU00001", "BV1RESU00002", "BV1RESU00003"]
            run_dir, base = make_bili_run(td_p, bvids, base_offset_sec=180)
            p1 = run_dir / "evidence" / "run1_412.json"
            w.write_json(p1, {
                "ok": False, "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频",
                "stopped_reason": "risk_code_-412_on_deal_BV1RESU00002",
                "receipts": [
                    {"id": bvids[0], "outcome": "acknowledged", "started_at": (base + timedelta(seconds=10)).isoformat(), "at": (base + timedelta(seconds=11)).isoformat(), "code": 0, "codes": {"favorite": 0}},
                    {"id": bvids[1], "outcome": "failed", "started_at": (base + timedelta(seconds=17)).isoformat(), "at": (base + timedelta(seconds=18)).isoformat(), "code": -412, "error": "risk_code_-412"},
                ],
                "snapshots": {
                    "before": {"snapshot_id": "r1_b", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频", "started_at": (base + timedelta(seconds=6)).isoformat(), "finished_at": (base + timedelta(seconds=8)).isoformat(), "method": "api", "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": []}]},
                    "round1": {"ok": False, "error": "http_risk_412", "snapshot_id": "r1_f1", "platform": "bilibili", "pages": []},
                    "round2": {"ok": False, "error": "http_risk_412", "snapshot_id": "r1_f2", "platform": "bilibili", "pages": []},
                },
            })
            self.assertEqual(weekly.main(["import-collection", "--run", str(run_dir), "--platform", "bilibili", "--input", str(p1)]), 2)
            mid_st = w.read_json(run_dir / "run.json")
            self.assertEqual(len(mid_st["attempts"]), 2)
            self.assertEqual(mid_st["attempts"][0]["outcome"], "acknowledged")
            self.assertEqual(mid_st["attempts"][1]["outcome"], "failed")

            p2 = run_dir / "evidence" / "run2_resume.json"
            w.write_json(p2, {
                "ok": True, "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频",
                "stopped_reason": None,
                "receipts": [
                    {"id": bvids[1], "outcome": "acknowledged", "started_at": (base + timedelta(seconds=60)).isoformat(), "at": (base + timedelta(seconds=61)).isoformat(), "code": 0, "codes": {"favorite": 0}},
                    {"id": bvids[2], "outcome": "acknowledged", "started_at": (base + timedelta(seconds=67)).isoformat(), "at": (base + timedelta(seconds=68)).isoformat(), "code": 0, "codes": {"favorite": 0}},
                ],
                "snapshots": {
                    "before": {"snapshot_id": "r2_b", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频", "started_at": (base + timedelta(seconds=50)).isoformat(), "finished_at": (base + timedelta(seconds=52)).isoformat(), "method": "api", "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": [{"id": bvids[0]}]}]},
                    "round1": {"snapshot_id": "r2_r1", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频", "started_at": (base + timedelta(seconds=75)).isoformat(), "finished_at": (base + timedelta(seconds=76)).isoformat(), "method": "api", "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": [{"id": x} for x in bvids]}]},
                    "round2": {"snapshot_id": "r2_r2", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频", "started_at": (base + timedelta(seconds=83)).isoformat(), "finished_at": (base + timedelta(seconds=84)).isoformat(), "method": "api", "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": [{"id": x} for x in bvids]}]},
                },
            })
            self.assertEqual(weekly.main(["import-collection", "--run", str(run_dir), "--platform", "bilibili", "--input", str(p2)]), 0)
            self.assertEqual(weekly.main(["finish", "--run", str(run_dir), "--catalog", str(td_p / "iso.sqlite")]), 0)

        # 2) 25 分钟前完成的采集（仍在 24h LINK_AGE 内）延迟 import-collection 正常通过；连续跨 15 分钟批次正常通过；但单步间隔超 15 分钟仍拦截
        with tempfile.TemporaryDirectory() as td:
            bvids = [f"BV1LONG000{i:02d}" for i in range(1, 17)]  # 16 条，每条间隔 65s，总耗时 1040s > 900s
            run_dir, base = make_bili_run(Path(td), bvids, base_offset_sec=2600)
            receipts = []
            for idx, bv in enumerate(bvids):
                t_s = (base + timedelta(seconds=15 + idx * 65)).isoformat()
                t_e = (base + timedelta(seconds=16 + idx * 65)).isoformat()
                receipts.append({"id": bv, "outcome": "acknowledged", "started_at": t_s, "at": t_e, "code": 0, "codes": {"favorite": 0}})
            p_long = run_dir / "evidence" / "long_ok.json"
            w.write_json(p_long, {
                "ok": True, "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频",
                "receipts": receipts,
                "snapshots": {
                    "before": {"snapshot_id": "lg_b", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频", "started_at": (base + timedelta(seconds=6)).isoformat(), "finished_at": (base + timedelta(seconds=8)).isoformat(), "method": "api", "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": []}]},
                    "round1": {"snapshot_id": "lg_r1", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频", "started_at": (base + timedelta(seconds=1100)).isoformat(), "finished_at": (base + timedelta(seconds=1102)).isoformat(), "method": "api", "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": [{"id": x} for x in bvids]}]},
                    "round2": {"snapshot_id": "lg_r2", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频", "started_at": (base + timedelta(seconds=1110)).isoformat(), "finished_at": (base + timedelta(seconds=1112)).isoformat(), "method": "api", "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": [{"id": x} for x in bvids]}]},
                },
            })
            self.assertEqual(weekly.main(["import-collection", "--run", str(run_dir), "--platform", "bilibili", "--input", str(p_long)]), 0)

        with tempfile.TemporaryDirectory() as td:
            run_dir, base = make_bili_run(Path(td), ["BV1GAP000001", "BV1GAP000002"], base_offset_sec=1400)
            st = w.read_json(run_dir / "run.json")
            with self.assertRaisesRegex(w.GateError, "stale_or_future_evidence"):
                rc.ingest_collection_result(run_dir, st, {
                    "ok": True, "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频",
                    "receipts": [
                        {"id": "BV1GAP000001", "outcome": "acknowledged", "started_at": (base + timedelta(seconds=10)).isoformat(), "at": (base + timedelta(seconds=11)).isoformat(), "code": 0, "codes": {"favorite": 0}},
                        {"id": "BV1GAP000002", "outcome": "acknowledged", "started_at": (base + timedelta(seconds=1000)).isoformat(), "at": (base + timedelta(seconds=1001)).isoformat(), "code": 0, "codes": {"favorite": 0}},
                    ],
                    "snapshots": {
                        "before": {"snapshot_id": "gap_b", "platform": "bilibili", "account_id": "acc_bv", "folder_id": "4103329683", "folder_name": "水球泡周报视频", "started_at": (base + timedelta(seconds=6)).isoformat(), "finished_at": (base + timedelta(seconds=8)).isoformat(), "method": "api", "pages": [{"business_code": 0, "request_cursor": "1", "has_more": False, "next_cursor": None, "items": []}]},
                    },
                }, persist=False)

    def test_collect_plan_strict_watched_file_validation(self):
        """验证显式 --watched-file 缺失或损坏时 collect-plan 立即报错，绝不静默清空黑名单。"""
        dy_1 = "7689404171926108874"
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            run_dir = td_p / "run"
            ev = run_dir / "evidence"
            ev.mkdir(parents=True)
            for nm in ("source.json", "detail.json", "discovery.json", "pages.json"):
                w.write_json(ev / nm, {"ok": True})
            base = datetime.now(timezone.utc) - timedelta(seconds=40)
            t0, t1, t2 = base.isoformat(), (base + timedelta(seconds=2)).isoformat(), (base + timedelta(seconds=4)).isoformat()
            st = w.initialize({
                "title": "Watched Validation Test", "source_week": "20261122", "numbered_item_count": 1, "quick_news_count": 0,
                "items": [{"key": "d1", "title": "D1", "section": "V", "number": 1, "url": f"https://www.douyin.com/video/{dy_1}"}],
            }, mode="collect")
            st = w.apply_events(st, run_dir, [
                {"type": "permissions", "publish": False, "collect": ["douyin"], "user_request": "t"},
                {"type": "register_source", "source_id": "src", "kind": "feishu", "document_id": "d1", "revision_id": 1, "checked_at": t0, "evidence_files": ["evidence/source.json"]},
                {"type": "target", "platform": "douyin", "authenticated": True, "account_id": "acc_dy", "complete": True, "checked_at": t0, "desired_name": "水球泡视频周报", "folders": [{"name": "水球泡视频周报", "collects_id_str": "7672986870204225339"}], "allow_empty_initial": True, "evidence_files": ["evidence/discovery.json"]},
                {"type": "source", "key": "d1", "source_id": "src", "item_title": "D1", "ownership": "same_item", "revision_id": 1, "observed_url": f"https://www.douyin.com/video/{dy_1}", "author": "a", "evidence_files": ["evidence/source.json"]},
                {"type": "verify", "key": "d1", "url": f"https://www.douyin.com/video/{dy_1}", "observed_title": "D1", "observed_author": "a", "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "ok"}, "availability": {"status": "available", "checked_at": t0, "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": dy_1}, "evidence_files": ["evidence/detail.json"]},
                {"type": "inventory", "expected_numbered": 1, "expected_quick_news": 0, "all_source_parts_reviewed": True, "layout_reviewed": True, "evidence_files": ["evidence/source.json"]},
            ], t0)
            w.import_snapshot(st, run_dir, {
                "snapshot_id": "init_dy", "platform": "douyin", "account_id": "acc_dy",
                "folder_id": "7672986870204225339", "folder_name": "水球泡视频周报",
                "started_at": t1, "finished_at": t2, "method": "api", "evidence_files": ["evidence/pages.json"],
                "pages": [{"business_code": 0, "request_cursor": "0", "has_more": False, "next_cursor": None, "items": []}],
            }, t2)
            w.write_json(run_dir / "run.json", st)

            # 先用合法黑名单写入 watched_excluded
            good_wf = td_p / "good.json"
            good_wf.write_text(json.dumps([dy_1]), encoding="utf-8")
            self.assertEqual(weekly.main(["collect-plan", "--run", str(run_dir), "--platform", "douyin", "--runtime", "antigravity", "--watched-file", str(good_wf)]), 0)
            self.assertIn(dy_1, w.read_json(run_dir / "run.json")["collection_exclusions"]["douyin"])

            # 缺失 / 损坏 JSON / 非数组格式都必须抛出 GateError，且不破坏已有排除记录
            bad_json = td_p / "bad.json"
            bad_json.write_text("{not valid json", encoding="utf-8")
            bad_shape = td_p / "bad_shape.json"
            bad_shape.write_text(json.dumps({"unexpected": 1}), encoding="utf-8")

            with self.assertRaisesRegex(w.GateError, "watched_file_missing"):
                weekly.main(["collect-plan", "--run", str(run_dir), "--platform", "douyin", "--runtime", "antigravity", "--watched-file", str(td_p / "missing.json")])
            with self.assertRaisesRegex(w.GateError, "invalid_watched_file_json"):
                weekly.main(["collect-plan", "--run", str(run_dir), "--platform", "douyin", "--runtime", "antigravity", "--watched-file", str(bad_json)])
            with self.assertRaisesRegex(w.GateError, "invalid_watched_file_format_expected_list"):
                weekly.main(["collect-plan", "--run", str(run_dir), "--platform", "douyin", "--runtime", "antigravity", "--watched-file", str(bad_shape)])
            self.assertIn(dy_1, w.read_json(run_dir / "run.json")["collection_exclusions"]["douyin"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
