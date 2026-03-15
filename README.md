# grötfluence image-describer

Automatic photo-to-blog pipeline for [grötfluence.se](https://grötfluence.se).

Send a photo to a Telegram bot → AI describes it → Claude writes a sarcastic Swedish caption → published to WordPress.

## Pipeline

```
Telegram photo
    ↓
qwen2.5vl:3b-gpu (frmwrk-ai, llama-vision-proxy:8082)  →  English visual description
    ↓
Claude Haiku (claude-haiku-4-5-20251001)  →  Swedish title + caption + meal category + ingredient tags
    ↓
Quality threshold: >1 unseen ingredient? → pause, ask /confirm or /cancel via Telegram
    ↓
WordPress REST API  →  published post with featured image
    ↓
Telegram reply with post URL (+ quality warnings if any)
```

## Setup

### Requirements

- Docker + Docker Compose
- Vision model endpoint (OpenAI-compatible, e.g. llama.cpp server with a vision model)
- Anthropic API key (for Claude Haiku)
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- WordPress site with REST API enabled and an Application Password

### Environment variables

Copy `.env.example` to `.env` and fill in:

```
ANTHROPIC_API_KEY=
WEB_PASSWORD=                # password for the web UI (username defaults to "martin")
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=     # random string — set when registering the webhook
TELEGRAM_ALLOWED_USER_ID=    # numeric Telegram user ID — get from @userinfobot
WP_USERNAME=
WP_APP_PASSWORD=             # WP Admin → Users → Profile → Application Passwords
```

Optional overrides (set in `docker-compose.yml` or `.env`):

```
VISION_BASE_URL=http://frmwrk-ai:8082/v1   # OpenAI-compatible vision endpoint
VISION_MODEL=qwen2.5vl:3b-gpu
WP_URL=https://grötfluence.se
```

### Run

```bash
docker compose up -d --build
```

App listens on port 4545.

### Register Telegram webhook

```bash
curl "https://api.telegram.org/bot{TOKEN}/setWebhook?url=https://your-domain.com/telegram/webhook&secret_token={WEBHOOK_SECRET}"
```

### Deploy to a server

```bash
bash deploy.sh
```

## WordPress requirements

- REST API must be enabled (Settings → Permalinks → set to anything except "Plain")
- Categories and tags are created automatically if they don't exist

## Telegram usage

- Send a photo — pipeline runs automatically
- Optionally add a caption to the photo for ingredient hints (e.g. "med banan")
- Meal category (frukost/lunch/fika/middag/kvällsgröt) is inferred from current Stockholm time
- Tags default to `havregröt`; blue tones → `blåbär`, red tones → `lingon`
- If >1 unseen ingredient is detected, the post is held and you are asked to `/confirm` or `/cancel`

## Telegram bot commands

| Command | Description |
|---------|-------------|
| `/confirm` | Publish a held post (triggered by unseen ingredient check) |
| `/cancel` | Discard a held post |
| `/status` | Show the last published post |
| `/title New title` | Update the title of the last post |
| `/caption New text` | Update the caption of the last post |
| `/tags tag1, tag2` | Update the tags of the last post |
| `/delete` | Unpublish the last post (sets to draft) |

## Web UI

The app also exposes a simple web UI at `/` for manual caption generation, protected by HTTP Basic Auth.

## Admin endpoints

All require HTTP Basic Auth.

| Endpoint | Description |
|----------|-------------|
| `POST /admin/reindex` | Refresh the quality checker index from WordPress |
| `POST /admin/backfill-vision` | Run all WP posts missing real vision descriptions through the vision model (idempotent, runs in background) |

### Backfill vision descriptions

To populate vision descriptions for existing posts:

```bash
curl -u martin:PASSWORD -X POST https://your-domain.com/admin/backfill-vision
# then watch progress:
docker logs -f <container> 2>&1 | grep backfill
```

## Quality checker

After each publish, `quality.py` checks for:
- Banned phrases (hardcoded list of overused expressions)
- English word leakage in title/caption/tags
- Previously unseen ingredient tags (>1 new = hold for confirmation)
- Caption similarity to recent posts (Jaccard similarity)
- Vision-tag coherence (does the description support the tags?)
- Meal type plausibility (is the time right for frukost/lunch/etc?)

Warnings are appended to the Telegram confirmation message.
