# gmail-tinder

Tiny terminal Gmail triage app built on top of [`gws`](https://github.com/googleworkspace/cli).

**Requirements:** Python **3.10+** (stdlib only — no `pip` packages for this repo). A terminal with **curses** support (macOS, Linux, and most Unix environments).

This project is **not** affiliated with Google. Gmail access is through your own Google Cloud project and [`gws`](https://github.com/googleworkspace/cli); use at your own risk and in line with Google’s terms.

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

Install [`gws` (googleworkspace-cli)](https://github.com/googleworkspace/cli#installation) and the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) (`gcloud`) on your `PATH`. `gws auth setup` uses `gcloud` to create or select a project and enable APIs.

This app **only** talks to Gmail through **`gws`**. You need a **Google Cloud project** with the **Gmail API enabled** and a **`gws` login** that includes the Gmail modify scope.

### Option A — `gws auth setup` (recommended)

Run the interactive wizard, then log in with the scope this app needs (both commands):

```bash
gws auth setup
gws auth login --scopes https://www.googleapis.com/auth/gmail.modify
```

`gws auth setup` walks through Google Cloud project setup and OAuth; see the [gws authentication docs](https://github.com/googleworkspace/cli#authentication).

If the OAuth app is in **Testing** mode, add your Google account under **Test users** on the [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent) for that project. Otherwise you may see **Access blocked** (see [gws troubleshooting](https://github.com/googleworkspace/cli#access-blocked-or-403-during-login)).

If Gmail still errors after setup, enable the API explicitly with Option B or open the [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com) page for your project and click **Enable**.

### Option B — Manual setup (Google Cloud CLI)

Use this when you prefer not to use `gws auth setup`, or when setup did not enable Gmail. `gcloud config set project` only selects a project; it does **not** create one.

1. Sign in:

   ```bash
   gcloud auth login
   ```

2. Create a project if you need one (project IDs are globally unique: lowercase letters, digits, hyphens):

   ```bash
   gcloud projects create YOUR_GCP_PROJECT_ID --name="Gmail Tinder"
   ```

3. Select the project and enable the Gmail API:

   ```bash
   gcloud config set project YOUR_GCP_PROJECT_ID
   gcloud services enable gmail.googleapis.com --project YOUR_GCP_PROJECT_ID
   ```

4. Optional — confirm Gmail API is enabled:

   ```bash
   gcloud services list --enabled --project=YOUR_GCP_PROJECT_ID --filter="name:gmail.googleapis.com"
   ```

5. On the [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent) for that project, under **Test users**, add the same account you use in the browser during `gws auth login`.

6. Sign in with `gws`:

   ```bash
   gws auth login --scopes https://www.googleapis.com/auth/gmail.modify
   ```

Optional: `gws auth status` should list `gmail.googleapis.com` under `enabled_apis` and `https://www.googleapis.com/auth/gmail.modify` under `scopes`.

If you already ran `gws auth login` before enabling the API or adding a test user:

```bash
gws auth logout
gws auth login --scopes https://www.googleapis.com/auth/gmail.modify
```

## Run

```bash
python3 app.py
```

From the repo root you can also use the launcher (make it executable once: `chmod +x gmail-tinder`):

```bash
./gmail-tinder
```

Optional flags:

```bash
python3 app.py --label-name GmailTinderArchive --max-results 50
python3 app.py --query 'older_than:30d category:promotions'
python3 app.py --reset-progress
```

## Privacy

Google credentials are handled by **`gws`** and Google (not this app). Progress and stats are stored locally in `.gmail_tinder_state.json` in the same directory as `app.py` (ignored by git).

## Tests

```bash
python3 -m unittest discover -v
```

`SetupInstructionsTests` uses the placeholder project id `example-gcp-project-id` unless you set **`GMAIL_TINDER_TEST_GCP_PROJECT_ID`** (handy to confirm output strings match your real project locally without putting that id in git).

## License

[MIT](LICENSE)

## Notes

- The first run auto-creates the label if it does not already exist.
- If Gmail API access or auth is wrong, the app exits with copy-paste `gcloud` / `gws` steps instead of a raw `gws` error.
- Messages are loaded in batches and you can continue to the next batch without restarting the app.
- The app remembers which message IDs you already handled, so restarting resumes from where you left off.
- Review-time stats now persist across runs and appear in the dashboard and stats screen as all-time totals.
- Use `--reset-progress` if you want to forget that saved progress and start from the top again.
- Press `q` to quit early.
