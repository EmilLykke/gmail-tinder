#!/usr/bin/env python3
"""Minimal terminal Gmail triage tool backed by `gws`."""

from __future__ import annotations

import argparse
import base64
import curses
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any


GWS_BINARY = "gws"
GCLOUD_BINARY = "gcloud"
DEFAULT_LABEL = "GmailTinderArchive"
REQUIRED_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
AUTH_FIX_COMMAND = (
    "gws auth login --scopes https://www.googleapis.com/auth/gmail.modify"
)
STATE_FILE = Path(__file__).with_name(".gmail_tinder_state.json")


class GwsError(RuntimeError):
    """Raised when a gws command fails."""


@dataclass
class MessagePreview:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    preview_text: str


@dataclass
class MessageBatch:
    messages: list[MessagePreview]
    next_page_token: str | None


@dataclass
class LastAction:
    kind: str
    preview: MessagePreview
    batch_index: int
    batch_number: int
    review_seconds: float


@dataclass
class AppState:
    handled_ids: set[str]
    all_time_reviewed_count: int = 0
    all_time_archived_count: int = 0
    all_time_kept_count: int = 0
    all_time_review_seconds: float = 0.0
    all_time_session_seconds: float = 0.0


@dataclass
class SessionStats:
    session_started_at: float
    current_message_started_at: float
    reviewed_count: int = 0
    archived_count: int = 0
    kept_count: int = 0
    total_review_seconds: float = 0.0


MANUAL_REVIEW_SECONDS = 30.0


def run_gws(args: list[str], *, expect_json: bool = True) -> Any:
    command = [GWS_BINARY, *args]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GwsError(
            "Could not find `gws` on PATH. Install googleworkspace-cli and try again."
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise GwsError(stderr or f"`{' '.join(command)}` failed with exit code {completed.returncode}.")

    if not expect_json:
        return completed.stdout

    payload = completed.stdout.strip()
    if not payload:
        return {}
    return json.loads(payload)


def get_auth_status() -> dict[str, Any]:
    status = run_gws(["auth", "status"])
    if not isinstance(status, dict):
        raise GwsError("Unexpected response from `gws auth status`.")
    return status


def load_state() -> AppState:
    if not STATE_FILE.exists():
        return AppState(handled_ids=set())
    try:
        payload = json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return AppState(handled_ids=set())
    handled_ids = payload.get("handled_ids", [])
    if not isinstance(handled_ids, list):
        return AppState(handled_ids=set())
    return AppState(
        handled_ids={item for item in handled_ids if isinstance(item, str)},
        all_time_reviewed_count=int(payload.get("all_time_reviewed_count", 0)),
        all_time_archived_count=int(payload.get("all_time_archived_count", 0)),
        all_time_kept_count=int(payload.get("all_time_kept_count", 0)),
        all_time_review_seconds=float(payload.get("all_time_review_seconds", 0.0)),
        all_time_session_seconds=float(payload.get("all_time_session_seconds", 0.0)),
    )


def save_state(state: AppState) -> None:
    payload = {
        "handled_ids": sorted(state.handled_ids),
        "all_time_reviewed_count": state.all_time_reviewed_count,
        "all_time_archived_count": state.all_time_archived_count,
        "all_time_kept_count": state.all_time_kept_count,
        "all_time_review_seconds": state.all_time_review_seconds,
        "all_time_session_seconds": state.all_time_session_seconds,
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2))


def mark_handled(state: AppState, message_id: str) -> None:
    state.handled_ids.add(message_id)
    save_state(state)


def unmark_handled(state: AppState, message_id: str) -> None:
    state.handled_ids.discard(message_id)
    save_state(state)


def record_review(state: AppState, action: str, review_seconds: float) -> None:
    state.all_time_reviewed_count += 1
    state.all_time_review_seconds += review_seconds
    if action == "archive":
        state.all_time_archived_count += 1
    elif action == "keep":
        state.all_time_kept_count += 1
    save_state(state)


def undo_recorded_review(state: AppState, action: str, review_seconds: float) -> None:
    state.all_time_reviewed_count = max(0, state.all_time_reviewed_count - 1)
    state.all_time_review_seconds = max(0.0, state.all_time_review_seconds - review_seconds)
    if action == "archive":
        state.all_time_archived_count = max(0, state.all_time_archived_count - 1)
    elif action == "keep":
        state.all_time_kept_count = max(0, state.all_time_kept_count - 1)
    save_state(state)


