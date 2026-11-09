import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src import main as watcher


class StatusWebhookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"
        self.path.write_text(json.dumps({
            "seen_clips": ["twitch-old"],
            "kick_seen_clips": ["kick-old"],
        }))
        self.now = datetime(2026, 11, 9, 23, 10, tzinfo=timezone.utc)
        self.status_url = "https://discord.invalid/status"
        for name, value in (
            ("STATE_FILE", str(self.path)),
            ("DISCORD_WEBHOOK", "https://discord.invalid/twitch"),
            ("KICK_WEBHOOK", "https://discord.invalid/kick"),
            ("STATUS_WEBHOOK", self.status_url),
        ):
            p = patch.object(watcher, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, kwargs in (
            ("now_utc", {"return_value": self.now}),
            ("get_twitch_clips", {"return_value": []}),
            ("get_kick_clips", {"return_value": []}),
            ("time.sleep", {}),
            ("requests.post", {"return_value": Mock(status_code=204)}),
        ):
            p = patch("src.main." + name, **kwargs)
            mock = p.start()
            self.addCleanup(p.stop)
            if name == "get_twitch_clips":
                self.twitch = mock
            if name == "get_kick_clips":
                self.kick = mock
            if name == "requests.post":
                self.post = mock

    def clip(self, platform, clip_id, minutes_ago=2):
        return {
            "id": clip_id,
            "title": clip_id,
            "url": (
                "https://clips.twitch.tv/" + clip_id
                if platform == "Twitch"
                else "https://kick.com/jotta?clip=" + clip_id
            ),
            "created_at": (self.now - timedelta(minutes=minutes_ago)).isoformat(),
        }

    def status_messages(self):
        return [
            call.kwargs["json"]["content"]
            for call in self.post.call_args_list
            if call.args[0] == self.status_url
        ]

    def test_zero_clips_still_reports_both_platforms_in_plain_messages(self):
        watcher.main()
        self.assertEqual(self.status_messages(), [
            "🟢 Twitch: verificação realizada às 23:10 (hora de Portugal). Clips novos detetados: 0.",
            "🟢 Kick: verificação realizada às 23:10 (hora de Portugal). Clips novos detetados: 0.",
        ])
        self.assertEqual(self.post.call_count, 2)
        for call in self.post.call_args_list:
            payload = call.kwargs["json"]
            self.assertEqual(call.args[0], self.status_url)
            self.assertIn("content", payload)
            self.assertNotIn("embeds", payload)
            self.assertNotIn("components", payload)
            self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_new_clips_reported_to_status_webhook_without_replacing_clip_webhooks(self):
        self.twitch.return_value = [self.clip("Twitch", "one")]
        self.kick.return_value = [self.clip("Kick", "one"), self.clip("Kick", "two")]
        watcher.main()
        self.assertEqual(self.status_messages(), [
            "🟢 Twitch: verificação realizada às 23:10 (hora de Portugal). Clips novos detetados: 1.",
            "🟢 Kick: verificação realizada às 23:10 (hora de Portugal). Clips novos detetados: 2.",
        ])
        by_url = [call.args[0] for call in self.post.call_args_list]
        self.assertEqual(by_url.count("https://discord.invalid/twitch"), 1)
        self.assertEqual(by_url.count("https://discord.invalid/kick"), 2)
        self.assertEqual(by_url.count(self.status_url), 2)
        state = json.loads(self.path.read_text())
        self.assertIn("one", state["seen_clips"])
        self.assertIn("two", state["kick_seen_clips"])

    def test_failure_is_not_reported_as_success(self):
        self.kick.side_effect = RuntimeError("Kick indisponível")
        with self.assertRaisesRegex(RuntimeError, "Kick: verificação ou envio falhou"):
            watcher.main()
        self.assertEqual(len(self.status_messages()), 2)
        self.assertTrue(self.status_messages()[0].startswith("🟢 Twitch:"))
        self.assertTrue(self.status_messages()[1].startswith("🔴 Kick:"))
        self.assertNotIn("0.", self.status_messages()[1])

    def test_disabled_kick_is_not_reported_as_checked(self):
        with patch.object(watcher, "KICK_WEBHOOK", None):
            watcher.main()
        self.kick.assert_not_called()
        self.assertTrue(self.status_messages()[0].startswith("🟢 Twitch:"))
        self.assertTrue(self.status_messages()[1].startswith("⚪ Kick:"))

    def test_status_webhook_is_optional_and_does_not_change_clip_notifications(self):
        with patch.object(watcher, "STATUS_WEBHOOK", None):
            self.twitch.return_value = [self.clip("Twitch", "new")]
            watcher.main()
        self.assertEqual(self.status_messages(), [])
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.post.call_args.args[0], "https://discord.invalid/twitch")

    def test_status_webhook_failure_does_not_break_clip_notifications(self):
        self.twitch.return_value = [self.clip("Twitch", "new")]
        self.post.side_effect = [
            Mock(status_code=204),  # Clip Twitch
            Mock(status_code=400),  # Relatório Twitch
            Mock(status_code=400),  # Relatório Kick
        ]
        watcher.main()
        self.assertEqual(self.post.call_count, 3)
        self.assertIn("new", json.loads(self.path.read_text())["seen_clips"])

    def test_portuguese_summer_time(self):
        self.now = datetime(2026, 7, 9, 12, 30, tzinfo=timezone.utc)
        watcher.main()
        self.assertIn("13:30 (hora de Portugal)", self.status_messages()[0])
        self.assertIn("13:30 (hora de Portugal)", self.status_messages()[1])


if __name__ == "__main__":
    unittest.main()
