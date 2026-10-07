"""Sequential public metadata reads; no browser/cookies and no favorite writes.
Results are availability clues, never proof that a candidate is the source work.
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
import weekly_core as w


def classify(bvid, payload):
    code = payload.get("code")
    d = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    result = {"requested_id": bvid, "observed_id": d.get("bvid"), "title": d.get("title", ""), "author": (d.get("owner") or {}).get("name", ""), "duration": d.get("duration"), "metadata": {"code": code, "state": d.get("state"), "videos": d.get("videos")}, "checked_at": w.now(), "identity_verified": False}
    if type(code) is int and code == 0 and d.get("bvid") == bvid and type(d.get("state")) is int and d["state"] == 0 and type(d.get("videos")) is int and d["videos"] > 0:
        result["availability"] = "metadata_accessible"
    elif code in (-101, -111):
        result["availability"] = "login_required"
    elif code in (-352, -412, 412, 429):
        result["availability"] = "challenge_or_rate_limit"
    elif code == -404:
        result["availability"] = "not_found_on_this_read"
    elif code in (62002, 62004) or (type(d.get("state")) is int and d["state"] != 0):
        result["availability"] = "restricted_or_unavailable"
    else:
        result["availability"] = "unknown"
    return result


def probe(bvid, timeout=12):
    w.valid_id("bilibili", bvid)
    request = urllib.request.Request("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2 * 1024 * 1024 + 1)
            w.require(len(body) <= 2 * 1024 * 1024, "metadata_response_too_large")
            payload = json.loads(body.decode("utf-8"))
            w.require(isinstance(payload, dict), "metadata_not_object")
            return classify(bvid, payload)
    except urllib.error.HTTPError as e:
        return {"requested_id": bvid, "checked_at": w.now(), "availability": "challenge_or_rate_limit" if e.code in (403, 412, 429) else "network_or_http_error", "http_status": e.code, "identity_verified": False}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return {"requested_id": bvid, "checked_at": w.now(), "availability": "network_or_schema_error", "error": str(e)[:200], "identity_verified": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ids", nargs="+", required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    ids = list(dict.fromkeys(args.ids))
    w.require(1 <= len(ids) <= 60, "probe_batch_limit_60")
    for b in ids:
        w.valid_id("bilibili", b)
    results = []
    for n, b in enumerate(ids):
        if n:
            time.sleep(1.5)
        result = probe(b)
        results.append(result)
        w.write_json(args.out, {"read_only": True, "results": results, "unread_ids": ids[len(results):], "warning": "Not a source-identity or full playback test; do not replace originals from this probe alone."})
        if result["availability"] == "challenge_or_rate_limit":
            break
    print(json.dumps({"read_only": True, "out": str(args.out), "results": results, "remaining": len(ids) - len(results)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
