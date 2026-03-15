import asyncio
import os
import base64
import imghdr
from contextlib import asynccontextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo
import httpx
import secrets
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from quality import QualityChecker

VISION_BASE_URL = os.getenv("VISION_BASE_URL", "http://frmwrk-ai:8082/v1")
VISION_MODEL = os.getenv("VISION_MODEL", "qwen2.5vl:3b-gpu")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

WEB_USERNAME = os.getenv("WEB_USERNAME", "martin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
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
    "Du skriver korta bildtexter om din dagliga gröt. "
    "Skriv på äkta, vardaglig svenska — inte översatt engelska. Använd svenska idiom och ett naturligt svenskt tonfall. "
    "FÖRBJUDNA FRASER — använd aldrig: 'Ingen har frågat', 'Jag gjorde det ändå', 'Hepp', 'Jaha', 'Dags igen'. "
    "Välj ett NYTT angreppssätt varje gång från t.ex.: sensorisk beskrivning (temperatur, konsistens, doft), "
    "väderrelaterad koppling, kroppskänsla, popkulturreferens på svenska, absurd metafor, konstigt stolt, "
    "existentiell resignation, andra person ('du vet den känslan när...'), milstolpskommentar. "
    "Håll det till 1-2 meningar. Ingen uppmuntran, inga hashtags, inga emojis."
)

