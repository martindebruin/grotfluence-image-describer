import os
import base64
import imghdr
from datetime import date, datetime
from zoneinfo import ZoneInfo
import httpx
import anthropic
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://rpi-ai:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "llava-phi3")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TEXT_MODEL = os.getenv("TEXT_MODEL", "claude-haiku-4-5-20251001")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID")
WP_URL = os.getenv("WP_URL", "https://grötfluence.se")
WP_USERNAME = os.getenv("WP_USERNAME")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

MEDIA_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}

SYSTEM_PROMPT = (
    "Du är en sarkastisk svensk grötbloggare som driver bloggen grötfluence.se. "
    "Du skriver korta, självförnedranande bildtexter om din dagliga gröt. "
    "Skriv på äkta, vardaglig svenska — inte översatt engelska. Använd svenska idiom och ett naturligt svenskt tonfall. "
    "Exempel på rätt ton: 'Med chiafrön som en jävla hipster.' eller 'Tredje dagen i rad. Ingen har frågat.' "
    "Håll det till 1-2 meningar. Torr humor, ingen uppmuntran, inga hashtags, inga emojis."
)

POST_SYSTEM_PROMPT = (
    "Du är en sarkastisk svensk grötbloggare som driver bloggen grötfluence.se. "
    "Du skriver korta, självförnedranande inlägg om din dagliga gröt. "
    "Skriv på äkta, vardaglig svenska — inte översatt engelska. "
    "Exempel på rätt ton för titel: 'Med chiafrön som en jävla hipster' eller 'Tredje dagen i rad' "
    "Exempel på rätt ton för bildtext: 'Ingen har frågat. Jag gjorde det ändå.' "
    "Svara ALLTID med giltig JSON och ingenting annat. Inga kommentarer, ingen förklaring.\n\n"
    "JSON-schemat du ska returnera:\n"
    '{"title": "2-5 ord", "caption": "1-2 meningar", "meal_type": "frukost|lunch|fika|middag|kvällsgröt", "tags": ["tagg1", "tagg2"]}\n\n'
    "Regler för meal_type baserat på tid:\n"
    "  frukost: 05-10, lunch: 10-14, fika: 14-16, middag: 16-21, kvällsgröt: 21-05\n\n"
    "Regler för tags:\n"
    "  - Inkludera alltid 'havregröt' om inte användarens notat anger något annat.\n"
    "  - Lägg till 'blåbär' om bilden innehåller blå/lila färger.\n"
    "  - Lägg till 'lingon' om bilden innehåller röda bär.\n"
    "  - Lägg till andra relevanta ingredienser om de är tydliga i bilden eller notatet.\n"
    "  - Max 4 taggar."
)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("/app/index.html") as f:
        return f.read()


@app.post("/describe")
async def describe(file: UploadFile = File(...)):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set")

    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large (max 20MB)")

    kind = imghdr.what(None, h=data)
    if not kind and (file.content_type or "").startswith("image/"):
        kind = file.content_type.split("/")[1]
    if kind not in MEDIA_TYPES and kind not in MEDIA_TYPES.values():
        raise HTTPException(status_code=415, detail=f"Unsupported image type: {kind}")

    img_b64 = base64.b64encode(data).decode()

    # Step 1: llava-phi3 describes the image in English (local)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0)) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": VISION_MODEL,
                    "stream": False,
                    "prompt": "Describe what you see in this image. Focus on the food: appearance, colors, toppings, texture, presentation. Be objective and specific.",
                    "images": [img_b64],
                },
            )
            resp.raise_for_status()
            visual_description = resp.json()["response"]
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"Cannot reach Ollama at {OLLAMA_URL}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Ollama error: {e.response.text[:200]}")

    # Step 2: Claude writes the sarcastic Swedish caption
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=TEXT_MODEL,
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Dagens datum: {date.today().strftime('%A %d %B %Y')}.\nHär är en beskrivning av dagens gröt: {visual_description}\n\nSkriv en bildtext till bloggen.",
            }
        ],
    )

    caption = response.content[0].text
    return JSONResponse({"description": caption})


# --- Telegram helpers ---

async def download_telegram_photo(file_id: str) -> bytes:
    """Download the highest-resolution photo from Telegram and return raw bytes."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id},
        )
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]

        img_resp = await client.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        )
        img_resp.raise_for_status()
        return img_resp.content


async def send_telegram_message(chat_id: int, text: str) -> None:
    """Send a text message to a Telegram chat."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )


async def generate_post_content(description: str, current_time: str, telegram_caption: str = "") -> dict:
    """Call Claude once and get back title, caption, meal_type and tags in Swedish."""
    import json

    user_note = f"\nAnvändarens notat: {telegram_caption}" if telegram_caption.strip() else ""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=TEXT_MODEL,
        max_tokens=300,
        system=POST_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Aktuell tid: {current_time}.\n"
                    f"Bildbeskrivning: {description}"
                    f"{user_note}\n\n"
                    "Returnera JSON."
                ),
            }
        ],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


