import asyncio
import base64
import logging
import os

import httpx
from atproto import Client, models

from db_community import get_wallet, is_processed, save_post, update_post_airdrop

logger = logging.getLogger(__name__)

BSKY_HANDLE     = os.getenv("BSKY_HANDLE", "")
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD", "")
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL", "60"))
AIRDROP_URL     = os.getenv("AIRDROP_URL", "")
AIRDROP_SECRET  = os.getenv("AIRDROP_SECRET", "")
AIRDROP_AMOUNT  = int(os.getenv("AIRDROP_AMOUNT", "25"))
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "http://frmwrk-ai:8082/v1")
VISION_MODEL    = os.getenv("VISION_MODEL", "qwen2.5vl:3b-gpu")

_followed: set[str] = set()


async def _is_porridge(image_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            img_resp = await client.get(image_url)
            img_resp.raise_for_status()
            content_type = img_resp.headers.get("content-type", "image/jpeg").split(";")[0]
            img_b64 = base64.b64encode(img_resp.content).decode()
            resp = await client.post(
                f"{VISION_BASE_URL}/chat/completions",
                json={
                    "model": VISION_MODEL,
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{img_b64}"}},
                        {"type": "text", "text": "Analysera bilden och beskriv exakt vilken grötsort och vilka toppings/tillbehör som syns."},
                    ]}],
                    "max_tokens": 100,
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            description = resp.json()["choices"][0]["message"]["content"].strip()
            result = "gröt" in description.lower()
            logger.info("[bsky] Vision: %s → %s", description[:80], "gröt" if result else "EJ gröt")
            return result
    except Exception as e:
        logger.warning("[bsky] Vision check failed (%s) — allowing post", e)
        return True


async def _load_followed(client: Client):
    loop = asyncio.get_event_loop()
    handles = set()
    cursor  = None
    while True:
        _cursor = cursor
        resp = await loop.run_in_executor(
            None,
            lambda: client.app.bsky.graph.get_follows({"actor": BSKY_HANDLE, "limit": 100, "cursor": _cursor}),
        )
        for f in resp.follows:
            handles.add(f.handle.lower())
        cursor = resp.cursor
        if not cursor:
            break
    _followed.clear()
    _followed.update(handles)
    logger.info("[bsky] Loaded %d followed accounts", len(handles))


async def _trigger_airdrop(post_id: str, handle: str):
    if not AIRDROP_URL or not AIRDROP_SECRET:
        return
    wallet = await get_wallet(handle)
    if not wallet:
        logger.info("[bsky] No wallet for %s — skipping airdrop", handle)
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{AIRDROP_URL}/airdrop",
                json={"wallet": wallet, "amount": AIRDROP_AMOUNT},
                headers={"x-secret": AIRDROP_SECRET},
            )
            resp.raise_for_status()
            tx = resp.json()["tx"]
        await update_post_airdrop(post_id, tx, "sent")
        logger.info("[bsky] Airdrop sent to %s: %s", handle, tx)
    except Exception as e:
        await update_post_airdrop(post_id, None, "failed")
        logger.error("[bsky] Airdrop failed for %s: %s", handle, e)


def _image_url(did: str, cid: str) -> str:
    return f"https://cdn.bsky.app/img/feed_fullsize/plain/{did}/{cid}@jpeg"


def _post_url(handle: str, uri: str) -> str:
    rkey = uri.split("/")[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def _process_notification(notif) -> dict | None:
    if notif.reason != "mention":
        return None

    author   = notif.author
    handle   = author.handle.lower()

    if handle not in _followed:
        return None

    post_id  = notif.uri
    post_url = _post_url(handle, notif.uri)

    embed     = getattr(notif.record, "embed", None)
    image_url = None
    if embed:
        images = getattr(embed, "images", None)
        if images:
            ref = images[0].image.ref.link
            image_url = _image_url(author.did, ref)

    if not image_url:
        return None

    alias = author.display_name or author.handle
    return {
        "post_id":   post_id,
        "post_url":  post_url,
        "alias":     alias,
        "image_url": image_url,
        "handle":    handle,
        "notif":     notif,
    }


async def bsky_poll_loop():
    if not BSKY_HANDLE or not BSKY_APP_PASSWORD:
        logger.warning("[bsky] BSKY_HANDLE / BSKY_APP_PASSWORD not set — listener disabled")
        return

    loop   = asyncio.get_event_loop()
    client = Client()
    await loop.run_in_executor(None, lambda: client.login(BSKY_HANDLE, BSKY_APP_PASSWORD))
    await _load_followed(client)
    logger.info("[bsky] Polling every %ds", POLL_INTERVAL)

    followed_refresh = loop.time()

    while True:
        try:
            if loop.time() - followed_refresh > 3600:
                await _load_followed(client)
                followed_refresh = loop.time()

            resp = await loop.run_in_executor(
                None,
                lambda: client.app.bsky.notification.list_notifications({"limit": 30}),
            )

            for notif in resp.notifications:
                candidate = _process_notification(notif)
                if not candidate:
                    continue

                if await is_processed(candidate["post_id"]):
                    continue

                ok = await _is_porridge(candidate["image_url"])
                if not ok:
                    logger.info("[bsky] Vision rejected post from %s", candidate["handle"])
                    try:
                        parent    = models.create_strong_ref(candidate["notif"])
                        reply_ref = models.AppBskyFeedPost.ReplyRef(root=parent, parent=parent)
                        await loop.run_in_executor(
                            None,
                            lambda: client.send_post(text="Det där såg tyvärr inte ut som gröt. 🥣", reply_to=reply_ref),
                        )
                    except Exception as e:
                        logger.warning("[bsky] Could not send rejection reply: %s", e)
                    continue

                await save_post(candidate["post_id"], candidate["post_url"], candidate["alias"], candidate["image_url"], "bluesky")
                logger.info("[bsky] Saved post %s from @%s", candidate["post_id"], candidate["handle"])
                await _trigger_airdrop(candidate["post_id"], candidate["handle"])

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("[bsky] Poll error: %s", e)

        await asyncio.sleep(POLL_INTERVAL)
