# grötfluence image-describer

Automatic photo-to-blog pipeline for [grötfluence.se](https://grötfluence.se).

Send a photo to a Telegram bot → AI describes it → text model writes a sarcastic Swedish caption → published to Directus CMS.

## Pipeline

```
Telegram photo
    ↓
qwen2.5vl:3b-gpu (frmwrk-ai, llama-vision-proxy:8082)  →  English visual description
    ↓
text model (mistral-small:24b or Claude)  →  Swedish title + caption + meal category + ingredient tags
    ↓
Quality threshold: >1 unseen ingredient? → pause, ask /confirm or /cancel via Telegram
    ↓
Directus REST API  →  published post with hero image, category, tags
    ↓
Telegram reply with post URL (+ quality warnings if any)
```

## Setup

### Requirements

- Docker + Docker Compose
- Vision model endpoint (OpenAI-compatible)
- Text model endpoint (OpenAI-compatible) or Anthropic API key
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Directus instance with a static API token

### Environment variables

Copy `.env.example` to `.env` and fill in:

```
WEB_PASSWORD=                # password for the web UI (username defaults to "martin")
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=     # random string — set when registering the webhook
TELEGRAM_ALLOWED_USER_ID=    # numeric Telegram user ID — get from @userinfobot
DIRECTUS_URL=                # e.g. https://cms.example.com
DIRECTUS_TOKEN=              # Directus static API token
BLOG_URL=                    # public blog URL, e.g. https://example.com (post URLs: /p/{slug})
```

Optional overrides:

```
VISION_BASE_URL=http://frmwrk-ai:8082/v1   # OpenAI-compatible vision endpoint
VISION_MODEL=qwen2.5vl:3b-gpu
TEXT_PROVIDER=openai                        # "openai" (local via TEXT_BASE_URL) or "anthropic"
TEXT_BASE_URL=http://frmwrk-ai:8080/v1     # used when TEXT_PROVIDER=openai
TEXT_MODEL=mistral-small:24b
ANTHROPIC_API_KEY=                          # required when TEXT_PROVIDER=anthropic
```

### Directus requirements

Collections required: `posts`, `categories`, `tags`, `posts_tags` (M2M junction).

`posts` fields: `id`, `status`, `published_at`, `title`, `slug`, `excerpt`, `body`, `hero_image` (uuid FK to directus_files), `category` (int FK), `tags` (M2M via posts_tags).

Categories and tags are created automatically if they don't exist. The `posts → posts_tags` relation should have `one_deselect_action: delete` so that deleting a post cascades to its junction rows.

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
| `/caption New text` | Update the excerpt and body of the last post |
| `/tags tag1, tag2` | Update the tags of the last post |
| `/delete` | Unpublish the last post (sets status to draft) |

## Web UI

The app also exposes a simple web UI at `/` for manual caption generation, protected by HTTP Basic Auth.

## Admin endpoints

All require HTTP Basic Auth.

| Endpoint | Description |
|----------|-------------|
| `POST /admin/reindex` | Refresh the quality checker index from Directus |
| `POST /admin/backfill-vision` | Run all posts missing vision descriptions through the vision model (idempotent, background) |

## Quality checker

After each publish, `quality.py` checks for:
- Banned phrases (hardcoded list of overused expressions)
- English word leakage in title/caption/tags
- Previously unseen ingredient tags (>1 new = hold for confirmation)
- Caption similarity to recent posts (Jaccard similarity)
- Vision-tag coherence (does the description support the tags?)
- Meal type plausibility (is the time right for frukost/lunch/etc?)

Warnings are appended to the Telegram confirmation message. The quality index is seeded from Directus on startup and refreshed hourly.