POST_SYSTEM_PROMPT = (
    "HÅRT KRAV — läs detta innan du skriver något:\n"
    "Dessa fraser och titlar är BANNLYSTA. De har använts hundratals gånger redan. "
    "Om du skriver någon av dessa misslyckas du med uppgiften:\n"
    "  • 'som en jävla hipster'\n"
    "  • 'som en duktig person'\n"
    "  • 'Ingen har frågat'\n"
    "  • 'Jag gjorde det ändå' / 'Ingen frågade'\n"
    "  • 'Lunch' (som hel titel)\n"
    "  • 'Hepp' / 'Jaha' / 'Hej hej' / 'Dags igen' / 'Mer' / 'Äntligen'\n"
    "  • '[veckodag] igen' (t.ex. 'Måndag igen', 'Tisdag igen')\n"
    "  • 'Ny vecka samma gröt'\n"
    "  • 'granola' (det heter alltid 'gröt')\n\n"
    "Du är en sarkastisk svensk grötbloggare som driver bloggen grötfluence.se. "
    "Skriv på äkta, vardaglig svenska — inte översatt engelska. "
    "Alla ord i title och caption måste vara på svenska, även ingrediensnamn "
    "(t.ex. 'chiafrön' inte 'chia seeds', 'blåbär' inte 'blueberries').\n\n"
    "Välj ett av dessa angreppssätt — välj ett du inte nyligen använt:\n"
    "  • Sensoriskt: konsistens, temperatur, doft — konkret och oväntat\n"
    "  • Väder/årstid: koppla gröten till vad som händer utomhus\n"
    "  • Kroppskänsla: tröttheten, hungern, mättheten, regret\n"
    "  • Milstolpe: hur länge detta pågått, dagar, säsonger\n"
    "  • Andra person: tilltala ett imaginärt vittne eller läsaren\n"
    "  • Absurd metafor: jämför med något helt orelaterat\n"
    "  • Popkulturreferens: parafrasera film/låt/tv men gröt-anpassad\n"
    "  • Pseudovetenskaplig: gröten som experiment eller intervention\n"
    "  • Oväntad stolthet: äkta entusiasm för något som inte förtjänar det\n\n"
    "Titeln ska vara specifik — inte bara konstatera veckodagen eller måltiden. "
    "Bildtexten ska tillföra något nytt utöver titeln.\n\n"
    "Kontrollera svaret innan du skickar: innehåller det någon bannlyst fras? Börja om i så fall.\n\n"
    "Svara ALLTID med giltig JSON och ingenting annat.\n\n"
    "JSON-schemat:\n"
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


_security = HTTPBasic()
_quality_checker: QualityChecker | None = None
_pending_post: dict | None = None


@asynccontextmanager
async def lifespan(app):
    global _quality_checker
    if WP_USERNAME and WP_APP_PASSWORD:
        _quality_checker = QualityChecker(
            wp_url=WP_URL,
            wp_username=WP_USERNAME,
            wp_app_password=WP_APP_PASSWORD,
        )
        await _quality_checker.ensure_db()
        await _quality_checker.refresh_index(force=True)
    yield


app = FastAPI(lifespan=lifespan)


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    ok = (
        WEB_PASSWORD
        and secrets.compare_digest(credentials.username.encode(), WEB_USERNAME.encode())
        and secrets.compare_digest(credentials.password.encode(), WEB_PASSWORD.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/", response_class=HTMLResponse)
async def index(_: None = Depends(require_auth)):
    with open("/app/index.html") as f:
        return f.read()


@app.post("/describe")
async def describe(file: UploadFile = File(...), _: None = Depends(require_auth)):
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large (max 20MB)")

    kind = imghdr.what(None, h=data)
    if not kind and (file.content_type or "").startswith("image/"):
        kind = file.content_type.split("/")[1]
    if kind not in MEDIA_TYPES and kind not in MEDIA_TYPES.values():
        raise HTTPException(status_code=415, detail=f"Unsupported image type: {kind}")

    img_b64 = base64.b64encode(data).decode()

    kind = kind or "jpeg"
    mime = MEDIA_TYPES.get(kind, "image/jpeg")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=300.0)) as client:
            # Step 1: vision model describes the image
            resp = await client.post(
                f"{VISION_BASE_URL}/chat/completions",
                json={
                    "model": VISION_MODEL,
                    "stream": False,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "Describe what you see in this image. Focus on the food: appearance, colors, toppings, texture, presentation. Be objective and specific."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    ]}],
                },
            )
            resp.raise_for_status()
            visual_description = resp.json()["choices"][0]["message"]["content"]

            # Step 2: text model writes the sarcastic Swedish caption
            resp2 = await client.post(
                f"{TEXT_BASE_URL}/chat/completions",
                json={
                    "model": TEXT_MODEL,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"Dagens datum: {date.today().strftime('%A %d %B %Y')}.\nHär är en beskrivning av dagens gröt: {visual_description}\n\nSkriv en bildtext till bloggen."},
                    ],
                },
            )
            resp2.raise_for_status()
            caption = resp2.json()["choices"][0]["message"]["content"]

    except httpx.ConnectError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach model server: {e}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Model server error: {e.response.text[:200]}")

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
    """Call Claude Haiku to get title, caption, meal_type and tags in Swedish."""
    import json
    import anthropic

    user_note = f"\nAnvändarens notat: {telegram_caption}" if telegram_caption.strip() else ""

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=POST_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": (
            f"Aktuell tid: {current_time}.\n"
            f"Bildbeskrivning: {description}"
            f"{user_note}\n\n"
            "Returnera JSON."
        )}],
    )
    raw = msg.content[0].text.strip()
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


async def post_to_wordpress(image_bytes: bytes, filename: str, title: str, caption: str, meal_type: str, tags: list[str]) -> tuple[str, int]:
    """Upload image to WP media library, create post with categories and tags, return (post URL, post ID)."""
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
        post_data = post_resp.json()
        return post_data["link"], post_data["id"]


