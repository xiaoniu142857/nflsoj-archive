"""讲解视频抓取功能的离线测试（不访问网络）。"""
import io
import json
import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from nflsoj_archive import nflsoj as core                     # noqa: E402

TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".test-tmp")

MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013"
     profiles="urn:mpeg:dash:profile:isoff-on-demand:2011" type="static"
     mediaPresentationDuration="PT2570.300049S">
  <Period id="0">
    <AdaptationSet id="1" contentType="audio">
      <ContentProtection value="cenc" schemeIdUri="urn:mpeg:dash:mp4protection:2011"
          cenc:default_KID="88facdfd-ba8e-e7ae-1abb-43c6e10860b6"/>
      <Representation id="0" codecs="mp4a.40.2" mimeType="audio/mp4">
        <BaseURL>audio.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
    <AdaptationSet id="0" contentType="video" width="2160" height="1080">
      <Representation id="1" codecs="avc1.640032" mimeType="video/mp4">
        <BaseURL>video.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

UUID1 = "05d65135-59d6-421c-8cb0-c9673f2ffb16"
UUID2 = "a9ba0323-bbc2-43c6-a299-6f7a7f9baa2c"
UUID3 = "a65c61fc-ceab-4765-acac-e05f193cd6ba"


def make_tmp_dir(prefix="v") -> str:
    os.makedirs(TMP_ROOT, exist_ok=True)
    path = os.path.join(TMP_ROOT, "%s%d" % (prefix, time.time_ns()))
    os.makedirs(path, exist_ok=True)
    return path


def rmtree_force(path):
    if not os.path.isdir(path):
        return
    for root_dir, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(root_dir, name), 0o666)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_dir()

    def tearDown(self):
        rmtree_force(self.tmp)


def make_console(width=100):
    stream = io.StringIO()
    console = core.Console(color=False, ascii_only=False, stream=stream, width=width)
    return console, stream


# ---------------------------------------------------------------------------
# 链接解析
# ---------------------------------------------------------------------------
class ExtractVideoLinksTests(unittest.TestCase):
    def test_markdown_link(self):
        text = "[20261121讲解](/videoshow/%s)" % UUID1
        links = core.extract_video_links(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["uuid"], UUID1)
        self.assertEqual(links[0]["title"], "20261121讲解")
        self.assertEqual(links[0]["order"], 1)

    def test_multiple_links_keep_order(self):
        text = ("[第一个](/videoshow/%s)\n\n正文说明\n\n[第二个](/videoshow/%s)"
                % (UUID1, UUID2))
        links = core.extract_video_links(text)
        self.assertEqual([item["uuid"] for item in links], [UUID1, UUID2])
        self.assertEqual([item["order"] for item in links], [1, 2])

    def test_dedup(self):
        text = "[a](/videoshow/%s) 和 [b](/videoshow/%s)" % (UUID1, UUID1)
        self.assertEqual(len(core.extract_video_links(text)), 1)

    def test_absolute_url(self):
        text = "[讲解](https://nflsoi.cc:20035/videoshow/%s)" % UUID1
        links = core.extract_video_links(text)
        self.assertEqual(links[0]["uuid"], UUID1)

    def test_other_links_ignored(self):
        text = "[seg](/judge/segment-tree-lab.html)\n[讲解](/videoshow/%s)" % UUID1
        links = core.extract_video_links(text)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["uuid"], UUID1)

    def test_bare_link(self):
        links = core.extract_video_links("见 /videoshow/%s 处" % UUID1)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["title"], "")

    def test_empty_and_invalid(self):
        for value in (None, "", 123, [], "/videoshow/短"):
            self.assertEqual(core.extract_video_links(value), [], value)

    def test_title_with_brackets_and_spaces(self):
        text = "[S+ 数位 DP 讲解（上）](/videoshow/%s)" % UUID1
        links = core.extract_video_links(text)
        self.assertEqual(links[0]["title"], "S+ 数位 DP 讲解（上）")

    def test_link_with_extra_query(self):
        text = "[讲解](/videoshow/%s?t=10)" % UUID1
        self.assertEqual(core.extract_video_links(text)[0]["uuid"], UUID1)