def record_session_time(state: AppState, session_seconds: float) -> None:
    state.all_time_session_seconds += max(0.0, session_seconds)
    save_state(state)


def format_seconds(seconds: float) -> str:
    rounded = max(0, int(round(seconds)))
    minutes, secs = divmod(rounded, 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def average_review_seconds(stats: SessionStats) -> float:
    if stats.reviewed_count == 0:
        return 0.0
    return stats.total_review_seconds / stats.reviewed_count


def average_persisted_review_seconds(state: AppState) -> float:
    if state.all_time_reviewed_count == 0:
        return 0.0
    return state.all_time_review_seconds / state.all_time_reviewed_count


def estimated_time_saved_seconds(stats: SessionStats) -> float:
    baseline = MANUAL_REVIEW_SECONDS * stats.reviewed_count
    return max(0.0, baseline - stats.total_review_seconds)


def session_elapsed_seconds(stats: SessionStats) -> float:
    return max(0.0, time.monotonic() - stats.session_started_at)


def emails_per_hour(stats: SessionStats) -> float:
    elapsed = session_elapsed_seconds(stats)
    if elapsed <= 0 or stats.reviewed_count == 0:
        return 0.0
    return stats.reviewed_count * 3600.0 / elapsed


def pause_stats_clock(stats: SessionStats, pause_seconds: float) -> None:
    stats.session_started_at += pause_seconds
    stats.current_message_started_at += pause_seconds


def setup_instructions(project_id: str | None) -> str:
    project_flag = f" --project {project_id}" if project_id else ""
    project_hint = project_id or "YOUR_GCP_PROJECT_ID"
    lines = [
        "Gmail access is not ready for this project/account yet.",
        "You need both a Cloud project with the Gmail API enabled and a completed "
        f"`gws` login with Gmail modify scope:\n  {AUTH_FIX_COMMAND}",
        "",
        "Run these setup steps:",
    ]
    if project_id:
        lines.extend(
            [
                f"  gcloud config set project {project_hint}",
                "  (If that project does not exist yet, create it first:",
                f"   gcloud projects create {project_hint} --name=\"Gmail Tinder\")",
            ]
        )
    else:
        lines.extend(
            [
                "  gcloud auth login",
                "  gcloud projects create YOUR_GCP_PROJECT_ID --name=\"Gmail Tinder\"",
                "    (use a new globally unique project ID, or skip this line if you already have a project)",
                f"  gcloud config set project {project_hint}",
            ]
        )
    lines.extend(
        [
            f"  gcloud services enable gmail.googleapis.com{project_flag if project_id else ' --project YOUR_GCP_PROJECT_ID'}",
            f"  {AUTH_FIX_COMMAND}",
            "",
            "If Gmail still says insufficient permissions, run:",
            "  gws auth logout",
            f"  {AUTH_FIX_COMMAND}",
        ]
    )
    return "\n".join(lines)


def explain_gmail_error(raw_error: str, project_id: str | None) -> str:
    guidance = setup_instructions(project_id)
    if "insufficientPermissions" in raw_error or "insufficient authentication scopes" in raw_error.lower():
        return (
            "The current login is still not accepted for Gmail message changes.\n\n"
            f"{guidance}"
        )
    if "SERVICE_DISABLED" in raw_error or "accessNotConfigured" in raw_error:
        return (
            "The Gmail API is not enabled for the configured Google Cloud project.\n\n"
            f"{guidance}"
        )
    return f"{raw_error}\n\n{guidance}"


def ensure_auth_scope(status: dict[str, Any]) -> None:
    scopes = set(status.get("scopes", []))
    if REQUIRED_SCOPE not in scopes:
        raise GwsError(
            "Your current gws login is missing Gmail modify access.\n"
            f"Run:\n  {AUTH_FIX_COMMAND}"
        )


def list_labels(project_id: str | None = None) -> dict[str, Any]:
    try:
        return run_gws(
            [
                "gmail",
                "users",
                "labels",
                "list",
                "--params",
                json.dumps({"userId": "me"}),
                "--format",
                "json",
            ]
        )
    except GwsError as exc:
        raise GwsError(explain_gmail_error(str(exc), project_id)) from exc


def ensure_label(label_name: str, project_id: str | None = None) -> str:
    response = list_labels(project_id)

    for label in response.get("labels", []):
        if label.get("name") == label_name:
            return label["id"]

    try:
        created = run_gws(
            [
                "gmail",
                "users",
                "labels",
                "create",
                "--params",
                json.dumps({"userId": "me"}),
                "--json",
                json.dumps(
                    {
                        "name": label_name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    }
                ),
                "--format",
                "json",
            ]
        )
    except GwsError as exc:
        raise GwsError(explain_gmail_error(str(exc), project_id)) from exc
    return created["id"]


def list_message_ids(
    query: str,
    max_results: int,
    page_token: str | None = None,
) -> tuple[list[dict[str, str]], str | None]:
    params = {
        "userId": "me",
        "labelIds": ["INBOX"],
        "maxResults": max_results,
    }
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token

    response = run_gws(
        [
            "gmail",
            "users",
            "messages",
            "list",
            "--params",
            json.dumps(params),
            "--format",
            "json",
        ]
    )
    return response.get("messages", []), response.get("nextPageToken")


def fetch_message_preview(message_id: str) -> MessagePreview:
    params = {
        "userId": "me",
        "id": message_id,
        "format": "full",
    }
    response = run_gws(
        [
            "gmail",
            "users",
            "messages",
            "get",
            "--params",
            json.dumps(params),
            "--format",
            "json",
        ]
    )

    headers = {
        item["name"].lower(): item["value"]
        for item in response.get("payload", {}).get("headers", [])
        if "name" in item and "value" in item
    }
    return MessagePreview(
        message_id=response["id"],
        thread_id=response["threadId"],
        sender=headers.get("from", "(unknown sender)"),
        subject=headers.get("subject", "(no subject)"),
        date=normalize_date(headers.get("date", "")),
        preview_text=extract_preview_text(response),
    )


def archive_message(message_id: str, archive_label_id: str) -> None:
    run_gws(
        [
            "gmail",
            "users",
            "messages",
            "modify",
            "--params",
            json.dumps({"userId": "me", "id": message_id}),
            "--json",
            json.dumps(
                {
                    "addLabelIds": [archive_label_id],
                    "removeLabelIds": ["INBOX"],
                }
            ),
            "--format",
            "json",
        ]
    )


def undo_archive_message(message_id: str, archive_label_id: str) -> None:
    run_gws(
        [
            "gmail",
            "users",
            "messages",
            "modify",
            "--params",
            json.dumps({"userId": "me", "id": message_id}),
            "--json",
            json.dumps(
                {
                    "addLabelIds": ["INBOX"],
                    "removeLabelIds": [archive_label_id],
                }
            ),
            "--format",
            "json",
        ]
    )


def normalize_date(raw_date: str) -> str:
    if not raw_date:
        return "(no date)"
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError, IndexError):
        return raw_date

    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M")
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def clean_preview_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    text = text.replace("\r", "\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def decode_body_data(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    decoded = base64.urlsafe_b64decode(data + padding)
    return decoded.decode("utf-8", errors="replace")


def collect_text_parts(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    plain_texts: list[str] = []
    html_texts: list[str] = []
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")
    if body_data:
        decoded = decode_body_data(body_data)
        if mime_type == "text/plain":
            plain_texts.append(clean_preview_text(decoded))
        elif mime_type == "text/html":
            html_texts.append(clean_preview_text(decoded))

    for part in payload.get("parts", []) or []:
        child_plain, child_html = collect_text_parts(part)
        plain_texts.extend(child_plain)
        html_texts.extend(child_html)
    return plain_texts, html_texts


def extract_preview_text(message: dict[str, Any]) -> str:
    payload = message.get("payload", {})
    plain_texts, html_texts = collect_text_parts(payload)
    plain_texts = [text for text in plain_texts if text]
    html_texts = [text for text in html_texts if text]
    if plain_texts:
        return max(plain_texts, key=len)
    if html_texts:
        return max(html_texts, key=len)
    return clean_preview_text(message.get("snippet", "")) or "(empty)"


def init_colors() -> dict[str, int]:
    colors = {
        "base": curses.A_NORMAL,
        "header": curses.A_REVERSE | curses.A_BOLD,
        "label": curses.A_BOLD,
        "muted": curses.A_DIM,
        "border": curses.A_DIM,
        "archive": curses.A_BOLD,
        "keep": curses.A_BOLD,
        "quit": curses.A_BOLD,
        "status": curses.A_REVERSE,
    }

    if not curses.has_colors():
        return colors

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(2, curses.COLOR_CYAN, -1)
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_RED)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)

    colors.update(
        {
            "header": curses.color_pair(1) | curses.A_BOLD,
            "label": curses.color_pair(2) | curses.A_BOLD,
            "border": curses.color_pair(2),
            "archive": curses.color_pair(3) | curses.A_BOLD,
            "keep": curses.color_pair(4) | curses.A_BOLD,
            "quit": curses.color_pair(5) | curses.A_BOLD,
            "status": curses.color_pair(6) | curses.A_BOLD,
        }
    )
    return colors


