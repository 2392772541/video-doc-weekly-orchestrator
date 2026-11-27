# coding: utf-8
"""阶段 7（周报全期视频伴读逐字稿与云端转录库整合入库）单元测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import weekly_core as w
import weekly_transcripts as wt


def _build_sample_run(tmp_path: Path) -> Path:
    """构造一份包含抖音视频、B站视频、抖音图文、已失效视频与网页文章的标准周报工作区。"""
    run_dir = tmp_path / "vol_test"
    manifest = {
        "issue": {"title": "且曼内刊 - Vol.999 20261127", "week": "20261127"},
        "items": [
            {
                "key": "top-1",
                "section": "# 本周最TOP",
                "subsection": "",
                "number": 1,
                "title": "云端复用抖音视频",
                "kind": "video",
                "platform": "douyin",
                "url": "https://www.douyin.com/video/7710615223413067050",
            },
            {
                "key": "top-2",
                "section": "# 本周最TOP",
                "subsection": "",
                "number": 2,
                "title": "给 Gemini 道歉，请自觉排队",
                "kind": "article",
                "platform": "web",
                "body": "围绕 Gemini 展开的观点长文。",
                "url": "https://www.ifanr.com/1690572",
            },
            {
                "key": "vid-tingwu",
                "section": "# AI 优秀文章",
                "subsection": "#AI 视野",
                "number": 1,
                "title": "听悟正常转写B站视频",
                "kind": "video",
                "platform": "bilibili",
                "url": "https://www.bilibili.com/video/BV1Yas16LE3y",
            },
            {
                "key": "vid-quota-fb",
                "section": "# AI 优秀文章",
                "subsection": "#AI 视野",
                "number": 2,
                "title": "听悟额度用尽自动降级本地视频",
                "kind": "video",
                "platform": "douyin",
                "url": "https://www.douyin.com/video/7709736985308732529",
            },
            {
                "key": "vid-after-quota",
                "section": "# AI 优秀文章",
                "subsection": "#AI 视野",
                "number": 3,
                "title": "额度用尽后直走本地且无口播纯音乐",
                "kind": "video",
                "platform": "douyin",
                "url": "https://www.douyin.com/video/7707972026081750400",
            },
            {
                "key": "vid-note",
                "section": "# 自媒体与搞钱",
                "subsection": "#视频制作",
                "number": 1,
                "title": "清退的风还是刮到了科普赛道",
                "kind": "video",
                "platform": "douyin",
                "url": "https://www.douyin.com/note/7709896810064128870",
            },
            {
                "key": "vid-dead",
                "section": "# 自媒体与搞钱",
                "subsection": "#视频制作",
                "number": 2,
                "title": "已下架视频示例",
                "kind": "video",
                "platform": "douyin",
                "url": "https://www.douyin.com/video/7707501978092864680",
            },
        ],
    }
    state = w.initialize(manifest, "full")
    # 模拟已发布的飞书文档与 1 条确认失效的抖音排除项
    state["publication"] = {
        "verified": True,
        "document_id": "DocTokenTest999",
        "document_url": "https://my.feishu.cn/docx/DocTokenTest999",
        "content_hash": w.content_hash(state),
    }
    # 模拟 canonical_url 与 id 已就位（与真实完成前 6 阶段后的 run.json 一致）
    for it in state["items"]:
        norm = w.normalize_url(it["original_url"])
        it["canonical_url"] = norm["url"]
        it["id"] = norm["id"]

    run_dir.mkdir(parents=True, exist_ok=True)
    ev_dir = run_dir / "evidence"
    ev_dir.mkdir(exist_ok=True)
    dead_ev = ev_dir / "dead_7707501978092864680.json"
    dead_ev.write_text(json.dumps({"id": "7707501978092864680", "status": "unavailable"}, ensure_ascii=False), encoding="utf-8")
    state["collection_exclusions"] = {
        "douyin": {
            "7707501978092864680": {
                "id": "7707501978092864680",
                "platform": "douyin",
                "reason": "dead_link_confirmed",
                "detail": {"status": "unavailable", "unavailable_banner": True, "message": "你要观看的视频不存在"},
                "recorded_at": w.now(),
                "evidence": w.file_evidence(run_dir, ["evidence/dead_7707501978092864680.json"]),
            }
        },
        "bilibili": {},
    }
    w.write_json(run_dir / "run.json", state)
    return run_dir


def test_stage7_full_flow_with_cloud_reuse_quota_fallback_and_index(tmp_path: Path):
    """验证云端复用 + 听悟转写 + 额度耗尽无缝切 FunASR + 无口播/图文/失效跳过 + 整合入库 + 跨期索引挂接。"""
    run_dir = _build_sample_run(tmp_path)
    before_hash = w.content_hash(w.read_json(run_dir / "run.json"))

    tingwu_calls: list[str] = []
    funasr_calls: list[str] = []
    extracted_urls: list[str] = []
    saved_payloads: list[dict] = []

    def fake_cloud_list():
        return [
            {
                "id": 88,
                "title": "已有云端记录",
                "source_url": "https://www.douyin.com/video/7710615223413067050",
                "chars": 42,
            }
        ]

    def fake_cloud_get(item_id: int):
        assert item_id == 88
        return {"id": 88, "text": "这是云端库里早已转好的\u200b逐字稿正文。"}

    def fake_extract(url: str, out_dir: Path, title_hint: str):
        extracted_urls.append(url)
        mp3 = out_dir / f"tmp_{len(extracted_urls)}.mp3"
        mp3.write_bytes(b"ID3FAKEAUDIO")
        return {"success": True, "status": "ok", "audio_path": str(mp3), "duration_sec": 125.0, "title": title_hint}

    def fake_tingwu(audio_path: Path, tag: str, out_txt: Path, logs_dir: Path):
        tingwu_calls.append(tag)
        if "BV1Yas16LE3y" in tag:
            return {
                "ok": True,
                "status": "ok",
                "text": "第一段通义听悟\u200c正文。\n\n第二段通义听悟正文。",
                "detail_url": "https://tingwu.aliyun.com/doc/transcripts/abc123",
                "message": "ok",
            }
        # 第二次调听悟模拟额度耗尽
        return {
            "ok": False,
            "status": "error",
            "text": "",
            "detail_url": "",
            "message": "通义听悟页面提示上传受限或时长不足，已立即停止。",
        }

    def fake_funasr(audio_path: Path, out_txt: Path):
        funasr_calls.append(audio_path.name)
        if len(funasr_calls) == 1:
            return {"ok": True, "text": "这是本机显卡离线兜底转出的第一句。" * 25}
        raise RuntimeError("FunASR 转写结果为空")

    def fake_save(**kwargs):
        saved_payloads.append(kwargs)
        return {"item_id": 501, "detail_url": "https://gz.grzdt.com/zhuanlu/item/501", "action": "created", "chars": len(kwargs["text"])}

    def fake_verify(item_id: int):
        assert item_id == 501
        return {"ok": True, "fts_ok": True, "item_id": 501}

    res = wt.run_build_transcripts(
        run_dir,
        engine="tingwu",
        upload_to_zhuanlu=True,
        cloud_list_fn=fake_cloud_list,
        cloud_get_fn=fake_cloud_get,
        cloud_save_fn=fake_save,
        cloud_verify_fn=fake_verify,
        extract_audio_fn=fake_extract,
        tingwu_fn=fake_tingwu,
        funasr_fn=fake_funasr,
    )

    assert res["ok"] is True
    assert res["status"] == "complete"
    stats = res["stats"]
    assert stats["video_total"] == 6
    assert stats["reused_cloud"] == 1
    assert stats["engine_tingwu"] == 1
    assert stats["engine_funasr_fallback"] == 1
    assert stats["transcribed_ok"] == 3
    assert stats["skipped_dead_link"] == 1
    assert stats["skipped_photo_note"] == 1
    assert stats["skipped_no_speech"] == 1
    assert stats["pending"] == 0

    # 额度耗尽后第 3 条待转视频不再浪费调用通义听悟，直接切 FunASR
    assert len(tingwu_calls) == 2
    assert len(funasr_calls) == 2

    # 临时 MP3 已自动清理
    assert list((run_dir / "transcripts" / "_tmp_audio").glob("*.mp3")) == []

    # 伴读 Markdown 校验：无零宽水印、含目录、含网页文章简介、含听悟和本机离线标注
    md = (run_dir / "report" / "companion_transcript.md").read_text(encoding="utf-8")
    assert "\u200b" not in md and "\u200c" not in md
    assert "速查编号目录" in md
    assert "云端转录库复用（#88）" in md
    assert "本机离线转写（FunASR）" in md
    assert "围绕 Gemini 展开的观点长文。" in md
    assert "抖音图文笔记，无口播音轨" in md
    assert "原视频已下架/不可访问" in md

    # 云端整合入库参数校验
    assert len(saved_payloads) == 1
    assert saved_payloads[0]["category"] == "直播回放与周刊"
    assert saved_payloads[0]["source_url"] == "https://my.feishu.cn/docx/DocTokenTest999"
    assert saved_payloads[0]["overwrite"] is True
    assert res["zhuanlu"]["detail_url"] == "https://gz.grzdt.com/zhuanlu/item/501"

    # 回写 run.json 不改变 content_hash，且 export_index 正确渲染“伴读逐字稿”链接
    st_after = w.read_json(run_dir / "run.json")
    assert w.content_hash(st_after) == before_hash
    assert st_after["companion_transcript"]["zhuanlu_url"] == "https://gz.grzdt.com/zhuanlu/item/501"

    cat = tmp_path / "catalog.sqlite"
    w.export_index(st_after, run_dir, cat)
    idx_html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "https://gz.grzdt.com/zhuanlu/item/501" in idx_html
    assert "伴读逐字稿" in idx_html


def test_stage7_need_login_pauses_and_resumes_cleanly(tmp_path: Path):
    """验证通义听悟掉登录时立即保存断点暂停且不上传残缺草稿，登录后可无缝续跑。"""
    run_dir = _build_sample_run(tmp_path)
    saved_called = False
    login_ready = False

    def fake_extract(url: str, out_dir: Path, title_hint: str):
        mp3 = out_dir / "sample.mp3"
        mp3.write_bytes(b"ID3")
        return {"success": True, "status": "ok", "audio_path": str(mp3), "duration_sec": 30.0, "title": title_hint}

    def fake_tingwu(audio_path: Path, tag: str, out_txt: Path, logs_dir: Path):
        if not login_ready:
            return {"ok": False, "status": "need_login", "text": "", "message": "未登录，请扫码"}
        return {"ok": True, "status": "ok", "text": f"正文_{tag}", "detail_url": "https://tingwu.aliyun.com/doc/transcripts/ok", "message": "ok"}

    def fake_save(**kwargs):
        nonlocal saved_called
        saved_called = True
        return {"item_id": 502, "detail_url": "https://gz.grzdt.com/zhuanlu/item/502", "action": "created", "chars": len(kwargs["text"])}

    # 第一次遇到掉登录：暂停、不上传
    r1 = wt.run_build_transcripts(
        run_dir,
        cloud_list_fn=lambda: [],
        cloud_get_fn=lambda _id: {},
        cloud_save_fn=fake_save,
        cloud_verify_fn=lambda _id: {"ok": True, "fts_ok": True},
        extract_audio_fn=fake_extract,
        tingwu_fn=fake_tingwu,
        funasr_fn=lambda a, o: {"ok": True, "text": "unused"},
    )
    assert r1["ok"] is False
    assert r1["status"] == "need_login"
    assert saved_called is False

    # 模拟用户扫码登录后续跑：自动完成全部并入库
    login_ready = True
    r2 = wt.run_build_transcripts(
        run_dir,
        cloud_list_fn=lambda: [],
        cloud_get_fn=lambda _id: {},
        cloud_save_fn=fake_save,
        cloud_verify_fn=lambda _id: {"ok": True, "fts_ok": True},
        extract_audio_fn=fake_extract,
        tingwu_fn=fake_tingwu,
        funasr_fn=lambda a, o: {"ok": True, "text": "unused"},
    )
    assert r2["ok"] is True
    assert r2["status"] == "complete"
    assert saved_called is True
    assert r2["stats"]["transcribed_ok"] == 4
