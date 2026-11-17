# Google Ads Chatbot (proof of concept)

A chatbot that answers questions about Google Ads data (campaigns, ad groups, ads, keywords,
metrics) and can make the changes the MCP server supports: labels, label associations,
standalone assets and video uploads. It uses Google's official
[Google Ads MCP server](https://github.com/googleads/google-ads-mcp) (v0.0.6) for data access
and Google's Gemini model (free tier through Google AI Studio) to drive it. Every change needs
a click on **Confirm** in the chat before it is made.

It runs with **one shared Google login**: the app uses the Google sign-in of whoever sets it up,
and everyone who opens the chatbot sees the Google Ads accounts that login can access. This
suits an agency team working through one manager (MCC) account. There is no sign-in on the
web page, so run it only where your team can reach it.

```
Browser (static/index.html)
   │  POST /chat {message | confirm, history}     POST /upload (attachments)
   ▼
FastAPI (app/main.py) ── function-calling loop (app/agent.py) ──► Gemini API
   │
   └─ MCP client (app/mcp_client.py) ──stdio──► google-ads-mcp ──► Google Ads API
                                     (credentials/credentials.json)
```

On startup the backend launches `google-ads-mcp` as a subprocess, connects with the MCP Python
SDK and converts its tools to Gemini function declarations. Each `/chat` request runs the
model in a loop (up to 10 steps), runs the tools it asks for through the MCP session, and
returns the answer plus the updated conversation history. Credentials stay on the server.

The MCP server provides these tools:

| Tool | What it does |
|---|---|
| `customers_list_accessible_customers` | Lists the customer IDs your login can access |
| `metadata_get_resource_metadata` | Lists the fields, metrics and segments available for a resource |
| `search_search` | Runs a GAQL query (resource, fields, conditions, orderings, limit) |
| `labels_manage_labels` | **Write.** Creates, updates or removes account labels |
| `labels_manage_label_associations` | **Write.** Attaches or detaches a label on a campaign, ad group, keyword or the account |
| `assets_create_asset` | **Write.** Creates a standalone TEXT, IMAGE, YOUTUBE_VIDEO, SITELINK, CALLOUT or STRUCTURED_SNIPPET asset |
| `assets_upload_video_asset` | **Write.** Uploads a video to YouTube through Google Ads (unlisted by default) |

The server can't create, edit, pause or enable campaigns, ad groups, ads, budgets or bids, and
can't link assets to campaigns or ad groups. The chatbot says so when asked.

### How a change is made

Each write tool has a two-step *propose and confirm* flow built into the MCP server:

1. Gemini calls the tool with `validate_only=true`. The server checks the change with the
   Google Ads API and returns the list of changes plus a checksum. Nothing changes yet.
2. Gemini calls the tool again with `validate_only=false` and the checksum. **The app doesn't
   run this call.** It stops and shows a **Confirm change in Google Ads** card listing the
   changes, with **Confirm** and **Cancel** buttons.
3. The change is made only after **Confirm** is clicked. **Cancel**, or sending a new message
   instead, makes no change. A card can be used once, and it expires after 30 minutes.

To attach an image (for an IMAGE asset) or a video (for a YouTube upload), use the **Attach**
button. Files are saved in `uploads/` on the server. The write tools may only read files from
that folder, never other paths on the server.

To run the chatbot read-only, set `labels: false` and `assets: false` in
[`tools_config.yaml`](tools_config.yaml) and restart.

## Requirements

- Python 3.11+
- [pipx](https://pipx.pypa.io/) **or** [uv](https://docs.astral.sh/uv/) (`uvx`) to run the MCP server
- A free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)
- A Google account with access to your Google Ads accounts (for an agency: the manager account)
- A Google Cloud project with Google Ads API **Explorer, Basic or Standard access** (check the
  Google Ads API overview page in the Cloud console and apply if needed). No developer token is
  needed: developer tokens were sunset on September 9, 2026, and access levels now belong to
  the project.

## Setup

### 1. Create a Google Cloud project and enable the Google Ads API

1. Open the [Google Cloud console](https://console.cloud.google.com/) and create a project
   (or pick an existing one). Note its **project ID**.
2. Go to **APIs & Services → Library**, search for **Google Ads API** and click **Enable**.
3. On the Google Ads API overview page, check the project's **access level** (Explorer, Basic
   or Standard). If it has none, apply for one there.

### 2. Set up the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen** (shown as **Google Auth Platform** in newer
   consoles).
