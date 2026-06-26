# Writers Bloc Discord Scanner on GitHub Actions

This package runs the Writers Bloc word counter through **GitHub Actions** instead of a 24/7 VM.

It is not a permanently online Discord bot. It is a scheduled scanner:

1. GitHub Actions wakes up on a cron schedule or manual trigger.
2. The Python script uses the Discord REST API to fetch recent messages from `3MM Writer Manuscripts` channels.
3. It downloads `.docx`, `.md`, `.txt`, and `.rtf` attachments.
4. It counts words.
5. It updates `writers_bloc_3mm_state.json`.
6. It edits the writer dashboard messages in Discord.
7. It commits the updated JSON state back to the repo.

## Why this works

Discord keeps channel message history. The scanner does not need to be online at the exact moment someone uploads a file. It can wake up later, scan the recent messages, count any unprocessed uploads, and update dashboards.

## What it cannot do

Because GitHub Actions is not a persistent Discord Gateway process, this setup cannot provide instant slash-command handling inside Discord. Commands are handled by running the GitHub Actions workflow manually with inputs.

Use this if Oracle Cloud / a VPS is not available and you are okay with delayed counting.

## Required Discord bot permissions

The same Discord bot token can be used. The bot must have access to the server and channels.

Recommended bot permissions:

- View Channels
- Read Message History
- Send Messages
- Manage Messages is optional
- Attach Files is optional
- Embed Links is optional

The bot also needs Message Content Intent enabled in the Discord Developer Portal so the API can see message content/attachments reliably.

## Required GitHub repo setup

Use a private GitHub repository if possible. The state file does not contain the bot token, but it does contain Discord channel/member IDs and writing progress.

Add these files to the repo root:

```text
.github/workflows/writers-bloc-discord-scan.yml
discord_scheduled_scanner.py
word_counter.py
writer_weekly_goals.json
writers_bloc_3mm_state.json
requirements.txt
```

Then add repository secrets:

```text
DISCORD_BOT_TOKEN = your Discord bot token
DISCORD_GUILD_ID = your Writers Bloc server/guild ID
```

In GitHub:

1. Open the repo.
2. Go to **Settings**.
3. Go to **Secrets and variables**.
4. Go to **Actions**.
5. Click **New repository secret**.
6. Add `DISCORD_BOT_TOKEN`.
7. Add `DISCORD_GUILD_ID`.

Do not commit the bot token into the repo.

## Getting your Discord guild/server ID

In Discord:

1. User Settings → Advanced.
2. Enable Developer Mode.
3. Right-click the Writers Bloc server icon.
4. Click **Copy Server ID**.

That value is `DISCORD_GUILD_ID`.

## How often it runs

The workflow currently uses:

```yaml
schedule:
  - cron: '17 * * * *'
```

That means once per hour at minute 17, in UTC.

Examples:

```yaml
# Every hour
- cron: '17 * * * *'

# Every 30 minutes
- cron: '7,37 * * * *'

# Every evening at 8:17 PM Central during daylight time, which is 01:17 UTC next day
- cron: '17 1 * * *'

# Twice per day
- cron: '17 1,13 * * *'
```

Do not set it to every minute. GitHub scheduled workflows are not meant to be a constantly-running bot replacement.

## Manual operations

Open GitHub → repo → Actions → **Writers Bloc Discord Scheduled Scanner** → **Run workflow**.

### Normal scan

```text
mode = scan-all
limit_per_channel = 250
```

### Recount one writer

```text
mode = recount-writer
writer = Conner
limit_per_channel = 500
```

or more precise:

```text
mode = recount-writer
channel_id = 1514693923103047803
limit_per_channel = 500
```

### Ignore a duplicate or side-story doc

```text
mode = ignore-doc
message_id = 123456789012345678
writer = Conner
reason = Duplicate upload / side story not part of manuscript
limit_per_channel = 500
```

The workflow will mark that message ignored, rebuild the writer total, and update the dashboard.

### Clear a message count

```text
mode = clear-message
message_id = 123456789012345678
writer = Conner
reason = Wrong file uploaded
limit_per_channel = 500
```

### Recount one exact message

```text
mode = recount-message
message_id = 123456789012345678
writer = Conner
```

### Clear all counted words for one writer

```text
mode = clear-all-writer
writer = Conner
reason = Reset writer total
limit_per_channel = 500
```

### Clear everything

Be careful:

```text
mode = clear-everything
reason = Full reset
limit_per_channel = 500
```

## State file

`writers_bloc_3mm_state.json` is the scanner memory.

It stores:

- writer channel mappings
- dashboard message IDs
- goals
- counted submissions
- ignored messages
- cleared messages
- duplicate message records
- processed messages

The workflow commits this file back to the repo after each successful run.

## Goals file

`writer_weekly_goals.json` is the source of truth for weekly goals. It fuzzy-matches aliases against channel names and saved display names.

Current examples:

- Conner / Connor / Hale → 2,000
- Carter / Horton / F1rst Frosty → 2,500
- Evans / Lyn / Lynn → 1,000
- Pilgrim / Cameron → 2,500
- Chief / Mariam / Miriam → 3,000

Update this JSON file when goals change.

## Important limitations

This is delayed. If the workflow runs hourly, dashboard updates happen hourly.

This is not a live bot. Slash commands like `/ignore-doc` will not work unless the normal Discord bot is running somewhere. For GitHub Actions, use workflow_dispatch inputs instead.

GitHub Actions runners are temporary. That is why the workflow commits `writers_bloc_3mm_state.json` back to the repository.

## Troubleshooting

### Workflow fails with Discord 401

The token is wrong. Replace the `DISCORD_BOT_TOKEN` secret.

### Workflow fails with Discord 403

The bot does not have permission to read/edit that channel, or the bot is not in the server.

### It cannot find the category

Make sure the category is exactly:

```text
3MM Writer Manuscripts
```

or change `DISCORD_CATEGORY_NAME` in the workflow env.

### It sees channels but not attachments/content

Enable Message Content Intent in the Discord Developer Portal and make sure the bot has Read Message History.

### It double-counts

The scanner uses SHA-256 hashes of file bytes to skip exact duplicate files per writer. If the writer uploads a slightly changed file, it is a different file and will count. Use `ignore-doc` for duplicates/side stories.

### Dashboard does not update

Run workflow manually:

```text
mode = recount-writer
writer = Their Name
limit_per_channel = 500
```

If dashboard message ID is wrong/deleted, the scanner should create a new dashboard message.