async def get_or_create_wp_term(client: httpx.AsyncClient, taxonomy: str, name: str, auth_header: dict) -> int:
    """Return WordPress term ID for the given taxonomy and name, creating it if it doesn't exist."""
    search_resp = await client.get(
        f"{WP_URL}/wp-json/wp/v2/{taxonomy}",
        headers=auth_header,
        params={"search": name, "per_page": 5},
    )
    search_resp.raise_for_status()
    for term in search_resp.json():
        if term["name"].lower() == name.lower():
            return term["id"]

    create_resp = await client.post(
        f"{WP_URL}/wp-json/wp/v2/{taxonomy}",
        headers={**auth_header, "Content-Type": "application/json"},
        json={"name": name},
    )
    create_resp.raise_for_status()
    return create_resp.json()["id"]


async def post_to_wordpress(image_bytes: bytes, filename: str, title: str, caption: str, meal_type: str, tags: list[str]) -> str:
    """Upload image to WP media library, create post with categories and tags, return the post URL."""
    credentials = base64.b64encode(
        f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()
    ).decode()
    auth_header = {"Authorization": f"Basic {credentials}"}

    # Detect content type
    kind = imghdr.what(None, h=image_bytes) or "jpeg"
    content_type = MEDIA_TYPES.get(kind, "image/jpeg")

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0)) as client:
        # Resolve category and tags to IDs (create if missing)
        category_id = await get_or_create_wp_term(client, "categories", meal_type, auth_header)
        tag_ids = []
        for tag in tags:
            tag_ids.append(await get_or_create_wp_term(client, "tags", tag, auth_header))

        # 1. Upload media
        media_resp = await client.post(
            f"{WP_URL}/wp-json/wp/v2/media",
            headers={
                **auth_header,
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": content_type,
            },
            content=image_bytes,
        )
        media_resp.raise_for_status()
        media_id = media_resp.json()["id"]

        # 2. Create post
        post_resp = await client.post(
            f"{WP_URL}/wp-json/wp/v2/posts",
            headers={**auth_header, "Content-Type": "application/json"},
            json={
                "title": title,
                "content": caption,
                "status": "publish",
                "featured_media": media_id,
                "categories": [category_id],
                "tags": tag_ids,
            },
        )
        post_resp.raise_for_status()
        return post_resp.json()["link"]


async def _process_telegram_photo(chat_id: int, file_id: str, filename: str, telegram_caption: str = "") -> None:
    """Background task: run full pipeline and reply with the post URL."""
    try:
        await send_telegram_message(chat_id, "Bearbetar... ett ögonblick.")

        # Download photo
        image_bytes = await download_telegram_photo(file_id)

        # Vision description
        img_b64 = base64.b64encode(image_bytes).decode()
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0)) as client:
            resp = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": VISION_MODEL,
                    "stream": False,
                    "prompt": "Describe what you see in this image. Focus on the food: appearance, colors, toppings, texture, presentation. Be objective and specific.",
                    "images": [img_b64],
                },
            )
            resp.raise_for_status()
            description = resp.json()["response"]

        # Claude: title, caption, meal_type, tags
        now_stockholm = datetime.now(ZoneInfo("Europe/Stockholm"))
        current_time = now_stockholm.strftime("%H:%M")
        post_content = await generate_post_content(description, current_time, telegram_caption)
        title = post_content["title"]
        caption = post_content["caption"]
        meal_type = post_content.get("meal_type", "frukost")
        tags = post_content.get("tags", ["havregröt"])

        # WordPress
        post_url = await post_to_wordpress(image_bytes, filename, title, caption, meal_type, tags)

        await send_telegram_message(chat_id, f"Publicerat! {post_url}")

    except Exception as e:
        await send_telegram_message(chat_id, f"Fel: {e}")


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive Telegram updates. Respond immediately; process photo in background."""
    update = await request.json()

    message = update.get("message") or update.get("channel_post")
    if not message:
        return JSONResponse({"ok": True})

    # Security: only accept messages from the allowed user
    sender_id = str(message.get("from", {}).get("id", ""))
    if sender_id != TELEGRAM_ALLOWED_USER_ID:
        return JSONResponse({"ok": True})

    photos = message.get("photo")
    if not photos:
        return JSONResponse({"ok": True})

    chat_id = message["chat"]["id"]
    # Telegram sends multiple sizes; pick the largest (last in list)
    best_photo = photos[-1]
    file_id = best_photo["file_id"]
    filename = f"grot_{message.get('message_id', 'photo')}.jpg"
    telegram_caption = message.get("caption", "")

    background_tasks.add_task(_process_telegram_photo, chat_id, file_id, filename, telegram_caption)
    return JSONResponse({"ok": True})