2. Choose **External** (or **Internal** if you use Google Workspace), and fill in the app name
   and support email.
3. Under **Data access**, add the scope `https://www.googleapis.com/auth/adwords`.
4. Under **Audience**, add the Google account that has access to your Google Ads accounts as a
   test user, **or publish the app to production**. While the app is in *Testing*, refresh
   tokens expire after 7 days and you would have to sign in again every week. For your own
   login you don't need Google's verification; you'll just see an "unverified app" warning.

### 3. Create a Desktop OAuth client

1. Go to **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Desktop app**. Give it a name and click **Create**.
3. Copy the **client ID** and **client secret**; they go into `.env` in the next step.

### 4. Configure and install

```bash
cd google-ads-chatbot-poc
cp .env.example .env          # Windows: copy .env.example .env
```

Fill in `.env`:

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | yes | Free key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `GEMINI_MODEL` | no | Gemini model ID (default `gemini-3.8-flash`) |
| `GOOGLE_PROJECT_ID` | yes | Google Cloud project ID from step 1 |
| `GOOGLE_OAUTH_CLIENT_ID` | for step 5 | Desktop OAuth client ID from step 3 |
| `GOOGLE_OAUTH_CLIENT_SECRET` | for step 5 | Desktop OAuth client secret from step 3 |
| `GOOGLE_APPLICATION_CREDENTIALS` | no | Where the sign-in is saved (default `./credentials/credentials.json`) |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | no | Manager (MCC) account ID without dashes, to reach all its client accounts |
| `GOOGLE_ADS_DEFAULT_CUSTOMER_ID` | no | Account the chatbot uses when a question doesn't name one |
| `GOOGLE_ADS_MCP_COMMAND` | no | Full command that starts the MCP server, if the default doesn't suit you |

Create a virtual environment and install the Python dependencies:

```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Install a runner for the MCP server (pick one):

```bash
pip install --user pipx && pipx ensurepath     # pipx
# or install uv: https://docs.astral.sh/uv/getting-started/installation/
```

You don't need to install `google-ads-mcp` separately. The app starts it with
`pipx run --spec git+https://github.com/googleads/google-ads-mcp.git@v0.0.6 google-ads-mcp`, or
with `uvx --from git+https://github.com/googleads/google-ads-mcp.git@v0.0.6 google-ads-mcp` if
pipx isn't found. The version is pinned in `app/config.py` because the confirm flow depends on
how that version's write tools work. Both download and cache the server on first use (this can take a minute).

### 5. Sign in once

```bash
python scripts/auth.py
```

A browser window opens. Sign in with the Google account that can see your Google Ads accounts
and allow access. The script saves an ADC `authorized_user` file (client ID, client secret and
refresh token) to `credentials/credentials.json`. The app passes that path to the MCP server as
`GOOGLE_APPLICATION_CREDENTIALS`. The file is git-ignored; keep it private.

**Alternatives.** If you already use the Google Cloud CLI, you can skip this step and point
`GOOGLE_APPLICATION_CREDENTIALS` at the file created by
`gcloud auth application-default login --scopes https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform --client-id-file=client_secret.json`.
The official server also reads a client ID/secret directly from env vars
(`GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` / `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`), but only in its
remote HTTP mode, where each MCP client signs in through the server. This app runs the server
locally over stdio, so it uses the ADC file.

### 6. Test the connection, then run the app

```bash
python scripts/test_mcp.py
```

This starts the MCP server, prints its tools and calls `list_accessible_customers`. You should
see a list like `customers/1234567890`. Then start the app:

```bash
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. The terminal logs every tool call (name, arguments and duration).
`GET /health` lists the connected MCP tools. To let teammates on your network use it, run
`uvicorn app.main:app --host 0.0.0.0` and share `http://<your-ip>:8000`.

### 7. Example questions

