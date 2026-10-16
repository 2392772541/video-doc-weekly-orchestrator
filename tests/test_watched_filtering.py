"""Unit tests for watched video filtering and deduplication."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import real_collector


class TestWatchedFiltering(unittest.TestCase):
    def setUp(self):
        self.items = [
            {
                "key": "i001",
                "title": "电影解析A",
                "section": "每周电影",
                "subsection": "",
                "platform": "bilibili",
                "url": "https://www.bilibili.com/video/BV1dKeB6HEvJ"
            },
            {
                "key": "i002",
                "title": "TOP精选B",
                "section": "本周最TOP",
                "subsection": "",
                "platform": "douyin",
                "url": "https://www.douyin.com/video/7695302751908478458"
            },
            {
                "key": "i003",
                "title": "认知升级C",
                "section": "游戏人生，说地谈天",
                "subsection": "热点/认知",
                "platform": "douyin",
                "url": "https://www.douyin.com/video/7651922649496593073"
            },
            {
                "key": "i004",
                "title": "关系情感D",
                "section": "游戏人生，说地谈天",
                "subsection": "关系/情感",
                "platform": "douyin",
                "url": "https://www.douyin.com/video/7695284409359944995"
            },
            {
                "key": "i005",
                "title": "AI剪辑E",
                "section": "自媒体与搞钱",
                "subsection": "视频制作",
                "platform": "bilibili",
                "url": "https://www.bilibili.com/video/BV1BxWF6ZEbL"
            }
        ]

    def test_normalize_category_name(self):
        self.assertEqual(real_collector.normalize_category_name("热点/认知"), "热点认知")
        self.assertEqual(real_collector.normalize_category_name("轻松 / 日常"), "轻松日常")
        self.assertEqual(real_collector.normalize_category_name("AI基础"), "AI基础")
        self.assertEqual(real_collector.normalize_category_name(""), "默认分类")

    def test_extract_video_id(self):
        self.assertEqual(real_collector.extract_video_id("https://www.douyin.com/video/7695302751908478458"), "7695302751908478458")
        self.assertEqual(real_collector.extract_video_id("https://www.bilibili.com/video/BV1dKeB6HEvJ"), "BV1dKeB6HEvJ")
        self.assertEqual(real_collector.extract_video_id({"id": "7695302751908478458"}), "7695302751908478458")
        self.assertEqual(real_collector.extract_video_id({"url": "https://www.bilibili.com/video/BV1BxWF6ZEbL"}), "BV1BxWF6ZEbL")

    def test_filter_watched_items(self):
        watched_vids = ["7695302751908478458", "BV1dKeB6HEvJ"]  # i001 and i002 are watched
        unwatched, watched_ex = real_collector.filter_watched_items(self.items, watched_vids)
        self.assertEqual(len(watched_ex), 2)
        excluded_keys = [it["key"] for it in watched_ex]
        self.assertIn("i001", excluded_keys)
        self.assertIn("i002", excluded_keys)
        self.assertEqual(len(unwatched), 3)

    def test_filter_watched_items_empty_blacklist(self):
        unwatched, watched_ex = real_collector.filter_watched_items(self.items, [])
        self.assertEqual(len(unwatched), len(self.items))
        self.assertEqual(len(watched_ex), 0)


if __name__ == "__main__":
    unittest.main()
