import asyncio
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts import earnings_state

with patch("dotenv.load_dotenv"):
    from scripts import earnings_reactions


def process_state_update(
    state_path,
    section,
    key,
    value,
    start_event,
    delay,
):
    store = earnings_state.EarningsStateStore(state_path)
    start_event.wait()

    def mutation(state):
        if delay:
            time.sleep(delay)
        state[section][key] = value

    store.transaction(mutation)


class StateStoreValidationTests(unittest.TestCase):
    def test_missing_file_and_absent_sections_normalize_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = earnings_state.EarningsStateStore(state_path)

            self.assertEqual(store.load(), store.empty_state())
            self.assertFalse(state_path.exists())

            state_path.write_text(
                json.dumps({"public": {}, "future_field": {"kept": True}}),
                encoding="utf-8",
            )
            loaded = store.load()

        self.assertEqual(loaded["future_field"], {"kept": True})
        for section in earnings_state.KNOWN_SECTIONS:
            self.assertEqual(loaded[section], {})

    def test_invalid_top_level_and_safety_sections_fail_closed(self):
        invalid_values = (
            [],
            {"public": []},
            {"private": "bad"},
            {"signal_queue": None},
            {"manual_signal_drafts": []},
            {"post_signal_reviews": []},
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = earnings_state.EarningsStateStore(state_path)

            for value in invalid_values:
                with self.subTest(value=value):
                    state_path.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaises(
                        earnings_state.EarningsStateValidationError
                    ):
                        store.load()

    def test_malformed_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(
                earnings_state.EarningsStateValidationError,
                "not valid JSON",
            ):
                earnings_state.EarningsStateStore(state_path).load()

    def test_invalid_quotes_reset_without_discarding_other_records(self):
        original = {
            "public": {"public-key": {"symbol": "ONE"}},
            "private": {"private-key": {"symbol": "TWO"}},
            "quotes": ["bad"],
            "signal_queue": {"token": {"sent_to_signals": True}},
            "future_field": {"kept": True},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(original), encoding="utf-8")
            store = earnings_state.EarningsStateStore(state_path)
            loaded = store.load()

            self.assertEqual(loaded["quotes"], {})
            self.assertEqual(loaded["public"], original["public"])
            self.assertEqual(loaded["private"], original["private"])
            self.assertEqual(loaded["signal_queue"], original["signal_queue"])
            self.assertEqual(loaded["future_field"], {"kept": True})
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                original,
            )


class StateStoreConcurrencyTests(unittest.TestCase):
    def run_process_updates(self, updates):
        context = multiprocessing.get_context("spawn")

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            start_event = context.Event()
            processes = [
                context.Process(
                    target=process_state_update,
                    args=(
                        str(state_path),
                        section,
                        key,
                        value,
                        start_event,
                        delay,
                    ),
                )
                for section, key, value, delay in updates
            ]

            for process in processes:
                process.start()

            start_event.set()

            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)

            return earnings_state.EarningsStateStore(state_path).load()

    def test_processes_updating_different_sections_preserve_both(self):
        state = self.run_process_updates(
            (
                ("public", "public-key", {"symbol": "ONE"}, 0.1),
                (
                    "signal_queue",
                    "signal-key",
                    {"sent_to_signals": True},
                    0,
                ),
            )
        )

        self.assertEqual(state["public"]["public-key"]["symbol"], "ONE")
        self.assertTrue(
            state["signal_queue"]["signal-key"]["sent_to_signals"]
        )

    def test_processes_updating_different_records_preserve_both(self):
        state = self.run_process_updates(
            (
                ("public", "first", {"symbol": "ONE"}, 0.1),
                ("public", "second", {"symbol": "TWO"}, 0),
            )
        )

        self.assertEqual(set(state["public"]), {"first", "second"})


