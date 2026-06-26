# Writers Bloc GitHub Actions Discord Scanner with Upload Replies

This package runs the Writers Bloc 3MM word counter from GitHub Actions instead of a 24/7 server.

It is **not** a live Discord Gateway bot. It is a scheduled REST scanner:

1. GitHub Actions wakes up on a cron schedule or manual `Run workflow`.
2. The scanner reads recent messages in the `3MM Writer Manuscripts` category.
3. It counts `.docx`, `.md`, `.markdown`, `.txt`, and `.rtf` uploads.
4. It skips ignored, cleared, already-counted, and duplicate files.
5. It updates `writers_bloc_3mm_state.json`.
6. It edits the writer dashboard.
7. It replies to each counted upload message with writer-facing feedback.
8. It commits the updated JSON state back to the repo.

## New reply behavior

When a writer uploads a supported document, the scheduled scanner will reply to the upload message in this format:

```text
Counted **9,043 words** from **Body_of_Water.docx (9,043)** for @Mr. Cameron Pilgrim.
This week: **9,043 / 2,500**. Cumulative: **9,043**. Weekly goal met.
```

It will not reply instantly unless you manually run the workflow. On the hourly cron, the reply appears the next time the workflow runs.

The scanner stores reply state in `writers_bloc_3mm_state.json` under:

```json
count_replies
skipped_replies
```

That prevents repeat replies every hour.

## Files

Required files in the repo root:

```text
.github/workflows/writers-bloc-discord-scan.yml
discord_scheduled_scanner.py
word_counter.py
writer_weekly_goals.json
writers_bloc_3mm_state.json
requirements.txt
```

## GitHub Secrets

Add these repository secrets:

```text
DISCORD_BOT_TOKEN
DISCORD_GUILD_ID
```

The token must never be committed to the repo.

## Manual run

In GitHub:

```text
Actions -> Writers Bloc Discord Scheduled Scanner -> Run workflow
```

Most common manual options:

```text
mode: scan-all
limit_per_channel: 250
```

This scans every writer channel, counts new uploads, sends missing replies, updates dashboards, and commits state.

If the dashboard already counted the file but the bot never replied to the original message, run:

```text
mode: reply-missing
limit_per_channel: 250
```

That will scan already-counted messages and send any missing upload feedback replies.

## Duplicate behavior

If the same exact file is uploaded again, the scanner detects the duplicate using SHA-256 file hashes. It does not add the words again. It can also reply to the duplicate upload telling the writer that it was skipped.

## Ignore / clear / recount

Use workflow dispatch modes:

```text
ignore-doc
clear-message
recount-message
recount-writer
clear-all-writer
clear-everything
```

For duplicate uploads, use:

```text
mode: ignore-doc
writer: Conner
message_id: THE_DUPLICATE_MESSAGE_ID
reason: Duplicate upload
limit_per_channel: 500
```

Then run:

```text
mode: recount-writer
writer: Conner
limit_per_channel: 500
```

## Schedule

The default schedule is hourly at minute 17 UTC:

```yaml
schedule:
  - cron: '17 * * * *'
```

You can also run it manually from your phone using GitHub Actions.

## Notes

- This is not instant, but it works without your computer being on.
- Do not run the local live bot and the GitHub Actions scanner at the same time unless you understand the risk of duplicated actions.
- Keep the repository private because the state file contains Discord IDs and writing progress.
- The scanner commits `writers_bloc_3mm_state.json` after every successful run that changes state.
