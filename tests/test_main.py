import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src import main as watcher


class TwitchWatcherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_path = Path(temporary.name) / "state.json"
        self.state_path.write_text(json.dumps({"seen_clips": ["already-sent"]}))
        self.now = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
        self.webhook = "https://discord.invalid/test-webhook"
        for name, value in (
            ("STATE_FILE", str(self.state_path)),
            ("DISCORD_WEBHOOK", self.webhook),
        ):
            patcher = patch.object(watcher, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, kwargs in (
            ("now_utc", {"return_value": self.now}),
            ("time.sleep", {}),
            ("get_twitch_clips", {}),
            ("requests.post", {"return_value": Mock(status_code=204)}),
        ):
            patcher = patch(f"src.main.{target}", **kwargs)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            if target == "get_twitch_clips":
                self.fetch = mock
            elif target == "requests.post":
                self.post = mock

    def clip(self, clip_id, minutes_ago=1):
        return {
            "id": clip_id,
            "title": f"Clip {clip_id}",
            "url": f"https://clips.twitch.tv/{clip_id}",
            "created_at": (self.now - timedelta(minutes=minutes_ago)).isoformat(),
        }

    def saved_ids(self):
        return json.loads(self.state_path.read_text())["seen_clips"]

    def sent_clip_urls(self):
        return [
            call.kwargs["json"]["embeds"][0]["url"]
            for call in self.post.call_args_list
            if "embeds" in call.kwargs["json"]
        ]

    def test_only_new_recent_clips_are_sent_in_order_without_duplicates(self):
        self.fetch.return_value = [
            self.clip("newer"), self.clip("already-sent"), self.clip("older", 10),
            self.clip("newer"), self.clip("too-old", 16), self.clip("future", -1),
        ]

        watcher.main()

        self.fetch.assert_called_once_with()
        self.assertEqual(self.sent_clip_urls(), [
            "https://clips.twitch.tv/older", "https://clips.twitch.tv/newer",
        ])
        self.assertEqual(self.saved_ids(), ["already-sent", "older", "newer"])
        self.assertEqual(self.post.call_count, 2)
        for call in self.post.call_args_list:
            self.assertEqual(call.args[0], self.webhook)
            payload = call.kwargs["json"]
            self.assertEqual(payload["username"], "JOTTA Twitch Clip Watcher")
            self.assertEqual(payload["allowed_mentions"], {"parse": []})
            if "embeds" in payload:
                self.assertEqual(payload["embeds"][0]["color"], 0x9146FF)

    def test_no_new_clips_does_not_send_discord_messages(self):
        for clips in (
            [],
            [self.clip("already-sent"), self.clip("too-old", 16), self.clip("future", -1)],
        ):
            with self.subTest(clips=clips):
                self.fetch.return_value = clips

                watcher.main()

                self.post.assert_not_called()
                self.assertEqual(self.saved_ids(), ["already-sent"])

    def test_partial_failure_saves_successes_and_retries_only_unsent_clips(self):
        self.fetch.return_value = [
            self.clip("first", 3), self.clip("retry", 2), self.clip("last", 1),
        ]
        self.post.side_effect = [Mock(status_code=status) for status in (204, 400, 204)]

        with self.assertRaisesRegex(RuntimeError, "envios para o Discord falharam"):
            watcher.main()

        self.assertEqual(self.saved_ids(), ["already-sent", "first", "last"])
        self.post.reset_mock(side_effect=True)
        watcher.main()
        self.assertEqual(self.sent_clip_urls(), ["https://clips.twitch.tv/retry"])
        self.assertEqual(self.saved_ids(), ["already-sent", "first", "last", "retry"])

    def test_missing_webhook_fails_without_fetching_or_changing_history(self):
        with patch.object(watcher, "DISCORD_WEBHOOK", None):
            with self.assertRaisesRegex(RuntimeError, "DISCORD_CLIP_WEBHOOK_URL"):
                watcher.main()
        self.fetch.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(self.saved_ids(), ["already-sent"])

    def test_single_new_clip_sends_only_clip_message(self):
        self.fetch.return_value = [self.clip("new")]
        watcher.main()
        self.assertEqual(self.post.call_count, 1)
        payload = self.post.call_args.kwargs["json"]
        self.assertIn("embeds", payload)
        self.assertEqual(payload["content"], "🎬 **NOVO CLIP DO JOTTA | TWITCH**")
        self.assertEqual(self.saved_ids(), ["already-sent", "new"])


if __name__ == "__main__":
    unittest.main()