class StateStoreAtomicityTests(unittest.TestCase):
    def test_transaction_preserves_unknown_fields_and_fsyncs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps({"future_field": {"kept": True}}),
                encoding="utf-8",
            )
            store = earnings_state.EarningsStateStore(state_path)

            with patch.object(
                earnings_state.os,
                "fsync",
                wraps=os.fsync,
            ) as fsync:
                state, _ = store.transaction(
                    lambda value: value["public"].update(
                        {"key": {"symbol": "ACME"}}
                    )
                )

            fsync.assert_called_once()
            self.assertEqual(state["future_field"], {"kept": True})
            self.assertIn("key", store.load()["public"])

    def test_mutation_exception_releases_lock_without_writing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps({"public": {"old": {"symbol": "OLD"}}}),
                encoding="utf-8",
            )
            original_bytes = state_path.read_bytes()
            store = earnings_state.EarningsStateStore(state_path)

            def fail(state):
                state["public"]["new"] = {"symbol": "NEW"}
                raise RuntimeError("synthetic mutation failure")

            with self.assertRaisesRegex(RuntimeError, "mutation failure"):
                store.transaction(fail)

            self.assertEqual(state_path.read_bytes(), original_bytes)
            self.assertEqual(list(Path(temp_dir).glob(".*.tmp")), [])

            state, _ = store.transaction(
                lambda value: value["private"].update(
                    {"after": {"symbol": "AFTER"}}
                )
            )
            self.assertIn("after", state["private"])

    def test_replace_failure_preserves_prior_file_cleans_temp_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            original = {"public": {"old": {"symbol": "OLD"}}}
            state_path.write_text(json.dumps(original), encoding="utf-8")
            original_bytes = state_path.read_bytes()
            store = earnings_state.EarningsStateStore(state_path)

            with patch.object(
                earnings_state.os,
                "replace",
                side_effect=OSError("synthetic replace failure"),
            ):
                with self.assertRaisesRegex(
                    earnings_state.EarningsStateError,
                    "Could not write earnings state",
                ):
                    store.transaction(
                        lambda value: value["public"].update(
                            {"new": {"symbol": "NEW"}}
                        )
                    )

            self.assertEqual(state_path.read_bytes(), original_bytes)
            self.assertEqual(list(Path(temp_dir).glob(".*.tmp")), [])

            state, _ = store.transaction(
                lambda value: value["private"].update(
                    {"after": {"symbol": "AFTER"}}
                )
            )
            self.assertIn("after", state["private"])

    def test_each_write_uses_a_unique_temporary_name_and_cleans_it(self):
        created_paths = []
        real_mkstemp = earnings_state.tempfile.mkstemp

        def capture_mkstemp(*args, **kwargs):
            descriptor, path = real_mkstemp(*args, **kwargs)
            created_paths.append(path)
            return descriptor, path

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = earnings_state.EarningsStateStore(state_path)

            with patch.object(
                earnings_state.tempfile,
                "mkstemp",
                side_effect=capture_mkstemp,
            ):
                store.transaction(
                    lambda value: value["public"].update({"one": {}})
                )
                store.transaction(
                    lambda value: value["public"].update({"two": {}})
                )

            self.assertEqual(len(created_paths), 2)
            self.assertNotEqual(created_paths[0], created_paths[1])
            self.assertEqual(list(Path(temp_dir).glob(".*.tmp")), [])


class StateValidationBeforeNetworkTests(unittest.TestCase):
    def test_corrupt_public_state_stops_before_any_external_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps({"public": []}),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True), patch.object(
                sys,
                "argv",
                ["earnings_reactions.py", "--post", "--date", "2026-08-06"],
            ), patch.object(
                earnings_reactions,
                "STATE_FILE",
                state_path,
            ), patch.object(
                earnings_reactions,
                "get_completed_reports",
            ) as get_reports, patch.object(
                earnings_reactions,
                "send_discord_message",
            ) as send_discord, patch.object(
                earnings_reactions,
                "send_private_review_with_chart",
            ) as send_private, redirect_stdout(StringIO()):
                with self.assertRaises(
                    earnings_state.EarningsStateValidationError
                ):
                    earnings_reactions.main()

            get_reports.assert_not_called()
            send_discord.assert_not_called()
            send_private.assert_not_called()

    def test_corrupt_signal_state_stops_review_bot_before_discord_resolution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(
                json.dumps({"signal_queue": []}),
                encoding="utf-8",
            )

            with patch.object(
                earnings_reactions,
                "STATE_FILE",
                state_path,
            ), patch.object(
                earnings_reactions,
                "required_env",
            ) as required_env, patch.object(
                earnings_reactions,
                "resolve_webhook_channel_id",
            ) as resolve_channel, patch.object(
                earnings_reactions.urllib.request,
                "urlopen",
                side_effect=AssertionError("network must remain blocked"),
            ):
                with self.assertRaises(
                    earnings_state.EarningsStateValidationError
                ):
                    asyncio.run(
                        earnings_reactions.run_review_button_bot()
                    )

            required_env.assert_not_called()
            resolve_channel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
