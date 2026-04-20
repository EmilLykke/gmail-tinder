# gmail-tinder

Tiny terminal Gmail triage app built on top of [`gws`](https://github.com/googleworkspace/cli).

## What it does

- Loads a batch of inbox messages.
- Shows sender, subject, date, and snippet.
- `Left arrow`: "delete later" by removing the message from `INBOX` and adding a custom Gmail label.
- `Right arrow`: keep the message and move on.
- `H`: archive, like Vim left.
- `L`: keep, like Vim right.
- `U`: undo the last swipe.
- `S`: open the session stats screen.
- At the end of a batch, press `N` to load the next batch of messages.
- The app shows live session stats like average review time, emails handled, and an estimated time-saved number.
- The app now starts on a dashboard where you can begin with arrows or `H`/`L`, open stats, or quit.

This does not move mail to Gmail trash. It archives the message out of the inbox and tags it so you can review the batch later in Gmail.

## First-time setup

Enable the Gmail API for your GCP project and log in with Gmail modify scope:

```bash
gcloud config set project goole-cloud-project-id
gcloud services enable gmail.googleapis.com --project goole-cloud-project-id
gws auth login --scopes https://www.googleapis.com/auth/gmail.modify
```

If you already logged in before enabling the API, reset and log in again:

```bash
gws auth logout
gws auth login --scopes https://www.googleapis.com/auth/gmail.modify
```

## Run

```bash
python3 app.py
```

Optional flags:

```bash
python3 app.py --label-name GmailTinderArchive --max-results 50
python3 app.py --query 'older_than:30d category:promotions'
python3 app.py --reset-progress
```

## Notes

- The first run auto-creates the label if it does not already exist.
- If Gmail API access is missing, the app now stops with `gcloud` setup commands instead of a raw `gws` error.
- Messages are loaded in batches and you can continue to the next batch without restarting the app.
- The app remembers which message IDs you already handled, so restarting resumes from where you left off.
- Review-time stats now persist across runs and appear in the dashboard and stats screen as all-time totals.
- Use `--reset-progress` if you want to forget that saved progress and start from the top again.
- Press `q` to quit early.
