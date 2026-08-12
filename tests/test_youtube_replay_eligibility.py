import unittest
from unittest.mock import patch


with patch("dotenv.load_dotenv"):
    from scripts import replay_to_discord


def youtube_item(
    video_id: str,
    *,
    duration: str,
    actual_end_time: str = "",
) -> dict:
    item = {
        "id": video_id,
        "snippet": {
            "title": "🔴 Live Trading Crypto Futures Forex Stocks - NY Open",
            "publishedAt": "2026-08-11T10:00:00Z",
            "channelId": "channel",
            "channelTitle": "Main Line Trades",
            "thumbnails": {},
        },
        "contentDetails": {"duration": duration},
    }
    if actual_end_time:
        item["liveStreamingDetails"] = {
            "actualEndTime": actual_end_time,
        }
    return item


class YouTubeReplayEligibilityTests(unittest.TestCase):
    def test_video_details_requests_live_streaming_metadata(self):
        with patch.object(
            replay_to_discord,
            "youtube_api_get",
            return_value={"items": []},
        ) as api_get:
            replay_to_discord.fetch_video_details(["video"], "api-key")

        self.assertEqual(
            api_get.call_args.args[1]["part"],
            "snippet,contentDetails,liveStreamingDetails",
        )

    def test_completed_archived_livestream_is_eligible(self):
        video = replay_to_discord.normalize_video_item(
            youtube_item(
                "completed",
                duration="PT2H14M",
                actual_end_time="2026-08-11T12:14:00Z",
            )
        )

        self.assertIsNotNone(video)
        self.assertTrue(replay_to_discord.is_completed_replay(video))
        self.assertEqual(video["duration"], "2h 14m")

    def test_offline_scheduled_livestream_is_not_eligible(self):
        video = replay_to_discord.normalize_video_item(
            youtube_item("scheduled", duration="P0D")
        )

        self.assertIsNotNone(video)
        self.assertFalse(replay_to_discord.is_completed_replay(video))
        self.assertEqual(video["duration"], "Unknown")

    def test_ended_but_still_processing_livestream_is_not_eligible(self):
        video = replay_to_discord.normalize_video_item(
            youtube_item(
                "processing",
                duration="P0D",
                actual_end_time="2026-08-11T12:14:00Z",
            )
        )

        self.assertIsNotNone(video)
        self.assertFalse(replay_to_discord.is_completed_replay(video))

    def test_recorded_duration_without_confirmed_end_is_not_eligible(self):
        video = replay_to_discord.normalize_video_item(
            youtube_item("live", duration="PT35M")
        )

        self.assertIsNotNone(video)
        self.assertFalse(replay_to_discord.is_completed_replay(video))

    def test_matching_uploads_excludes_placeholder_and_keeps_replay(self):
        placeholder = replay_to_discord.normalize_video_item(
            youtube_item("placeholder", duration="P0D")
        )
        completed = replay_to_discord.normalize_video_item(
            youtube_item(
                "completed",
                duration="PT1H30M",
                actual_end_time="2026-08-11T11:30:00Z",
            )
        )

        with patch.object(
            replay_to_discord,
            "get_uploads_playlist_id",
            return_value="uploads",
        ), patch.object(
            replay_to_discord,
            "youtube_api_get",
            return_value={
                "items": [
                    {"contentDetails": {"videoId": "placeholder"}},
                    {"contentDetails": {"videoId": "completed"}},
                ]
            },
        ), patch.object(
            replay_to_discord,
            "fetch_video_details",
            return_value=[placeholder, completed],
        ):
            videos = replay_to_discord.fetch_matching_uploads(
                "channel",
                "api-key",
                "🔴 Live Trading",
                25,
            )

        self.assertEqual([video["id"] for video in videos], ["completed"])


if __name__ == "__main__":
    unittest.main()