async def update_wordpress_post(post_id: int, **fields) -> dict:
    """PATCH a WordPress post. Accepts title=, content=, tags=[ids], status=."""
    credentials = base64.b64encode(
        f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()
    ).decode()
    auth_header = {"Authorization": f"Basic {credentials}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=60.0)) as client:
        resp = await client.patch(
            f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
            headers=auth_header,
            json=fields,
        )
        resp.raise_for_status()
        return resp.json()


async def _publish_and_notify(
    chat_id: int,
    image_bytes: bytes,
    filename: str,
    title: str,
    caption: str,
    meal_type: str,
    tags: list[str],
    description: str,
    now_stockholm: datetime,
) -> None:
    """Upload to WordPress, run quality checks, and send Telegram confirmation."""
    post_url, wp_post_id = await post_to_wordpress(image_bytes, filename, title, caption, meal_type, tags)

    if _quality_checker:
        report = await _quality_checker.check(
            title=title,
            caption=caption,
            meal_type=meal_type,
            tags=tags,
            vision_description=description,
            current_time=now_stockholm,
        )
        await _quality_checker.store_vision_description(wp_post_id, description)
        await _quality_checker.store_post(
            wp_post_id=wp_post_id,
            title=title,
            caption=caption,
            tags=tags,
            meal_type=meal_type,
            published_at=now_stockholm,
        )
        if report.has_issues():
            msg = f"Publicerat! {post_url}\n\n⚠️ Kvalitetsvarningar:\n{report.telegram_summary()}"
        else:
            msg = f"Publicerat! {post_url}"
    else:
        msg = f"Publicerat! {post_url}"

    await send_telegram_message(chat_id, msg)


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
                f"{VISION_BASE_URL}/chat/completions",
                json={
                    "model": VISION_MODEL,
                    "stream": False,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "Describe what you see in this image. Focus on the food: appearance, colors, toppings, texture, presentation. Be objective and specific."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    ]}],
                },
            )
            resp.raise_for_status()
            description = resp.json()["choices"][0]["message"]["content"]

        # Local text model: title, caption, meal_type, tags
        now_stockholm = datetime.now(ZoneInfo("Europe/Stockholm"))
        current_time = now_stockholm.strftime("%H:%M")
        post_content = await generate_post_content(description, current_time, telegram_caption)
        title = post_content["title"]
        caption = post_content["caption"]
        meal_type = post_content.get("meal_type", "frukost")
        tags = post_content.get("tags", ["havregröt"])

        # Quality threshold: require confirmation if > 1 previously unseen ingredient
        if _quality_checker:
            new_tags = _quality_checker.get_new_tags(tags)
            if len(new_tags) > 1:
                global _pending_post
                _pending_post = {
                    "image_bytes": image_bytes,
                    "filename": filename,
                    "title": title,
                    "caption": caption,
                    "meal_type": meal_type,
                    "tags": tags,
                    "description": description,
                    "now_stockholm": now_stockholm,
                    "chat_id": chat_id,
                }
                await send_telegram_message(chat_id, (
                    f"⚠️ {len(new_tags)} okända ingredienser: {', '.join(new_tags)}\n\n"
                    f"Titel: {title}\n"
                    f"Bildtext: {caption}\n"
                    f"Taggar: {', '.join(tags)}\n\n"
                    "/confirm för att publicera, /cancel för att avbryta."
                ))
                return

        await _publish_and_notify(chat_id, image_bytes, filename, title, caption, meal_type, tags, description, now_stockholm)

    except Exception as e:
        await send_telegram_message(chat_id, f"Fel: {e}")


