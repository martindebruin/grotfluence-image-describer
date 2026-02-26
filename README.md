# grötfluence image-describer

Automatic photo-to-blog pipeline for [grötfluence.se](https://grötfluence.se).

Send a photo to a Telegram bot → AI describes it → Claude writes a sarcastic Swedish caption → published to WordPress.

## Pipeline

```
Telegram photo
    ↓
llava-phi3 on a Raspberry Pi  →  English visual description
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
- Ollama running somewhere with `llava-phi3` loaded
- Anthropic API key
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

`WP_URL` defaults to `https://grötfluence.se`. Override in `docker-compose.yml` if needed.
`OLLAMA_URL` defaults to `http://ollama-host:11434`. Override to point at your Ollama instance.

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

## Web UI

The app also exposes a simple web UI at `/` for manual caption generation, protected by HTTP Basic Auth.

## Ollama / vision model

The vision model (`llava-phi3`) can run on any machine reachable from the Docker container. Set `OLLAMA_URL` and the `extra_hosts` entry in `docker-compose.yml` accordingly.