def draw_box(
    stdscr: curses.window,
    top: int,
    left: int,
    height: int,
    width: int,
    title: str,
    colors: dict[str, int],
) -> None:
    right = left + width - 1
    bottom = top + height - 1
    if height < 3 or width < 4:
        return

    stdscr.addch(top, left, curses.ACS_ULCORNER, colors["border"])
    stdscr.hline(top, left + 1, curses.ACS_HLINE, width - 2, colors["border"])
    stdscr.addch(top, right, curses.ACS_URCORNER, colors["border"])
    stdscr.vline(top + 1, left, curses.ACS_VLINE, height - 2, colors["border"])
    stdscr.vline(top + 1, right, curses.ACS_VLINE, height - 2, colors["border"])
    stdscr.addch(bottom, left, curses.ACS_LLCORNER, colors["border"])
    stdscr.hline(bottom, left + 1, curses.ACS_HLINE, width - 2, colors["border"])
    stdscr.addch(bottom, right, curses.ACS_LRCORNER, colors["border"])

    if title and width > len(title) + 4:
        stdscr.addnstr(top, left + 2, f" {title} ", width - 4, colors["label"])


def add_wrapped_lines(
    stdscr: curses.window,
    top: int,
    left: int,
    width: int,
    lines: list[str],
    max_lines: int,
    attr: int,
) -> int:
    row = top
    for line in lines:
        wrapped = textwrap.wrap(line, width=width) or [""]
        for chunk in wrapped:
            if row >= top + max_lines:
                return row
            stdscr.addnstr(row, left, chunk, width, attr)
            row += 1
    return row


