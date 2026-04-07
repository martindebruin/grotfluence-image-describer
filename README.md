# grötfluence image-describer

Automatic photo-to-blog pipeline for [grötfluence.se](https://grötfluence.se).

Send a photo to a Telegram bot → vision model describes it → text model writes a sarcastic Swedish caption → published to Directus CMS.

Community posts from Mastodon and Bluesky are monitored in parallel — verified porridge photos are saved and rewarded with a $GRÖT airdrop.

## Pipelines

### Telegram (personal)

```
Telegram photo
    ↓
Vision model (fine-tuned Qwen2.5-VL-3B)  →  Swedish porridge description
    ↓
Text model  →  Swedish title + caption + meal category + ingredient tags
    ↓
Quality threshold: >1 unseen ingredient? → pause, ask /confirm or /cancel
    ↓
Directus REST API  →  published post with hero image, category, tags
    ↓
Telegram reply with post URL (+ quality warnings if any)
```

### Community (Mastodon + Bluesky)

```
Mention @oat_tracker with a photo
    ↓
Vision sanity check (fine-tuned model) — "gröt" in description?
    ↓ yes                              ↓ no
Saved to community.db          Reply: "Det där såg tyvärr inte ut som gröt."
    ↓
Airdrop 25 $GRÖT to registered wallet (if wallet registered)
```

**Wallet registration:** mention or DM `@oat_tracker` with your Solana wallet address (no image). Reply confirms registration.

## Setup

### Requirements

- Docker + Docker Compose
- OpenAI-compatible vision model endpoint (fine-tuned Qwen2.5-VL-3B recommended)
- OpenAI-compatible text model endpoint, or an Anthropic API key
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Directus instance with API token
- Mastodon bot account + access token
- Bluesky bot account + app password
- grot-social airdrop microservice (see below)

### Environment variables

```
WEB_PASSWORD=                # password for the web UI 
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=     # random string — set when registering the webhook
TELEGRAM_ALLOWED_USER_ID=    
DIRECTUS_URL=                # e.g. https://cms.example.com
DIRECTUS_TOKEN=              # Directus API token
BLOG_URL=                    # public blog URL, e.g. https://example.com (posts at /p/{slug})

# Mastodon community listener
MASTODON_INSTANCE=https://mastodon.social
MASTODON_ACCESS_TOKEN=

# Bluesky community listener
BSKY_HANDLE=                 
BSKY_APP_PASSWORD=

# $GRÖT airdrop microservice
AIRDROP_URL=                 
AIRDROP_SECRET=             
AIRDROP_AMOUNT=25
```

Optional overrides:

```
VISION_BASE_URL=             # OpenAI-compatible vision endpoint 
VISION_MODEL=                # vision model name (default: qwen2.5vl:3b-gpu)
TEXT_PROVIDER=openai         # "openai" (default) or "anthropic"
TEXT_BASE_URL=               # OpenAI-compatible text endpoint
TEXT_MODEL=                  # text model name (default: mistral-small:24b)
ANTHROPIC_API_KEY=           # required when TEXT_PROVIDER=anthropic
POLL_INTERVAL=60             # social listener poll interval in seconds
```

## Vision model

The vision step uses a **fine-tuned Qwen2.5-VL-3B-Instruct** model (LoRA, trained on 550+ real porridge photos). It outputs Swedish ingredient descriptions directly — `"Detta är havregröt med lingon och banan."` — rather than generic English descriptions. This feeds the text model more accurate ingredient names and is used as the sanity check for community posts (description must contain "gröt").

The fine-tuned model is served by `llama-server-vision` on `dedicated machine`. Requests go through `llama-vision-proxy` on `:8082` which converts WebP/BMP/etc to JPEG before forwarding.

Training pipeline is at `/home/martin/claude/training/` on `dedicated machine`. A cron job runs every Sunday at 03:00 and retrains automatically when ≥50 new posts have been published since the last training run.

## Airdrop microservice

Community posts that pass the vision check trigger a $GRÖT airdrop via a separate microservice (`grot-social`) running on separate server. 

`POST /airdrop` — protected by `x-secret` header. Body: `{"wallet": "...", "amount": 25}`.

## Data

- `quality.db` — quality checker index (post cache, vision descriptions, tag history)
- `community.db` — community posts from Mastodon/Bluesky + wallet registrations

Both SQLite databases are persisted at `./data/` (volume-mounted).

### Directus requirements

Collections: `posts`, `categories`, `tags`, `posts_tags` (M2M junction).

`posts` fields: `id`, `status`, `published_at`, `title`, `slug`, `excerpt`, `body`, `hero_image` (uuid), `category` (int FK), `tags` (M2M via posts_tags).

Categories and tags are created automatically if they don't exist. Set `one_deselect_action: delete` on the `posts → posts_tags` relation so deleting a post cascades to its junction rows.

Slugs are checked for uniqueness before posting. If a slug already exists (e.g. same title as a past post), a `-2`, `-3`, … suffix is appended automatically.

### Run

```bash
docker compose up -d --build
```

App listens on port 4545.

### Register Telegram webhook

```bash
curl "https://api.telegram.org/bot{TOKEN}/setWebhook?url=https://your-domain.com/telegram/webhook&secret_token={WEBHOOK_SECRET}"
```

### Deploy

```bash
bash deploy.sh
```

## Telegram usage

- Send a photo — the pipeline runs automatically
- Optionally add a caption to the photo as an ingredient hint (e.g. "med banan")
- Meal category (frukost/lunch/fika/middag/kvällsgröt) is inferred from Stockholm time
- If >1 previously unseen ingredient tag is detected, the post is held pending `/confirm`

## Telegram bot commands

| Command | Description |
|---------|-------------|
| `/confirm` | Publish a held post |
| `/cancel` | Discard a held post |
| `/status` | Show the last published post |
| `/title New title` | Update the title of the last post |
| `/caption New text` | Update the caption of the last post |
| `/tags tag1, tag2` | Replace the tags of the last post |
| `/delete` | Unpublish the last post (sets to draft) |



## Admin endpoints

All require HTTP Basic Auth.

| Endpoint | Description |
|----------|-------------|
| `POST /admin/reindex` | Rebuild the quality checker index from Directus |
| `POST /admin/backfill-vision` | Run posts missing vision descriptions through the vision model (background, idempotent) |

## Quality checker

After each publish, `quality.py` runs a set of checks and appends any warnings to the Telegram reply:

- **Banned phrases** — hardcoded list of overused expressions
- **English leakage** — English words in title, caption, or tags
- **Tag novelty** — flags previously unseen ingredient tags (>1 new triggers a hold)
- **Caption similarity** — Jaccard similarity against the 30 most recent posts
- **Vision-tag coherence** — checks that tags are supported by the vision description
- **Meal type plausibility** — validates the meal type against the current Stockholm time

The index is loaded from a local SQLite database (`/app/data/quality.db`), seeded from Directus on startup and refreshed hourly.

## Logging

Pipeline errors are logged to stdout via Python's `logging` module and appear in `docker compose logs`. HTTP errors include the response body; unexpected exceptions include a full stack trace. Both are also sent to Telegram.
