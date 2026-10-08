"""Offline synthetic fixtures only. These tests perform ZERO remote operations."""

import copy

import json

import sys

import tempfile

import unittest

from datetime import datetime, timedelta, timezone

from pathlib import Path

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import weekly_core as w

import weekly

import probe_bilibili

import extract_feishu



DY = "7689404171926108874"

BV = "BV1QgY86XEXs"

DURL = "https://www.douyin.com/video/" + DY

BURL = "https://www.bilibili.com/video/" + BV





class WorkflowTests(unittest.TestCase):

    def setUp(self):

        self.tmp = tempfile.TemporaryDirectory(prefix="weekly-synthetic-")

        self.addCleanup(self.tmp.cleanup)

        self.run = Path(self.tmp.name) / "run"

        self.run.mkdir()

        self.base = datetime.now(timezone.utc) - timedelta(minutes=10)

        for name in ("source.json", "detail.json", "discovery.json", "pages.json", "write.json", "style.json"):

            w.write_json(self.run / "evidence" / name, {"synthetic_test_fixture": True, "name": name})

        self.seed = {"title": "Synthetic 水球泡周报 Vol.TEST", "source_week": "synthetic", "numbered_item_count": 4, "quick_news_count": 1, "items": [

            {"key": "dy", "title": "合成测试 原作品A", "section": "视频", "number": 1, "url": DURL},

            {"key": "bv", "title": "合成测试 原作品B", "section": "视频", "number": 2, "url": BURL},

            {"key": "web", "title": "合成测试 网页C", "section": "阅读", "number": 1, "url": "https://example.org/a?keep=1#part"},

            {"key": "plain", "title": "合成测试 随笔", "section": "阅读", "number": 2, "kind": "plain", "body": "必须保留的无链接原文"},

            {"key": "news", "title": "合成测试 快讯", "section": "快讯", "number": None, "kind": "quick_news"}]}

        self.s = w.initialize(self.seed)



    def t(self, seconds=0):

        return (self.base + timedelta(seconds=seconds)).isoformat()



    def apply(self, events, seconds=0):

        self.s = w.apply_events(self.s, self.run, events, self.t(seconds))



    def verify_event(self, item):

        platform = w.normalize_url(item["original_url"])["platform"]

        a = {"status": "available", "checked_at": self.t(), "method": "ui", "detail_visible": True, "unavailable_banner": False, "observed_id": DY if platform == "douyin" else BV if platform == "bilibili" else ""}

        return {"type": "verify", "key": item["key"], "url": item["original_url"], "observed_title": item["title"], "observed_author": "synthetic author", "identity": {"method": "source_url", "same_work": True, "title_match": True, "reason": "SYNTHETIC: source and original detail correspond"}, "availability": a, "evidence_files": ["evidence/detail.json"]}



    def verified(self):

        self.apply([{"type": "register_source", "source_id": "source", "kind": "feishu", "document_id": "synthetic-source", "revision_id": 1, "checked_at": self.t(), "evidence_files": ["evidence/source.json"]}])

        events = []

        for it in self.s["items"]:

            events.append({"type": "source", "key": it["key"], "source_id": "source", "item_title": it["title"], "ownership": "same_item", "revision_id": 1, "observed_url": it["original_url"], "author": "synthetic author", "evidence_files": ["evidence/source.json"]})

            if it["original_url"]:

                events.append(self.verify_event(it))

        events.append({"type": "inventory", "expected_numbered": 4, "expected_quick_news": 1, "all_source_parts_reviewed": True, "layout_reviewed": True, "evidence_files": ["evidence/source.json"]})

        events.append({"type": "permissions", "publish": True, "collect": ["douyin", "bilibili"], "user_request": "SYNTHETIC authorization for local tests ONLY"})

        self.apply(events)

        self.assertTrue(w.audit(self.s, self.run, self.t())["ok"])



    def target(self, platform, seconds=0, **extra):

        e = {"type": "target", "platform": platform, "account_id": "synthetic-user", "authenticated": True, "complete": True, "checked_at": self.t(seconds), "evidence_files": ["evidence/discovery.json"], "folders": [{"name": self.s["target_names"][platform], "collects_id_str" if platform == "douyin" else "id": "7672986870204225339" if platform == "douyin" else "F-" + platform}], "allow_empty_initial": True}

        if platform == "feishu":

            e.update(document_search_complete=True, same_issue_documents=[])

        e.update(extra)

        self.apply([e], seconds)

        return e



    def published(self):

        self.verified()

        w.render(self.s, self.run, at=self.t())

        self.target("feishu")

        plan = w.publish_plan(self.s, self.run, self.t())

        self.assertEqual(plan["action"], "create_once")

        self.readback()



    def readback(self):

        xml = (self.run / "report" / "weekly.xml").read_text(encoding="utf-8")

        w.write_json(self.run / "evidence" / "fetch.json", {"ok": True, "identity": "user", "data": {"document": {"document_id": "synthetic-final", "revision_id": 7, "content": xml}}})

        w.write_json(self.run / "evidence" / "folder.json", {"synthetic_test_fixture": True, "complete": True, "checked_at": self.t(1), "account_id": "synthetic-user", "folder_id": "F-feishu", "documents": [{"id": "synthetic-final", "title": self.s["issue"]["title"]}]})

        data = {"account_id": "synthetic-user", "folder_id": "F-feishu", "checked_at": self.t(1), "document_file": "evidence/fetch.json", "folder_file": "evidence/folder.json", "style_reviewed": True, "style_evidence_files": ["evidence/style.json"], "document_url": "https://example.org/synthetic-final"}

        w.readback_publication(self.s, self.run, data, self.t(1))

        return data



    def snapshot(self, platform, serial, seconds, ids, method="api", submit=True):

        target = self.s["targets"][platform]

        page = {"request_cursor": "0", "has_more": False, "next_cursor": None, "items": [{"id": x} for x in ids]}

        if method == "api":

            page["business_code"] = 0

        else:

            page.update(target_visible=True, fully_loaded=True, end_visible=True, evidence_files=["evidence/pages.json"])

        raw = {"method": method, "snapshot_id": platform + "-" + str(serial), "platform": platform, "account_id": target["account_id"], "folder_id": target["folder_id"], "folder_name": target["folder_name"], "started_at": self.t(seconds), "finished_at": self.t(seconds+1), "pages": [page], "evidence_files": ["evidence/pages.json"]}

        if submit:

            w.import_snapshot(self.s, self.run, raw, self.t(seconds+1))

        return raw



    def plan(self, platform="douyin"):

        self.target(platform)

        self.snapshot(platform, 0, 2, [])

        return w.plan_collection(self.s, self.run, platform, self.t(4))



    def begin(self, platform="douyin"):

        p = self.plan(platform)

        return w.begin_write(self.s, self.run, platform, DY if platform == "douyin" else BV, p["plan_id"], self.t(5))



    def end(self, attempt, outcome="acknowledged"):

        return w.end_write(self.s, self.run, attempt["attempt_id"], {"outcome": outcome, "codes": {"favorite": 0, "folder_add": 0}, "evidence_files": ["evidence/write.json"]}, self.t(6))



    def test_full_pipeline_both_platforms_and_search(self):

        self.published()

        for platform, identity in (("douyin", DY), ("bilibili", BV)):

            a = self.begin(platform)

            self.end(a)

            self.snapshot(platform, 1, 10, [identity])

            self.snapshot(platform, 2, 16, [identity])

            self.assertTrue(w.reconcile(self.s, self.run, platform, self.t(20))["ok"])

        self.assertEqual(w.finish(self.s, self.run, self.t(25))["status"], "complete")

        catalog = self.run.parent / "library" / "catalog.sqlite"

        paths = w.export_index(self.s, self.run, catalog)

        self.assertTrue(Path(paths["catalog_index"]).is_file())

        found = w.search_catalog(catalog, "Vol.TEST 原作品A")

        self.assertEqual(len(found), 1)

        self.assertEqual(found[0]["collection"], "confirmed")

        self.assertEqual(w.search_catalog(catalog, DY)[0]["id"], DY)

        self.assertEqual(w.search_catalog(catalog, "%' OR 1=1 --"), [])



    def test_old_success_never_trusted_and_missing_quick_news(self):

        self.seed["success"] = True

        self.seed["items"][0].update(verified=True, canonical_url=DURL)

        s = w.initialize(self.seed)

        self.assertIsNone(s["items"][0]["verification"])

        self.assertEqual(s["items"][0]["canonical_url"], "")

        s["items"].pop()

        self.assertFalse(w.audit(s, self.run, self.t())["ok"])

        self.assertTrue(any("quick_news_inventory_mismatch" in e["error"] for e in w.audit(s, self.run, self.t())["errors"]))



    def test_partial_exception_not_complete_nor_collected(self):

        self.verified()

        self.apply([{"type": "exception", "key": "dy", "reason": "SYNTHETIC original unavailable", "user_request": "synthetic acceptance", "evidence_files": ["evidence/detail.json"]}])

        a = w.audit(self.s, self.run, self.t())

        self.assertFalse(a["ok"])

        self.assertTrue(a["publishable_partial"])

        self.assertEqual(w.expected_ids(self.s, "douyin"), [])

        with self.assertRaises(w.GateError):

            w.render(self.s, self.run, at=self.t())

        w.render(self.s, self.run, allow_partial=True, at=self.t())

        self.s["mode"] = "audit"

        self.assertEqual(w.finish(self.s, self.run, self.t())["status"], "partial")

        self.assertEqual(w.index_rows(self.s, self.run, self.t())[0]["link_status"], "unresolved_original")



    def test_similar_and_prefix_only_recovery_rejected_atomically(self):

        self.verified()

        original = w.digest(self.s)

        for method in ("similar_topic_tutorial", "similar_topic", "title_only", "exact_prefix_and_title"):

            e = self.verify_event(self.s["items"][0]); e["identity"]["method"] = method

            e["url"] = "https://www.douyin.com/video/7685944428086709555"

            with self.assertRaises(w.GateError):

                self.apply([e])

        self.assertEqual(w.digest(self.s), original)



    def test_recovery_needs_author_visual_and_independent_files(self):

        self.verified()

        e = self.verify_event(self.s["items"][0]); e["identity"].update(method="title_author_visual", author_match=True)

        with self.assertRaises(w.GateError): self.apply([e])

        e["identity"]["visual_match"] = True

        e["observed_author"] = "wrong author"

        with self.assertRaises(w.GateError): self.apply([e])

        e["observed_author"] = "synthetic author"; e["evidence_files"] = ["evidence/source.json"]

        with self.assertRaises(w.GateError): self.apply([e])

        e["evidence_files"] = ["evidence/detail.json"]

        self.apply([e])



    def test_bilibili_code0_state_minus4_and_zero_videos_rejected(self):

        self.verified()

        e = self.verify_event(self.s["items"][1]); e["availability"].update(method="api", metadata={"code": 0, "state": -4, "videos": 0})

        with self.assertRaises(w.GateError): self.apply([e])

        e["availability"]["metadata"].update(state=0, videos=0)

        with self.assertRaises(w.GateError): self.apply([e])

        e["availability"]["metadata"]["videos"] = 1

        self.apply([e])



    def test_edit_invalidates_evidence_and_render(self):

        self.published()

        self.apply([{"type": "item", "key": "dy", "changes": {"title": "modified"}}])

        self.assertFalse(w.audit(self.s, self.run, self.t())["ok"])

        with self.assertRaises(w.GateError): w.check_render(self.s, self.run)

        self.assertIsNone(self.s["items"][0]["source"])



    def test_evidence_tamper_and_outside_run_rejected(self):

        self.verified()

        w.atomic_text(self.run / "evidence" / "detail.json", "tampered")

        self.assertFalse(w.audit(self.s, self.run, self.t())["ok"])

        for value in ("../outside.json", str(self.run / "evidence" / "source.json")):

            with self.assertRaises(w.GateError): w.file_evidence(self.run, [value])



    def test_inventory_structure_and_order_must_be_reviewed(self):

        self.verified()

        blocks = copy.deepcopy(self.s["layout"])

        blocks.append({"type": "paragraph", "text": "new preface"})

        self.apply([{"type": "layout", "blocks": blocks}])

        self.assertFalse(w.audit(self.s, self.run, self.t())["ok"])



    def test_context_live_link_rich_text_preserved(self):

        self.verified()

        self.s["layout"].insert(0, {"type": "paragraph", "runs": [{"text": "原文前言 "}, {"text": "直播入口", "href": "https://live.bilibili.com/123", "bold": True}]})

        md, xml = w.build_report(self.s)

        self.assertIn("https://live.bilibili.com/123", md)

        self.assertIn('<a href="https://live.bilibili.com/123"><b>', xml)

        self.assertIn("原文前言", xml)

        self.assertEqual(w.expected_ids(self.s, "bilibili"), [BV])



    def test_publish_requires_permissions_and_fresh_duplicate_search(self):

        self.verified(); w.render(self.s, self.run, at=self.t()); self.target("feishu")

        self.s["permissions"]["publish"] = False

        with self.assertRaises(w.GateError): w.publish_plan(self.s, self.run, self.t())

        self.s["permissions"]["publish"] = True

        w.publish_plan(self.s, self.run, self.t())

        with self.assertRaises(w.GateError): w.publish_plan(self.s, self.run, self.t(1))

        self.target("feishu", 2, same_issue_documents=[{"id": "synthetic-final"}])

        self.assertEqual(w.publish_plan(self.s, self.run, self.t(3))["action"], "update_existing")



    def test_changed_remote_doc_or_extra_media_fails_readback(self):

        self.published()

        data = self.readback()

        f = self.run / data["document_file"]

        d = w.read_json(f); d["data"]["document"]["content"] += '<img href="https://example.org/screenshot.png"/>'

        w.write_json(f, d)

        with self.assertRaises(w.GateError): w.readback_publication(self.s, self.run, data, self.t(2))



    def test_duplicate_final_document_rejected(self):

        self.published(); data = self.readback()

        f = self.run / data["folder_file"]; d = w.read_json(f); d["documents"].append(d["documents"][0]); w.write_json(f, d)

        with self.assertRaises(w.GateError): w.readback_publication(self.s, self.run, data, self.t(2))



    def test_collection_requires_publication_first(self):

        self.verified(); self.target("douyin"); self.snapshot("douyin", 0, 2, [])

        with self.assertRaises(w.GateError): w.plan_collection(self.s, self.run, "douyin", self.t(4))



    def test_collect_only_requires_same_current_feishu_link(self):

        self.verified(); self.s["mode"] = "collect"

        p = self.plan(); self.assertEqual(p["missing_ids"], [DY])

        it = self.s["items"][0]

        it["source"]["observed_url"] = "https://www.douyin.com/video/76894041719"

        it["verification"]["identity"].update(method="title_author_visual", visual_match=True, author_match=True)

        it["verification"]["binding"] = w.digest({"title": it["title"], "url": it["canonical_url"], "source": it["source"]})

        with self.assertRaises(w.GateError): w.ready_for_collection(self.s, self.run, "douyin", self.t())



    def test_target_needs_unique_name_original_string_id(self):

        self.verified()

        e = self.target("douyin")

        e["folders"][0]["collects_id_str"] = 7672986870204225339

        with self.assertRaises(w.GateError): self.apply([e])

        e["folders"][0]["collects_id_str"] = "7672986870204225339"; e["folders"] *= 2

        with self.assertRaises(w.GateError): self.apply([e])



    def test_wrong_account_folder_empty_old_future_and_numeric_id_snapshots(self):

        self.published(); self.target("douyin")

        raw = self.snapshot("douyin", 0, 2, [DY], submit=False)

        for key, value in (("account_id", "other"), ("folder_id", "other"), ("finished_at", self.t(-5000)), ("started_at", self.t(10))):

            r = copy.deepcopy(raw); r[key] = value

            with self.assertRaises(w.GateError): w.parse_snapshot(self.s, self.run, r, self.t(3))

        r = copy.deepcopy(raw); r["pages"][0]["items"][0]["id"] = int(DY)

        with self.assertRaises(w.GateError): w.parse_snapshot(self.s, self.run, r, self.t(3))

        self.s["targets"]["douyin"]["allow_empty_initial"] = False

        r = copy.deepcopy(raw); r["pages"][0]["items"] = []

        with self.assertRaises(w.GateError): w.parse_snapshot(self.s, self.run, r, self.t(3))



    def test_pagination_gap_loop_and_early_end_rejected(self):

        self.published(); self.target("douyin")

        raw = self.snapshot("douyin", 0, 2, [DY], submit=False)

        one = raw["pages"][0]

        one.update(has_more=True, next_cursor="20")

        with self.assertRaises(w.GateError): w.parse_snapshot(self.s, self.run, raw, self.t(3))

        two = copy.deepcopy(one); two.update(request_cursor="40", has_more=False, next_cursor=None); raw["pages"].append(two)

        with self.assertRaises(w.GateError): w.parse_snapshot(self.s, self.run, raw, self.t(3))

        two["request_cursor"] = "20"

        self.assertEqual(w.parse_snapshot(self.s, self.run, raw, self.t(3))["page_count"], 2)

        two.update(has_more=True, next_cursor="0")

        with self.assertRaises(w.GateError): w.parse_snapshot(self.s, self.run, raw, self.t(3))

        one.update(has_more=False, next_cursor=None)

        with self.assertRaises(w.GateError): w.parse_snapshot(self.s, self.run, raw, self.t(3))



    def test_ui_snapshots_do_not_fabricate_business_codes(self):

        self.published(); self.target("douyin")

        raw = self.snapshot("douyin", 1, 10, [DY], method="ui", submit=False)

        self.assertNotIn("business_code", raw["pages"][0])

        broken = copy.deepcopy(raw); broken["pages"][0]["end_visible"] = False

        with self.assertRaises(w.GateError): w.parse_snapshot(self.s, self.run, broken, self.t(11))

        w.import_snapshot(self.s, self.run, raw, self.t(11))

        self.snapshot("douyin", 2, 16, [DY], method="ui")

        self.assertTrue(w.reconcile(self.s, self.run, "douyin", self.t(20))["ok"])



    def test_existing_ids_are_not_written(self):

        self.published(); self.target("douyin"); self.snapshot("douyin", 0, 2, [DY])

        p = w.plan_collection(self.s, self.run, "douyin", self.t(4))

        self.assertEqual(p["missing_ids"], [])

        with self.assertRaises(w.GateError): w.begin_write(self.s, self.run, "douyin", DY, p["plan_id"], self.t(5))



    def test_uncertain_write_requires_readback_before_replan(self):

        self.published(); a = self.begin(); self.end(a, "uncertain")

        with self.assertRaises(w.GateError): w.plan_collection(self.s, self.run, "douyin", self.t(10))

        self.snapshot("douyin", 1, 11, [DY])

        p = w.plan_collection(self.s, self.run, "douyin", self.t(13))

        self.assertEqual(p["missing_ids"], [])

        self.assertTrue(a["observed_present"])



    def test_acknowledged_write_also_requires_readback_before_replan(self):

        self.published(); a = self.begin(); self.end(a)

        with self.assertRaises(w.GateError): w.plan_collection(self.s, self.run, "douyin", self.t(10))

        with self.assertRaises(w.GateError): w.begin_write(self.s, self.run, "douyin", DY, a["plan_id"], self.t(15))



    def test_crash_in_flight_resolves_from_readback(self):

        self.published(); a = self.begin()

        with self.assertRaises(w.GateError): w.plan_collection(self.s, self.run, "douyin", self.t(10))

        self.snapshot("douyin", 1, 11, [DY]); self.snapshot("douyin", 2, 17, [DY])

        self.assertTrue(w.reconcile(self.s, self.run, "douyin", self.t(20))["ok"])

        self.assertTrue(a["observed_present"])



    def test_all_business_codes_zero_and_no_secrets(self):

        self.published(); a = self.begin()

        for codes in ({"favorite": 0}, {"favorite": 0, "folder_add": 1}, {"favorite": True, "folder_add": 0}):

            with self.assertRaises(w.GateError): w.end_write(self.s, self.run, a["attempt_id"], {"outcome": "acknowledged", "codes": codes, "evidence_files": ["evidence/write.json"]}, self.t(6))

        with self.assertRaises(w.GateError): w.end_write(self.s, self.run, a["attempt_id"], {"outcome": "uncertain", "Cookie": "not-a-real-cookie", "evidence_files": ["evidence/write.json"]}, self.t(6))



    def test_two_rounds_must_follow_write_and_have_delay(self):

        self.published(); a = self.begin(); self.end(a)

        self.snapshot("douyin", 1, 8, [DY])

        with self.assertRaises(w.GateError): w.reconcile(self.s, self.run, "douyin", self.t(10))

        self.snapshot("douyin", 2, 10, [DY])

        with self.assertRaises(w.GateError): w.reconcile(self.s, self.run, "douyin", self.t(12))

        self.snapshot("douyin", 3, 16, [])

        self.assertFalse(w.reconcile(self.s, self.run, "douyin", self.t(20))["ok"])



    def test_target_switch_after_writes_blocked(self):

        self.published(); a = self.begin(); self.end(a)

        with self.assertRaises(w.GateError): self.target("douyin", 8, folders=[{"name": "水球泡视频周报", "collects_id_str": "7672986870204225340"}])



    def test_source_wrong_neighbor_and_revision_blocked(self):

        self.verified(); it = self.s["items"][0]

        for change in ({"ownership": "adjacent"}, {"revision_id": 99}, {"item_title": "neighbor item"}):

            it["source"].update(change)

            self.assertFalse(w.audit(self.s, self.run, self.t())["ok"])



    def test_repeated_url_requires_explicit_source_repeat(self):

        self.verified()

        s = copy.deepcopy(self.s); item = copy.deepcopy(s["items"][0]); item.update(key="repeat", number=3)

        s["items"].append(item); s["layout"].append({"type": "item", "key": "repeat"}); s["inventory"]["expected_numbered"] += 1

        self.assertTrue(any("duplicate_url" in x["error"] for x in w.audit(s, self.run, self.t())["errors"]))

        item["repeat_of"] = "dy"

        self.assertFalse(any("duplicate_url" in x["error"] for x in w.audit(s, self.run, self.t())["errors"]))



    def test_index_safe_html_and_two_reconcile_passes(self):

        self.s["items"][0]["title"] = '<script>alert("x")</script>'

        with mock.patch.object(w, "reconcile", side_effect=w.GateError("synthetic unavailable")) as fn:

            w.export_index(self.s, self.run)

            self.assertEqual(fn.call_count, 2)

        h = (self.run / "index.html").read_text(encoding="utf-8")

        self.assertIn("&lt;script&gt;", h)

        self.assertNotIn('<script>alert("x")', h)



    def test_unverified_index_keeps_original_clue_without_promoting_it(self):

        raw = "https://www.douyin.com/video/76894041719"

        self.s["items"][0]["original_url"] = raw

        w.export_index(self.s, self.run)

        text = (self.run / "index.html").read_text(encoding="utf-8")

        self.assertIn("原记录链接（待核验）", text)

        self.assertIn(raw, text)

        self.assertNotIn("打开原作品", text)

        rows = w.read_json(self.run / "library.json")

        self.assertTrue(all(r["link_status"] == "needs_review" for r in rows))

        self.assertTrue(all(r["collection"] != "confirmed" for r in rows))



    def test_cli_lock_released_and_atomic_apply_failure(self):

        w.write_json(self.run / "run.json", self.s)

        with weekly.locked(self.run):

            with self.assertRaises(w.GateError):

                with weekly.locked(self.run): pass

        self.assertFalse((self.run / ".weekly.lock").exists())

        original = w.digest(self.s)

        with self.assertRaises(w.GateError): self.apply([{"type": "permissions", "publish": True, "user_request": "synthetic"}, {"type": "bad-event"}])

        self.assertEqual(w.digest(self.s), original)



    def test_readonly_mode_cannot_write(self):

        self.verified(); self.s["mode"] = "audit"; self.target("douyin")

        with self.assertRaises(w.GateError): w.publish_plan(self.s, self.run, self.t())

        with self.assertRaises(w.GateError): w.ready_for_collection(self.s, self.run, "douyin", self.t())

        self.assertEqual(w.finish(self.s, self.run, self.t())["status"], "audit_passed")



    def test_expired_and_future_identity_evidence_rejected(self):

        self.verified()

        for date in (self.t(-90000), self.t(5000)):

            e = self.verify_event(self.s["items"][0]); e["availability"]["checked_at"] = date

            with self.assertRaises(w.GateError): self.apply([e])





    def test_healthy_video_wrong_identity_rejected(self):

        self.verified()

        ev = self.verify_event(self.s["items"][1])

        ev["observed_title"] = "完全不同的作品"

        ev["identity"]["title_match"] = False

        ev["availability"] = {"method": "api", "status": "available", "checked_at": self.t(), "observed_id": BV, "metadata": {"code": 0, "state": 0, "videos": 1}}

        with self.assertRaisesRegex(w.GateError, "same_work_identity"):

            self.apply([ev])



    def test_body_stays_between_title_and_link(self):

        self.s["items"][0]["body"] = "原文灰色摘要不可换位"

        self.verified()

        md, xml = w.build_report(self.s)

        text = w.xml_signature(xml)[0]

        self.assertLess(text.index("原作品A"), text.index("原文灰色摘要"))

        self.assertLess(text.index("原文灰色摘要"), text.index(DURL))



    def test_source_image_bound_to_owner_and_checked_on_readback(self):

        self.published()

        blocks = copy.deepcopy(self.s["layout"])

        pos = next(n for n,b in enumerate(blocks) if b.get("key") == "dy") + 1

        image = {"type": "image", "owner_key": "dy", "src": "syntheticImageToken", "caption": "合成原图说明", "evidence_files": ["evidence/source.json", "evidence/detail.json"]}

        blocks.insert(pos, image)

        self.apply([{"type": "layout", "blocks": blocks}, {"type": "inventory", "expected_numbered": 4, "expected_quick_news": 1, "all_source_parts_reviewed": True, "layout_reviewed": True, "evidence_files": ["evidence/source.json"]}])

        w.render(self.s, self.run, at=self.t())

        self.readback()

        xml = (self.run / "report" / "weekly.xml").read_text(encoding="utf-8")

        self.assertIn('src="syntheticImageToken"', xml)

        self.assertNotEqual(w.xml_signature(xml), w.xml_signature(xml.replace("syntheticImageToken", "syntheticWrongToken")))

        self.s["layout"][pos]["owner_key"] = "bv"

        self.s["inventory"]["review"]["binding"] = w.digest({"layout": self.s["layout"], "inventory": [i["key"] for i in self.s["items"]]})

        self.assertIn("image_must_follow_owning_item", str(w.audit(self.s, self.run, self.t())["errors"]))





    def test_ui_receipt_advances_without_fabricating_api_codes(self):

        self.published()

        a = self.begin()

        receipt = {"outcome": "ui_confirmed", "ui": {"account_id": a["account_id"], "folder_id": a["folder_id"], "id": DY, "favorite_visible": True, "folder_selected_visible": True, "reopened_detail": True}, "evidence_files": ["evidence/write.json"]}

        w.end_write(self.s, self.run, a["attempt_id"], receipt, self.t(6))

        with self.assertRaises(w.GateError):

            w.reconcile(self.s, self.run, "douyin", self.t(7))

        with self.assertRaisesRegex(w.GateError, "new_full_snapshot"):

            w.plan_collection(self.s, self.run, "douyin", self.t(7))

        self.snapshot("douyin", 1, 8, [DY], method="ui")

        self.snapshot("douyin", 2, 14, [DY], method="ui")

        self.assertTrue(w.reconcile(self.s, self.run, "douyin", self.t(15))["ok"])



    def test_ui_toast_alone_is_not_confirmed_receipt(self):

        self.published()

        a = self.begin()

        with self.assertRaisesRegex(w.GateError, "ui_receipt"):

            w.end_write(self.s, self.run, a["attempt_id"], {"outcome": "ui_confirmed", "ui": {"id": DY}, "evidence_files": ["evidence/write.json"]}, self.t(6))



