import asyncio
import base64
import logging
import os
import re

import httpx
from mastodon import Mastodon

from db_community import get_wallet, is_processed, save_post, save_wallet, update_post_airdrop

logger = logging.getLogger(__name__)

INSTANCE        = os.getenv("MASTODON_INSTANCE", "https://mastodon.social")
TOKEN           = os.getenv("MASTODON_ACCESS_TOKEN", "")
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL", "60"))
AIRDROP_URL     = os.getenv("AIRDROP_URL", "")
AIRDROP_SECRET  = os.getenv("AIRDROP_SECRET", "")
AIRDROP_AMOUNT  = int(os.getenv("AIRDROP_AMOUNT", "25"))
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "http://frmwrk-ai:8082/v1")
VISION_MODEL    = os.getenv("VISION_MODEL", "qwen2.5vl:3b-gpu")

_WALLET_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
_followed: set[str] = set()


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


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
            logger.info("[mastodon] Vision: %s → %s", description[:80], "gröt" if result else "EJ gröt")
            return result
    except Exception as e:
        logger.warning("[mastodon] Vision check failed (%s) — allowing post", e)
        return True


async def _load_followed(api: Mastodon):
    loop = asyncio.get_event_loop()
    me = await loop.run_in_executor(None, api.me)
    page = await loop.run_in_executor(None, lambda: api.account_following(me["id"]))
    handles = set()
    while page:
        for acc in page:
            handles.add(f"@{acc['acct']}".lower())
        _page = page
        page = await loop.run_in_executor(None, lambda: api.fetch_next(_page))
    _followed.clear()
    _followed.update(handles)
    logger.info("[mastodon] Loaded %d followed accounts", len(handles))


async def _trigger_airdrop(post_id: str, handle: str):
    if not AIRDROP_URL or not AIRDROP_SECRET:
        return
    wallet = await get_wallet(handle)
    if not wallet:
        logger.info("[mastodon] No wallet for %s — skipping airdrop", handle)
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
        logger.info("[mastodon] Airdrop sent to %s: %s", handle, tx)
    except Exception as e:
        await update_post_airdrop(post_id, None, "failed")
        logger.error("[mastodon] Airdrop failed for %s: %s", handle, e)


async def _handle_wallet_registration(api: Mastodon, status: dict, handle: str):
    text = _strip_html(status.get("content", ""))
    match = _WALLET_RE.search(text)
    if not match:
        return
    wallet = match.group(0)
    await save_wallet(handle, "mastodon", wallet)
    logger.info("[mastodon] Wallet registered for %s: %s", handle, wallet)
    loop = asyncio.get_event_loop()
    try:
        sid = status["id"]
        await loop.run_in_executor(None, lambda: api.status_post(
            f"Plånboken registrerad! Du får {AIRDROP_AMOUNT} $GRÖT per grötinlägg. 🥣",
            in_reply_to_id=sid,
            visibility="direct",
        ))
    except Exception as e:
        logger.warning("[mastodon] Could not reply to %s: %s", handle, e)


async def _process_notification(api: Mastodon, notif: dict) -> dict | None:
    if notif.get("type") != "mention":
        return None

    status  = notif.get("status", {})
    account = status.get("account", {})
    handle  = f"@{account.get('acct', '')}".lower()

    has_images = bool(status.get("media_attachments"))
    if not has_images:
        await _handle_wallet_registration(api, status, handle)
        return None

    if handle not in _followed:
        return None

    post_id  = str(status["id"])
    post_url = status.get("url") or status.get("uri", "")

    if await is_processed(post_id):
        return None

    image_url = None
    for att in status.get("media_attachments", []):
        if att.get("type") == "image":
            image_url = att.get("url")
            break

    if not image_url:
        return None

    alias = account.get("display_name") or account.get("username") or handle
    return {
        "post_id":   post_id,
        "post_url":  post_url,
        "alias":     alias,
        "image_url": image_url,
        "handle":    handle,
        "status_id": status["id"],
    }


async def mastodon_poll_loop():
    if not TOKEN:
        logger.warning("[mastodon] MASTODON_ACCESS_TOKEN not set — listener disabled")
        return

    loop = asyncio.get_event_loop()
    api  = Mastodon(access_token=TOKEN, api_base_url=INSTANCE)
    await _load_followed(api)
    logger.info("[mastodon] Polling every %ds", POLL_INTERVAL)

    followed_refresh = loop.time()
    since_id = None

    while True:
        try:
            if loop.time() - followed_refresh > 3600:
                await _load_followed(api)
                followed_refresh = loop.time()

            kwargs = {"types": ["mention"], "limit": 30}
            if since_id:
                kwargs["since_id"] = since_id

            _kw = kwargs
            notifs = await loop.run_in_executor(None, lambda: api.notifications(**_kw))
            if notifs:
                since_id = notifs[0]["id"] if isinstance(notifs[0], dict) else notifs[0].id

            for notif in notifs:
                notif_dict = notif if isinstance(notif, dict) else dict(notif)
                candidate = await _process_notification(api, notif_dict)
                if not candidate:
                    continue

                ok = await _is_porridge(candidate["image_url"])
                if not ok:
                    logger.info("[mastodon] Vision rejected post from %s", candidate["handle"])
                    sid = candidate["status_id"]
                    try:
                        await loop.run_in_executor(None, lambda: api.status_post(
                            "Det där såg tyvärr inte ut som gröt. 🥣",
                            in_reply_to_id=sid,
                            visibility="unlisted",
                        ))
                    except Exception as e:
                        logger.warning("[mastodon] Could not send rejection reply: %s", e)
                    continue

                await save_post(candidate["post_id"], candidate["post_url"], candidate["alias"], candidate["image_url"], "mastodon")
                logger.info("[mastodon] Saved post %s from %s", candidate["post_id"], candidate["handle"])
                await _trigger_airdrop(candidate["post_id"], candidate["handle"])

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("[mastodon] Poll error: %s", e)

        await asyncio.sleep(POLL_INTERVAL)
