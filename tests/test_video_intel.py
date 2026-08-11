import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch


with patch("dotenv.load_dotenv"):
    from scripts import replay_to_discord


def video(
    video_id,
    published,
    channel="Example Channel",
    *,
    duration_seconds=750,
):
    return {
        "id": video_id,
        "title": f"Video {video_id}",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "published": published,
        "duration": "12m 30s",
        "duration_seconds": duration_seconds,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        "channel_id": f"channel-{channel}",
        "channel_title": channel,
    }


class FakeDiscordResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class VideoIntelTests(unittest.TestCase):
    def test_approved_channel_handles_are_exact(self):
        self.assertEqual(
            replay_to_discord.VIDEO_INTEL_CHANNEL_HANDLES,
            (
                "@JasonPizzinoOfficial",
                "@TheDiaryOfACEO",
                "@GrahamStephan",
                "@HeresyFinancial",
            ),
        )

    def test_handle_resolution_uses_youtube_for_handle(self):
        with patch.object(
            replay_to_discord,
            "youtube_api_get",
            return_value={
                "items": [
                    {
                        "id": "channel-id",
                        "snippet": {"title": "Channel Name"},
                        "contentDetails": {
                            "relatedPlaylists": {"uploads": "uploads-id"}
                        },
                    }
                ]
            },
        ) as api_get:
            result = replay_to_discord.get_channel_for_handle(
                "@ChannelHandle", "api-key"
            )

        self.assertEqual(
            result,
            {
                "handle": "@ChannelHandle",
                "id": "channel-id",
                "title": "Channel Name",
                "uploads_playlist_id": "uploads-id",
            },
        )
        api_get.assert_called_once_with(
            "channels",
            {
                "part": "snippet,contentDetails",
                "forHandle": "@ChannelHandle",
                "maxResults": 1,
            },
            "api-key",
        )

    def test_three_channel_fetch_deduplicates_and_sorts(self):
        channels = {
            "@one": {
                "handle": "@one",
                "id": "one-id",
                "title": "One",
                "uploads_playlist_id": "one-uploads",
            },
            "@two": {
                "handle": "@two",
                "id": "two-id",
                "title": "Two",
                "uploads_playlist_id": "two-uploads",
            },
            "@three": {
                "handle": "@three",
                "id": "three-id",
                "title": "Three",
                "uploads_playlist_id": "three-uploads",
            },
        }
        videos = {
            "one-uploads": [video("later", "2026-08-02T00:00:00Z", "One")],
            "two-uploads": [video("first", "2026-08-01T00:00:00Z", "Two")],
            "three-uploads": [video("later", "2026-08-02T00:00:00Z", "Three")],
        }
        with patch.object(
            replay_to_discord,
            "get_channel_for_handle",
            side_effect=lambda handle, api_key: channels[handle],
        ), patch.object(
            replay_to_discord,
            "fetch_playlist_videos",
            side_effect=lambda playlist, api_key, limit: videos[playlist],
        ):
            result = replay_to_discord.fetch_video_intel_videos(
                ("@one", "@two", "@three"), "api-key", 25
            )

        self.assertEqual([item["id"] for item in result], ["first", "later"])

    def test_video_intel_excludes_shorts_and_unknown_durations(self):
        channels = {
            "@one": {
                "handle": "@one",
                "id": "one-id",
                "title": "One",
                "uploads_playlist_id": "one-uploads",
            }
        }
        uploads = [
            video("short-71", "2026-08-11T12:00:00Z", duration_seconds=71),
            video("short-180", "2026-08-11T12:01:00Z", duration_seconds=180),
            video("unknown", "2026-08-11T12:02:00Z", duration_seconds=None),
            video("long-181", "2026-08-11T12:03:00Z", duration_seconds=181),
            video("long", "2026-08-11T12:04:00Z", duration_seconds=1200),
        ]

        with patch.object(
            replay_to_discord,
            "get_channel_for_handle",
            side_effect=lambda handle, api_key: channels[handle],
        ), patch.object(
            replay_to_discord,
            "fetch_playlist_videos",
            return_value=uploads,
        ):
            result = replay_to_discord.fetch_video_intel_videos(
                ("@one",), "api-key", 25
            )

        self.assertEqual([item["id"] for item in result], ["long-181", "long"])

    def test_normalized_video_retains_duration_seconds_for_filtering(self):
        normalized = replay_to_discord.normalize_video_item(
            {
                "id": "short",
                "snippet": {
                    "title": "Short upload",
                    "publishedAt": "2026-08-11T12:00:00Z",
                },
                "contentDetails": {"duration": "PT1M11S"},
            }
        )

        self.assertEqual(normalized["duration"], "1m 11s")
        self.assertEqual(normalized["duration_seconds"], 71)

    def test_embed_matches_approved_video_card_style(self):
        item = video("intel", "2026-08-11T12:00:00Z", "Jason Pizzino")
        embed = replay_to_discord.build_video_intel_embed(item)

        self.assertEqual(embed["color"], replay_to_discord.BRAND_ELECTRIC_BLUE)
        self.assertEqual(embed["author"], {"name": "Main Line Trades"})
        self.assertEqual(embed["title"], "🎥 Video intel")
        self.assertEqual(embed["url"], item["url"])
        self.assertEqual(embed["image"], {"url": item["thumbnail"]})
        self.assertEqual(embed["fields"][0], {
            "name": "📺 Source",
            "value": "Jason Pizzino",
            "inline": False,
        })
        self.assertEqual(embed["fields"][-1]["name"], "▶️ Watch Video")
        self.assertEqual(embed["footer"]["text"], "Main Line Trades • Video Intel")

    def test_webhook_payload_is_mention_safe(self):
        captured = {}

        def request(url, *, data, headers, method):
            captured.update(url=url, data=data, headers=headers, method=method)
            return object()

        with patch.object(
            replay_to_discord.urllib.request,
            "Request",
            side_effect=request,
        ), patch.object(
            replay_to_discord.urllib.request,
            "urlopen",
            return_value=FakeDiscordResponse(),
        ):
            replay_to_discord.post_video_intel_embed(
                "video-intel-webhook",
                video("intel", "2026-08-11T12:00:00Z"),
            )

        payload = json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(captured["url"], "video-intel-webhook")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(payload["username"], "Main Line Trades Video Intel")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"]), 1)

    def test_first_run_seeds_all_three_channels_without_posting(self):
        existing = [
            video("one", "2026-08-01T00:00:00Z", "One"),
            video("two", "2026-08-02T00:00:00Z", "Two"),
            video("three", "2026-08-03T00:00:00Z", "Three"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "video-intel.json"
            with patch.object(
                replay_to_discord, "VIDEO_INTEL_STATE_PATH", state_path
            ), patch.object(
                replay_to_discord,
                "fetch_video_intel_videos",
                return_value=existing,
            ) as fetch, patch.object(
                replay_to_discord,
                "post_video_intel_embed",
            ) as post, patch.dict(
                os.environ,
                {
                    "YOUTUBE_API_KEY": "api-key",
                    "VIDEO_INTEL_WEBHOOK": "webhook",
                },
                clear=True,
            ), patch(
                "sys.argv",
                ["replay_to_discord.py", "--post", "--feed", "video-intel"],
            ):
                self.assertEqual(replay_to_discord.main(), 0)

            fetch.assert_called_once_with(
                replay_to_discord.VIDEO_INTEL_CHANNEL_HANDLES,
                "api-key",
                replay_to_discord.DEFAULT_FETCH_LIMIT,
            )
            post.assert_not_called()
            self.assertEqual(json.loads(state_path.read_text()), ["one", "two", "three"])

    def test_later_run_posts_only_new_videos_oldest_first(self):
        old = video("old", "2026-08-01T00:00:00Z", "One")
        first = video("first", "2026-08-11T12:00:00Z", "Two")
        second = video("second", "2026-08-11T12:01:00Z", "Three")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "video-intel.json"
            state_path.write_text('["old"]\n', encoding="utf-8")
            with patch.object(
                replay_to_discord, "VIDEO_INTEL_STATE_PATH", state_path
            ), patch.object(
                replay_to_discord,
                "fetch_video_intel_videos",
                return_value=[old, first, second],
            ), patch.object(
                replay_to_discord,
                "post_video_intel_embed",
            ) as post, patch.object(
                replay_to_discord.time, "sleep"
            ), patch.dict(
                os.environ,
                {
                    "YOUTUBE_API_KEY": "api-key",
                    "VIDEO_INTEL_WEBHOOK": "webhook",
                },
                clear=True,
            ), patch(
                "sys.argv",
                [
                    "replay_to_discord.py",
                    "--post",
                    "--feed",
                    "video-intel",
                    "--limit",
                    "3",
                ],
            ):
                self.assertEqual(replay_to_discord.main(), 0)

            self.assertEqual(
                post.call_args_list,
                [call("webhook", first), call("webhook", second)],
            )
            self.assertEqual(
                json.loads(state_path.read_text()), ["old", "first", "second"]
            )

    def test_service_timer_and_missing_webhook_are_safe(self):
        systemd = replay_to_discord.PROJECT_ROOT / "deploy" / "systemd"
        service = (systemd / "mainline-youtube-video-intel.service").read_text(
            encoding="utf-8"
        )
        timer = (systemd / "mainline-youtube-video-intel.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("Type=oneshot", service)
        self.assertIn("--post --feed video-intel --limit 3", service)
        self.assertIn("OnUnitActiveSec=30min", timer)
        self.assertIn("Unit=mainline-youtube-video-intel.service", timer)

        with patch.object(
            replay_to_discord, "fetch_video_intel_videos"
        ) as fetch, patch.dict(
            os.environ, {"YOUTUBE_API_KEY": "api-key"}, clear=True
        ), patch(
            "sys.argv",
            ["replay_to_discord.py", "--post", "--feed", "video-intel"],
        ):
            with self.assertRaisesRegex(RuntimeError, "VIDEO_INTEL_WEBHOOK"):
                replay_to_discord.main()
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
