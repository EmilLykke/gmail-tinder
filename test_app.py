import json
import os
import unittest

import app

# Default keeps the repo free of personal GCP project IDs. To assert against your
# own project when running tests locally: GMAIL_TINDER_TEST_GCP_PROJECT_ID=your-id
_TEST_GCP_PROJECT_ID = os.environ.get(
    "GMAIL_TINDER_TEST_GCP_PROJECT_ID", "example-gcp-project-id"
)


class NormalizeDateTests(unittest.TestCase):
    def test_empty_date(self) -> None:
        self.assertEqual(app.normalize_date(""), "(no date)")

    def test_unparseable_date_falls_back_to_original(self) -> None:
        self.assertEqual(app.normalize_date("not a date"), "not a date")


class ParserTests(unittest.TestCase):
    def test_default_label_name(self) -> None:
        parser = app.build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.label_name, app.DEFAULT_LABEL)
        self.assertEqual(args.max_results, 25)


class SetupInstructionsTests(unittest.TestCase):
    def test_setup_instructions_include_project_specific_enable_command(self) -> None:
        project_id = _TEST_GCP_PROJECT_ID
        instructions = app.setup_instructions(project_id)
        self.assertIn(
            f"gcloud services enable gmail.googleapis.com --project {project_id}",
            instructions,
        )
        self.assertIn(f"gcloud projects create {project_id}", instructions)

    def test_setup_instructions_without_project_includes_create_and_auth_login(self) -> None:
        instructions = app.setup_instructions(None)
        self.assertIn("gcloud auth login", instructions)
        self.assertIn("gcloud projects create YOUR_GCP_PROJECT_ID", instructions)
        self.assertIn(
            "gcloud services enable gmail.googleapis.com --project YOUR_GCP_PROJECT_ID",
            instructions,
        )


class MessageBatchTests(unittest.TestCase):
    def test_load_previews_preserves_next_page_token(self) -> None:
        original_list_message_ids = app.list_message_ids
        original_fetch_message_preview = app.fetch_message_preview
        try:
            app.list_message_ids = lambda **kwargs: ([{"id": "abc"}], "next-token")
            app.fetch_message_preview = lambda message_id: app.MessagePreview(
                message_id=message_id,
                thread_id="thread",
                sender="sender",
                subject="subject",
                date="date",
                preview_text="preview",
            )
            batch = app.load_previews("", 1, app.AppState(handled_ids=set()))
        finally:
            app.list_message_ids = original_list_message_ids
            app.fetch_message_preview = original_fetch_message_preview

        self.assertEqual(len(batch.messages), 1)
        self.assertEqual(batch.next_page_token, "next-token")

    def test_load_previews_skips_handled_ids(self) -> None:
        original_list_message_ids = app.list_message_ids
        original_fetch_message_preview = app.fetch_message_preview
        try:
            app.list_message_ids = lambda **kwargs: ([{"id": "seen"}, {"id": "new"}], None)
            app.fetch_message_preview = lambda message_id: app.MessagePreview(
                message_id=message_id,
                thread_id="thread",
                sender="sender",
                subject=message_id,
                date="date",
                preview_text="preview",
            )
            batch = app.load_previews("", 25, app.AppState(handled_ids={"seen"}))
        finally:
            app.list_message_ids = original_list_message_ids
            app.fetch_message_preview = original_fetch_message_preview

        self.assertEqual([message.message_id for message in batch.messages], ["new"])


class UndoTests(unittest.TestCase):
    def test_undo_archive_message_uses_inverse_label_update(self) -> None:
        original_run_gws = app.run_gws
        calls = []
        try:
            app.run_gws = lambda args, expect_json=True: calls.append(args) or {}
            app.undo_archive_message("msg-1", "Label_123")
        finally:
            app.run_gws = original_run_gws

        self.assertEqual(calls[0][0:4], ["gmail", "users", "messages", "modify"])
        payload = json.loads(calls[0][7])
        self.assertEqual(payload["addLabelIds"], ["INBOX"])
        self.assertEqual(payload["removeLabelIds"], ["Label_123"])


class StatsTests(unittest.TestCase):
    def test_average_review_seconds(self) -> None:
        stats = app.SessionStats(
            session_started_at=0.0,
            current_message_started_at=0.0,
            reviewed_count=4,
            total_review_seconds=40.0,
        )
        self.assertEqual(app.average_review_seconds(stats), 10.0)

    def test_estimated_time_saved_seconds_never_negative(self) -> None:
        stats = app.SessionStats(
            session_started_at=0.0,
            current_message_started_at=0.0,
            reviewed_count=1,
            total_review_seconds=100.0,
        )
        self.assertEqual(app.estimated_time_saved_seconds(stats), 0.0)

    def test_pause_stats_clock_shifts_both_timers(self) -> None:
        stats = app.SessionStats(
            session_started_at=10.0,
            current_message_started_at=20.0,
        )
        app.pause_stats_clock(stats, 5.0)
        self.assertEqual(stats.session_started_at, 15.0)
        self.assertEqual(stats.current_message_started_at, 25.0)

    def test_average_persisted_review_seconds(self) -> None:
        state = app.AppState(
            handled_ids=set(),
            all_time_reviewed_count=3,
            all_time_review_seconds=45.0,
        )
        self.assertEqual(app.average_persisted_review_seconds(state), 15.0)

    def test_record_and_undo_review_updates_persisted_stats(self) -> None:
        state = app.AppState(handled_ids=set())
        original_save_state = app.save_state
        try:
            app.save_state = lambda _state: None
            app.record_review(state, "archive", 12.0)
            app.undo_recorded_review(state, "archive", 12.0)
        finally:
            app.save_state = original_save_state

        self.assertEqual(state.all_time_reviewed_count, 0)
        self.assertEqual(state.all_time_archived_count, 0)
        self.assertEqual(state.all_time_review_seconds, 0.0)

    def test_record_session_time_updates_persisted_total(self) -> None:
        state = app.AppState(handled_ids=set())
        original_save_state = app.save_state
        try:
            app.save_state = lambda _state: None
            app.record_session_time(state, 120.0)
        finally:
            app.save_state = original_save_state

        self.assertEqual(state.all_time_session_seconds, 120.0)


if __name__ == "__main__":
    unittest.main()
