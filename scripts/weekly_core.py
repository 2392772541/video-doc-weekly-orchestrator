"""Evidence-first weekly workflow. Standard library only; never writes to a website."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

SCHEMA = 2
VIDEO_PLATFORMS = {"douyin", "bilibili"}
MAX_JSON = 12 * 1024 * 1024
LINK_AGE = 86400
SNAPSHOT_AGE = 900
WAIT_SECONDS = 5
BVID = re.compile(r"BV[A-Za-z0-9]{10}\Z")
AWEME = re.compile(r"[0-9]{19}\Z")
SECRET_KEYS = {"cookie", "cookies", "authorization", "access_token", "refresh_token", "password", "client_secret", "bili_jct", "msToken", "a_bogus"}


class GateError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise GateError(message)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def stamp(value):
    require(isinstance(value, str) and value, "timestamp_required")
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(d.tzinfo is not None, "timestamp_needs_timezone")
        return d.timestamp()
    except (TypeError, ValueError) as e:
        raise GateError("invalid_timestamp: " + str(value)) from e


def fresh(value, max_age, at=None):
    delta = stamp(at or now()) - stamp(value)
    require(0 <= delta <= max_age, "stale_or_future_evidence: " + str(value))


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(compact(value).encode("utf-8")).hexdigest()


def read_json(path):
    path = Path(path)
    require(path.stat().st_size <= MAX_JSON, "json_too_large")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".weekly-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def no_secrets(value):
    if isinstance(value, dict):
        for k, v in value.items():
            require(str(k).casefold() not in {x.casefold() for x in SECRET_KEYS}, "secret_field_not_allowed: " + str(k))
            no_secrets(v)
    elif isinstance(value, list):
        for v in value:
            no_secrets(v)


def clean_text(value):
    return re.sub(r"\s+", "", html.unescape(str(value or ""))).strip()


def normalize_url(value):
    """Syntactic normalization, NOT source identity or availability verification."""
    require(isinstance(value, str), "url_must_be_string")
    value = html.unescape(value.strip())
    if value.startswith(("www.", "m.douyin.", "m.bilibili.")):
        value = "https://" + value
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError as e:
        raise GateError("invalid_url_structure") from e
    require(u.scheme in ("http", "https") and u.hostname and not u.username and not u.password, "invalid_http_url")
    require(not re.search(r"[\s\x00-\x1f]", value), "url_contains_whitespace")
    host = u.hostname.lower()
    require(port in (None, 80, 443), "unexpected_url_port")
    if host in {"b23.tv", "v.douyin.com"}:
        raise GateError("short_url_needs_observed_redirect")
    if host in {"www.douyin.com", "douyin.com", "m.douyin.com"}:
        m = re.fullmatch(r"/(video|note)/([0-9]+)/?", u.path)
        if m:
            kind, identity = m.groups()
        elif u.path in ("", "/") and len(parse_qs(u.query).get("modal_id", [])) == 1:
            kind, identity = "video", parse_qs(u.query)["modal_id"][0]
        else:
            raise GateError("douyin_not_a_work_url")
        require(bool(AWEME.fullmatch(identity)), "douyin_id_requires_19_digits_do_not_pad")
        return {"platform": "douyin", "id": identity, "url": f"https://www.douyin.com/{kind}/{identity}"}
    if host in {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}:
        m = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", u.path)
        require(bool(m), "bilibili_needs_full_bvid_video_url")
        identity = m.group(1)
        query = parse_qs(u.query).get("p", [])
        require(len(query) <= 1 and (not query or (query[0].isdigit() and int(query[0]) > 0)), "invalid_bilibili_part")
        suffix = "?p=" + str(int(query[0])) if query else ""
        return {"platform": "bilibili", "id": identity, "url": f"https://www.bilibili.com/video/{identity}" + suffix}
    require(host not in {"live.bilibili.com", "live.douyin.com"}, "live_url_not_a_weekly_work")
    return {"platform": "web", "id": "", "url": urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, u.query, u.fragment))}


def valid_id(platform, value):
    require(isinstance(value, str), "ids_must_remain_strings")
    pattern = BVID if platform == "bilibili" else AWEME
    require(bool(pattern.fullmatch(value)), "invalid_" + platform + "_id")
    return value


def file_evidence(run, paths):
    require(isinstance(paths, list) and paths, "evidence_files_required")
    result = []
    root = Path(run).resolve()
    for name in paths:
        require(isinstance(name, str) and not Path(name).is_absolute(), "evidence_must_be_run_relative")
        p = (root / name).resolve()
        require(p.is_relative_to(root / "evidence") and p.is_file(), "evidence_missing_or_outside_run: " + name)
        require(0 < p.stat().st_size <= MAX_JSON, "evidence_file_empty_or_too_large")
        result.append({"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    return result


def check_evidence(run, refs):
    require(isinstance(refs, list) and refs, "evidence_references_missing")
    for ref in refs:
        actual = file_evidence(run, [ref["path"]])[0]
        require(actual["sha256"] == ref["sha256"], "evidence_changed: " + ref["path"])


def clean_heading_text(val):
    s = str(val or "").strip()
    return re.sub(r"^#+\s*", "", s).strip()


def default_layout(items):
    layout, group = [], None
    for it in items:
        sec = clean_heading_text(it.get("section") or "未分组")
        raw_sub = it.get("subsection") or it.get("sub_section") or it.get("subcategory") or it.get("sub_category") or ""
        subsec = clean_heading_text(raw_sub)
        if subsec == sec:
            subsec = ""
        pair = (sec, subsec)
        if pair != group:
            if group is None or pair[0] != group[0]:
                layout.append({"type": "heading", "level": 2, "text": pair[0]})
            if pair[1]:
                layout.append({"type": "heading", "level": 3, "text": pair[1]})
            group = pair
        layout.append({"type": "item", "key": it["key"]})
    return layout


def initialize(seed, mode="full"):
    require(mode in {"full", "publish", "collect", "audit"}, "invalid_mode")
    require(isinstance(seed.get("items"), list) and seed["items"], "items_required")
    no_secrets(seed)
    require(mode in {"collect", "audit"} or seed.get("scope") != "collection_inventory_only_not_full_report", "video_only_inventory_cannot_publish_full_report")
    items = []
    for n, src in enumerate(seed["items"], 1):
        raw = src.get("original_url", src.get("url", ""))
        require(isinstance(raw, str), "original_url_must_be_string")
        try:
            link = normalize_url(raw)
        except GateError:
            link = {"url": "", "platform": "douyin" if "douyin.com" in raw else "bilibili" if "bilibili.com/video" in raw else "web" if raw else "plain", "id": ""}
        kind = src.get("kind") or ("video" if link["platform"] in VIDEO_PLATFORMS else "link" if raw else "plain")
        sec = clean_heading_text(src.get("section") or "未分组")
        raw_sub = src.get("subsection") or src.get("sub_section") or src.get("subcategory") or src.get("sub_category") or ""
        subsec = clean_heading_text(raw_sub)
        if subsec == sec:
            subsec = ""
        it = {"key": src.get("key", f"i{n:04d}"), "section": sec, "subsection": subsec, "number": src.get("number", n), "title": src.get("title", ""), "body": src.get("body", ""), "kind": kind, "original_url": raw, "canonical_url": "", "platform": link["platform"], "id": "", "author": "", "source": None, "verification": None, "candidates": [], "repeat_of": src.get("repeat_of"), "exception": None}
        items.append(it)
    require(len({x["key"] for x in items}) == len(items), "duplicate_item_key")
    require(all(x["title"] for x in items), "empty_item_title")
    def_layout = default_layout(items)
    if "layout" in seed and seed["layout"]:
        items_have_subsections = any(i.get("subsection") for i in items)
        layout_has_l3 = any(b.get("type") == "heading" and b.get("level") == 3 for b in seed["layout"])
        if items_have_subsections and not layout_has_l3:
            layout = copy.deepcopy(def_layout)
        else:
            layout = seed["layout"]
    else:
        layout = def_layout
    return {"schema_version": SCHEMA, "run_id": str(uuid.uuid4()), "created_at": now(), "updated_at": now(), "mode": mode, "scope": seed.get("scope", "full_report"), "issue": {"title": seed.get("title", "未命名周报"), "week": seed.get("source_week", "")}, "permissions": {"publish": False, "collect": [], "user_request": ""}, "sources": {}, "inventory": {"expected_numbered": seed.get("numbered_item_count", sum(i["kind"] != "quick_news" for i in items)), "expected_quick_news": seed.get("quick_news_count", sum(i["kind"] == "quick_news" for i in items)), "review": None}, "layout": layout, "items": items, "target_names": {"feishu": seed.get("folder_name", "水球泡周报"), "douyin": "水球泡视频周报", "bilibili": "水球泡周报视频"}, "targets": {}, "publication": {}, "rendered": {}, "snapshots": {"douyin": [], "bilibili": []}, "plans": {}, "attempts": [], "status": "draft", "migration_note": "Existing URLs and old success flags are NOT verification. Review complete layout, introductions and quick news against source."}


def get_item(state, key):
    found = [i for i in state["items"] if i["key"] == key]
    require(len(found) == 1, "unknown_or_duplicate_item_key: " + str(key))
    return found[0]


def content_hash(state):
    keys = ("key", "title", "body", "section", "subsection", "number", "kind", "canonical_url", "platform", "id", "author", "repeat_of", "exception")
    return digest({"issue": state["issue"], "layout": state["layout"], "items": [{k: i.get(k) for k in keys} for i in state["items"]]})


def validate_source(state, run, it):
    src = it.get("source")
    require(isinstance(src, dict), "source_anchor_required")
    parent = state["sources"].get(src.get("source_id"))
    require(parent is not None, "source_not_registered")
    require(src.get("ownership") == "same_item", "adjacent_or_uncertain_source_ownership")
    require(clean_text(src.get("item_title")) == clean_text(it["title"]), "source_title_not_bound_to_item")
    check_evidence(run, src.get("evidence"))
    if parent["kind"] == "video":
        p = Path(parent["path"])
        require(p.is_file() and p.stat().st_size == parent["size"] and p.stat().st_mtime_ns == parent["mtime_ns"], "source_video_changed_or_missing")
        t = src.get("timestamp_seconds")
        require(type(t) in (int, float) and math.isfinite(t) and 0 <= t <= parent.get("duration_seconds", float("inf")), "invalid_source_timestamp")
    else:
        check_evidence(run, parent["evidence"])
        require(src.get("revision_id") == parent["revision_id"], "source_revision_mismatch")


def validate_verified(state, run, it, at=None):
    validate_source(state, run, it)
    if it["kind"] == "plain" or (it["kind"] == "quick_news" and not it["original_url"] and not it["canonical_url"]):
        require(not it["canonical_url"], "plain_item_has_link")
        return
    v = it.get("verification")
    require(isinstance(v, dict), "unverified_link")
    require(v.get("binding") == digest({"title": it["title"], "url": it["canonical_url"], "source": it["source"]}), "verification_binding_stale")
    link = normalize_url(it["canonical_url"])
    require(link["platform"] == it["platform"] and link["id"] == it["id"], "canonical_identity_mismatch")
    require((it["kind"] == "video") == (link["platform"] in VIDEO_PLATFORMS), "kind_platform_mismatch")
    check_evidence(run, v.get("evidence"))
    identity = v.get("identity", {})
    require(identity.get("same_work") is True and identity.get("title_match") is True, "same_work_identity_not_proven")
    method = identity.get("method")
    if method == "source_url":
        require(normalize_url(it["source"].get("observed_url", ""))["url"] == link["url"], "source_url_not_same_as_resolved_url")
    elif method == "title_author_visual":
        require(identity.get("author_match") is True and identity.get("visual_match") is True, "recovery_needs_author_and_visual_match")
        require(it["source"].get("author") and clean_text(it["source"]["author"]) == clean_text(v.get("observed_author")), "recovery_author_not_grounded_in_source")
        require(len({r["path"] for r in v["evidence"] + it["source"]["evidence"]}) >= 2, "recovery_needs_independent_evidence")
    elif method == "observed_redirect":
        require(identity.get("redirect_from") == it["source"].get("observed_url") and identity.get("redirect_to") == link["url"], "redirect_chain_mismatch")
        require(urlsplit(identity.get("redirect_from", "")).hostname in {"v.douyin.com", "b23.tv"}, "redirect_source_not_short_link")
    else:
        raise GateError("identity_method_rejected: " + str(method))
    require(v.get("observed_title") and identity.get("reason"), "identity_review_description_required")
    a = v.get("availability", {})
    require(a.get("status") == "available", "not_available: " + str(a.get("status")))
    fresh(a.get("checked_at"), LINK_AGE, at)
    if link["platform"] in VIDEO_PLATFORMS:
        require(a.get("observed_id") == link["id"], "observed_video_id_mismatch")
    if link["platform"] == "bilibili" and a.get("method") == "api":
        m = a.get("metadata", {})
        require(type(m.get("code")) is int and m["code"] == 0 and type(m.get("state")) is int and m["state"] == 0 and type(m.get("videos")) is int and m["videos"] > 0, "bilibili_code0_is_not_playability")
    elif a.get("method") == "ui":
        require(a.get("detail_visible") is True and a.get("unavailable_banner") is False, "ui_availability_not_confirmed")
    elif link["platform"] == "douyin" and a.get("method") == "api":
        require(type(a.get("business_code")) is int and a["business_code"] == 0 and a.get("detail_present") is True, "douyin_detail_missing")
    elif link["platform"] == "web" and a.get("method") == "http":
        require(type(a.get("http_status")) is int and 200 <= a["http_status"] < 400 and a.get("content_matches") is True, "web_http200_not_identity")
    else:
        raise GateError("availability_method_not_supported")


def audit(state, run, at=None):
    errors, exceptions = [], []
    def check(label, fn):
        try:
            fn()
        except (GateError, KeyError, TypeError, ValueError, OSError) as e:
            errors.append({"item": label, "error": str(e)})
    require(state.get("schema_version") == SCHEMA, "unsupported_schema")
    items = state["items"]
    require(items, "empty_manifest")
    inv = state["inventory"]
    def inventory_check():
        require(sum(i["kind"] != "quick_news" for i in items) == inv["expected_numbered"], "numbered_inventory_mismatch")
        require(sum(i["kind"] == "quick_news" for i in items) == inv["expected_quick_news"], "quick_news_inventory_mismatch")
        review = inv.get("review") or {}
        require(review.get("all_source_parts_reviewed") is True and review.get("layout_reviewed") is True, "source_structure_not_reviewed")
        require(review.get("binding") == digest({"layout": state["layout"], "inventory": [i["key"] for i in items]}), "inventory_review_stale")
        check_evidence(run, review.get("evidence"))
        order = [b["key"] for b in state["layout"] if b["type"] == "item"]
        require(order == [i["key"] for i in items], "layout_must_contain_each_item_once_in_source_order")
        for b in state["layout"]:
            require(b["type"] in {"item", "heading", "paragraph", "image"}, "unsupported_layout_block")
            if b["type"] == "heading":
                require(type(b.get("level")) is int and 1 <= b["level"] <= 6 and b.get("text"), "bad_heading")
            if b["type"] == "paragraph":
                paragraph_runs(b)
            if b["type"] == "image":
                require(isinstance(b.get("src"), str) and re.fullmatch(r"[A-Za-z0-9_-]{8,200}", b["src"]), "image_requires_real_feishu_src_token")
                require(b.get("owner_key") in {i["key"] for i in items}, "image_owner_required")
                owner_item = next((i for i in items if i["key"] == b["owner_key"]), None)
                if owner_item and owner_item.get("canonical_url") and (owner_item.get("screenshot") or "录像截图对照" in b.get("caption", "")):
                    require(False, f"item_with_url_must_not_have_screenshot:{b['owner_key']}")
                position = state["layout"].index(b)
                preceding = [x["key"] for x in state["layout"][:position] if x["type"] == "item"]
                require(preceding and preceding[-1] == b["owner_key"], "image_must_follow_owning_item")
                require(b.get("caption"), "image_caption_required")
                check_evidence(run, b.get("evidence"))
    check("inventory", inventory_check)
    seen, numbering = {}, set()
    for it in items:
        require(it["kind"] in {"video", "link", "plain", "quick_news"}, "unsupported_item_kind")
        n = (it["section"], it["subsection"], it["number"])
        if it["kind"] != "quick_news" and n in numbering:
            errors.append({"item": it["key"], "error": "duplicate_number_in_section"})
        numbering.add(n)
        if it.get("canonical_url") and it.get("screenshot"):
            errors.append({"item": it["key"], "error": f"item_with_url_must_not_have_screenshot:{it['key']}"})
        if it.get("exception"):
            exceptions.append(it["key"])
            check(it["key"], lambda it=it: validate_source(state, run, it))
            check(it["key"], lambda it=it: check_evidence(run, it["exception"].get("evidence")))
        else:
            check(it["key"], lambda it=it: validate_verified(state, run, it, at))
        u = it.get("canonical_url")
        if u and u in seen and it.get("repeat_of") != seen[u]:
            errors.append({"item": it["key"], "error": "duplicate_url_requires_source_verified_repeat_of"})
        if u:
            seen.setdefault(u, it["key"])
    counts = {"items": len(items), "numbered": sum(i["kind"] != "quick_news" for i in items), "quick_news": sum(i["kind"] == "quick_news" for i in items), "douyin": sum(i["platform"] == "douyin" for i in items), "bilibili": sum(i["platform"] == "bilibili" for i in items)}
    return {"ok": not errors and not exceptions, "publishable_partial": not errors and bool(exceptions), "errors": errors, "exceptions": exceptions, "counts": counts, "content_hash": content_hash(state), "checked_at": at or now()}


def apply_events(state, run, events, at=None):
    state = copy.deepcopy(state)
    no_secrets(events)
    require(isinstance(events, list) and events, "events_array_required")
    for e in events:
        typ = e["type"]
        if typ == "permissions":
            require(e.get("user_request"), "current_user_request_required")
            require(type(e.get("publish", False)) is bool, "publish_permission_must_be_bool")
            platforms = e.get("collect", [])
            require(isinstance(platforms, list) and set(platforms) <= VIDEO_PLATFORMS, "bad_collect_permission")
            state["permissions"] = {"publish": e.get("publish", False), "collect": platforms, "user_request": e["user_request"]}
        elif typ == "register_source":
            sid = e["source_id"]
            require(sid not in state["sources"], "source_id_immutable_use_new_id_for_new_revision")
            if e["kind"] == "video":
                p = Path(e["path"]).resolve()
                require(p.is_file(), "source_video_missing")
                source = {"kind": "video", "path": str(p), "size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns}
                if "duration_seconds" in e:
                    require(type(e["duration_seconds"]) in (int, float) and 0 < e["duration_seconds"] < 1e7, "bad_duration")
                    source["duration_seconds"] = e["duration_seconds"]
            elif e["kind"] == "feishu":
                require(e.get("document_id") and e.get("revision_id") is not None, "document_and_revision_required")
                fresh(e["checked_at"], LINK_AGE, at)
                source = {"kind": "feishu", "document_id": e["document_id"], "revision_id": e["revision_id"], "checked_at": e["checked_at"], "evidence": file_evidence(run, e["evidence_files"])}
            else:
                raise GateError("unsupported_source_kind")
            state["sources"][sid] = source
        elif typ == "item":
            it = get_item(state, e["key"])
            allowed = {"title", "body", "section", "subsection", "number", "kind", "repeat_of"}
            require(set(e["changes"]) <= allowed, "item_patch_cannot_override_identity_or_success")
            it.update(e["changes"])
            it["verification"], it["source"] = None, None
        elif typ == "add_item":
            seed = initialize({"items": [e["item"]]})["items"][0]
            require(seed["key"] not in {i["key"] for i in state["items"]}, "duplicate_item_key")
            state["items"].append(seed)
            state["inventory"]["review"] = None
        elif typ == "layout":
            require(isinstance(e["blocks"], list) and e["blocks"], "layout_blocks_required")
            blocks = copy.deepcopy(e["blocks"])
            for block in blocks:
                if block.get("type") == "image":
                    require("evidence_files" in block, "image_upload_and_source_evidence_required")
                    block["evidence"] = file_evidence(run, block.pop("evidence_files"))
            state["layout"] = blocks
            state["inventory"]["review"] = None
        elif typ == "inventory":
            require(e.get("all_source_parts_reviewed") is True and e.get("layout_reviewed") is True, "inventory_review_incomplete")
            for k in ("expected_numbered", "expected_quick_news"):
                require(type(e.get(k)) is int and e[k] >= 0, "invalid_inventory_count")
                state["inventory"][k] = e[k]
            state["inventory"]["review"] = {"all_source_parts_reviewed": True, "layout_reviewed": True, "binding": digest({"layout": state["layout"], "inventory": [i["key"] for i in state["items"]]}), "evidence": file_evidence(run, e["evidence_files"])}
        elif typ == "source":
            it = get_item(state, e["key"])
            src = {k: e[k] for k in ("source_id", "item_title", "ownership", "observed_url", "author", "timestamp_seconds", "revision_id") if k in e}
            src["evidence"] = file_evidence(run, e["evidence_files"])
            it["source"], it["verification"] = src, None
            validate_source(state, run, it)
        elif typ == "candidate":
            it = get_item(state, e["key"])
            it["candidates"].append({"url": e["url"], "title": e.get("title", ""), "author": e.get("author", ""), "match_type": e["match_type"], "reason": e["reason"], "evidence": file_evidence(run, e["evidence_files"]), "recorded_at": at or now()})
        elif typ == "verify":
            it = get_item(state, e["key"])
            require(e.get("identity", {}).get("method") not in {"similar_topic_tutorial", "similar_topic", "title_only", "exact_prefix_and_title"}, "similar_or_prefix_only_candidate_is_not_original")
            link = normalize_url(e["url"])
            it.update(canonical_url=link["url"], platform=link["platform"], id=link["id"], author=e.get("observed_author", ""), exception=None)
            v = {k: e[k] for k in ("identity", "availability", "observed_title", "observed_author") if k in e}
            v["evidence"] = file_evidence(run, e["evidence_files"])
            v["binding"] = digest({"title": it["title"], "url": it["canonical_url"], "source": it["source"]})
            it["verification"] = v
            validate_verified(state, run, it, at)
        elif typ == "exception":
            it = get_item(state, e["key"])
            require(e.get("user_request") and e.get("reason"), "explicit_user_acceptance_of_missing_original_required")
            validate_source(state, run, it)
            it["exception"] = {"user_request": e["user_request"], "reason": e["reason"], "evidence": file_evidence(run, e["evidence_files"])}
            it["canonical_url"], it["verification"] = "", None
        elif typ == "target":
            platform = e["platform"]
            require(platform in VIDEO_PLATFORMS | {"feishu"}, "bad_target_platform")
            require(e.get("authenticated") is True and isinstance(e.get("account_id"), str) and e["account_id"], "authenticated_account_id_required")
            require(e.get("complete") is True and isinstance(e.get("folders"), list), "incomplete_folder_discovery")
            fresh(e["checked_at"], SNAPSHOT_AGE, at)
            desired = e.get("desired_name", state["target_names"][platform])
            matches = [f for f in e["folders"] if f["name"] == desired]
            require(len(matches) == 1, "folder_name_must_match_exactly_one")
            folder = matches[0]
            field = "collects_id_str" if platform == "douyin" else "id"
            require(isinstance(folder.get(field), str) and folder[field], "folder_id_must_be_original_string_field: " + field)
            if platform == "douyin":
                require(bool(AWEME.fullmatch(folder[field])), "bad_collects_id_str")
            previous = state["targets"].get(platform)
            if previous and (previous["account_id"], previous["folder_id"]) != (e["account_id"], folder[field]):
                require(not any(a["platform"] == platform for a in state["attempts"]), "target_change_after_writes_requires_new_run")
                require(platform != "feishu" or not state.get("publication") and not state.get("publication_attempt"), "publication_target_change_requires_new_run")
            if previous and previous["account_id"] != e["account_id"]:
                require(e.get("account_change_user_request"), "account_changed_needs_user_confirmation")
            target = {"account_id": e["account_id"], "folder_id": folder[field], "folder_name": desired, "folder_url": folder.get("url", ""), "checked_at": e["checked_at"], "evidence": file_evidence(run, e["evidence_files"]), "allow_empty_initial": e.get("allow_empty_initial") is True}
            if platform == "feishu":
                require(e.get("document_search_complete") is True, "same_issue_document_search_not_complete")
                docs = e.get("same_issue_documents", [])
                require(isinstance(docs, list) and len(docs) <= 1, "multiple_same_issue_documents_do_not_auto_move_or_delete")
                target["existing_document_id"] = docs[0]["id"] if docs else ""
            state["target_names"][platform], state["targets"][platform] = desired, target
            if platform in state["plans"]:
                del state["plans"][platform]
        else:
            raise GateError("unsupported_event_type: " + str(typ))
    state["updated_at"], state["status"] = at or now(), "in_progress"
    return state


def expected_ids(state, platform):
    return list(dict.fromkeys(i["id"] for i in state["items"] if i["platform"] == platform and i["kind"] == "video" and not i.get("exception")))


def parse_snapshot(state, run, raw, at=None):
    no_secrets(raw)
    platform = raw["platform"]
    require(platform in VIDEO_PLATFORMS, "unsupported_snapshot_platform")
    target = state["targets"].get(platform)
    require(target, "resolve_target_before_snapshot")
    for k in ("account_id", "folder_id", "folder_name"):
        require(raw.get(k) == target[k], "snapshot_target_mismatch: " + k)
    fresh(raw["finished_at"], SNAPSHOT_AGE, at)
    require(stamp(raw["started_at"]) <= stamp(raw["finished_at"]), "snapshot_time_order")
    method = raw.get("method", "api")
    require(method in {"api", "ui"}, "snapshot_method_must_be_api_or_ui")
    pages, ids, seen, previous_next = raw.get("pages"), [], set(), None
    require(isinstance(pages, list) and 1 <= len(pages) <= 200, "snapshot_pages_required_max200")
    for n, page in enumerate(pages):
        if method == "api":
            require(type(page.get("business_code")) is int and page["business_code"] == 0, "snapshot_business_error")
        else:
            require(page.get("target_visible") is True and page.get("fully_loaded") is True, "ui_snapshot_not_loaded_or_wrong_target")
            require(page.get("has_more") is True or page.get("end_visible") is True, "ui_needs_visible_terminal_state_not_scroll_guess")
            file_evidence(run, page.get("evidence_files"))
        cursor = page.get("request_cursor")
        require(isinstance(cursor, str) and cursor not in seen, "repeated_or_invalid_page_cursor")
        require(n == 0 or cursor == previous_next, "pagination_gap")
        seen.add(cursor)
        require(type(page.get("has_more")) is bool, "explicit_pagination_end_required")
        require(isinstance(page.get("items"), list), "snapshot_items_not_array")
        for entry in page["items"]:
            if entry.get("unavailable") is True and not entry.get("id"):
                require(isinstance(entry.get("provider_id"), str) and entry["provider_id"], "tombstone_needs_provider_id")
                continue
            ids.append(valid_id(platform, entry.get("id")))
        if page["has_more"]:
            previous_next = page.get("next_cursor")
            require(isinstance(previous_next, str) and previous_next and previous_next not in seen and n < len(pages)-1, "pagination_not_finished_or_looping")
        else:
            require(n == len(pages)-1 and page.get("next_cursor") in (None, ""), "pages_after_terminal_page")
    require(not pages[-1]["has_more"], "pagination_not_finished")
    require(ids or target.get("allow_empty_initial"), "empty_folder_not_assumed_complete_confirm_first")
    require(raw.get("snapshot_id") and isinstance(raw["snapshot_id"], str), "snapshot_id_required")
    old = state["snapshots"][platform]
    require(raw["snapshot_id"] not in {s["snapshot_id"] for s in old}, "snapshot_id_already_used")
    if old:
        require(stamp(raw["started_at"]) >= stamp(old[-1]["finished_at"]), "snapshot_not_newer_than_previous")
    return {"snapshot_id": raw["snapshot_id"], "method": method, "platform": platform, "account_id": raw["account_id"], "folder_id": raw["folder_id"], "folder_name": raw["folder_name"], "started_at": raw["started_at"], "finished_at": raw["finished_at"], "page_count": len(pages), "ids": list(dict.fromkeys(ids)), "evidence": file_evidence(run, list(dict.fromkeys(raw["evidence_files"] + [x for p in pages for x in p.get("evidence_files", [])]))), "pages_hash": digest(pages)}


def import_snapshot(state, run, raw, at=None):
    s = parse_snapshot(state, run, raw, at)
    platform = s["platform"]
    state["snapshots"][platform].append(s)
    state["plans"].pop(platform, None)
    for attempt in state["attempts"]:
        if attempt["platform"] == platform and attempt["account_id"] == s["account_id"] and attempt["folder_id"] == s["folder_id"] and stamp(s["started_at"]) > stamp(attempt.get("finished_at", attempt["started_at"])):
            attempt["observed_in_snapshot"] = s["snapshot_id"]
            attempt["observed_present"] = attempt["id"] in s["ids"]
    return s


def ready_for_collection(state, run, platform, at=None):
    require(platform in state["permissions"]["collect"] and state["permissions"]["user_request"], "collection_not_authorized_in_current_run")
    result = audit(state, run, at)
    require(result["ok"] or result["publishable_partial"], "link_or_inventory_gate_failed_run_audit")
    target = state["targets"].get(platform)
    require(target, "collection_target_not_resolved")
    fresh(target["checked_at"], LINK_AGE, at)
    check_evidence(run, target["evidence"])
    if state["mode"] in {"full", "publish"}:
        pub = state.get("publication", {})
        require(pub.get("verified") is True and pub.get("content_hash") == content_hash(state), "publish_or_repair_then_readback_before_collecting")
        fresh(pub["checked_at"], LINK_AGE, at)
        check_evidence(run, pub["evidence"])
    elif state["mode"] == "collect":
        for it in state["items"]:
            if it["platform"] == platform and not it.get("exception"):
                source = state["sources"][it["source"]["source_id"]]
                require(source["kind"] == "feishu", "collect_mode_requires_current_feishu_source")
                fresh(source["checked_at"], LINK_AGE, at)
                # Existing document links cannot be silently replaced only in the collection list.
                require(normalize_url(it["source"].get("observed_url", ""))["url"] == it["canonical_url"], "repaired_link_not_synced_to_source_document")
    else:
        raise GateError("audit_only_mode_cannot_collect")


def plan_collection(state, run, platform, at=None):
    ready_for_collection(state, run, platform, at)
    snapshots = state["snapshots"][platform]
    require(snapshots, "full_fresh_snapshot_required")
    s = snapshots[-1]
    target = state["targets"][platform]
    require(s["folder_id"] == target["folder_id"] and s["account_id"] == target["account_id"], "snapshot_from_old_target")
    fresh(s["finished_at"], SNAPSHOT_AGE, at)
    check_evidence(run, s["evidence"])
    for a in state["attempts"]:
        if a["platform"] == platform:
            require(stamp(s["started_at"]) > stamp(a.get("finished_at", a["started_at"])), "any_previous_write_requires_new_full_snapshot_before_replan")
    desired = expected_ids(state, platform)
    missing = [i for i in desired if i not in set(s["ids"])]
    plan = {"plan_id": str(uuid.uuid4()), "platform": platform, "account_id": target["account_id"], "folder_id": target["folder_id"], "folder_name": target["folder_name"], "content_hash": content_hash(state), "snapshot_id": s["snapshot_id"], "created_at": at or now(), "target_ids": desired, "existing_ids": [i for i in desired if i not in missing], "missing_ids": missing, "excluded_exceptions": [i["key"] for i in state["items"] if i["platform"] == platform and i.get("exception")]}
    state["plans"][platform] = plan
    return plan


def begin_write(state, run, platform, identity, plan_id, at=None):
    at = at or now()
    ready_for_collection(state, run, platform, at)
    plan = state["plans"].get(platform, {})
    require(plan.get("plan_id") == plan_id and plan.get("content_hash") == content_hash(state), "stale_or_wrong_plan")
    fresh(plan["created_at"], SNAPSHOT_AGE, at)
    require(identity in plan["missing_ids"], "id_not_in_fresh_missing_set")
    target = state["targets"][platform]
    require(plan["account_id"] == target["account_id"] and plan["folder_id"] == target["folder_id"], "plan_target_changed")
    relevant = [a for a in state["attempts"] if a["platform"] == platform]
    for a in relevant:
        require(a["outcome"] in {"acknowledged", "ui_confirmed"} or a.get("observed_in_snapshot"), "unreconciled_write_must_stop_batch")
        require(not(a["plan_id"] == plan_id and a["id"] == identity), "do_not_replay_same_plan_item")
    if relevant:
        require(stamp(at) - stamp(relevant[-1].get("finished_at", relevant[-1]["started_at"])) >= WAIT_SECONDS, "wait_at_least_5_seconds_between_writes")
    attempt = {"attempt_id": str(uuid.uuid4()), "platform": platform, "id": identity, "plan_id": plan_id, "account_id": target["account_id"], "folder_id": target["folder_id"], "started_at": at, "outcome": "in_flight"}
    state["attempts"].append(attempt)
    return attempt


def end_write(state, run, attempt_id, data, at=None):
    no_secrets(data)
    matches = [a for a in state["attempts"] if a["attempt_id"] == attempt_id]
    require(len(matches) == 1 and matches[0]["outcome"] == "in_flight", "attempt_not_in_flight")
    a = matches[0]
    outcome = data["outcome"]
    require(outcome in {"acknowledged", "ui_confirmed", "uncertain", "failed"}, "invalid_write_outcome")
    if outcome == "acknowledged":
        codes = data.get("codes", {})
        needed = ("favorite", "folder_add") if a["platform"] == "douyin" else ("favorite",)
        require(all(type(codes.get(k)) is int and codes[k] == 0 for k in needed), "all_write_business_codes_must_be_zero")
    if outcome == "ui_confirmed":
        ui = data.get("ui", {})
        require(all(ui.get(k) == a[k] for k in ("account_id", "folder_id", "id")), "ui_receipt_wrong_identity_or_folder")
        require(ui.get("favorite_visible") is True and ui.get("folder_selected_visible") is True and ui.get("reopened_detail") is True, "ui_receipt_requires_reopened_work_and_selected_target_folder")
        require(not data.get("codes"), "do_not_invent_business_codes_for_ui_receipt")
        a["ui"] = ui
    require(stamp(at or now()) >= stamp(a["started_at"]), "write_time_order")
    a.update(outcome=outcome, finished_at=at or now(), codes=data.get("codes", {}), detail=data.get("detail", ""), evidence=file_evidence(run, data["evidence_files"]))
    require(stamp(a["finished_at"]) >= stamp(a["started_at"]), "write_time_order")
    return a


def reconcile(state, run, platform, at=None):
    ready_for_collection(state, run, platform, at)
    desired = expected_ids(state, platform)
    snapshots = state["snapshots"][platform]
    require(len(snapshots) >= 2, "two_complete_readback_rounds_required")
    first, second = snapshots[-2:]
    require(stamp(second["started_at"]) - stamp(first["finished_at"]) >= WAIT_SECONDS, "rounds_must_be_at_least_5_seconds_apart")
    target = state["targets"][platform]
    writes = [a for a in state["attempts"] if a["platform"] == platform]
    for s in (first, second):
        require(s["account_id"] == target["account_id"] and s["folder_id"] == target["folder_id"], "readback_target_changed")
        fresh(s["finished_at"], LINK_AGE, at)
        check_evidence(run, s["evidence"])
        require(all(stamp(s["started_at"]) > stamp(a.get("finished_at", a["started_at"])) for a in writes), "readback_predates_last_write")
    missing = {"round1": [x for x in desired if x not in set(first["ids"])], "round2": [x for x in desired if x not in set(second["ids"])]}
    return {"ok": not any(missing.values()), "platform": platform, "target_count": len(desired), "folder_name": target["folder_name"], "folder_id": target["folder_id"], "account_id": target["account_id"], "missing": missing, "snapshot_ids": [first["snapshot_id"], second["snapshot_id"]], "pages": [first["page_count"], second["page_count"]], "verified_at": second["finished_at"], "content_hash": content_hash(state)}


CALLOUT = "阅读与收藏提示：黑色常规标题为抖音 / B站作品；红色加粗标题为图文长文 / 网页资讯 / 开源工具 / 随笔。未确认的原作品不会用相似视频顶替。"


def paragraph_runs(block):
    if "runs" not in block:
        require(isinstance(block.get("text"), str), "bad_paragraph")
        return [{"text": block["text"]}]
    runs = block["runs"]
    require(isinstance(runs, list) and runs, "paragraph_runs_required")
    for r in runs:
        require(isinstance(r.get("text"), str), "paragraph_run_text_required")
        if "href" in r:
            u = urlsplit(r["href"])
            require(u.scheme in {"https", "http"} and u.hostname and not u.username and not u.password and not re.search(r"[\s\x00-\x1f]", r["href"]), "invalid_context_link")
        require(set(r) <= {"text", "href", "bold"} and type(r.get("bold", False)) is bool, "unsupported_paragraph_style")
    return runs


def build_report(state):
    root = ET.Element("weekly-root")
    ET.SubElement(root, "title").text = state["issue"]["title"]
    callout = ET.SubElement(root, "callout", emoji="💡")
    ET.SubElement(callout, "p").text = CALLOUT
    md = ["# " + state["issue"]["title"], "", "> " + CALLOUT, ""]
    image_owner_keys = {x.get("owner_key") for x in state.get("layout", []) if x.get("type") == "image"}
    current_level = 2
    for b in state["layout"]:
        if b["type"] == "heading":
            current_level = b["level"]
            ET.SubElement(root, "h" + str(b["level"])).text = b["text"]
            md.extend(["#" * b["level"] + " " + b["text"], ""])
        elif b["type"] == "paragraph":
            p, pieces = ET.SubElement(root, "p"), []
            for r in paragraph_runs(b):
                dest = ET.SubElement(p, "a", href=r["href"]) if r.get("href") else p
                dest = ET.SubElement(dest, "b") if r.get("bold") else dest
                ET.SubElement(dest, "span").text = r["text"]
                if r.get("href"):
                    piece = '<a href="' + html.escape(r["href"], quote=True) + '">' + html.escape(r["text"]) + '</a>'
                else:
                    piece = html.escape(r["text"])
                pieces.append("**" + piece + "**" if r.get("bold") else piece)
            md.extend(["".join(pieces), ""])
        elif b["type"] == "image":
            ET.SubElement(root, "img", {"src": b["src"], "caption": b["caption"]})
            md.extend(["图片：" + b["caption"] + "（已上传飞书；本地原图见对应证据）", ""])
        else:
            it = get_item(state, b["key"])
            label = (str(it["number"]) + ". " if it["number"] is not None else "") + it["title"]
            item_level = min(current_level + 1, 6)
            h = ET.SubElement(root, "h" + str(item_level))
            if it["kind"] == "video":
                h.text = label
                md.append("#" * item_level + " " + label)
            else:
                bold = ET.SubElement(h, "b")
                ET.SubElement(bold, "span", {"text-color": "red"}).text = label
                md.append("#" * item_level + ' <b><span style="color:#d83931">' + html.escape(label) + '</span></b>')
            if it.get("exception"):
                notice = "原作品未确认，未替代、未收藏：" + it["exception"]["reason"]
                ET.SubElement(root, "p").text = notice
                md.append(notice)
            if it["body"]:
                ET.SubElement(root, "p").text = it["body"]
                md.append(it["body"])
            if it.get("candidate_url") and (not it.get("canonical_url") or it.get("exception")):
                cand_notice = "候选链接（待核验）：" + it["candidate_url"]
                p_cand = ET.SubElement(root, "p")
                p_cand.text = "候选链接（待核验）："
                ET.SubElement(p_cand, "a", href=it["candidate_url"]).text = it["candidate_url"]
                md.append(cand_notice)
            if it.get("screenshot") and b["key"] not in image_owner_keys:
                p_shot = ET.SubElement(root, "p")
                p_shot.text = "【原录像截图核验参考：" + it["screenshot"] + "】"
                md.append("【原录像截图核验参考：" + it["screenshot"] + "】")
            if it["canonical_url"] and not it.get("exception"):
                p = ET.SubElement(root, "p")
                ET.SubElement(p, "a", href=it["canonical_url"]).text = it["canonical_url"]
                md.append(it["canonical_url"])
            md.append("")
    xml = "".join(ET.tostring(child, encoding="unicode", short_empty_elements=False) for child in root)
    markdown = "\n".join(md)
    require(not re.search(r"reconstructed_doc|selected_frames|rapid_crops|ocr_work|link_crops|(?<![A-Za-z0-9])[A-Za-z]:[\\/]", markdown), "temporary_paths_leaked_into_clean_document")
    return markdown, xml


def render(state, run, allow_partial=False, at=None):
    result = audit(state, run, at)
    require(result["ok"] or (allow_partial and result["publishable_partial"]), "cannot_render_unverified_final_run_audit")
    markdown, xml = build_report(state)
    base = Path(run) / "report"
    atomic_text(base / "document.md", markdown)
    atomic_text(base / "weekly.xml", xml)
    state["rendered"] = {"content_hash": content_hash(state), "md_sha256": hashlib.sha256(markdown.encode()).hexdigest(), "xml_sha256": hashlib.sha256(xml.encode()).hexdigest(), "partial": not result["ok"], "at": at or now()}
    write_json(Path(run) / "audit" / "prepublish.json", result)
    return {"markdown": str(base / "document.md"), "xml": str(base / "weekly.xml"), **state["rendered"]}


def check_render(state, run):
    r = state.get("rendered", {})
    require(r.get("content_hash") == content_hash(state), "render_is_stale_regenerate_from_single_manifest")
    for name, key in (("document.md", "md_sha256"), ("weekly.xml", "xml_sha256")):
        p = Path(run) / "report" / name
        require(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == r[key], "render_file_changed: " + name)


def publish_plan(state, run, at=None):
    at = at or now()
    require(state["mode"] in {"full", "publish"}, "mode_does_not_publish")
    require(state["permissions"].get("publish") is True and state["permissions"]["user_request"], "publish_not_authorized")
    a = audit(state, run, at)
    require(a["ok"] or a["publishable_partial"], "publish_gate_failed")
    check_render(state, run)
    target = state["targets"].get("feishu")
    require(target, "feishu_folder_and_duplicate_search_required")
    fresh(target["checked_at"], SNAPSHOT_AGE, at)
    check_evidence(run, target["evidence"])
    pending = state.get("publication_attempt")
    require(not pending or stamp(target["checked_at"]) > stamp(pending["started_at"]), "previous_publish_uncertain_search_live_document_before_retry")
    saved = state.get("publication", {})
    if saved.get("verified") and saved.get("content_hash") == content_hash(state):
        action, doc_id = "verify_only", saved["document_id"]
    else:
        doc_id = target["existing_document_id"]
        action = "update_existing" if doc_id else "create_once"
    plan = {"action": action, "document_id": doc_id, "folder_id": target["folder_id"], "title": state["issue"]["title"], "content_hash": content_hash(state), "started_at": at, "xml": str(Path(run) / "report" / "weekly.xml")}
    state["publication_attempt"] = plan
    return plan


def xml_signature(xml):
    xml = re.sub(r"<\?xml[^>]*\?>", "", xml)
    root = ET.fromstring("<weekly-root>" + xml + "</weekly-root>")
    text = clean_text("".join(root.itertext()))
    links = [e.get("href") for e in root.iter() if e.tag == "a"]
    media = [(e.tag, e.get("src", e.get("token", e.get("href", "")))) for e in root.iter() if e.tag in {"img", "image", "file", "source", "video", "sheet", "bitable", "whiteboard", "iframe"}]
    return text, links, media


def readback_publication(state, run, data, at=None):
    no_secrets(data)
    require(state["permissions"].get("publish") is True, "publish_not_authorized")
    check_render(state, run)
    fresh(data["checked_at"], SNAPSHOT_AGE, at)
    target = state["targets"].get("feishu")
    require(target, "feishu_target_missing")
    require(data.get("account_id") == target["account_id"] and data.get("folder_id") == target["folder_id"], "publication_account_or_folder_mismatch")
    paths = [data["document_file"], data["folder_file"]] + data.get("style_evidence_files", [])
    refs = file_evidence(run, paths)
    fetched = read_json(Path(run) / data["document_file"])
    require(fetched.get("ok") is True and fetched.get("identity") == "user", "feishu_fetch_not_successful_user_read")
    doc = fetched["data"]["document"]
    require(doc.get("document_id") and doc.get("revision_id") is not None, "feishu_revision_missing")
    existing = target.get("existing_document_id")
    require(not existing or existing == doc["document_id"], "created_duplicate_instead_of_updating_existing")
    expected = (Path(run) / "report" / "weekly.xml").read_text(encoding="utf-8")
    require(xml_signature(doc["content"]) == xml_signature(expected), "live_document_text_links_order_or_media_mismatch")
    folder = read_json(Path(run) / data["folder_file"])
    require(folder.get("complete") is True and folder.get("folder_id") == target["folder_id"] and folder.get("account_id") == target["account_id"], "folder_listing_not_complete_or_wrong_target")
    fresh(folder["checked_at"], SNAPSHOT_AGE, at)
    matches = [d for d in folder.get("documents", []) if d.get("title") == state["issue"]["title"]]
    require(len(matches) == 1 and matches[0].get("id") == doc["document_id"], "folder_must_contain_exactly_one_current_final_document")
    require(data.get("style_reviewed") is True and data.get("style_evidence_files"), "black_red_format_needs_live_visual_or_full_style_evidence")
    state["publication"] = {"verified": True, "document_id": doc["document_id"], "revision_id": doc["revision_id"], "document_url": data.get("document_url", ""), "folder_id": target["folder_id"], "account_id": target["account_id"], "checked_at": data["checked_at"], "content_hash": content_hash(state), "partial": state["rendered"]["partial"], "evidence": refs}
    state.pop("publication_attempt", None)
    return state["publication"]


def finish(state, run, at=None):
    report = audit(state, run, at)
    output = {"status": "partial", "audit": report, "collections": {}, "errors": []}
    if state["mode"] in {"full", "publish"}:
        p = state.get("publication", {})
        if not(p.get("verified") and p.get("content_hash") == content_hash(state)):
            output["errors"].append("publication_not_currently_verified")
        else:
            try:
                check_render(state, run)
                check_evidence(run, p["evidence"])
                fresh(p["checked_at"], LINK_AGE, at)
            except (GateError, OSError) as e:
                output["errors"].append(str(e))
    if state["mode"] in {"full", "collect"}:
        for platform in sorted(VIDEO_PLATFORMS):
            if not any(i["kind"] == "video" and i["platform"] == platform and not i.get("exception") for i in state["items"]):
                output["collections"][platform] = {"ok": True, "not_applicable": True, "target_count": 0}
                continue
            try:
                c = reconcile(state, run, platform, at)
                output["collections"][platform] = c
                if not c["ok"]:
                    output["errors"].append(platform + ": missing_target_ids")
            except (GateError, OSError, KeyError) as e:
                output["collections"][platform] = {"ok": False, "error": str(e)}
                output["errors"].append(platform + ": " + str(e))
    if report["ok"] and not output["errors"]:
        output["status"] = "audit_passed" if state["mode"] == "audit" else "complete"
    state["status"] = output["status"]
    state["last_result"] = output
    return output


def index_rows(state, run, at=None):
    # At most two reconciliation passes, not one whole-manifest audit per item.
    collections = {}
    for platform in VIDEO_PLATFORMS:
        try:
            collections[platform] = reconcile(state, run, platform, at)
        except (GateError, OSError, KeyError, ValueError):
            collections[platform] = None
    rows = []
    for it in state["items"]:
        try:
            validate_verified(state, run, it, at)
            link_status = "verified"
        except (GateError, OSError, KeyError, ValueError):
            link_status = "needs_review"
        if it.get("exception"):
            link_status = "unresolved_original"
        collection = "not_applicable"
        verified_at = ""
        if it["kind"] == "video":
            try:
                c = collections.get(it["platform"])
                require(c is not None, "collection_not_reconciled")
                matched = it["id"] and it["id"] not in c["missing"]["round1"] + c["missing"]["round2"]
                collection = "confirmed" if matched and link_status == "verified" else "pending"
                verified_at = c["verified_at"] if collection == "confirmed" else ""
            except (GateError, OSError, KeyError, ValueError):
                collection = "pending"
        cand_url = it.get("candidate_url") or next((c.get("url") for c in it.get("candidates", []) if c.get("match_type") == "text_search_candidate"), "")
        screenshot = it.get("screenshot") or next((c.get("screenshot") for c in it.get("candidates", []) if c.get("screenshot")), "")
        rows.append({"record_key": state["run_id"] + ":" + it["key"], "run_id": state["run_id"], "item_key": it["key"], "issue": state["issue"]["title"], "week": state["issue"]["week"], "section": it["section"], "subsection": it.get("subsection", ""), "title": it["title"], "author": it["author"], "platform": it["platform"], "id": it["id"] or next(iter(re.findall(r"BV[A-Za-z0-9]{10}|(?<![0-9])[0-9]{19}(?![0-9])", it["original_url"])), ""), "original_url": it["original_url"], "url": it["canonical_url"], "link_status": link_status, "collection": collection, "verified_at": verified_at, "candidate_url": cand_url, "screenshot": screenshot, "source": it["source"], "candidates": it["candidates"], "document_url": state.get("publication", {}).get("document_url", ""), "folder_name": state["targets"].get(it["platform"], {}).get("folder_name", ""), "folder_url": state["targets"].get(it["platform"], {}).get("folder_url", ""), "run_path": str(Path(run).resolve())})
    return rows


def render_index_html(rows, destination):
    def safe_link(url, label):
        if not url or urlsplit(url).scheme not in {"https", "http"}:
            return ""
        return '<a rel="noreferrer" target="_blank" href="' + html.escape(url, quote=True) + '">' + html.escape(label) + '</a>'
    body = []
    for r in rows:
        label = "已核验收藏" if r["collection"] == "confirmed" else "未确认原作品" if r["link_status"] != "verified" else "待收藏" if r["collection"] == "pending" else "阅读项"
        links = safe_link(r["url"], "打开原作品") if r["link_status"] == "verified" else ""
        if r["link_status"] != "verified" and r["original_url"]:
            links += '<details><summary>原记录链接（待核验）</summary>' + safe_link(r["original_url"], r["original_url"]) + '</details>'
        if r.get("candidate_url") and r.get("link_status") != "verified":
            links += ' ' + safe_link(r["candidate_url"], "候选搜索链接")
        if r.get("screenshot"):
            links += ' <a rel="noreferrer" target="_blank" href="' + html.escape(r["screenshot"], quote=True) + '">查看录屏截图</a>'
        links += " " + safe_link(r["document_url"], "本期周报") + " " + safe_link(r["folder_url"], "收藏夹")
        sec_label = r["section"] + ((" - " + r["subsection"]) if r.get("subsection") else "")
        fields = [r["issue"], sec_label, r["title"], r["author"], r["platform"], r["id"], label, r["verified_at"], r["folder_name"]]
        body.append('<tr><td>' + '</td><td>'.join(html.escape(str(x)) for x in fields) + '</td><td>' + links + '</td></tr>')
    html_doc = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>周报作品索引</title><style>body{font:16px/1.6 system-ui;margin:32px;color:#142238;background:#f5f7fb}h1{margin-bottom:6px}input{padding:12px;width:min(90%,680px);margin:16px 0;border:1px solid #bcc8d8;border-radius:8px}table{border-collapse:collapse;background:white}th,td{text-align:left;padding:12px;border-bottom:1px solid #e0e5ec;max-width:380px;overflow-wrap:anywhere}th{background:#e8eff9}a{color:#165bb2;white-space:nowrap}.scroll{overflow:auto}</style><h1>周报作品索引</h1><p>可按期号、标题、作者、视频 ID 搜索。跨期总索引显示最近最多5000项，更多可用命令查询。核验是所标时间的证据，不保证平台永久保留。相似候选没有冒充原作品。</p><input id="q" placeholder="输入关键词，例如标题、作者、BV号"><div class="scroll"><table><thead><tr><th>期号</th><th>栏目</th><th>标题</th><th>作者</th><th>平台</th><th>ID</th><th>状态</th><th>核验时间</th><th>收藏夹</th><th>入口</th></tr></thead><tbody>' + ''.join(body) + '</tbody></table></div><script>document.getElementById("q").addEventListener("input",function(){const q=this.value.toLowerCase().trim().split(/\\s+/);document.querySelectorAll("tbody tr").forEach(r=>{r.hidden=!q.every(s=>r.textContent.toLowerCase().includes(s))})})</script></html>'
    atomic_text(destination, html_doc)


def export_index(state, run, catalog=None):
    rows = index_rows(state, run)
    write_json(Path(run) / "library.json", rows)
    render_index_html(rows, Path(run) / "index.html")
    if catalog:
        catalog = Path(catalog)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(catalog), timeout=10)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS entries (record_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, search_text TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("DELETE FROM entries WHERE run_id=?", (state["run_id"],))
            db.executemany("INSERT INTO entries VALUES (?,?,?,?)", [(r["record_key"], state["run_id"], ' '.join(str(r.get(k, "")) for k in ("issue", "week", "section", "subsection", "title", "author", "platform", "id", "original_url", "url")).lower(), compact(r)) for r in rows])
            all_rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM entries ORDER BY rowid DESC LIMIT 5000")]
        render_index_html(all_rows, catalog.parent / "index.html")
    return {"items": len(rows), "index": str(Path(run) / "index.html"), "json": str(Path(run) / "library.json"), "catalog": str(catalog) if catalog else None, "catalog_index": str(catalog.parent / "index.html") if catalog else None}


def search_catalog(catalog, query, limit=30):
    require(Path(catalog).is_file(), "catalog_missing_run_index_first")
    require(1 <= limit <= 200, "search_limit_1_to_200")
    terms = query.lower().split()
    require(terms, "query_required")
    clauses = " AND ".join("instr(search_text,?)>0" for _ in terms)
    uri = Path(catalog).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM entries WHERE " + clauses + " ORDER BY rowid DESC LIMIT ?", [*terms, limit])]