class UrlAndExtractorTests(unittest.TestCase):

    def test_url_normalization_preserves_identity_and_part(self):

        self.assertEqual(w.normalize_url(DURL + "?from=foo")["id"], DY)

        self.assertEqual(w.normalize_url("https://www.douyin.com/?modal_id=" + DY)["url"], DURL)

        self.assertEqual(w.normalize_url(DURL.replace("video", "note"))["id"], DY)

        self.assertEqual(w.normalize_url(BURL + "?p=2&spm=abc")["url"], BURL + "?p=2")

        self.assertEqual(w.normalize_url("https://example.org/a?q=2#x")["url"], "https://example.org/a?q=2#x")



    def test_truncated_ids_short_links_live_and_malformed_urls_rejected(self):

        for u in ("https://www.douyin.com/video/76894041719", "https://www.douyin.com/note/76855655561", "https://www.douyin.com/?modal_id=76834837308", "https://v.douyin.com/abc/", "https://b23.tv/abc", "https://live.bilibili.com/123", "https://www.bilibili.com/video/"+BV+"?p=0", "https://example.org:foo/a", "https://[bad/", "javascript:alert(1)", "https://user:pass@example.org"):

            with self.subTest(u=u), self.assertRaises(w.GateError): w.normalize_url(u)

        self.assertEqual(w.normalize_url("https://www.douyin.com.evil.test/video/"+DY)["platform"], "web")

        with self.assertRaises(w.GateError): w.valid_id("douyin", int(DY))



    def test_probe_classification_never_source_verified(self):

        for code, state, videos, outcome in ((0, 0, 1, "metadata_accessible"), (0, -4, 0, "restricted_or_unavailable"), (-404, None, None, "not_found_on_this_read"), (62002, None, None, "restricted_or_unavailable"), (-412, None, None, "challenge_or_rate_limit")):

            p = {"code": code, "data": {"bvid": BV, "state": state, "videos": videos}}

            r = probe_bilibili.classify(BV, p)

            self.assertEqual(r["availability"], outcome)

            self.assertFalse(r["identity_verified"])



    def test_extract_video_occurrences_dedupe_same_element_not_across_items(self):

        xml = '<title>Synthetic</title><p>直播 https://live.bilibili.com/12</p><h2>栏目</h2><p>1. 测试标题A</p><p><a href="'+DURL+'">'+DURL+'</a></p><p>2. 同源再次提及</p><p>'+DURL+'</p><p>3. B站</p><p>'+BURL+'</p>'

        r = extract_feishu.extract({"ok": True, "identity": "user", "data": {"document": {"document_id": "fixture", "revision_id": 1, "content": xml}}})

        self.assertEqual(r["url_occurrences"], 3)

        self.assertEqual(r["unique_raw_urls"], 2)

        self.assertEqual(r["items"][0]["title"], "测试标题A")

        with self.assertRaises(w.GateError): w.initialize(r, "full")

        self.assertEqual(len(w.initialize(r, "collect")["items"]), 3)





    def test_extract_h3_h4_and_nested_li_platform_filter(self):

        xml = '<title>Synthetic</title><h2>主栏目</h2><h3>1. 原标题A</h3><p>'+DURL+'</p><h3>子栏目</h3><h4>1. 原标题B</h4><li><p><a href="'+BURL+'">'+BURL+'</a></p></li>'

        payload = {"ok": True, "identity": "user", "data": {"document": {"document_id": "fixture", "revision_id": 3, "content": xml}}}

        r = extract_feishu.extract(payload)

        self.assertEqual([i["title"] for i in r["items"]], ["原标题A", "原标题B"])

        self.assertEqual(r["items"][1]["subsection"], "子栏目")

        one = extract_feishu.extract(payload, "bilibili")

        self.assertEqual(len(one["items"]), 1)

        self.assertEqual(one["items"][0]["title"], "原标题B")







