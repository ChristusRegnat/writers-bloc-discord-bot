"""
discord_scheduled_scanner.py

GitHub Actions scheduled scanner for Writers Bloc 3MM manuscript channels.

This is NOT a persistent Gateway bot. It is a REST scanner designed for GitHub Actions:
- Runs on schedule or workflow_dispatch.
- Fetches recent Discord messages from the 3MM Writer Manuscripts category.
- Counts .docx/.md/.markdown/.txt/.rtf attachments.
- Skips ignored/cleared messages and exact duplicate file hashes.
- Updates writers_bloc_3mm_state.json.
- Edits or creates each writer dashboard once per changed writer.

Required environment variables:
- DISCORD_BOT_TOKEN
- DISCORD_GUILD_ID

Optional environment variables:
- DISCORD_CATEGORY_NAME, default "3MM Writer Manuscripts"
- STATE_FILE, default "writers_bloc_3mm_state.json"
- GOALS_FILE, default "writer_weekly_goals.json"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from word_counter import count_words, count_words_from_file_bytes, is_supported_filename

API_BASE = "https://discord.com/api/v10"
BOT_VERSION = "2026-06-26-github-actions-scheduled-scanner-v1"
DEFAULT_CATEGORY_NAME = "3MM Writer Manuscripts"
DEFAULT_WEEKLY_GOAL = 1500
MIN_PASTED_WORDS_TO_COUNT = 50
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
REPORT_FILE = Path("gha_scan_report.md")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "").strip()
CATEGORY_NAME = os.environ.get("DISCORD_CATEGORY_NAME", DEFAULT_CATEGORY_NAME).strip() or DEFAULT_CATEGORY_NAME
STATE_FILE = Path(os.environ.get("STATE_FILE", "writers_bloc_3mm_state.json"))
GOALS_FILE = Path(os.environ.get("GOALS_FILE", "writer_weekly_goals.json"))
DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"}

SESSION = requests.Session()
REPORT_LINES: List[str] = []


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    REPORT_LINES.append(line)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_week_key() -> str:
    now = datetime.now(timezone.utc).isocalendar()
    return f"{now.year}-W{now.week:02d}"


def normalize_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def display_from_channel_name(channel_name: str) -> str:
    return re.sub(r"[-_]+", " ", channel_name).strip().title() or channel_name


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json_file(path: Path, data: Any) -> None:
    if DRY_RUN:
        log(f"DRY_RUN: not writing {path}")
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_state() -> dict:
    state = load_json_file(STATE_FILE, {"version": BOT_VERSION, "guilds": {}})
    state.setdefault("version", BOT_VERSION)
    state.setdefault("guilds", {})
    return state


def save_state(state: dict) -> None:
    state["version"] = BOT_VERSION
    save_json_file(STATE_FILE, state)


def load_goals() -> List[dict]:
    data = load_json_file(GOALS_FILE, {"goals": []})
    if isinstance(data, list):
        return data
    return data.get("goals", []) if isinstance(data, dict) else []


def guild_state(state: dict) -> dict:
    gs = state.setdefault("guilds", {}).setdefault(str(GUILD_ID), {})
    gs.setdefault("writers", {})
    gs.setdefault("processed_messages", {})
    gs.setdefault("ignored_messages", {})
    gs.setdefault("cleared_messages", {})
    gs.setdefault("duplicate_messages", {})
    gs.setdefault("settings", {"default_weekly_goal": DEFAULT_WEEKLY_GOAL})
    return gs


def request_json(method: str, path: str, **kwargs: Any) -> Any:
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing")
    url = path if path.startswith("http") else API_BASE + path
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bot {TOKEN}"
    if "json" in kwargs:
        headers.setdefault("Content-Type", "application/json")

    attempt = 0
    while True:
        attempt += 1
        resp = SESSION.request(method, url, headers=headers, timeout=60, **kwargs)
        if resp.status_code == 429:
            try:
                payload = resp.json()
                retry_after = float(payload.get("retry_after", 1.0))
            except Exception:
                retry_after = 2.0
            sleep_for = retry_after + 0.25
            log(f"Discord rate limit on {method} {path}. Sleeping {sleep_for:.2f}s before retry.")
            time.sleep(sleep_for)
            continue
        if resp.status_code >= 400:
            text = resp.text[:1000]
            raise RuntimeError(f"Discord API error {resp.status_code} for {method} {path}: {text}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()


def request_bytes(url: str) -> bytes:
    attempt = 0
    while True:
        attempt += 1
        resp = SESSION.get(url, timeout=90)
        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:
                retry_after = 2.0
            sleep_for = retry_after + 0.25
            log(f"Attachment/CDN rate limit. Sleeping {sleep_for:.2f}s before retry.")
            time.sleep(sleep_for)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Attachment download failed {resp.status_code}: {url}")
        return resp.content


def get_bot_user() -> dict:
    return request_json("GET", "/users/@me")


def get_guild_channels() -> List[dict]:
    return request_json("GET", f"/guilds/{GUILD_ID}/channels")


def find_category_and_channels(channels: List[dict]) -> Tuple[dict, List[dict]]:
    categories = [c for c in channels if c.get("type") == 4]
    category = next((c for c in categories if c.get("name") == CATEGORY_NAME), None)
    if not category:
        names = ", ".join(c.get("name", "?") for c in categories)
        raise RuntimeError(f"Could not find category named {CATEGORY_NAME!r}. Existing categories: {names}")
    children = [c for c in channels if str(c.get("parent_id")) == str(category["id"]) and c.get("type") in {0, 5, 15}]
    children.sort(key=lambda c: (c.get("position", 0), c.get("name", "")))
    return category, children


def fetch_channel_messages(channel_id: str, limit: int) -> List[dict]:
    all_messages: List[dict] = []
    before: Optional[str] = None
    remaining = max(int(limit), 1)
    while remaining > 0:
        page_size = min(100, remaining)
        path = f"/channels/{channel_id}/messages?limit={page_size}"
        if before:
            path += f"&before={before}"
        page = request_json("GET", path)
        if not page:
            break
        all_messages.extend(page)
        remaining -= len(page)
        before = page[-1]["id"]
        if len(page) < page_size:
            break
        time.sleep(0.2)
    all_messages.sort(key=lambda m: int(m["id"]))
    return all_messages


def fetch_message(channel_id: str, message_id: str) -> dict:
    return request_json("GET", f"/channels/{channel_id}/messages/{message_id}")


def send_message(channel_id: str, content: str) -> dict:
    if DRY_RUN:
        log(f"DRY_RUN: would send message to channel {channel_id}: {content[:80]}")
        return {"id": "dry-run-message"}
    return request_json("POST", f"/channels/{channel_id}/messages", json={"content": content[:1900]})


def edit_message(channel_id: str, message_id: str, content: str) -> None:
    if DRY_RUN:
        log(f"DRY_RUN: would edit dashboard {message_id} in channel {channel_id}")
        return
    request_json("PATCH", f"/channels/{channel_id}/messages/{message_id}", json={"content": content[:1900]})


def message_has_attachment_or_manual(message: dict) -> bool:
    if message.get("attachments"):
        return True
    content = message.get("content") or ""
    return bool(re.match(r"^!manual_count\s+\d+", content.strip(), flags=re.IGNORECASE))


def writer_key_from_channel(channel: dict) -> str:
    topic = channel.get("topic") or ""
    match = re.search(r"Writer user id: (\d+)", topic)
    if match:
        return match.group(1)
    return f"channel:{channel['id']}"


def match_goal(display_name: str, channel_name: str, goals: List[dict]) -> Optional[dict]:
    candidates = {normalize_key(display_name), normalize_key(channel_name), normalize_key(display_from_channel_name(channel_name))}
    for goal in goals:
        aliases = goal.get("aliases") or [goal.get("name", "")]
        for alias in aliases:
            alias_key = normalize_key(str(alias))
            if not alias_key:
                continue
            for candidate in candidates:
                if not candidate:
                    continue
                if candidate == alias_key:
                    return goal
                if candidate.startswith(alias_key + " ") or alias_key.startswith(candidate + " "):
                    return goal
    return None


def ensure_writer_record(state: dict, channel: dict, goals: List[dict]) -> dict:
    gs = guild_state(state)
    writers = gs.setdefault("writers", {})
    key = writer_key_from_channel(channel)
    ws = writers.setdefault(key, {})
    ws.setdefault("user_id", key)
    ws.setdefault("channel_id", str(channel["id"]))
    ws.setdefault("dashboard_message_id", None)
    ws.setdefault("submissions", [])
    ws.setdefault("goal", DEFAULT_WEEKLY_GOAL)
    ws.setdefault("goal_source", "default")

    if not ws.get("display_name"):
        ws["display_name"] = display_from_channel_name(channel.get("name", "writer"))
    ws["channel_id"] = str(channel["id"])

    matched = match_goal(str(ws.get("display_name", "")), str(channel.get("name", "")), goals)
    if matched and ws.get("goal_source") in {None, "default", "github_actions_goal_file", "known_goal_seed"}:
        ws["goal"] = int(matched["goal"])
        ws["goal_source"] = "github_actions_goal_file"
        ws["goal_name_match"] = matched.get("name")
        ws["goal_updated_at"] = utc_now_iso()
    return ws


def totals_for_writer(ws: dict) -> Tuple[int, int, Optional[dict]]:
    submissions = ws.get("submissions", [])
    week = current_week_key()
    cumulative = sum(int(item.get("words", 0)) for item in submissions)
    weekly = sum(int(item.get("words", 0)) for item in submissions if item.get("week") == week)
    last = submissions[-1] if submissions else None
    return cumulative, weekly, last


def progress_bar(done: int, goal: int, width: int = 20) -> str:
    if goal <= 0:
        return "Goal not set"
    ratio = min(done / goal, 1.0)
    filled = int(round(ratio * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def dashboard_text(ws: dict, channel: dict) -> str:
    cumulative, weekly, last = totals_for_writer(ws)
    goal = int(ws.get("goal") or DEFAULT_WEEKLY_GOAL)
    percent = round((weekly / goal) * 100, 1) if goal > 0 else 0
    last_line = "No submissions counted yet."
    if last:
        last_line = f"{last.get('words', 0):,} words from {last.get('source', 'unknown source')} on {last.get('created_at', '')}"
    return (
        f"**3MM Writer Dashboard: {ws.get('display_name', channel.get('name', 'Writer'))}**\n\n"
        f"Channel: <#{channel['id']}>\n"
        f"Weekly goal: **{goal:,} words**\n"
        f"Goal source: {ws.get('goal_source', 'default')}\n"
        f"This week ({current_week_key()}): **{weekly:,} / {goal:,} words** ({percent}%)\n"
        f"Progress: `{progress_bar(weekly, goal)}`\n"
        f"Cumulative counted total: **{cumulative:,} words**\n"
        f"Last counted submission: {last_line}\n\n"
        f"Upload `.docx`, `.md`, `.txt`, or `.rtf` files here for automatic counting. "
        f"This dashboard is updated by GitHub Actions on a schedule, so it may not update instantly."
    )


def find_dashboard_in_messages(messages: List[dict], bot_user_id: str) -> Optional[str]:
    for msg in reversed(messages):
        author = msg.get("author") or {}
        content = msg.get("content") or ""
        if str(author.get("id")) == str(bot_user_id) and "3MM Writer Dashboard" in content:
            return str(msg["id"])
    return None


def update_dashboard(ws: dict, channel: dict, bot_user_id: str, recent_messages: List[dict]) -> bool:
    content = dashboard_text(ws, channel)
    dashboard_id = ws.get("dashboard_message_id")
    if not dashboard_id:
        dashboard_id = find_dashboard_in_messages(recent_messages, bot_user_id)
        if dashboard_id:
            ws["dashboard_message_id"] = str(dashboard_id)

    if dashboard_id:
        try:
            current = fetch_message(str(channel["id"]), str(dashboard_id))
            if (current.get("content") or "") == content:
                return False
            edit_message(str(channel["id"]), str(dashboard_id), content)
            return True
        except Exception as exc:
            log(f"Could not edit dashboard in #{channel.get('name')}: {exc}. Will create a new dashboard.")

    msg = send_message(str(channel["id"]), content)
    ws["dashboard_message_id"] = str(msg["id"])
    return True


def remove_submissions_by_message(ws: dict, message_id: str) -> int:
    old = ws.get("submissions", [])
    new = [s for s in old if str(s.get("message_id")) != str(message_id)]
    ws["submissions"] = new
    return len(old) - len(new)


def file_hash_seen(ws: dict, file_hash: str, message_id: str) -> Optional[dict]:
    for sub in ws.get("submissions", []):
        if str(sub.get("message_id")) == str(message_id):
            continue
        if file_hash in set(sub.get("file_hashes") or []):
            return sub
    return None


def submission_already_exists(ws: dict, message_id: str) -> bool:
    return any(str(s.get("message_id")) == str(message_id) for s in ws.get("submissions", []))


def count_message_for_writer(state: dict, ws: dict, channel: dict, message: dict, force: bool = False) -> Tuple[int, str]:
    gs = guild_state(state)
    message_id = str(message["id"])
    ignored = gs.setdefault("ignored_messages", {})
    cleared = gs.setdefault("cleared_messages", {})
    duplicates = gs.setdefault("duplicate_messages", {})
    processed = gs.setdefault("processed_messages", {})

    if message_id in ignored:
        return 0, "ignored"
    if message_id in cleared and not force:
        return 0, "cleared"
    if submission_already_exists(ws, message_id) and not force:
        return 0, "already-counted"

    total_words = 0
    counted_sources: List[str] = []
    file_hashes: List[str] = []
    errors: List[str] = []

    for attachment in message.get("attachments") or []:
        filename = attachment.get("filename") or "attachment"
        size = int(attachment.get("size") or 0)
        if not is_supported_filename(filename):
            continue
        if size > MAX_ATTACHMENT_BYTES:
            errors.append(f"{filename} too large")
            continue
        url = attachment.get("url")
        if not url:
            errors.append(f"{filename} missing URL")
            continue
        data = request_bytes(url)
        file_hash = hashlib.sha256(data).hexdigest()
        seen = file_hash_seen(ws, file_hash, message_id)
        if seen and not force:
            duplicates[message_id] = {
                "message_id": message_id,
                "channel_id": str(channel["id"]),
                "display_name": ws.get("display_name"),
                "filename": filename,
                "duplicate_of_message_id": seen.get("message_id"),
                "file_hash": file_hash,
                "detected_at": utc_now_iso(),
            }
            log(f"Duplicate skipped in #{channel.get('name')}: {filename} duplicates message {seen.get('message_id')}")
            continue
        words = count_words_from_file_bytes(filename, data)
        total_words += words
        file_hashes.append(file_hash)
        counted_sources.append(f"{filename} ({words:,})")

    content = message.get("content") or ""
    manual_match = re.match(r"^!manual_count\s+([0-9][0-9,]*)\s*(.*)$", content.strip(), flags=re.IGNORECASE | re.DOTALL)
    if manual_match:
        words = int(manual_match.group(1).replace(",", ""))
        note = manual_match.group(2).strip() or "manual count"
        total_words += words
        counted_sources.append(f"manual count: {note[:80]} ({words:,})")
    elif total_words == 0 and content and not content.strip().startswith("!"):
        pasted_words = count_words(content)
        if pasted_words >= MIN_PASTED_WORDS_TO_COUNT:
            total_words += pasted_words
            counted_sources.append(f"pasted text ({pasted_words:,})")

    if total_words <= 0:
        processed[message_id] = True
        return 0, "; ".join(errors) if errors else "nothing-countable"

    if force:
        remove_submissions_by_message(ws, message_id)
        cleared.pop(message_id, None)
        ignored.pop(message_id, None)
        duplicates.pop(message_id, None)

    ws.setdefault("submissions", []).append(
        {
            "words": int(total_words),
            "source": ", ".join(counted_sources) if counted_sources else "message",
            "channel_id": str(channel["id"]),
            "message_id": message_id,
            "week": current_week_key(),
            "created_at": utc_now_iso(),
            "discord_created_at": message.get("timestamp"),
            "file_hashes": file_hashes,
            "source_type": "github_actions_scan",
        }
    )
    processed[message_id] = True
    return total_words, "counted"


def scan_channel(state: dict, channel: dict, goals: List[dict], bot_user_id: str, limit: int, force_rebuild: bool = False) -> Tuple[int, int]:
    ws = ensure_writer_record(state, channel, goals)
    log(f"Scanning #{channel.get('name')} ({channel['id']}) for {ws.get('display_name')} limit={limit} force_rebuild={force_rebuild}")
    messages = fetch_channel_messages(str(channel["id"]), limit=limit)
    if force_rebuild:
        old_count = len(ws.get("submissions", []))
        # Keep manual slash submissions that are not tied to a visible message; visible manual prefix messages will be re-read.
        preserved = [s for s in ws.get("submissions", []) if not s.get("message_id") and str(s.get("source_type", "")).startswith("manual")]
        ws["submissions"] = preserved
        log(f"Rebuild for #{channel.get('name')}: removed {old_count - len(preserved)} old visible submission(s), preserved {len(preserved)} manual/no-message submission(s).")

    counted = 0
    changed = 0
    for message in messages:
        author = message.get("author") or {}
        if str(author.get("id")) == str(bot_user_id):
            continue
        if not message_has_attachment_or_manual(message):
            continue
        words, status = count_message_for_writer(state, ws, channel, message, force=False)
        if words > 0:
            counted += words
            changed += 1
            log(f"Counted {words:,} words from message {message['id']} in #{channel.get('name')}")
        elif status not in {"already-counted", "nothing-countable"}:
            log(f"Skipped message {message['id']} in #{channel.get('name')}: {status}")

    if changed or force_rebuild:
        update_dashboard(ws, channel, bot_user_id, messages)
    return changed, counted


def writer_matches(ws: dict, channel: dict, query: str) -> bool:
    q = normalize_key(query)
    if not q:
        return False
    values = [
        str(ws.get("user_id", "")),
        str(ws.get("display_name", "")),
        str(channel.get("name", "")),
        display_from_channel_name(str(channel.get("name", ""))),
        str(channel.get("id", "")),
    ]
    for value in values:
        key = normalize_key(value)
        if key == q or key.startswith(q + " ") or q.startswith(key + " ") or q in key:
            return True
    return False


def find_channel_for_writer(state: dict, channels: List[dict], goals: List[dict], writer: str = "", channel_id: str = "") -> Tuple[dict, dict]:
    matches: List[Tuple[dict, dict]] = []
    for channel in channels:
        ws = ensure_writer_record(state, channel, goals)
        if channel_id and str(channel.get("id")) == str(channel_id):
            return channel, ws
        if writer and writer_matches(ws, channel, writer):
            matches.append((channel, ws))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"#{c.get('name')}" for c, _ in matches)
        raise RuntimeError(f"Writer query {writer!r} matched multiple channels: {names}. Use channel_id.")
    raise RuntimeError(f"Could not find writer/channel for writer={writer!r} channel_id={channel_id!r}")


def apply_ignore_or_clear(state: dict, channel: dict, ws: dict, message_id: str, mode: str, reason: str) -> int:
    gs = guild_state(state)
    bucket_name = "ignored_messages" if mode == "ignore" else "cleared_messages"
    bucket = gs.setdefault(bucket_name, {})
    bucket[str(message_id)] = {
        "message_id": str(message_id),
        "channel_id": str(channel["id"]),
        "writer": ws.get("display_name"),
        "reason": reason or mode,
        "created_at": utc_now_iso(),
        "source": "github_actions_workflow_dispatch",
    }
    removed = remove_submissions_by_message(ws, str(message_id))
    gs.setdefault("processed_messages", {}).pop(str(message_id), None)
    log(f"{mode.title()} message {message_id} for {ws.get('display_name')}; removed {removed} counted submission(s).")
    return removed


def clear_all_writer(state: dict, channel: dict, ws: dict, reason: str, limit: int) -> int:
    messages = fetch_channel_messages(str(channel["id"]), limit=limit)
    gs = guild_state(state)
    cleared = gs.setdefault("cleared_messages", {})
    marked = 0
    for message in messages:
        if message_has_attachment_or_manual(message):
            cleared[str(message["id"])] = {
                "message_id": str(message["id"]),
                "channel_id": str(channel["id"]),
                "writer": ws.get("display_name"),
                "reason": reason or "clear all writer",
                "created_at": utc_now_iso(),
                "source": "github_actions_workflow_dispatch",
            }
            marked += 1
    removed = len(ws.get("submissions", []))
    ws["submissions"] = []
    log(f"Cleared all visible countable messages for {ws.get('display_name')}: marked {marked}, removed {removed} submission(s).")
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Writers Bloc GitHub Actions Discord scanner")
    parser.add_argument("--mode", default="scan-all", choices=["scan-all", "recount-writer", "ignore-doc", "clear-message", "clear-all-writer", "clear-everything", "recount-message"])
    parser.add_argument("--writer", default="", help="Writer display/name/channel query for targeted modes")
    parser.add_argument("--channel-id", default="", help="Discord channel ID for targeted modes")
    parser.add_argument("--message-id", default="", help="Discord message ID for ignore/clear/recount-message")
    parser.add_argument("--limit-per-channel", type=int, default=250)
    parser.add_argument("--reason", default="")
    args = parser.parse_args()

    if not TOKEN:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN secret/env var")
    if not GUILD_ID:
        raise RuntimeError("Missing DISCORD_GUILD_ID secret/env var")

    log(f"Writers Bloc GitHub Actions scanner version {BOT_VERSION}")
    log(f"Mode={args.mode} category={CATEGORY_NAME!r} state={STATE_FILE} goals={GOALS_FILE} dry_run={DRY_RUN}")

    state = load_state()
    goals = load_goals()
    bot_user = get_bot_user()
    bot_user_id = str(bot_user["id"])
    channels_all = get_guild_channels()
    _category, writer_channels = find_category_and_channels(channels_all)
    log(f"Found {len(writer_channels)} writer channel(s) under {CATEGORY_NAME!r}.")

    changed_channels = 0
    counted_words = 0

    if args.mode == "scan-all":
        for channel in writer_channels:
            changed, words = scan_channel(state, channel, goals, bot_user_id, args.limit_per_channel, force_rebuild=False)
            changed_channels += 1 if changed else 0
            counted_words += words
            time.sleep(0.35)

    elif args.mode == "recount-writer":
        channel, ws = find_channel_for_writer(state, writer_channels, goals, writer=args.writer, channel_id=args.channel_id)
        changed, words = scan_channel(state, channel, goals, bot_user_id, args.limit_per_channel, force_rebuild=True)
        changed_channels = 1
        counted_words = words

    elif args.mode in {"ignore-doc", "clear-message"}:
        if not args.message_id:
            raise RuntimeError(f"{args.mode} requires --message-id")
        # Prefer supplied channel/writer. If not supplied, search recent channel histories for the message.
        if args.channel_id or args.writer:
            channel, ws = find_channel_for_writer(state, writer_channels, goals, writer=args.writer, channel_id=args.channel_id)
        else:
            found = None
            for c in writer_channels:
                try:
                    fetch_message(str(c["id"]), args.message_id)
                    found = c
                    break
                except Exception:
                    continue
            if not found:
                raise RuntimeError("Could not find the message in any writer channel. Supply channel_id.")
            channel = found
            ws = ensure_writer_record(state, channel, goals)
        mode = "ignore" if args.mode == "ignore-doc" else "clear"
        apply_ignore_or_clear(state, channel, ws, args.message_id, mode, args.reason)
        # Recount after changing ignore/clear state so totals and dashboard are correct.
        scan_channel(state, channel, goals, bot_user_id, args.limit_per_channel, force_rebuild=True)
        changed_channels = 1

    elif args.mode == "recount-message":
        if not args.message_id:
            raise RuntimeError("recount-message requires --message-id")
        channel, ws = find_channel_for_writer(state, writer_channels, goals, writer=args.writer, channel_id=args.channel_id)
        gs = guild_state(state)
        gs.setdefault("ignored_messages", {}).pop(str(args.message_id), None)
        gs.setdefault("cleared_messages", {}).pop(str(args.message_id), None)
        gs.setdefault("processed_messages", {}).pop(str(args.message_id), None)
        remove_submissions_by_message(ws, str(args.message_id))
        message = fetch_message(str(channel["id"]), str(args.message_id))
        words, status = count_message_for_writer(state, ws, channel, message, force=True)
        log(f"Recount message {args.message_id}: {words:,} words status={status}")
        messages = fetch_channel_messages(str(channel["id"]), limit=20)
        update_dashboard(ws, channel, bot_user_id, messages)
        changed_channels = 1
        counted_words = words

    elif args.mode == "clear-all-writer":
        channel, ws = find_channel_for_writer(state, writer_channels, goals, writer=args.writer, channel_id=args.channel_id)
        clear_all_writer(state, channel, ws, args.reason, args.limit_per_channel)
        messages = fetch_channel_messages(str(channel["id"]), limit=20)
        update_dashboard(ws, channel, bot_user_id, messages)
        changed_channels = 1

    elif args.mode == "clear-everything":
        gs = guild_state(state)
        for channel in writer_channels:
            ws = ensure_writer_record(state, channel, goals)
            clear_all_writer(state, channel, ws, args.reason or "clear everything", args.limit_per_channel)
            messages = fetch_channel_messages(str(channel["id"]), limit=20)
            update_dashboard(ws, channel, bot_user_id, messages)
            changed_channels += 1
            time.sleep(0.35)

    save_state(state)
    log(f"Done. Changed dashboards/channels={changed_channels}; new/recounted words={counted_words:,}")
    REPORT_FILE.write_text("# Writers Bloc GitHub Actions Scan Report\n\n```text\n" + "\n".join(REPORT_LINES) + "\n```\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"FATAL: {type(exc).__name__}: {exc}")
        REPORT_FILE.write_text("# Writers Bloc GitHub Actions Scan Report - FAILED\n\n```text\n" + "\n".join(REPORT_LINES) + "\n```\n", encoding="utf-8")
        raise
