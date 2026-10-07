#!/usr/bin/env python3
"""One local entry point. External publish/favorite actions are performed by the agent."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
import weekly_core as w


def output(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def memory_status():
    if os.name == "nt":
        class Memory(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in ("total_phys", "available_phys", "total_page", "available_page", "total_virtual", "available_virtual", "extended")]
        m = Memory()
        m.length = ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return {"known": False, "heavy_work_allowed": False}
        total, available = m.total_phys, m.available_phys
    elif Path("/proc/meminfo").exists():
        d = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()}
        total, available = d["MemTotal"], d["MemAvailable"]
    else:
        return {"known": False, "heavy_work_allowed": False}
    return {"known": True, "total_gib": round(total / 2**30, 2), "available_gib": round(available / 2**30, 2), "heavy_work_allowed": available >= max(4 * 2**30, total * 0.15), "policy": "one OCR worker; 2 CPU threads; one browser task tab; pause heavy work below max(4 GiB,15% RAM)"}


@contextmanager
def locked(run):
    path = Path(run).resolve()
    w.require((path / "run.json").is_file(), "run_missing_initialize_first")
    lock = path / ".weekly.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as e:
        raise w.GateError("run_locked_do_not_run_parallel_writers: " + str(lock)) from e
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "at": w.now()}, f)
        yield path, w.read_json(path / "run.json")
    finally:
        lock.unlink(missing_ok=True)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Read memory and dependency availability; starts nothing")
    init = sub.add_parser("init", help="Migrate legacy/new item inventory; never trusts old success")
    init.add_argument("--run", type=Path, required=True)
    init.add_argument("--manifest", type=Path, required=True)
    init.add_argument("--mode", choices=("full", "publish", "collect", "audit"), default="full")
    res = sub.add_parser("resolve-links", help="Scan and verify missing or truncated video links")
    res.add_argument("--manifest", type=Path, required=True)
    res.add_argument("--output", type=Path)
    res.add_argument("--apply-resolutions", type=Path)
    search = sub.add_parser("search", help="Query a local cross-issue catalog")
    search.add_argument("--catalog", type=Path, required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=30)
    cap = sub.add_parser("capture-screenshots", help="Extract video frame screenshots from source video recording")
    cap.add_argument("--run", type=Path, required=True)
    cap.add_argument("--keys", nargs="*", default=None)
    emb = sub.add_parser("embed-screenshots", help="Embed uploaded screenshot tokens into layout as image blocks")
    emb.add_argument("--run", type=Path, required=True)
    emb.add_argument("--receipts", type=Path, help="Path to json receipts mapping key to {file_token, file}")
    for name in ("apply", "audit", "render", "publish-plan", "published", "snapshot", "plan", "begin-write", "end-write", "reconcile", "status", "finish", "index"):
        s = sub.add_parser(name)
        s.add_argument("--run", type=Path, required=True)
        if name in {"apply", "snapshot", "published", "end-write"}:
            s.add_argument("--input", type=Path, required=True)
        if name in {"plan", "begin-write", "reconcile"}:
            s.add_argument("--platform", choices=sorted(w.VIDEO_PLATFORMS), required=True)
        if name == "begin-write":
            s.add_argument("--id", required=True)
            s.add_argument("--plan-id", required=True)
        if name == "end-write":
            s.add_argument("--attempt-id", required=True)
        if name == "render":
            s.add_argument("--allow-partial", action="store_true", help="Only explicitly accepted unresolved originals; remains partial")
        if name in {"index", "finish"}:
            s.add_argument("--catalog", type=Path)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "doctor":
        output({"memory": memory_status(), "python": sys.executable, "dependencies": {n: shutil.which(n) for n in ("ffmpeg", "ffprobe", "lark-cli", "opencli")}, "network_requests": 0, "external_writes": 0})
        return 0
    if args.command == "search":
        output(w.search_catalog(args.catalog, args.query, args.limit))
        return 0
    if args.command == "resolve-links":
        import resolve_links
        data = json.loads(args.manifest.read_text(encoding="utf-8"))
        report = resolve_links.scan_and_report_issues(data)
        if args.apply_resolutions and args.apply_resolutions.is_file():
            resolutions = json.loads(args.apply_resolutions.read_text(encoding="utf-8"))
            applied = 0
            for item in data.get("items", []):
                k = item.get("key")
                t = item.get("title")
                res_entry = resolutions.get(k) or resolutions.get(t)
                if res_entry:
                    c_url = res_entry if isinstance(res_entry, str) else res_entry.get("url")
                    c_title = res_entry.get("title", "") if isinstance(res_entry, dict) else ""
                    c_author = res_entry.get("author", "") if isinstance(res_entry, dict) else ""
                    v = resolve_links.verify_resolution(item, c_url, c_title, c_author)
                    if v["verified"]:
                        item["url"] = v["resolved_url"]
                        item["resolved_by"] = v["method"]
                        applied += 1
            report["applied_resolutions"] = applied
            args.manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        output(report)
        return 0
    if args.command == "init":
        run = args.run.resolve()
        w.require(not (run / "run.json").exists(), "run_exists_use_resume_not_reinitialize")
        state = w.initialize(w.read_json(args.manifest), args.mode)
        run.mkdir(parents=True, exist_ok=True)
        (run / "evidence").mkdir(exist_ok=True)
        w.write_json(run / "run.json", state)
        w.write_json(run / "evidence" / "seed-inventory.json", w.read_json(args.manifest))
        output({"run": str(run), "items": len(state["items"]), "mode": args.mode, "verified_links": 0, "next": "read assets/observations.example.json and references/operations.md; register source, verify inventory and every link"})
        return 0
    code = 0
    with locked(args.run) as (run, state):
        command = args.command
        old_hash = w.digest(state)
        if command == "apply":
            events = w.read_json(args.input)
            state = w.apply_events(state, run, events)
            result = {"applied": len(events), "content_hash": w.content_hash(state)}
        elif command == "capture-screenshots":
            import resolve_links
            result = resolve_links.capture_screenshots_for_run(run, state, args.keys)
        elif command == "embed-screenshots":
            import resolve_links
            receipts_path = args.receipts or (run / "evidence" / "feishu_image_receipts.json")
            w.require(receipts_path.is_file(), f"receipts_file_missing: {receipts_path}")
            receipts = w.read_json(receipts_path)
            result = resolve_links.embed_captured_screenshots_into_layout(run, state, receipts)
            w.write_json(run / "run.json", state)
            render_res = w.render(state, run, allow_partial=True)
            result["rendered_content_hash"] = render_res["content_hash"]
        elif command == "audit":
            result = w.audit(state, run)
            w.write_json(run / "audit" / "latest.json", result)
            code = 0 if result["ok"] else 2
        elif command == "render":
            result = w.render(state, run, args.allow_partial)
        elif command == "publish-plan":
            result = w.publish_plan(state, run)
        elif command == "published":
            result = w.readback_publication(state, run, w.read_json(args.input))
        elif command == "snapshot":
            result = w.import_snapshot(state, run, w.read_json(args.input))
        elif command == "plan":
            result = w.plan_collection(state, run, args.platform)
        elif command == "begin-write":
            result = w.begin_write(state, run, args.platform, args.id, args.plan_id)
        elif command == "end-write":
            result = w.end_write(state, run, args.attempt_id, w.read_json(args.input))
        elif command == "reconcile":
            result = w.reconcile(state, run, args.platform)
            w.write_json(run / "audit" / (args.platform + "-reconcile.json"), result)
            code = 0 if result["ok"] else 2
        elif command == "index":
            result = w.export_index(state, run, args.catalog)
        elif command == "finish":
            result = w.finish(state, run)
            result["index"] = w.export_index(state, run, args.catalog)
            w.write_json(run / "audit" / "delivery.json", result)
            code = 0 if result["status"] in {"complete", "audit_passed"} else 2
        else:
            result = {"run_id": state["run_id"], "issue": state["issue"], "status": state["status"], "mode": state["mode"], "counts": w.audit(state, run)["counts"], "permissions": state["permissions"], "pending_writes": [a for a in state["attempts"] if a["outcome"] in {"in_flight", "uncertain", "failed"} and not a.get("observed_in_snapshot")], "next": "audit -> recover evidence -> render -> publish/readback -> fresh snapshot -> plan/diff -> serial writes -> two snapshots/reconcile -> finish/index"}
        if old_hash != w.digest(state):
            state["updated_at"] = w.now()
            # A recoverable one-command backup, plus append-only command hashes (no secrets).
            w.atomic_text(run / "run.previous.json", (run / "run.json").read_text(encoding="utf-8"))
            w.write_json(run / "run.json", state)
            with (run / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(w.compact({"at": state["updated_at"], "command": command, "before_hash": old_hash, "after_hash": w.digest(state)}) + "\n")
        output(result)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (w.GateError, OSError, KeyError, TypeError, ValueError, w.ET.ParseError) as e:
        output({"ok": False, "error": str(e), "external_writes": 0, "note": "This helper does not call remote write APIs. Preserve checkpoint and repair evidence."})
        raise SystemExit(2)