def safe_addnstr(
    stdscr: curses.window,
    y: int,
    x: int,
    text: str,
    width: int,
    attr: int = 0,
) -> None:
    if width <= 0:
        return
    height, screen_width = stdscr.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= screen_width:
        return
    max_width = min(width, screen_width - x)
    if max_width <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, max_width, attr)
    except curses.error:
        pass


def draw_screen(
    stdscr: curses.window,
    preview: MessagePreview,
    index: int,
    total: int,
    label_name: str,
    status_line: str,
    stats: SessionStats,
    colors: dict[str, int],
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 18 or width < 76:
        message = "Make the terminal larger. Minimum size: 76x18."
        safe_addnstr(stdscr, 0, 0, message, width - 1, colors["quit"])
        stdscr.refresh()
        return

    stdscr.bkgd(" ", colors["base"])
    header = f" gmail-tinder  {index}/{total} "
    progress = f" {total - index} left in batch "
    safe_addnstr(stdscr, 0, 0, header.ljust(max(0, width - len(progress))), width - 1, colors["header"])
    safe_addnstr(stdscr, 0, max(0, width - len(progress)), progress, len(progress), colors["header"])

    card_top = 2
    card_height = 8
    card_left = 2
    card_width = width - 4
    draw_box(stdscr, card_top, card_left, card_height, card_width, "Message", colors)

    content_left = card_left + 2
    content_width = card_width - 4
    safe_addnstr(stdscr, card_top + 1, content_left, "FROM", content_width, colors["label"])
    safe_addnstr(stdscr, card_top + 1, content_left + 8, preview.sender, content_width - 8, curses.A_BOLD)
    safe_addnstr(stdscr, card_top + 2, content_left, "SUBJECT", content_width, colors["label"])
    safe_addnstr(stdscr, card_top + 2, content_left + 8, preview.subject, content_width - 8, curses.A_BOLD)
    safe_addnstr(stdscr, card_top + 3, content_left, "DATE", content_width, colors["label"])
    safe_addnstr(stdscr, card_top + 3, content_left + 8, preview.date, content_width - 8, colors["muted"])
    safe_addnstr(stdscr, card_top + 4, content_left, "PACE", content_width, colors["label"])
    pace_text = (
        f"This email: {format_seconds(time.monotonic() - stats.current_message_started_at)}"
        f"  Avg: {format_seconds(average_review_seconds(stats))}"
        f"  Done: {stats.reviewed_count}"
    )
    safe_addnstr(stdscr, card_top + 4, content_left + 8, pace_text, content_width - 8, colors["muted"])
    safe_addnstr(stdscr, card_top + 5, content_left, "Decision", content_width, colors["label"])
    decision_text = f"Left archives to {label_name}. Right keeps it in the inbox."
    add_wrapped_lines(
        stdscr,
        card_top + 5,
        content_left + 10,
        content_width - 10,
        [decision_text],
        2,
        curses.A_NORMAL,
    )

    preview_top = card_top + card_height + 1
    footer_height = 5
    preview_height = height - preview_top - footer_height - 1
    draw_box(stdscr, preview_top, card_left, preview_height, card_width, "Email Preview", colors)
    preview_lines = [line for line in preview.preview_text.splitlines() if line.strip()] or ["(empty)"]
    add_wrapped_lines(
        stdscr,
        preview_top + 1,
        content_left,
        content_width,
        preview_lines,
        preview_height - 2,
        curses.A_NORMAL,
    )

    footer_top = height - footer_height
    draw_box(stdscr, footer_top, card_left, footer_height, card_width, "Actions", colors)
    safe_addnstr(stdscr, footer_top + 1, content_left, "[<-] Archive", content_width, colors["archive"])
    safe_addnstr(stdscr, footer_top + 1, content_left + 18, f"move out of inbox into {label_name}", content_width - 18, curses.A_NORMAL)
    safe_addnstr(stdscr, footer_top + 2, content_left, "[->] Keep", content_width, colors["keep"])
    safe_addnstr(stdscr, footer_top + 2, content_left + 18, "leave in inbox and continue", content_width - 18, curses.A_NORMAL)
    safe_addnstr(stdscr, footer_top + 2, max(content_left + 36, width - 30), "[S] Stats", 12, colors["label"])
    safe_addnstr(stdscr, footer_top + 3, content_left, "[U] Undo", content_width, colors["label"])
    safe_addnstr(
        stdscr,
        footer_top + 3,
        content_left + 18,
        f"reverse the last swipe  |  saved est. {format_seconds(estimated_time_saved_seconds(stats))}",
        content_width - 18,
        curses.A_NORMAL,
    )
    quit_col = max(content_left, width - 16)
    safe_addnstr(stdscr, footer_top + 1, quit_col, "[Q] Quit", width - quit_col - 2, colors["quit"])

    status_message = status_line or "Ready"
    safe_addnstr(stdscr, height - 1, 0, f" {status_message} ".ljust(width), width - 1, colors["status"])
    stdscr.refresh()


def draw_loading_screen(stdscr: curses.window, message: str, colors: dict[str, int]) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 8 or width < 40:
        safe_addnstr(stdscr, 0, 0, message, width - 1, colors["status"])
        stdscr.refresh()
        return

    box_width = min(max(len(message) + 8, 44), width - 4)
    box_height = 7
    top = max(1, (height - box_height) // 2)
    left = max(2, (width - box_width) // 2)
    draw_box(stdscr, top, left, box_height, box_width, "Loading", colors)
    safe_addnstr(stdscr, top + 2, left + 3, message, box_width - 6, curses.A_BOLD)
    safe_addnstr(stdscr, top + 4, left + 3, "Fetching the next emails from Gmail...", box_width - 6, colors["muted"])
    stdscr.refresh()


def show_dashboard_screen(
    stdscr: curses.window,
    first_batch_size: int,
    state: AppState,
    stats: SessionStats,
    colors: dict[str, int],
) -> str:
    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 18 or width < 76:
            safe_addnstr(stdscr, 0, 0, "Make the terminal larger. Minimum size: 76x18.", width - 1, colors["quit"])
            stdscr.refresh()
        else:
            stdscr.bkgd(" ", colors["base"])
            safe_addnstr(stdscr, 0, 0, " gmail-tinder dashboard ".ljust(width), width - 1, colors["header"])
            draw_box(stdscr, 2, 2, 9, width - 4, "Overview", colors)
            draw_box(stdscr, 12, 2, 8, width - 4, "Controls", colors)

            safe_addnstr(stdscr, 4, 5, f"Ready in first batch: {first_batch_size}", width - 10, curses.A_BOLD)
            safe_addnstr(stdscr, 5, 5, f"Saved progress across runs: {len(state.handled_ids)} emails", width - 10, curses.A_NORMAL)
            safe_addnstr(stdscr, 6, 5, f"All-time reviewed: {state.all_time_reviewed_count}", width - 10, curses.A_NORMAL)
            safe_addnstr(stdscr, 7, 5, f"All-time average: {format_seconds(average_persisted_review_seconds(state))}", width - 10, curses.A_NORMAL)
            safe_addnstr(stdscr, 8, 5, f"All-time session time: {format_seconds(state.all_time_session_seconds)}", width - 10, curses.A_NORMAL)
            safe_addnstr(stdscr, 9, 5, f"Estimated time saved this session: {format_seconds(estimated_time_saved_seconds(stats))}", width - 10, curses.A_NORMAL)

            safe_addnstr(stdscr, 14, 5, "[<-] or [H]  Start reviewing", width - 10, colors["archive"])
            safe_addnstr(stdscr, 15, 5, "[->] or [L]  Start reviewing", width - 10, colors["keep"])
            safe_addnstr(stdscr, 16, 5, "[S] Stats   [Q] Quit", width - 10, colors["label"])
            safe_addnstr(stdscr, height - 1, 0, " Open the inbox flow with arrows or H/L. ".ljust(width), width - 1, colors["status"])
            stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return "quit"
        if key in (ord("s"), ord("S")):
            show_stats_screen(stdscr, stats, state, colors)
            continue
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("H"), ord("l"), ord("L")):
            return "start"


def show_stats_screen(
    stdscr: curses.window,
    stats: SessionStats,
    state: AppState,
    colors: dict[str, int],
) -> None:
    opened_at = time.monotonic()
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    lines = [
        "Session",
        f"Reviewed this session: {stats.reviewed_count}",
        f"Archived: {stats.archived_count}",
        f"Kept: {stats.kept_count}",
        f"Average review time: {format_seconds(average_review_seconds(stats))}",
        f"Active review time: {format_seconds(stats.total_review_seconds)}",
        f"Session time: {format_seconds(session_elapsed_seconds(stats))}",
        f"Estimated time saved: {format_seconds(estimated_time_saved_seconds(stats))}",
        f"Pace: {emails_per_hour(stats):.1f} emails/hour",
        "",
        "All time",
        f"Reviewed across sessions: {state.all_time_reviewed_count}",
        f"Archived across sessions: {state.all_time_archived_count}",
        f"Kept across sessions: {state.all_time_kept_count}",
        f"Average review time overall: {format_seconds(average_persisted_review_seconds(state))}",
        f"Active review time overall: {format_seconds(state.all_time_review_seconds)}",
        f"Session time overall: {format_seconds(state.all_time_session_seconds)}",
        "",
        "Press any key to return. Timers are paused on this screen.",
    ]
    box_height = min(max(len(lines) + 4, 12), height - 4)
    top = max(1, (height - box_height) // 2)
    left = 2
    box_width = width - 4
    draw_box(stdscr, top, left, box_height, box_width, "Session Stats", colors)
    max_rows = box_height - 2
    for offset, line in enumerate(lines[:max_rows], start=1):
        safe_addnstr(stdscr, top + offset, left + 3, line, box_width - 6, curses.A_NORMAL)
    stdscr.refresh()
    stdscr.timeout(-1)
    stdscr.getch()
    pause_stats_clock(stats, time.monotonic() - opened_at)
    stdscr.timeout(250)


def render_done(
    stdscr: curses.window,
    archived: int,
    kept: int,
    has_more: bool,
    stats: SessionStats,
    colors: dict[str, int],
) -> str:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    draw_box(stdscr, 2, 2, min(12, height - 4), width - 4, "Batch Complete", colors)
    lines = [
        "You finished this batch.",
        "",
        f"Archived in this session: {archived}",
        f"Kept in this session: {kept}",
        f"Average review time: {format_seconds(average_review_seconds(stats))}",
        f"Session time: {format_seconds(session_elapsed_seconds(stats))}",
        f"Estimated time saved: {format_seconds(estimated_time_saved_seconds(stats))}",
        "",
    ]
    if has_more:
        lines.append("Press [<-]/[H] or [->]/[L] for next batch, [S] stats, [U] undo, or [Q] quit.")
    else:
        lines.append("No more messages are available right now.")
        lines.append("Press [S] for stats, [U] undo, or [Q] quit.")

    for row, line in enumerate(lines, start=4):
        if row >= height - 1:
            break
        safe_addnstr(stdscr, row, 5, line, width - 10, curses.A_BOLD if row == 4 else curses.A_NORMAL)
    stdscr.refresh()
    while True:
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return "quit"
        if has_more and key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("H"), ord("l"), ord("L")):
            return "next"
        if key in (ord("s"), ord("S")):
            return "stats"
        if key in (ord("u"), ord("U")):
            return "undo"


def run_tui(
    stdscr: curses.window,
    first_batch: MessageBatch,
    query: str,
    max_results: int,
    archive_label_id: str,
    label_name: str,
    state: AppState,
) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(250)
    colors = init_colors()

    archived = 0
    kept = 0
    status_line = "Use the left and right arrow keys."
    current_batch = first_batch
    last_action: LastAction | None = None
    batch_number = 0
    now = time.monotonic()
    stats = SessionStats(
        session_started_at=now,
        current_message_started_at=now,
    )
    session_recorded = False

    def finalize_session() -> None:
        nonlocal session_recorded
        if session_recorded:
            return
        record_session_time(state, session_elapsed_seconds(stats))
        session_recorded = True

    dashboard_action = show_dashboard_screen(
        stdscr,
        len(first_batch.messages),
        state,
        stats,
        colors,
    )
    if dashboard_action == "quit":
        finalize_session()
        return
    stats.current_message_started_at = time.monotonic()

    while True:
        messages = current_batch.messages
        next_page_token = current_batch.next_page_token
        batch_number += 1

        if not messages:
            render_done(stdscr, archived, kept, has_more=False, stats=stats, colors=colors)
            finalize_session()
            return

        index = 0
        while index < len(messages):
            preview = messages[index]
            if index == 0 or stats.current_message_started_at == 0:
                stats.current_message_started_at = time.monotonic()
            draw_screen(stdscr, preview, index + 1, len(messages), label_name, status_line, stats, colors)
            key = stdscr.getch()

            if key == -1:
                continue
            if key in (ord("q"), ord("Q")):
                finalize_session()
                return
            if key in (ord("s"), ord("S")):
                show_stats_screen(stdscr, stats, state, colors)
                status_line = "Back to inbox."
                continue
            if key in (ord("u"), ord("U")):
                if last_action and last_action.batch_number == batch_number and index == last_action.batch_index + 1:
                    if last_action.kind == "archive":
                        undo_archive_message(last_action.preview.message_id, archive_label_id)
                        archived -= 1
                        stats.archived_count -= 1
                        status_line = f"Undo archive: {last_action.preview.subject}"
                    else:
                        kept -= 1
                        stats.kept_count -= 1
                        status_line = f"Undo keep: {last_action.preview.subject}"
                    stats.reviewed_count -= 1
                    stats.total_review_seconds = max(
                        0.0,
                        stats.total_review_seconds - last_action.review_seconds,
                    )
                    unmark_handled(state, last_action.preview.message_id)
                    undo_recorded_review(state, last_action.kind, last_action.review_seconds)
                    index = last_action.batch_index
                    stats.current_message_started_at = time.monotonic()
                    last_action = None
                else:
                    status_line = "Nothing to undo."
                continue
            if key in (curses.KEY_RIGHT, ord("l"), ord("L")):
                review_seconds = time.monotonic() - stats.current_message_started_at
                stats.total_review_seconds += review_seconds
                stats.reviewed_count += 1
                stats.kept_count += 1
                kept += 1
                status_line = (
                    f"Kept in inbox: {preview.subject} "
                    f"({format_seconds(review_seconds)})"
                )
                mark_handled(state, preview.message_id)
                record_review(state, "keep", review_seconds)
                last_action = LastAction(
                    kind="keep",
                    preview=preview,
                    batch_index=index,
                    batch_number=batch_number,
                    review_seconds=review_seconds,
                )
                index += 1
                stats.current_message_started_at = time.monotonic()
                continue
            if key in (curses.KEY_LEFT, ord("h"), ord("H")):
                review_seconds = time.monotonic() - stats.current_message_started_at
                stats.total_review_seconds += review_seconds
                stats.reviewed_count += 1
                stats.archived_count += 1
                archive_message(preview.message_id, archive_label_id)
                archived += 1
                status_line = (
                    f"Archived to {label_name}: {preview.subject} "
                    f"({format_seconds(review_seconds)})"
                )
                mark_handled(state, preview.message_id)
                record_review(state, "archive", review_seconds)
                last_action = LastAction(
                    kind="archive",
                    preview=preview,
                    batch_index=index,
                    batch_number=batch_number,
                    review_seconds=review_seconds,
                )
                index += 1
                stats.current_message_started_at = time.monotonic()

        while True:
            action = render_done(
                stdscr,
                archived,
                kept,
                has_more=bool(next_page_token),
                stats=stats,
                colors=colors,
            )
            if action == "stats":
                show_stats_screen(stdscr, stats, state, colors)
                status_line = "Back to batch summary."
                continue
            break
        if action == "undo":
            if last_action and last_action.batch_number == batch_number and len(messages) == last_action.batch_index + 1:
                if last_action.kind == "archive":
                    undo_archive_message(last_action.preview.message_id, archive_label_id)
                    archived -= 1
                    stats.archived_count -= 1
                    status_line = f"Undo archive: {last_action.preview.subject}"
                else:
                    kept -= 1
                    stats.kept_count -= 1
                    status_line = f"Undo keep: {last_action.preview.subject}"
                stats.reviewed_count -= 1
                stats.total_review_seconds = max(
                    0.0,
                    stats.total_review_seconds - last_action.review_seconds,
                )
                unmark_handled(state, last_action.preview.message_id)
                undo_recorded_review(state, last_action.kind, last_action.review_seconds)
                index = last_action.batch_index
                stats.current_message_started_at = time.monotonic()
                last_action = None
                continue
            status_line = "Nothing to undo."
            continue
        if action == "quit" or not next_page_token:
            finalize_session()
            return
        draw_loading_screen(stdscr, "Loading next batch", colors)
        current_batch = load_previews(
            query=query,
            max_results=max_results,
            state=state,
            page_token=next_page_token,
        )
        status_line = "Loaded next batch."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tiny Gmail triage tool using googleworkspace-cli."
    )
    parser.add_argument(
        "--label-name",
        default=DEFAULT_LABEL,
        help=f"Gmail label to add when archiving from inbox. Default: {DEFAULT_LABEL}",
    )
    parser.add_argument(
        "--query",
        default="",
        help="Optional Gmail search query, applied in addition to labelIds=INBOX.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=25,
        help="Number of inbox messages to preload for this session.",
    )
    parser.add_argument(
        "--reset-progress",
        action="store_true",
        help="Forget saved progress and start from the top again.",
    )
    return parser


def load_previews(
    query: str,
    max_results: int,
    state: AppState,
    page_token: str | None = None,
) -> MessageBatch:
    collected: list[dict[str, str]] = []
    next_page_token = page_token
    seen_page_tokens: set[str | None] = set()

    while len(collected) < max_results:
        if next_page_token in seen_page_tokens:
            break
        seen_page_tokens.add(next_page_token)
        message_refs, next_page_token = list_message_ids(
            query=query,
            max_results=max_results,
            page_token=next_page_token,
        )
        if not message_refs:
            break

        unseen_refs = [
            item for item in message_refs
            if item.get("id") and item["id"] not in state.handled_ids
        ]
        collected.extend(unseen_refs[: max_results - len(collected)])

        if not next_page_token:
            break

    return MessageBatch(
        messages=[fetch_message_preview(item["id"]) for item in collected],
        next_page_token=next_page_token,
    )


def main() -> int:
    if shutil.which(GWS_BINARY) is None:
        print("`gws` is not installed or not on PATH.", file=sys.stderr)
        return 1

    parser = build_parser()
    args = parser.parse_args()

    if args.max_results <= 0:
        print("--max-results must be greater than 0.", file=sys.stderr)
        return 1

    try:
        state = load_state()
        if args.reset_progress:
            state.handled_ids.clear()
            state.all_time_reviewed_count = 0
            state.all_time_archived_count = 0
            state.all_time_kept_count = 0
            state.all_time_review_seconds = 0.0
            save_state(state)
        status = get_auth_status()
        ensure_auth_scope(status)
        archive_label_id = ensure_label(args.label_name, status.get("project_id"))
        first_batch = load_previews(args.query, args.max_results, state)
        if not first_batch.messages:
            print("No inbox messages matched this batch.", file=sys.stderr)
            return 0
        curses.wrapper(
            run_tui,
            first_batch,
            args.query,
            args.max_results,
            archive_label_id,
            args.label_name,
            state,
        )
        return 0
    except GwsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