- Which Google Ads accounts can I access?
- Spend, clicks, conversions by campaign last 7 days
- Top 10 keywords by cost with zero conversions last 30 days
- This month vs last month by campaign
- Show my top 5 campaigns by cost in the last 30 days, with CTR and CPA.
- How did account 1234567890 perform last week compared with the week before?
- Which ad groups in campaign "Brand – Search" had the most conversions this month?
- List the 10 keywords with the highest cost and no conversions in the last 30 days.
- What's the ROAS by campaign for last month?
- Which ads have the lowest CTR among those with more than 1,000 impressions in the last 14 days?
- What are the daily budgets of my enabled campaigns?

Changes (each one shows a Confirm card first):

- Create a label "Q4 Review" with color #FF9900.
- Add the label "Q4 Review" to my top 3 campaigns by cost last month.
- Remove the label "Old" from campaign "Brand – Search".
- Create a callout asset "Free shipping over $50".
- Create a sitelink asset "Contact us" pointing to https://example.com/contact.
- (Attach an image) Create an image asset from this file named "Holiday banner".
- (Attach a video) Upload this video to YouTube with the title "Spring promo".

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing required environment variables` | Fill in `.env` (copy from `.env.example`). |
| `No Google credentials found at ...` | Run `python scripts/auth.py` (step 5). |
| `auth.py` says no refresh token was returned | Remove the app's access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) and run it again. |
| `Could not find pipx or uvx` | Install pipx or uv; after `pipx ensurepath`, open a new terminal. Or set `GOOGLE_ADS_MCP_COMMAND`. |
| `invalid_grant` / `Token has been expired or revoked` | The sign-in expired (in *Testing* mode refresh tokens last 7 days). Run `scripts/auth.py` again, or publish the consent screen and then re-run it. |
| `invalid_client` | Wrong client ID/secret, or the client isn't a Desktop app. Fix `.env` and re-run `scripts/auth.py`. |
| Errors about the project, or `SERVICE_DISABLED` | Check that `GOOGLE_PROJECT_ID` is the project where the Google Ads API is enabled. |
| Access-level errors from the Google Ads API | Check the project's access level on the Google Ads API overview page in the Cloud console, and apply for Explorer/Basic access if needed. |
| `USER_PERMISSION_DENIED` | Set `GOOGLE_ADS_LOGIN_CUSTOMER_ID` to the manager account you use to reach the client account. |
| `Gemini API key was rejected` | Check `GEMINI_API_KEY` (create one at aistudio.google.com/apikey). |
| `Gemini free-tier rate limit reached` or `503 high demand` | Wait a minute, or switch `GEMINI_MODEL` (e.g. `gemini-3.5-flash-lite` or `gemini-2.5-flash`). |
| `Model ... was not found` | Check `GEMINI_MODEL`. |

## Notes and limits

- **Shared access, including writes.** Everyone using the chatbot sees what your Google login
  can see, and anyone who opens the page can create or remove labels, create assets and upload
  videos in those accounts (after clicking Confirm). There is no sign-in on the page, so don't
  expose it to the public internet. Set `labels`/`assets` to `false` in `tools_config.yaml` to
  turn writes off.
- **Limited writes.** Only labels, label associations, standalone assets and video uploads.
  Requests to pause campaigns or change bids or budgets get a "can't do that here" reply.
- **Video uploads block other requests.** The app sends one MCP call at a time, so other
  questions wait while a large video uploads (up to 30 minutes). Attachments are capped at 5 MB
  for images and 2 GB for videos. Files in `uploads/` are not deleted automatically.
- **Pending changes are kept in memory.** Restarting the server cancels any change waiting for
  confirmation. Ask again after a restart.
- **History lives in the browser.** Each person's page keeps their own conversation in a JS
  variable and sends it with each request. Reloading the page or clicking **New chat** starts
  over.
- **Large results are truncated** at 60,000 characters per tool call. The model is told when this
  happens so it can narrow the query.
- **Gemini free tier.** It costs nothing, but it has per-minute and per-day request limits
  shared by everyone using the app, can be slow or briefly unavailable at busy times, and each
  question can take several requests (one per tool step). Google states that free-tier prompts
  and responses **may be used to improve its products**, so your Google Ads data is shared on
  those terms. Enable billing in AI Studio (paid tier) if that's not acceptable. Free models at
  the time of writing: `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`,
  `gemini-2.5-pro`, `gemini-2.5-flash` (see the
  [pricing page](https://ai.google.dev/gemini-api/docs/pricing)).
- Recent conversion numbers can still change for several days after a click.
