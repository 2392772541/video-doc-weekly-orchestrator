"""Compatibility gate. Legacy nonempty URLs/audit success never imply verified originals."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import weekly_core as w


def validate(path):
    run = path if (path / "run.json").exists() else path.parent
    if (run / "run.json").exists():
        state = w.read_json(run / "run.json")
        result = w.audit(state, run)
        if state.get("rendered"):
            try:
                w.check_render(state, run)
            except (w.GateError, OSError) as e:
                result["ok"] = False
                result["errors"].append({"item": "render", "error": str(e)})
        return result
    manifest = w.read_json(path / "report_manifest.json")
    errors = [{"item": "legacy", "error": "per_item_source_identity_availability_evidence_missing_migrate_with_weekly_init"}]
    for n, it in enumerate(manifest.get("items", []), 1):
        if not it.get("url"):
            continue
        try:
            w.normalize_url(it["url"])
        except (w.GateError, ValueError) as e:
            errors.append({"item": n, "title": it.get("title", ""), "url": it["url"], "error": str(e)})
    return {"ok": False, "legacy": True, "items": len(manifest.get("items", [])), "errors": errors, "old_audit_success_ignored": True}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("report_dir", type=Path)
    args = ap.parse_args()
    result = validate(args.report_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (w.GateError, OSError, ValueError, KeyError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        raise SystemExit(2)
