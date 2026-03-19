# grötfluence image-describer

Automatic photo-to-blog pipeline for [grötfluence.se](https://grötfluence.se).

Send a photo to a Telegram bot → vision model describes it → text model writes a sarcastic Swedish caption → published to Directus CMS.

## Pipeline

```
Telegram photo
    ↓
Vision model (OpenAI-compatible)  →  English visual description
    ↓
Text model (OpenAI-compatible or Anthropic)  →  Swedish title + caption + meal category + ingredient tags
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
- OpenAI-compatible vision model endpoint
- OpenAI-compatible text model endpoint, or an Anthropic API key
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Directus instance with a static API token

### Environment variables

```
WEB_PASSWORD=                # password for the web UI (username defaults to "martin")
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=     # random string — set when registering the webhook
TELEGRAM_ALLOWED_USER_ID=    # numeric Telegram user ID — get from @userinfobot
DIRECTUS_URL=                # e.g. https://cms.example.com
DIRECTUS_TOKEN=              # Directus static API token
BLOG_URL=                    # public blog URL, e.g. https://example.com (posts at /p/{slug})
```

Optional overrides:

```
VISION_BASE_URL=             # OpenAI-compatible vision endpoint (default: http://frmwrk-ai:8082/v1)
VISION_MODEL=                # vision model name (default: qwen2.5vl:3b-gpu)
TEXT_PROVIDER=openai         # "openai" (default) or "anthropic"
TEXT_BASE_URL=               # OpenAI-compatible text endpoint (default: http://frmwrk-ai:8080/v1)
TEXT_MODEL=                  # text model name (default: mistral-small:24b)
ANTHROPIC_API_KEY=           # required when TEXT_PROVIDER=anthropic
```

### Directus requirements

Collections: `posts`, `categories`, `tags`, `posts_tags` (M2M junction).

`posts` fields: `id`, `status`, `published_at`, `title`, `slug`, `excerpt`, `body`, `hero_image` (uuid), `category` (int FK), `tags` (M2M via posts_tags).

Categories and tags are created automatically if they don't exist. Set `one_deselect_action: delete` on the `posts → posts_tags` relation so deleting a post cascades to its junction rows.

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

## Web UI

A simple web UI at `/` for manual caption generation, protected by HTTP Basic Auth.

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