async def _handle_telegram_command(chat_id: int, text: str) -> None:
    """Handle /title, /caption, /tags, /delete, /status, /confirm, /cancel bot commands."""
    global _pending_post

    parts = text.split(None, 1)
    command = parts[0].lower()

    if command == "/confirm":
        if not _pending_post:
            await send_telegram_message(chat_id, "Inget väntande inlägg.")
            return
        p = _pending_post
        _pending_post = None
        try:
            await _publish_and_notify(
                p["chat_id"], p["image_bytes"], p["filename"],
                p["title"], p["caption"], p["meal_type"], p["tags"],
                p["description"], p["now_stockholm"],
            )
        except Exception as e:
            await send_telegram_message(chat_id, f"Fel vid publicering: {e}")
        return

    if command == "/cancel":
        if not _pending_post:
            await send_telegram_message(chat_id, "Inget väntande inlägg.")
            return
        _pending_post = None
        await send_telegram_message(chat_id, "Avbrutet.")
        return

    if not _quality_checker:
        await send_telegram_message(chat_id, "Kvalitetskontroll inte aktiv.")
        return

    last = await _quality_checker.load_last_post()
    if not last and text.strip() != "/status":
        await send_telegram_message(chat_id, "Inget senaste inlägg att redigera.")
        return

    import json as _json
    import re as _re

    args = parts[1].strip() if len(parts) > 1 else ""

    if command == "/status":
        if not last:
            await send_telegram_message(chat_id, "Inget senaste inlägg.")
            return
        tags = _json.loads(last["tags"]) if last["tags"] else []
        tags_str = ", ".join(tags) if tags else "(inga)"
        post_url = f"{WP_URL}/?p={last['wp_post_id']}"
        msg = (
            f"Senaste inlägg: \"{last['title']}\"\n"
            f"Bildtext: {last['caption']}\n"
            f"Taggar: {tags_str}\n"
            f"{post_url}"
        )
        await send_telegram_message(chat_id, msg)
        return

    wp_post_id = last["wp_post_id"]

    if command == "/title":
        if not args:
            await send_telegram_message(chat_id, "Ange ny titel: /title Ny titel")
            return
        await update_wordpress_post(wp_post_id, title=args)
        await _quality_checker.update_last_post(title=args)
        await send_telegram_message(chat_id, "✓ Titel uppdaterad.")

    elif command == "/caption":
        if not args:
            await send_telegram_message(chat_id, "Ange ny rubrik: /caption Ny rubrik")
            return
        await update_wordpress_post(wp_post_id, title=args)
        await _quality_checker.update_last_post(caption=args)
        await send_telegram_message(chat_id, "✓ Rubrik uppdaterad.")

    elif command == "/tags":
        if not args:
            await send_telegram_message(chat_id, "Ange taggar: /tags havregröt, blåbär")
            return
        new_tags = [t.strip() for t in args.split(",") if t.strip()]
        credentials = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
        auth_header = {"Authorization": f"Basic {credentials}"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            tag_ids = []
            for tag in new_tags:
                tag_ids.append(await get_or_create_wp_term(client, "tags", tag, auth_header))
        await update_wordpress_post(wp_post_id, tags=tag_ids)
        await _quality_checker.update_last_post(tags=_json.dumps(new_tags))
        await send_telegram_message(chat_id, f"✓ Taggar uppdaterade: {', '.join(new_tags)}")

    elif command == "/delete":
        await update_wordpress_post(wp_post_id, status="draft")
        await send_telegram_message(chat_id, "✓ Inlägg avpublicerat (draft).")

    else:
        await send_telegram_message(chat_id, f"Okänt kommando: {command}\nTillgängliga: /title, /caption, /tags, /delete, /status, /confirm, /cancel")


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive Telegram updates. Respond immediately; process photo in background."""
    if TELEGRAM_WEBHOOK_SECRET:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(token, TELEGRAM_WEBHOOK_SECRET):
            return JSONResponse({"ok": False}, status_code=401)

    update = await request.json()

    message = update.get("message") or update.get("channel_post")
    if not message:
        return JSONResponse({"ok": True})

    # Security: only accept messages from the allowed user
    sender_id = str(message.get("from", {}).get("id", ""))
    if sender_id != TELEGRAM_ALLOWED_USER_ID:
        return JSONResponse({"ok": True})

    chat_id = message["chat"]["id"]

    # Handle text commands
    text = message.get("text", "")
    if text.startswith("/"):
        background_tasks.add_task(_handle_telegram_command, chat_id, text)
        return JSONResponse({"ok": True})

    photos = message.get("photo")
    if not photos:
        return JSONResponse({"ok": True})

    # Telegram sends multiple sizes; pick the largest (last in list)
    best_photo = photos[-1]
    file_id = best_photo["file_id"]
    filename = f"grot_{message.get('message_id', 'photo')}.jpg"
    telegram_caption = message.get("caption", "")

    background_tasks.add_task(_process_telegram_photo, chat_id, file_id, filename, telegram_caption)
    return JSONResponse({"ok": True})


async def _run_vision_backfill() -> None:
    """Fetch all WP posts, run those missing real vision descriptions through the vision model."""
    import aiosqlite
    from datetime import timezone

    db_path = "/app/data/quality.db"

    # Find post IDs that have no description or only a synthetic one
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT p.wp_post_id
            FROM post_cache p
            LEFT JOIN vision_descriptions v ON p.wp_post_id = v.wp_post_id
            WHERE v.description IS NULL
               OR v.description LIKE 'Ingredienser:%'
               OR v.description = 'Okänd maträtt.'
            ORDER BY p.published_at
        """) as cursor:
            post_ids = [row["wp_post_id"] async for row in cursor]

    if not post_ids:
        print("[backfill] Nothing to do — all posts already have vision descriptions.")
        return

    print(f"[backfill] {len(post_ids)} posts to process.")

    credentials = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
    auth_header = {"Authorization": f"Basic {credentials}"}

    # Fetch featured media URLs for all posts in one paginated call using _embed
    async with httpx.AsyncClient(timeout=60.0) as client:
        media_url: dict[int, str] = {}
        page = 1
        while True:
            resp = await client.get(
                f"{WP_URL}/wp-json/wp/v2/posts",
                headers=auth_header,
                params={"per_page": 100, "page": page, "_embed": "wp:featuredmedia", "_fields": "id,featured_media,_links,_embedded"},
            )
            if resp.status_code == 400:
                break
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for post in batch:
                pid = post["id"]
                if pid not in post_ids:
                    continue
                embedded = post.get("_embedded", {})
                featured = embedded.get("wp:featuredmedia", [])
                if featured and featured[0].get("source_url"):
                    media_url[pid] = featured[0]["source_url"]
            total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
            if page >= total_pages:
                break
            page += 1

    print(f"[backfill] Found featured images for {len(media_url)}/{len(post_ids)} posts.")

    processed = skipped = errors = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        for wp_post_id in post_ids:
            url = media_url.get(wp_post_id)
            if not url:
                skipped += 1
                continue
            try:
                img_resp = await client.get(url)
                img_resp.raise_for_status()
                img_b64 = base64.b64encode(img_resp.content).decode()

                # Retry up to 3 times with backoff on 5xx errors
                description = None
                for attempt in range(3):
                    try:
                        vision_resp = await client.post(
                            f"{VISION_BASE_URL}/chat/completions",
                            json={
                                "model": VISION_MODEL,
                                "stream": False,
                                "messages": [{"role": "user", "content": [
                                    {"type": "text", "text": "Describe what you see in this image. Focus on the food: appearance, colors, toppings, texture, presentation. Be objective and specific."},
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                                ]}],
                            },
                        )
                        vision_resp.raise_for_status()
                        description = vision_resp.json()["choices"][0]["message"]["content"]
                        break
                    except (httpx.HTTPStatusError, httpx.RemoteProtocolError) as e:
                        if attempt < 2:
                            wait = 10 * (attempt + 1)
                            print(f"[backfill] Post {wp_post_id} attempt {attempt+1} failed ({e}), retrying in {wait}s...")
                            await asyncio.sleep(wait)
                        else:
                            raise

                if description is None:
                    raise RuntimeError("No description after retries")

                async with aiosqlite.connect(db_path) as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO vision_descriptions (wp_post_id, description, stored_at) VALUES (?, ?, ?)",
                        (wp_post_id, description, datetime.now(timezone.utc).isoformat()),
                    )
                    await db.commit()

                processed += 1
                print(f"[backfill] {processed}/{len(post_ids) - skipped} done (post {wp_post_id})")

                # Wait until vision server is ready before next request
                vision_base = VISION_BASE_URL.replace("/v1", "")
                for _ in range(60):
                    try:
                        health = await client.get(f"{vision_base}/health", timeout=5.0)
                        if health.status_code == 200:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(2)

            except Exception as e:
                errors += 1
                print(f"[backfill] Error on post {wp_post_id}: {e}")

    print(f"[backfill] Complete — processed: {processed}, skipped (no image): {skipped}, errors: {errors}")


@app.post("/admin/backfill-vision", dependencies=[Depends(require_auth)])
async def backfill_vision(background_tasks: BackgroundTasks):
    """Run all WP posts without real vision descriptions through the vision model."""
    background_tasks.add_task(_run_vision_backfill)
    return {"status": "started", "message": "Backfill running in background — check docker logs for progress."}


@app.post("/admin/reindex", dependencies=[Depends(require_auth)])
async def reindex():
    """Bootstrap or refresh the quality checker index from WordPress."""
    if not _quality_checker:
        raise HTTPException(status_code=503, detail="Quality checker not configured (missing WP credentials)")
    count = await _quality_checker.bootstrap_from_wp()
    return {"indexed": count}