# ---------------------------------------------------------------------------
# MPD 解析与展示
# ---------------------------------------------------------------------------
class ParseMpdTests(unittest.TestCase):
    def test_duration(self):
        info = core.parse_mpd(MPD)
        self.assertAlmostEqual(info["durationSeconds"], 2570.3, places=1)

    def test_resolution_from_adaptation_set(self):
        info = core.parse_mpd(MPD)
        self.assertEqual((info["width"], info["height"]), (2160, 1080))

    def test_resolution_from_representation(self):
        mpd = MPD.replace('width="2160" height="1080"', "")
        mpd = mpd.replace('id="1" codecs', 'id="1" width="1920" height="1080" codecs')
        info = core.parse_mpd(mpd)
        self.assertEqual((info["width"], info["height"]), (1920, 1080))

    def test_duration_hours_and_minutes(self):
        mpd = MPD.replace("PT2570.300049S", "PT1H25M22.5S")
        self.assertAlmostEqual(core.parse_mpd(mpd)["durationSeconds"], 5122.5, places=1)

    def test_invalid_xml(self):
        info = core.parse_mpd("<MPD>没有闭合")
        self.assertEqual(info, {"durationSeconds": None, "width": None, "height": None})

    def test_empty(self):
        self.assertEqual(core.parse_mpd(""), {"durationSeconds": None, "width": None,
                                              "height": None})
        self.assertEqual(core.parse_mpd(None)["width"], None)


class DisplayTests(unittest.TestCase):
    def test_video_duration(self):
        self.assertEqual(core.video_duration({"durationSeconds": 5121.7}), "1:25:22")
        self.assertEqual(core.video_duration({"durationSeconds": 95}), "1:35")
        self.assertEqual(core.video_duration({"duration": 0}), "-")
        self.assertEqual(core.video_duration({}), "-")
        self.assertEqual(core.video_duration({"durationSeconds": "乱码"}), "-")

    def test_video_resolution(self):
        self.assertEqual(core.video_resolution({"width": 2160, "height": 1080}),
                         "1080p（2160x1080）")
        self.assertIn("720p", core.video_resolution({"width": 1280, "height": 720}))
        self.assertIn("4K", core.video_resolution({"width": 3840, "height": 2160}))
        self.assertEqual(core.video_resolution({}), "-")

    def test_format_size(self):
        self.assertEqual(core.format_size(0), "-")
        self.assertEqual(core.format_size(None), "-")
        self.assertIn("MB", core.format_size(60 * 1024 * 1024))
        self.assertIn("GB", core.format_size(3 * 1024 * 1024 * 1024))


class NormalizeVideoTests(unittest.TestCase):
    def test_basic(self):
        meta = {"displayName": "讲解 A", "status": "ready", "transcodeProgress": 100,
                "mpdUrl": "https://cdn/x.mpd", "videoUrl": "https://cdn/v.mp4",
                "audioUrl": "https://cdn/a.mp4", "keyHex": "aa", "keyIdHex": "bb"}
        video = core.normalize_video(meta, {"uuid": UUID1, "title": "链接标题", "order": 1},
                                    "https://nflsoi.cc:20035")
        self.assertEqual(video["title"], "讲解 A")           # 优先用站点显示名
        self.assertEqual(video["linkTitle"], "链接标题")
        self.assertTrue(video["encrypted"])
        self.assertEqual(video["keyHex"], "aa")
        self.assertEqual(video["source"], "https://nflsoi.cc:20035/videoshow/" + UUID1)
        self.assertIn("fetchedAt", video)

    def test_falls_back_to_link_title(self):
        video = core.normalize_video({}, {"uuid": UUID1, "title": "链接标题", "order": 2})
        self.assertEqual(video["title"], "链接标题")
        self.assertFalse(video["encrypted"])

    def test_no_title_uses_uuid_prefix(self):
        video = core.normalize_video({}, {"uuid": UUID1})
        self.assertTrue(video["title"].startswith("视频 "))

    def test_empty_meta(self):
        video = core.normalize_video(None, None, "")
        self.assertEqual(video["source"], "")
        self.assertEqual(video["status"], "")


# ---------------------------------------------------------------------------
# 抓取与保存（假客户端）
# ---------------------------------------------------------------------------
class FakeClient:
    """只实现视频抓取用到的接口。"""

    def __init__(self, videos=None, fail=(), manifest=MPD, timeout=30):
        self.videos = videos or {}
        self.fail = set(fail)
        self.manifest = manifest
        self.calls = []
        self.http = type("Http", (), {"timeout": timeout})()
        self.debug = False

    def video_info(self, uuid):
        self.calls.append(uuid)
        if uuid in self.fail:
            raise core.ApiError("模拟：视频不存在", 404)
        return self.videos.get(uuid, {"displayName": "视频 " + uuid[:6], "status": "ready",
                                      "mpdUrl": "https://cdn/%s.mpd" % uuid,
                                      "videoUrl": "https://cdn/%s/v.mp4" % uuid,
                                      "audioUrl": "https://cdn/%s/a.mp4" % uuid,
                                      "keyHex": "deadbeef", "keyIdHex": "cafebabe"})

    def close(self):
        pass


class FetchVideosTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.console, self.stream = make_console()
        self._orig = core.download_url
        core.download_url = lambda client, url, timeout=60: self.manifest_text.encode("utf-8")
        self.manifest_text = MPD

    def tearDown(self):
        core.download_url = self._orig
        super().tearDown()

    def test_respects_limit(self):
        links = [{"uuid": UUID1, "title": "a", "order": 1},
                 {"uuid": UUID2, "title": "b", "order": 2},
                 {"uuid": UUID3, "title": "c", "order": 3}]
        client = FakeClient()
        videos = core.fetch_videos(client, links, "https://x", limit=3, console=self.console)
        self.assertEqual(len(videos), 3)
        self.assertEqual(len(client.calls), 3)

        client2 = FakeClient()
        videos2 = core.fetch_videos(client2, links, "https://x", limit=2, console=self.console)
        self.assertEqual(len(videos2), 2)
        self.assertEqual(client2.calls, [UUID1, UUID2])       # 只抓前 2 个

    def test_default_limit_is_small(self):
        self.assertLessEqual(core.DEFAULT_VIDEO_LIMIT, 3)
        links = [{"uuid": "%08d-0000-0000-0000-000000000000" % i, "title": str(i), "order": i}
                 for i in range(1, 11)]
        client = FakeClient()
        videos = core.fetch_videos(client, links, "https://x", console=self.console)
        self.assertEqual(len(videos), core.DEFAULT_VIDEO_LIMIT)

    def test_manifest_enriches_duration(self):
        client = FakeClient()
        videos = core.fetch_videos(client, [{"uuid": UUID1, "title": "a", "order": 1}],
                                   "https://x", limit=1, console=self.console)
        self.assertAlmostEqual(videos[0]["durationSeconds"], 2570.3, places=1)
        self.assertEqual(videos[0]["height"], 1080)

    def test_single_failure_does_not_break_batch(self):
        client = FakeClient(fail=[UUID1])
        videos = core.fetch_videos(client,
                                   [{"uuid": UUID1, "title": "a", "order": 1},
                                    {"uuid": UUID2, "title": "b", "order": 2}],
                                   "https://x", limit=2, console=self.console)
        self.assertEqual(len(videos), 2)
        self.assertIn("error", videos[0])
        self.assertNotIn("error", videos[1])
        self.assertIn("元信息获取失败", self.stream.getvalue())

    def test_no_links(self):
        self.assertEqual(core.fetch_videos(FakeClient(), [], "https://x", limit=3), [])


class ArchiveVideosTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.console, self.stream = make_console()
        self._orig_download = core.download_url
        self._orig_download_file = core.download_url_to_file
        self._orig_probe = core.probe_media_size
        self.manifest_text = MPD
        core.download_url = lambda client, url, timeout=60: self.manifest_text.encode("utf-8")
        core.probe_media_size = lambda client, url, timeout=30: (
            116_948_798 if "v.mp4" in url else 80_635_572 if "a.mp4" in url else None)
        self.downloaded = []
        core.download_url_to_file = self.fake_download_file
        self.store = core.Store(os.path.join(self.tmp, "data"))
        self.store.load()
        self.videos_dir = os.path.join(self.tmp, "archive", "1-c", "videos")

    def tearDown(self):
        core.download_url = self._orig_download
        core.download_url_to_file = self._orig_download_file
        core.probe_media_size = self._orig_probe
        super().tearDown()

    def fake_download_file(self, client, url, path, max_mb=100, timeout=120):
        if "v.mp4" in url:
            self.downloaded.append(path)
            with open(path, "wb") as handle:
                handle.write(b"\x00" * 1024)
            return 1024, True
        if "a.mp4" in url:
            return 60 * 1024 * 1024, False          # 超过上限：只返回大小
        return 0, False

    def description(self, count=3):
        parts = []
        for index, uuid in enumerate((UUID1, UUID2, UUID3)[:count], 1):
            parts.append("[讲解 %d](/videoshow/%s)" % (index, uuid))
        return "\n\n".join(parts)

    def test_saves_metadata_and_manifest(self):
        records = core.archive_videos(self.store, self.console, FakeClient(),
                                      "1", self.description(1), self.videos_dir, limit=3)
        self.assertEqual(len(records), 1)
        files = os.listdir(self.videos_dir)
        self.assertTrue(any(name.endswith(".json") for name in files))
        self.assertTrue(any(name.endswith(".mpd") for name in files))
        record = records[0]
        self.assertTrue(record["encrypted"])
        self.assertEqual(record["durationSeconds"] and round(record["durationSeconds"]), 2570)
        json_path = os.path.join(self.videos_dir, [n for n in files if n.endswith(".json")][0])
        with open(json_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["keyHex"], "deadbeef")
        self.assertEqual(payload["linkedFrom"], "1")
        self.assertTrue(payload["mpdSaved"])
        self.assertNotIn("manifestText", payload)      # 清单正文不重复存进 JSON

    def test_limit_applies(self):
        records = core.archive_videos(self.store, self.console, FakeClient(),
                                      "1", self.description(3), self.videos_dir, limit=2)
        self.assertEqual(len(records), 2)
        self.assertEqual(len([n for n in os.listdir(self.videos_dir) if n.endswith(".json")]), 2)

    def test_download_media(self):
        records = core.archive_videos(self.store, self.console, FakeClient(), "1",
                                      self.description(1), self.videos_dir, limit=1,
                                      download_media=True, max_mb=50)
        record = records[0]
        self.assertTrue(record["downloaded"])
        self.assertEqual(record["downloadedBytes"], 1024)
        self.assertTrue(any(name.endswith(".video.mp4") for name in os.listdir(self.videos_dir)))
        # 音频超过 50MB 上限 → 跳过并说明原因
        self.assertTrue(any("audio" in msg for msg in record.get("downloadErrors", [])))
        # 下载了媒体就应生成解密助手（ffmpeg 需要按 keyHex 解密 CENC）
        names = os.listdir(self.videos_dir)
        self.assertIn("decrypt.ps1", names)
        self.assertIn("decrypt.sh", names)
        with open(os.path.join(self.videos_dir, "decrypt.ps1"), encoding="utf-8") as handle:
            helper = handle.read()
        self.assertIn("deadbeef", helper)
        self.assertIn(".video.mp4", helper)
        # JSON 里也要记录跳过原因，便于事后排查
        json_name = [n for n in names if n.endswith(".json")][0]
        with open(os.path.join(self.videos_dir, json_name), encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertTrue(payload.get("downloadErrors"))

    def test_no_download_by_default(self):
        records = core.archive_videos(self.store, self.console, FakeClient(), "1",
                                      self.description(1), self.videos_dir, limit=1)
        self.assertFalse(records[0]["downloaded"])
        self.assertFalse(any(name.endswith(".mp4") for name in os.listdir(self.videos_dir)))

    def test_video_failure_recorded(self):
        records = core.archive_videos(self.store, self.console, FakeClient(fail=[UUID1]),
                                      "1", self.description(2), self.videos_dir, limit=2)
        self.assertIn("error", records[0])
        self.assertNotIn("error", records[1])

    def test_no_videos_in_description(self):
        records = core.archive_videos(self.store, self.console, FakeClient(), "1",
                                      "只有文字，没有视频", self.videos_dir, limit=3)
        self.assertEqual(records, [])
        self.assertFalse(os.path.isdir(self.videos_dir))


# ---------------------------------------------------------------------------
# 归档 Markdown 里的视频区块
# ---------------------------------------------------------------------------
class ContestMarkdownVideoTests(unittest.TestCase):
    def test_video_section_rendered(self):
        text = core.render_contest_markdown({
            "contest": {"id": 2647, "name": "S+数位DP", "mode": "LECTURE"},
            "problems": [], "detail": {}, "files": {},
            "videos": [{"order": 1, "title": "S+数位DP20261117", "status": "ready",
                        "durationSeconds": 5121.7, "width": 2160, "height": 1080,
                        "totalBytes": 100 * 1024 * 1024, "encrypted": True,
                        "source": "https://x/videoshow/abc",
                        "localManifest": "videos/01-a.mpd"}],
            "base_url": "https://x",
        })
        self.assertIn("## 讲解视频", text)
        self.assertIn("[S+数位DP20261117](https://x/videoshow/abc)", text)
        self.assertIn("1:25:22", text)
        self.assertIn("ready", text)
        self.assertIn("100.0 MB", text)
        self.assertIn("CENC 加密", text)
        self.assertIn("videos/01-a.mpd", text)

    def test_no_video_section_when_empty(self):
        text = core.render_contest_markdown({
            "contest": {"id": 1, "name": "c"}, "problems": [], "detail": {}, "files": {},
            "base_url": "https://x",
        })
        self.assertNotIn("## 讲解视频", text)


# ---------------------------------------------------------------------------
# 解密助手脚本
# ---------------------------------------------------------------------------
class DecryptHelperTests(TmpCase):
    def test_command_shape(self):
        command = core.ffmpeg_decrypt_command("aabbcc", "01.video.mp4", "01.audio.mp4",
                                             "01.mp4")
        self.assertIn("-decryption_key aabbcc", command)
        self.assertIn('-i "01.video.mp4"', command)
        self.assertIn('-i "01.audio.mp4"', command)
        self.assertIn("-map 0:v:0", command)
        self.assertIn("-map 1:a:0", command)
        self.assertIn("-c copy", command)
        self.assertIn('"01.mp4"', command)

    def test_writes_both_scripts(self):
        records = [{"title": "第 1 讲", "keyHex": "deadbeef",
                    "localVideo": "videos/01-a.video.mp4",
                    "localAudio": "videos/01-a.audio.mp4", "downloaded": True}]
        names = core.write_decrypt_helper(self.tmp, records)
        self.assertEqual(sorted(names), ["decrypt.ps1", "decrypt.sh"])
        with open(os.path.join(self.tmp, "decrypt.ps1"), encoding="utf-8") as handle:
            ps1 = handle.read()
        with open(os.path.join(self.tmp, "decrypt.sh"), encoding="utf-8") as handle:
            sh = handle.read()
        for text in (ps1, sh):
            self.assertIn("deadbeef", text)
            self.assertIn("01-a.video.mp4", text)
            self.assertIn("01-a.audio.mp4", text)
        self.assertIn("Get-Command ffmpeg", ps1)

    def test_skips_records_without_key_or_media(self):
        records = [{"title": "无密钥", "localVideo": "a.video.mp4"},
                   {"title": "无媒体", "keyHex": "aa"}]
        self.assertEqual(core.write_decrypt_helper(self.tmp, records), [])
        self.assertEqual(os.listdir(self.tmp), [])

    def test_single_track(self):
        records = [{"title": "只有音频", "keyHex": "ff",
                    "localAudio": "videos/01.audio.mp4"}]
        core.write_decrypt_helper(self.tmp, records)
        with open(os.path.join(self.tmp, "decrypt.sh"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("-decryption_key ff", text)
        self.assertNotIn("-map 1:a:0", text)
        self.assertIn(".decrypted.m4a", text)      # 只有音频时不要假装是 mp4


# ---------------------------------------------------------------------------
# 站点 API 变更回归：conlist 不再带 returnCount
# ---------------------------------------------------------------------------
class ContestListQueryTests(unittest.TestCase):
    def test_no_return_count(self):
        """站点 2026-11 起拒绝 returnCount，带上会 400。"""
        captured = {}

        def fake(method, path, query=None, body=None, headers=None, retries=None):
            captured["query"] = query
            return 200, {}, b'{"contests": [], "count": 0}'

        client = core.Client("https://example.invalid")
        client.http.request = fake
        client.contest_list(skip=20, take=20)
        self.assertNotIn("returnCount", captured["query"])
        self.assertEqual(captured["query"]["skipCount"], 20)
        self.assertEqual(captured["query"]["takeCount"], 20)

    def test_video_info_uses_display_uuid(self):
        captured = {}

        def fake(method, path, query=None, body=None, headers=None, retries=None):
            captured["path"] = path
            captured["query"] = query
            return 200, {}, json.dumps({"displayName": "x", "status": "ready"}).encode()

        client = core.Client("https://example.invalid")
        client.http.request = fake
        client.video_info(UUID1)
        self.assertEqual(captured["path"], "/api/video/getVideo")
        self.assertEqual(captured["query"]["displayUuid"], UUID1)

    def test_video_info_requires_uuid(self):
        client = core.Client("https://example.invalid")
        with self.assertRaises(core.ApiError):
            client.video_info("")


if __name__ == "__main__":
    unittest.main()
