"""Unit tests for categorized video collection generator and category mapping."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import real_collector


class TestCategorizedCollection(unittest.TestCase):
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

    def test_build_category_mapping_douyin(self):
        dy_map = real_collector.build_category_mapping(self.items, platform="douyin")
        self.assertIn("本周最TOP", dy_map)
        self.assertIn("热点认知", dy_map)
        self.assertIn("关系情感", dy_map)
        self.assertNotIn("每周电影", dy_map)  # Bilibili only
        self.assertEqual(len(dy_map["本周最TOP"]), 1)
        self.assertEqual(len(dy_map["热点认知"]), 1)

    def test_filter_watched_items_and_mapping(self):
        watched_vids = ["7695302751908478458"]  # i002 is watched
        unwatched, watched_ex = real_collector.filter_watched_items(self.items, watched_vids)
        self.assertEqual(len(watched_ex), 1)
        self.assertEqual(watched_ex[0]["key"], "i002")
        self.assertEqual(len(unwatched), 4)

        # Now test build_category_mapping with watched filtering
        dy_map_filtered = real_collector.build_category_mapping(self.items, platform="douyin", watched_ids=watched_vids)
        self.assertNotIn("本周最TOP", dy_map_filtered)  # Since i002 was the only one in 本周最TOP
        self.assertIn("热点认知", dy_map_filtered)
        self.assertIn("关系情感", dy_map_filtered)

    def test_build_category_mapping_bilibili(self):
        bili_map = real_collector.build_category_mapping(self.items, platform="bilibili")
        self.assertIn("每周电影", bili_map)
        self.assertIn("视频制作", bili_map)
        self.assertNotIn("热点认知", bili_map)

    def test_generate_douyin_categorized_browser_script(self):
        dy_map = real_collector.build_category_mapping(self.items, platform="douyin")
        js = real_collector.generate_douyin_categorized_browser_script(dy_map)
        self.assertIn("runDouyinCategorizedCollection", js)
        self.assertIn("7695302751908478458", js)
        self.assertIn("7651922649496593073", js)
        self.assertIn("添加视频", js)
        self.assertIn("Preflight", js)

    def test_generate_bilibili_categorized_browser_script(self):
        bili_map = {
            "每周电影": {"folder_id": "4304497083", "items": ["BV1dKeB6HEvJ"]},
            "视频制作": {"folder_id": "4301498483", "items": ["BV1BxWF6ZEbL"]}
        }
        js = real_collector.generate_bilibili_categorized_browser_script(bili_map)
        self.assertIn("runBilibiliCategorizedCollection", js)
        self.assertIn("BV1dKeB6HEvJ", js)
        self.assertIn("4304497083", js)
        self.assertIn("BV1BxWF6ZEbL", js)
        self.assertIn("4301498483", js)
        self.assertIn("fav/resource/deal", js)


if __name__ == "__main__":
    unittest.main()
