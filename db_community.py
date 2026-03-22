import aiosqlite

DB_PATH = "/app/data/community.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id           TEXT PRIMARY KEY,
                post_url     TEXT,
                alias        TEXT,
                image_url    TEXT,
                platform     TEXT DEFAULT 'mastodon',
                airdrop_tx   TEXT,
                airdrop_status TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                handle       TEXT PRIMARY KEY,
                platform     TEXT NOT NULL,
                wallet       TEXT NOT NULL,
                registered_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


async def is_processed(post_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)) as cur:
            return await cur.fetchone() is not None


async def save_post(post_id: str, post_url: str, alias: str, image_url: str, platform: str = "mastodon"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO posts (id, post_url, alias, image_url, platform) VALUES (?, ?, ?, ?, ?)",
            (post_id, post_url, alias, image_url, platform),
        )
        await db.commit()


async def update_post_airdrop(post_id: str, tx: str | None, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE posts SET airdrop_tx = ?, airdrop_status = ? WHERE id = ?",
            (tx, status, post_id),
        )
        await db.commit()


async def save_wallet(handle: str, platform: str, wallet: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO wallets (handle, platform, wallet) VALUES (?, ?, ?)",
            (handle, platform, wallet),
        )
        await db.commit()


async def get_wallet(handle: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT wallet FROM wallets WHERE handle = ?", (handle,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None
