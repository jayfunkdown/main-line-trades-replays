import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch


with patch("dotenv.load_dotenv"):
    from scripts import replay_to_discord


def training_video(
    video_id: str,
    published: str,
    title: str | None = None,
) -> dict[str, str]:
    return {
        "id": video_id,
        "title": title or f"Training {video_id}",
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "published": published,
        "duration": "9m 30s",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
    }


class FakeDiscordResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class TrainingPlaylistTests(unittest.TestCase):
    def test_training_service_and_timer_use_safe_automatic_mode(self):
        systemd = replay_to_discord.PROJECT_ROOT / "deploy" / "systemd"
        service = (systemd / "mainline-youtube-training.service").read_text(
            encoding="utf-8"
        )
        timer = (systemd / "mainline-youtube-training.timer").read_text(
            encoding="utf-8"
        )

        self.assertIn("Type=oneshot", service)
        self.assertIn("--post --feed training", service)
        self.assertNotIn("--import-existing", service)
        self.assertIn("OnUnitActiveSec=30min", timer)
        self.assertIn("Unit=mainline-youtube-training.service", timer)

    def test_playlist_fetch_paginates_batches_and_sorts_oldest_first(self):
        playlist_pages = [
            {
                "items": [
                    {"contentDetails": {"videoId": "new"}},
                    {"contentDetails": {"videoId": "middle"}},
                ],
                "nextPageToken": "page-two",
            },
            {
                "items": [
                    {"contentDetails": {"videoId": "old"}},
                    {"contentDetails": {"videoId": "new"}},
                ]
            },
        ]
        details = {
            item["id"]: item
            for item in (
                training_video("old", "2024-01-01T00:00:00Z"),
                training_video("middle", "2025-01-01T00:00:00Z"),
                training_video("new", "2026-01-01T00:00:00Z"),
            )
        }
        detail_responses = {
            video_id: {
                "id": video_id,
                "snippet": {
                    "title": video["title"],
                    "publishedAt": video["published"],
                    "thumbnails": {
                        "maxres": {"url": video["thumbnail"]},
                    },
                },
                "contentDetails": {"duration": "PT9M30S"},
            }
            for video_id, video in details.items()
        }
        page_index = 0

        def api_get(endpoint, parameters, api_key):
            nonlocal page_index
            self.assertEqual(api_key, "api-key")
            if endpoint == "playlistItems":
                page = playlist_pages[page_index]
                page_index += 1
                return page
            ids = str(parameters["id"]).split(",")
            return {"items": [detail_responses[item] for item in ids]}

        with patch.object(
            replay_to_discord,
            "youtube_api_get",
            side_effect=api_get,
        ):
            videos = replay_to_discord.fetch_playlist_videos(
                "playlist",
                "api-key",
                10,
            )

        self.assertEqual([video["id"] for video in videos], ["old", "middle", "new"])
        self.assertEqual(page_index, 2)

    def test_training_embed_matches_bordered_replay_card_style(self):
        video = training_video("lesson", "2026-08-01T12:00:00Z", "Price Action")
        embed = replay_to_discord.build_training_embed(video)

        self.assertEqual(embed["color"], replay_to_discord.BRAND_ELECTRIC_BLUE)
        self.assertEqual(embed["author"], {"name": "Main Line Trades"})
        self.assertEqual(embed["title"], "🎓 Price Action")
        self.assertEqual(embed["url"], video["url"])
        self.assertEqual(embed["image"], {"url": video["thumbnail"]})
        self.assertEqual(embed["footer"]["text"], "Main Line Trades • Training Video")
        self.assertEqual(embed["fields"][2]["name"], "▶️ Watch Tutorial")

    def test_training_webhook_payload_is_mention_safe(self):
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
            replay_to_discord.post_training_embed(
                "training-webhook",
                training_video("lesson", "2026-08-01T12:00:00Z"),
            )

        payload = json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(captured["url"], "training-webhook")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(payload["username"], "Main Line Trades Training Videos")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"]), 1)

    def test_automatic_first_run_seeds_state_without_posting(self):
        videos = [
            training_video("old", "2024-01-01T00:00:00Z"),
            training_video("new", "2025-01-01T00:00:00Z"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "training.json"
            with patch.object(
                replay_to_discord,
                "TRAINING_STATE_PATH",
                state_path,
            ), patch.object(
                replay_to_discord,
                "fetch_playlist_videos",
                return_value=videos,
            ), patch.object(
                replay_to_discord,
                "post_training_embed",
            ) as post, patch.dict(
                os.environ,
                {
                    "YOUTUBE_API_KEY": "api-key",
                    "TRAINING_VIDEOS_WEBHOOK": "webhook",
                },
                clear=True,
            ), patch(
                "sys.argv",
                ["replay_to_discord.py", "--post", "--feed", "training"],
            ):
                self.assertEqual(replay_to_discord.main(), 0)

            self.assertEqual(json.loads(state_path.read_text()), ["old", "new"])
            post.assert_not_called()

    def test_preview_does_not_post_or_create_state(self):
        videos = [training_video("lesson", "2025-01-01T00:00:00Z")]
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "training.json"
            with patch.object(
                replay_to_discord,
                "TRAINING_STATE_PATH",
                state_path,
            ), patch.object(
                replay_to_discord,
                "fetch_playlist_videos",
                return_value=videos,
            ), patch.object(
                replay_to_discord,
                "post_training_embed",
            ) as post, patch.dict(
                os.environ,
                {"YOUTUBE_API_KEY": "api-key"},
                clear=True,
            ), patch(
                "sys.argv",
                ["replay_to_discord.py", "--preview", "--feed", "training"],
            ):
                self.assertEqual(replay_to_discord.main(), 0)

            self.assertFalse(state_path.exists())
            post.assert_not_called()

    def test_explicit_import_posts_oldest_first_and_new_runs_only_post_new_items(self):
        existing = [
            training_video("old", "2024-01-01T00:00:00Z"),
            training_video("middle", "2025-01-01T00:00:00Z"),
        ]
        later = training_video("new", "2026-01-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "training.json"
            with patch.object(
                replay_to_discord,
                "TRAINING_STATE_PATH",
                state_path,
            ), patch.object(
                replay_to_discord,
                "fetch_playlist_videos",
                return_value=existing,
            ), patch.object(
                replay_to_discord,
                "post_training_embed",
            ) as post, patch.object(
                replay_to_discord.time,
                "sleep",
            ), patch.dict(
                os.environ,
                {
                    "YOUTUBE_API_KEY": "api-key",
                    "TRAINING_VIDEOS_WEBHOOK": "webhook",
                },
                clear=True,
            ), patch(
                "sys.argv",
                [
                    "replay_to_discord.py",
                    "--post",
                    "--feed",
                    "training",
                    "--import-existing",
                ],
            ):
                self.assertEqual(replay_to_discord.main(), 0)
                self.assertEqual(post.call_args_list, [call("webhook", existing[0]), call("webhook", existing[1])])

            self.assertEqual(json.loads(state_path.read_text()), ["old", "middle"])

            all_videos = [*existing, later]
            with patch.object(
                replay_to_discord,
                "TRAINING_STATE_PATH",
                state_path,
            ), patch.object(
                replay_to_discord,
                "fetch_playlist_videos",
                return_value=all_videos,
            ), patch.object(
                replay_to_discord,
                "post_training_embed",
            ) as post, patch.object(
                replay_to_discord.time,
                "sleep",
            ), patch.dict(
                os.environ,
                {
                    "YOUTUBE_API_KEY": "api-key",
                    "TRAINING_VIDEOS_WEBHOOK": "webhook",
                },
                clear=True,
            ), patch(
                "sys.argv",
                ["replay_to_discord.py", "--post", "--feed", "training"],
            ):
                self.assertEqual(replay_to_discord.main(), 0)
                post.assert_called_once_with("webhook", later)

            self.assertEqual(json.loads(state_path.read_text()), ["old", "middle", "new"])

    def test_failed_import_records_only_successful_posts_for_safe_retry(self):
        videos = [
            training_video("first", "2024-01-01T00:00:00Z"),
            training_video("second", "2025-01-01T00:00:00Z"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "training.json"
            with patch.object(
                replay_to_discord,
                "TRAINING_STATE_PATH",
                state_path,
            ), patch.object(
                replay_to_discord,
                "fetch_playlist_videos",
                return_value=videos,
            ), patch.object(
                replay_to_discord,
                "post_training_embed",
                side_effect=[None, RuntimeError("delivery failed")],
            ), patch.object(
                replay_to_discord.time,
                "sleep",
            ), patch.dict(
                os.environ,
                {
                    "YOUTUBE_API_KEY": "api-key",
                    "TRAINING_VIDEOS_WEBHOOK": "webhook",
                },
                clear=True,
            ), patch(
                "sys.argv",
                [
                    "replay_to_discord.py",
                    "--post",
                    "--feed",
                    "training",
                    "--import-existing",
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, "delivery failed"):
                    replay_to_discord.main()

            self.assertEqual(json.loads(state_path.read_text()), ["first"])

    def test_missing_training_webhook_fails_before_youtube_access(self):
        with patch.object(
            replay_to_discord,
            "fetch_playlist_videos",
        ) as fetch, patch.dict(
            os.environ,
            {"YOUTUBE_API_KEY": "api-key"},
            clear=True,
        ), patch(
            "sys.argv",
            ["replay_to_discord.py", "--post", "--feed", "training"],
        ):
            with self.assertRaisesRegex(RuntimeError, "TRAINING_VIDEOS_WEBHOOK"):
                replay_to_discord.main()

        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
