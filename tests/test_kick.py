import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src import main as watcher


class KickWatcherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_path = Path(directory.name) / "state.json"
        self.state_path.write_text(json.dumps({
            "seen_clips": ["twitch-old"],
            "kick_seen_clips": ["kick-old"],
        }), encoding="utf-8")
        self.now = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)
        self.twitch_webhook = "https://discord.invalid/twitch"
        self.kick_webhook = "https://discord.invalid/kick"
        for name, value in (
            ("STATE_FILE", str(self.state_path)),
            ("DISCORD_WEBHOOK", self.twitch_webhook),
            ("KICK_WEBHOOK", self.kick_webhook),
        ):
            patcher = patch.object(watcher, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, kwargs in (
            ("now_utc", {"return_value": self.now}),
            ("get_twitch_clips", {"return_value": []}),
            ("get_kick_clips", {"return_value": []}),
            ("time.sleep", {}),
            ("requests.post", {"return_value": Mock(status_code=204)}),
        ):
            patcher = patch("src.main." + target, **kwargs)
            m = patcher.start()
            self.addCleanup(patcher.stop)
            if target == "get_kick_clips":
                self.get_kick = m
            if target == "get_twitch_clips":
                self.get_twitch = m
            if target == "requests.post":
                self.post = m

    def clip(self, clip_id, minutes_ago=1):
        return {
            "id": clip_id,
            "title": "Clip " + clip_id,
            "url": "https://kick.com/jotta?clip=" + clip_id,
            "creator_name": "clipador",
            "view_count": 13,
            "created_at": (self.now - timedelta(minutes=minutes_ago)).isoformat(),
        }

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_recent_kick_clips_only_once_in_order_and_correct_format(self):
        self.get_kick.return_value = [
            self.clip("newer"),
            self.clip("kick-old"),
            self.clip("older", 10),
            self.clip("older", 10),
            self.clip("too-old", 16),
            self.clip("future", -1),
        ]
        watcher.main()
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.state()["seen_clips"], ["twitch-old"])
        self.assertEqual(self.state()["kick_seen_clips"], ["kick-old", "older", "newer"])
        self.assertEqual(
            [c.kwargs["json"]["embeds"][0]["url"] for c in self.post.call_args_list],
            [self.clip("older")["url"], self.clip("newer")["url"]],
        )
        for call in self.post.call_args_list:
            self.assertEqual(call.args[0], self.kick_webhook)
            payload = call.kwargs["json"]
            self.assertEqual(payload["username"], "JOTTA Kick Clip Watcher")
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            self.assertNotIn("content", payload)
            embed = payload["embeds"][0]
            self.assertEqual(embed["author"]["name"], "🎬 Novo clip detetado!")
            self.assertEqual(embed["color"], 0x53FC18)
            self.assertEqual(embed["fields"][0]["value"], "clipador")
            self.assertEqual(embed["fields"][1]["value"], "13")
            self.assertEqual(embed["fields"][2]["value"], "09/11/2026 às 21:50 UTC" if embed["title"] == "Clip older" else "09/11/2026 às 21:59 UTC")
            self.assertNotIn("thumbnail", embed)
            self.assertNotIn("image", embed)
            self.assertEqual(
                payload["components"][0]["components"][0]["url"], embed["url"]
            )

    def test_no_kick_clips_sends_nothing(self):
        watcher.main()
        self.post.assert_not_called()
        self.assertEqual(self.state()["kick_seen_clips"], ["kick-old"])

    def test_partial_kick_failure_keeps_successful_history(self):
        self.get_kick.return_value = [
            self.clip("first", 3), self.clip("retry", 2), self.clip("last", 1)
        ]
        self.post.side_effect = [Mock(status_code=s) for s in (204, 400, 204)]
        with self.assertRaisesRegex(RuntimeError, "Kick: verificação ou envio falhou"):
            watcher.main()
        self.assertEqual(self.state()["kick_seen_clips"], ["kick-old", "first", "last"])
        self.post.reset_mock(side_effect=True)
        watcher.main()
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.post.call_args.kwargs["json"]["embeds"][0]["url"], self.clip("retry")["url"])
        self.assertEqual(self.state()["kick_seen_clips"], ["kick-old", "first", "last", "retry"])

    def test_twitch_failure_does_not_prevent_kick_check(self):
        self.get_twitch.side_effect = RuntimeError("Twitch API falhou")
        self.get_kick.return_value = [self.clip("works")]
        with self.assertRaisesRegex(RuntimeError, "Twitch API falhou"):
            watcher.main()
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.state()["kick_seen_clips"], ["kick-old", "works"])

    def test_kick_failure_does_not_prevent_twitch_check(self):
        self.get_twitch.return_value = [{
            "id": "twitch-new",
            "title": "Twitch clip",
            "url": "https://clips.twitch.tv/example",
            "created_at": self.now.isoformat(),
        }]
        self.get_kick.side_effect = RuntimeError("Kick indisponível")
        with self.assertRaisesRegex(RuntimeError, "Kick: verificação ou envio falhou"):
            watcher.main()
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.post.call_args.args[0], self.twitch_webhook)
        self.assertIn("twitch-new", self.state()["seen_clips"])

    def test_kick_without_webhook_is_skipped(self):
        with patch.object(watcher, "KICK_WEBHOOK", None):
            watcher.main()
        self.get_kick.assert_not_called()
        self.post.assert_not_called()


class KickAPITests(unittest.TestCase):
    def test_kick_api_pagination(self):
        at = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)
        data = [
            {"clips": [{
                "id": "clip-1",
                "title": "Primeiro",
                "created_at": at.isoformat(),
                "creator": {"username": "one"},
                "views": 77,
            }], "nextCursor": "page-2"},
            {"clips": [{
                "id": "clip-2",
                "title": "Segundo",
                "created_at": at.isoformat(),
                "creator": {"username": "two"},
                "view_count": 9,
            }], "nextCursor": None},
        ]
        with patch.object(watcher, "now_utc", return_value=at), patch(
            "src.main.requests.get",
            side_effect=[Mock(status_code=200, json=Mock(return_value=d)) for d in data],
        ) as get:
            clips = watcher.get_kick_clips()
        self.assertEqual([c["id"] for c in clips], ["clip-1", "clip-2"])
        self.assertEqual([c["view_count"] for c in clips], [77, 9])
        self.assertEqual(clips[0]["url"], "https://kick.com/jotta?clip=clip-1")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["cursor"], "page-2")

    def test_invalid_kick_response_raises_without_sending(self):
        with patch("src.main.requests.get", return_value=Mock(
            status_code=200, json=Mock(return_value={"message": "invalid"})
        )):
            with self.assertRaisesRegex(RuntimeError, "formato inesperado"):
                watcher.get_kick_clips()


if __name__ == "__main__":
    unittest.main()