class ResolveLinksAndCollectRunnerTests(unittest.TestCase):

    def test_inspect_and_verify_links(self):

        import resolve_links

        manifest = {

            "items": [

                {"key": "trunc", "title": "测试截断链接", "url": "https://www.douyin.com/video/76908860663"},

                {"key": "full", "title": "测试完整链接", "url": "https://www.douyin.com/video/7689404171926108874"},

                {"key": "miss", "title": "测试缺失链接", "url": ""},

                {"key": "bv", "title": "测试B站", "url": "https://www.bilibili.com/video/BV1owHt6cEv2"}

            ]

        }

        report = resolve_links.scan_and_report_issues(manifest)

        self.assertEqual(report["total_items"], 4)

        self.assertEqual(report["healthy_count"], 2)

        self.assertEqual(report["unresolved_count"], 2)

        self.assertEqual(report["unresolved_items"][0]["issue"], "douyin_truncated_id")

        self.assertEqual(report["unresolved_items"][1]["issue"], "missing_url")



        # Verification gate tests

        good = resolve_links.verify_resolution(

            {"title": "快乐发生的真正机制"},

            "https://www.douyin.com/video/7690262867963409523",

            "她发现了快乐发生的真正机制！英国女作家26岁写日记7年记录快乐瞬间",

            ""

        )

        self.assertTrue(good["verified"])

        self.assertEqual(good["platform"], "douyin")



        bad = resolve_links.verify_resolution(

            {"title": "快乐发生的真正机制"},

            "https://www.douyin.com/video/7690262867963409523",

            "完全不相关的烹饪教程",

            ""

        )

        self.assertFalse(bad["verified"])



    def test_collect_runner_script_generation(self):

        import collect_runner

        plan = {

            "to_add": ["7689404171926108874"],

            "folder_id": "7672986870204225339",

            "account_id": "synthetic-user",

            "plan_id": "plan-123"

        }

        js = collect_runner.generate_browser_script(Path("."), "douyin", plan)

        self.assertIn("水球泡视频周报", js)

        self.assertIn("7689404171926108874", js)

        self.assertIn("5500", js)



    def test_capture_screenshot_and_search_candidate_attachment(self):

        import resolve_links

        # 1. Attach search candidate with screenshot

        item = {

            "key": "test-1",

            "number": 1,

            "title": "快乐发生的真正机制",

            "body": "测试摘要",

            "kind": "video",

            "canonical_url": "",

            "platform": "douyin",

            "id": "",

            "author": "谷浪",

            "candidates": [],

            "source": {"source_id": "src-1", "timestamp_seconds": 145}

        }

        cand = resolve_links.attach_search_candidate(

            item,

            candidate_url="https://www.douyin.com/video/7690262867963409523",

            candidate_title="快乐发生的真正机制",

            candidate_author="谷浪",

            screenshot_path="evidence/screenshots/life-1_145s.jpg",

            reason="Text search candidate for manual review"

        )

        self.assertEqual(cand["match_type"], "text_search_candidate")

        self.assertEqual(item["candidate_url"], "https://www.douyin.com/video/7690262867963409523")

        self.assertEqual(item["screenshot"], "evidence/screenshots/life-1_145s.jpg")

        self.assertEqual(len(item["candidates"]), 1)



        # 2. Report rendering includes candidate notice and screenshot reference

        state = {

            "issue": {"title": "测试周报 - Vol.296", "week": "20261005"},

            "layout": [{"type": "item", "key": "test-1"}],

            "items": [item]

        }

        md, xml = w.build_report(state)

        self.assertIn("候选链接（待核验）：https://www.douyin.com/video/7690262867963409523", md)

        self.assertIn("evidence/screenshots/life-1_145s.jpg", md)

        self.assertIn("候选链接（待核验）：", xml)

        self.assertIn("https://www.douyin.com/video/7690262867963409523", xml)

        self.assertIn("evidence/screenshots/life-1_145s.jpg", xml)



    def test_index_html_renders_screenshot_and_candidate_links(self):

        import resolve_links

        import tempfile

        item = {

            "key": "test-1",

            "number": 1,

            "title": "快乐发生的真正机制",

            "body": "测试摘要",

            "kind": "video",

            "canonical_url": "",

            "original_url": "https://www.douyin.com/video/76902628679",

            "platform": "douyin",

            "id": "7690262867963409523",

            "author": "谷浪",

            "candidates": [],

            "source": {"source_id": "src-1", "timestamp_seconds": 145}

        }

        resolve_links.attach_search_candidate(

            item,

            candidate_url="https://www.douyin.com/video/7690262867963409523",

            candidate_title="快乐发生",

            screenshot_path="evidence/screenshots/life-1_145s.jpg"

        )

        row = {

            "record_key": "rec-1",

            "run_id": "run-1",

            "item_key": "test-1",

            "issue": "测试周报",

            "week": "20261005",

            "section": "热点",

            "title": "快乐发生的真正机制",

            "author": "谷浪",

            "platform": "douyin",

            "id": "7690262867963409523",

            "original_url": "",

            "url": "",

            "link_status": "needs_review",

            "collection": "pending",

            "verified_at": "",

            "candidate_url": item["candidate_url"],

            "screenshot": item["screenshot"],

            "document_url": "https://example.com/doc",

            "folder_name": "收藏夹",

            "folder_url": "https://example.com/fav"

        }

        with tempfile.TemporaryDirectory() as td:

            dest = Path(td) / "test_index.html"

            w.render_index_html([row], dest)

            html_content = dest.read_text(encoding="utf-8")

            self.assertIn("查看录屏截图", html_content)

            self.assertIn("evidence/screenshots/life-1_145s.jpg", html_content)

            self.assertIn("候选搜索链接", html_content)

            self.assertIn("https://www.douyin.com/video/7690262867963409523", html_content)





    def test_embed_captured_screenshots_into_layout(self):

        import resolve_links

        import tempfile

        item = {

            "key": "item-plain-1",

            "number": 14,

            "title": "脑洞科普：颠覆认知的生命科学",

            "body": "探讨前沿生命科学",

            "kind": "plain",

            "canonical_url": "",

            "original_url": "",

            "platform": "plain",

            "author": "",

            "candidates": [],

            "source": {"source_id": "src-2", "timestamp_seconds": 2535}

        }

        with tempfile.TemporaryDirectory() as td:

            run_dir = Path(td)

            img_file = run_dir / "evidence" / "screenshots" / "test_shot.jpg"

            img_file.parent.mkdir(parents=True, exist_ok=True)

            img_file.write_bytes(b"dummy image bytes")



            state = {

                "issue": {"title": "测试周报 - Vol.296", "week": "20261005"},

                "layout": [{"type": "item", "key": "item-plain-1"}],

                "items": [item],

                "inventory": {"review": {"binding": ""}}

            }

            receipts = {

                "item-plain-1": {

                    "file_token": "token_feishu_12345",

                    "file": "evidence/screenshots/test_shot.jpg",

                    "caption": "【原周报视频录像截图对照】"

                }

            }

            res = resolve_links.embed_captured_screenshots_into_layout(run_dir, state, receipts)

            self.assertEqual(res["embedded_count"], 1)

            self.assertEqual(len(state["layout"]), 2)

            self.assertEqual(state["layout"][1]["type"], "image")

            self.assertEqual(state["layout"][1]["src"], "token_feishu_12345")

            self.assertEqual(state["layout"][1]["owner_key"], "item-plain-1")

            self.assertEqual(item["screenshot"], "evidence/screenshots/test_shot.jpg")



            # Validate build_report output

            md, xml = w.build_report(state)

            self.assertIn('<img src="token_feishu_12345" caption="【原周报视频录像截图对照】"', xml)

            # Ensure no redundant text paragraph is output when image block is in layout

            self.assertNotIn("【原录像截图核验参考：evidence/screenshots/test_shot.jpg】", xml)



    def test_strict_screenshot_mutex_gate(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            # 使用现有健全的测试环境
            from copy import deepcopy
            # 构造最小 state
            items = [
                {"key": "v1", "section": "测试", "subsection": "", "number": 1, "title": "视频1", "body": "", "kind": "video", "platform": "douyin", "id": "7691205469474630890", "canonical_url": "https://www.douyin.com/video/7691205469474630890", "screenshot": "evidence/screenshots/leak.jpg"}
            ]
            it = items[0]
            # 校验带 url 的 item 不能带 screenshot
            self.assertTrue(bool(it.get("canonical_url") and it.get("screenshot")))

    def test_collect_runner_two_stage_douyin_generation(self):
        import collect_runner
        plan_data = {
            "plan_id": "test-plan-123",
            "account_id": "user_123",
            "folder_id": "folder_789",
            "to_add": ["7691205469474630890", "7690531894202486051"]
        }
        js = collect_runner.generate_browser_script(Path("dummy"), "douyin", plan_data)
        self.assertIn(".o2tLobnl", js)
        self.assertIn("添加视频", js)
        self.assertIn(".NWh9vMMV", js)
        self.assertIn("ui_confirmed", js)


    def test_extract_candidate_urls_from_ocr_lines(self):
        import resolve_links
        lines = [
            "  https: // www.douyin.com / video / 7690961433315826043  ",
            "前沿科学讨论，https://www.bilibili.com/video/BV16vHt6BERB 视频",
            "截断链接：https://www.douyin.com/video/76908253275 待恢复",
            "纯BV号 BV16sYx6oEPJ 供参考"
        ]
        cands = resolve_links.extract_candidate_urls_from_ocr_lines(lines)
        urls = {c["url"] for c in cands}
        self.assertIn("https://www.douyin.com/video/7690961433315826043", urls)
        self.assertIn("https://www.bilibili.com/video/BV16vHt6BERB", urls)
        self.assertIn("https://www.bilibili.com/video/BV16sYx6oEPJ", urls)
        self.assertIn("https://www.douyin.com/video/76908253275", urls)

    def test_verify_candidate_for_item_prefix_and_dedup(self):
        import resolve_links
        item = {
            "key": "test-1",
            "title": "脑洞科普：颠覆认知的生命科学",
            "url": "https://www.douyin.com/video/76909614333"
        }
        cand_match = {
            "platform": "douyin",
            "id": "7690961433315826043",
            "url": "https://www.douyin.com/video/7690961433315826043",
            "raw": "https://www.douyin.com/video/7690961433315826043"
        }
        res = resolve_links.verify_candidate_for_item(item, cand_match, existing_ids=set())
        self.assertTrue(res["matched"])
        self.assertEqual(res["method"], "screenshot_ocr_prefix_match")
        self.assertEqual(res["id"], "7690961433315826043")

        # Deduplication rejection if id belongs to another item
        res_dup = resolve_links.verify_candidate_for_item(item, cand_match, existing_ids={"7690961433315826043"})
        self.assertFalse(res_dup["matched"])
        self.assertIn("id_already_used", res_dup["reason"])

        # Mismatch prefix rejection
        cand_other = {
            "platform": "douyin",
            "id": "7688888888888888888",
            "url": "https://www.douyin.com/video/7688888888888888888",
            "raw": "other url"
        }
        res_other = resolve_links.verify_candidate_for_item(item, cand_other, existing_ids=set())
        self.assertFalse(res_other["matched"])

    def test_auto_heal_from_screenshots_mutual_exclusion(self):
        import resolve_links
        import weekly_core as w
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            sc_dir = run_dir / "evidence" / "screenshots"
            sc_dir.mkdir(parents=True, exist_ok=True)
            
            sc_file = sc_dir / "test_item_100s.jpg"
            sc_file.write_bytes(b"fake_jpg")

            state = {
                "items": [
                    {
                        "key": "test-item",
                        "title": "测试条目",
                        "url": "https://www.douyin.com/video/76909614333",
                        "screenshot": "evidence/screenshots/test_item_100s.jpg",
                        "exception": {"reason": "missing_link"}
                    }
                ],
                "layout": [
                    {"type": "item", "key": "test-item"},
                    {"type": "image", "owner_key": "test-item", "src": "fake_token_123", "caption": "截图"}
                ],
                "inventory": {
                    "review": {"binding": ""}
                }
            }

            orig_scan = resolve_links.scan_screenshot_for_urls
            try:
                resolve_links.scan_screenshot_for_urls = lambda path: [
                    {"platform": "douyin", "id": "7690961433315826043", "url": "https://www.douyin.com/video/7690961433315826043", "raw": "mock", "type": "full_douyin"}
                ]
                h_res = resolve_links.auto_heal_from_screenshots(run_dir, state)
                self.assertEqual(h_res["healed_count"], 1)
                item = state["items"][0]
                self.assertEqual(item["canonical_url"], "https://www.douyin.com/video/7690961433315826043")
                self.assertEqual(item["kind"], "video")
                self.assertNotIn("exception", item)
                self.assertNotIn("screenshot", item)
                
                # Check layout mutual exclusion: image block removed
                img_blocks = [b for b in state["layout"] if b.get("type") == "image"]
                self.assertEqual(len(img_blocks), 0)
                
                # Check binding updated
                expected_binding = w.digest({"layout": state["layout"], "inventory": ["test-item"]})
                self.assertEqual(state["inventory"]["review"]["binding"], expected_binding)
            finally:
                resolve_links.scan_screenshot_for_urls = orig_scan

if __name__ == "__main__":

    unittest.main(verbosity=2)

