# grötfluence image-describer

Automatic photo-to-blog pipeline for [grötfluence.se](https://grötfluence.se).

Send a photo to a Telegram bot → AI describes it → Claude writes a sarcastic Swedish caption → published to WordPress.

## Pipeline

```
Telegram photo
    ↓
llava-phi3 on Raspberry Pi 5  →  English visual description
    ↓
Claude Haiku  →  Swedish title + caption + meal category + ingredient tags
    ↓
WordPress REST API  →  published post with featured image
    ↓
Telegram reply with post URL
```

## Setup

### Requirements

- Docker + Docker Compose
- Ollama running somewhere with `llava-phi3` loaded (see [rpi-ai setup](#rpi-ai))
- Anthropic API key
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- WordPress site with REST API enabled and an Application Password

### Environment variables

Copy `.env.example` to `.env` and fill in:

```
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_ID=    # numeric Telegram user ID — get from @userinfobot
WP_USERNAME=
WP_APP_PASSWORD=             # WP Admin → Users → Profile → Application Passwords
```

`WP_URL` defaults to `https://grötfluence.se`. Override in `docker-compose.yml` if needed.

### Run locally

```bash
docker compose up -d --build
```

App listens on port 4545.

### Register Telegram webhook

```bash
curl "https://api.telegram.org/bot{TOKEN}/setWebhook?url=https://your-domain.com/telegram/webhook"
```

### Deploy to server

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

## Web UI

The app also exposes a simple web UI at `/` for manual caption generation (original functionality).

## rpi-ai

The vision model runs on a Raspberry Pi 5 with Hailo-10H accelerator via Ollama. The container reaches it via Tailscale hostname `rpi-ai`. The `extra_hosts` entry in `docker-compose.yml` maps this to the Pi's Tailscale IP.
