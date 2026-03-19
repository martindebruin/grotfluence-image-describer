"""
quality.py — standalone post-quality checker for grötfluence pipeline.
Zero imports from app.py; fully self-contained.
"""

import json
import re
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import httpx
import aiosqlite

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PostIndex:
    wp_post_id: int
    title: str
    caption: str          # HTML-stripped plain text
    tags: list[str]       # lowercase names
    meal_type: str        # frukost | lunch | fika | middag | kvällsgröt
    published_at: datetime  # UTC
    vision_description: Optional[str] = None  # None for pre-checker historical posts


@dataclass
class CheckResult:
    check_name: str
    severity: str         # "warning" | "error"
    message: str


@dataclass
class QualityReport:
    issues: list[CheckResult] = field(default_factory=list)

    def has_issues(self) -> bool:
        return bool(self.issues)

    def telegram_summary(self) -> str:
        lines = []
        for issue in self.issues:
            lines.append(f"• {issue.message}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BANNED_PHRASES = [
    "som en jävla hipster",
    "som en duktig person",
    "ingen har frågat",
    "jag gjorde det ändå",
    "hepp",
    "jaha",
    "dags igen",
    "ny vecka samma gröt",
    "granola",
]

BANNED_TITLE_EXACT = {"lunch", "mer", "äntligen"}

WEEKDAY_IGEN_RE = re.compile(
    r"\b(måndag|tisdag|onsdag|torsdag|fredag|lördag|söndag) igen\b",
    re.IGNORECASE,
)

ENGLISH_INGREDIENT_FRAGMENTS = [
    "blueberr", "strawberr", "raspberry", "banana", "cinnamon", "honey",
    "almond", "oatmeal", "porridge", "seeds", "nuts", "jam", "yogurt",
]

ENGLISH_WORD_RE = re.compile(r"\b[a-zA-Z]{4,}\b")

# Meal type → expected Stockholm hour ranges (inclusive start, exclusive end)
MEAL_HOURS: dict[str, tuple[int, int]] = {
    "frukost":    (5, 10),
    "lunch":      (10, 14),
    "fika":       (14, 16),
    "middag":     (16, 21),
    "kvällsgröt": (21, 29),  # 21-05 expressed as 21-29 for modular arithmetic
}

# Swedish stopwords for Jaccard similarity
SWEDISH_STOPWORDS = {
    "och", "i", "att", "det", "som", "en", "på", "är", "av", "för",
    "med", "till", "den", "har", "de", "inte", "om", "ett", "men",
    "var", "så", "han", "hon", "vi", "du", "min", "din", "sin",
    "eller", "när", "från", "ut", "sig", "nu", "än", "ja", "nej",
    "här", "där", "sedan", "efter", "även", "dock", "just", "man",
    "kan", "ska", "vill", "mot", "vid", "över", "under", "genom",
    "in", "upp", "ner", "igen", "då", "allt", "alla", "blir",
}

# Tag → keywords that the vision model might produce
TAG_VISION_KEYWORDS: dict[str, list[str]] = {
    "blåbär":     ["blue", "purple", "blueberr", "violet"],
    "lingon":     ["red berr", "lingon", "red", "cranberr"],
    "banan":      ["banana", "yellow"],
    "honung":     ["honey", "golden", "drizzle", "amber"],
    "chiafrön":   ["chia", "seed", "speck", "dot"],
    "jordgubbe":  ["strawberr", "red", "pink"],
    "hallon":     ["raspberry", "pink", "red berr"],
    "äpple":      ["apple", "green", "slice"],
    "päron":      ["pear", "green", "slice"],
    "mango":      ["mango", "orange", "yellow"],
    "kokos":      ["coconut", "white flake", "shred"],
    "nötter":     ["nut", "almond", "walnut", "cashew", "hazelnut"],
    "mandel":     ["almond", "sliver", "nut"],
    "kanel":      ["cinnamon", "brown", "spice"],
    "kardemumma": ["cardamom", "spice"],
    "vanilj":     ["vanilla", "cream", "pale"],
    "kakao":      ["cocoa", "chocolate", "dark", "brown"],
    "lax":        ["salmon", "pink", "fish"],
    "avokado":    ["avocado", "green", "slice"],
}


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS post_cache (
    wp_post_id   INTEGER PRIMARY KEY,
    title        TEXT,
    caption_text TEXT,
    tags         TEXT,
    meal_type    TEXT,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS vision_descriptions (
    wp_post_id  INTEGER PRIMARY KEY,
    description TEXT,
    stored_at   TEXT
);

CREATE TABLE IF NOT EXISTS last_post (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    wp_post_id  INTEGER,
    slug        TEXT,
    title       TEXT,
    caption     TEXT,
    tags        TEXT,
    meal_type   TEXT,
    posted_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_published ON post_cache(published_at);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).strip()


def _trigram_set(text: str) -> set[str]:
    t = text.lower()
    return {t[i:i+3] for i in range(len(t) - 2)} if len(t) >= 3 else set()


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


def _word_set(text: str, remove_stopwords: bool = True) -> set[str]:
    words = re.findall(r"\b\w+\b", text.lower())
    if remove_stopwords:
        words = [w for w in words if w not in SWEDISH_STOPWORDS]
    return set(words)


def _synthetic_description(tags: list[str], meal_type: str) -> str:
    parts = []
    if tags:
        parts.append("Ingredienser: " + ", ".join(tags))
    if meal_type:
        parts.append("Måltid: " + meal_type)
    return (". ".join(parts) + ".") if parts else "Okänd maträtt."


def _parse_date(date_str: str) -> datetime:
    """Parse an ISO 8601 date string (with or without Z/offset) to UTC datetime."""
    s = date_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.fromisoformat(s.split(".")[0])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# QualityChecker
# ---------------------------------------------------------------------------

class QualityChecker:
    def __init__(
        self,
        *,
        directus_url: str,
        directus_token: str,
        db_path: str = "/app/data/quality.db",
        index_ttl_seconds: int = 3600,
    ):
        self._directus_url = directus_url.rstrip("/")
        self._auth_header = {"Authorization": f"Bearer {directus_token}"}
        self._db_path = db_path
        self._ttl = index_ttl_seconds

        # In-memory index
        self._index: list[PostIndex] = []
        self._last_refresh: Optional[datetime] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Database setup
    # ------------------------------------------------------------------

    async def ensure_db(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA_SQL)
            # Migrate: add slug column if it doesn't exist yet
            try:
                await db.execute("ALTER TABLE last_post ADD COLUMN slug TEXT")
                await db.commit()
            except Exception:
                pass  # column already exists

    # ------------------------------------------------------------------
    # Directus fetching
    # ------------------------------------------------------------------

    async def _fetch_directus_posts(
        self, client: httpx.AsyncClient, *, after: Optional[str] = None
    ) -> list[dict]:
        """Fetch all posts from Directus (handles pagination)."""
        posts = []
        page = 1
        params: dict = {
            "fields": "id,title,body,tags.tags_id.name,category.name,published_at",
            "limit": 100,
            "sort": "published_at",
        }
        if after:
            params["filter[published_at][_gt]"] = after

        while True:
            params["page"] = page
            resp = await client.get(
                f"{self._directus_url}/items/posts",
                headers=self._auth_header,
                params=params,
                timeout=30.0,
            )
            resp.raise_for_status()
            batch = resp.json()["data"]
            if not batch:
                break
            posts.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return posts

    # ------------------------------------------------------------------
    # Bootstrap & refresh
    # ------------------------------------------------------------------

    async def bootstrap_from_directus(self) -> int:
        """Full index from Directus. Returns count of indexed posts."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            posts = await self._fetch_directus_posts(client)

        async with aiosqlite.connect(self._db_path) as db:
            count = 0
            for post in posts:
                post_id = post["id"]
                title = post["title"] or ""
                caption_text = _strip_html(post.get("body") or "")

                # Tags come back as [{"tags_id": {"name": "havregröt"}}, ...]
                raw_tags = post.get("tags") or []
                tag_names = [
                    t["tags_id"]["name"].lower()
                    for t in raw_tags
                    if isinstance(t, dict) and t.get("tags_id")
                ]

                # Category may be expanded as dict or just an ID
                cat = post.get("category")
                meal_type = cat["name"] if isinstance(cat, dict) else ""

                published_at = post.get("published_at") or ""
                tags_json = json.dumps(tag_names)

                await db.execute(
                    """INSERT OR REPLACE INTO post_cache
                       (wp_post_id, title, caption_text, tags, meal_type, published_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (post_id, title, caption_text, tags_json, meal_type, published_at),
                )

                synth = _synthetic_description(tag_names, meal_type)
                await db.execute(
                    """INSERT OR IGNORE INTO vision_descriptions (wp_post_id, description, stored_at)
                       VALUES (?, ?, ?)""",
                    (post_id, synth, datetime.now(timezone.utc).isoformat()),
                )
                count += 1

            await db.commit()

        await self.refresh_index(force=True)
        return count

    async def refresh_index(self, *, force: bool = False) -> None:
        """Load SQLite → memory; optionally sync new posts from Directus first."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            needs_sync = force or (
                self._last_refresh is None
                or (now - self._last_refresh).total_seconds() > self._ttl
            )

            if needs_sync and self._index:
                # Incremental: only fetch posts newer than our newest
                newest = max(p.published_at for p in self._index)
                after_str = newest.isoformat().replace("+00:00", "Z")

                async with httpx.AsyncClient(timeout=60.0) as client:
                    new_posts = await self._fetch_directus_posts(client, after=after_str)

                if new_posts:
                    async with aiosqlite.connect(self._db_path) as db:
                        for post in new_posts:
                            post_id = post["id"]
                            title = post["title"] or ""
                            caption_text = _strip_html(post.get("body") or "")
                            raw_tags = post.get("tags") or []
                            tag_names = [
                                t["tags_id"]["name"].lower()
                                for t in raw_tags
                                if isinstance(t, dict) and t.get("tags_id")
                            ]
                            cat = post.get("category")
                            meal_type = cat["name"] if isinstance(cat, dict) else ""
                            published_at = post.get("published_at") or ""
                            tags_json = json.dumps(tag_names)
                            await db.execute(
                                """INSERT OR REPLACE INTO post_cache
                                   (wp_post_id, title, caption_text, tags, meal_type, published_at)
                                   VALUES (?, ?, ?, ?, ?, ?)""",
                                (post_id, title, caption_text, tags_json, meal_type, published_at),
                            )
                        await db.commit()

            # Load everything from SQLite into memory
            index: list[PostIndex] = []
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT p.*, v.description FROM post_cache p LEFT JOIN vision_descriptions v ON p.wp_post_id = v.wp_post_id ORDER BY p.published_at"
                ) as cursor:
                    async for row in cursor:
                        try:
                            pub = _parse_date(row["published_at"]) if row["published_at"] else datetime.now(timezone.utc)
                        except (ValueError, KeyError):
                            pub = datetime.now(timezone.utc)
                        tags = json.loads(row["tags"]) if row["tags"] else []
                        index.append(PostIndex(
                            wp_post_id=row["wp_post_id"],
                            title=row["title"] or "",
                            caption=row["caption_text"] or "",
                            tags=tags,
                            meal_type=row["meal_type"] or "",
                            published_at=pub,
                            vision_description=row["description"],
                        ))

            self._index = index
            self._last_refresh = now

    # ------------------------------------------------------------------
    # Store helpers
    # ------------------------------------------------------------------

    async def store_vision_description(self, post_id: int, description: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO vision_descriptions (wp_post_id, description, stored_at)
                   VALUES (?, ?, ?)""",
                (post_id, description, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

    async def store_post(
        self,
        post_id: int,
        slug: str,
        title: str,
        caption: str,
        tags: list[str],
        meal_type: str,
        published_at: datetime,
    ) -> None:
        """Store a newly published post in both post_cache and last_post."""
        caption_text = _strip_html(caption)
        tags_json = json.dumps(tags)
        pub_iso = published_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO post_cache
                   (wp_post_id, title, caption_text, tags, meal_type, published_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (post_id, title, caption_text, tags_json, meal_type, pub_iso),
            )
            await db.execute(
                """INSERT OR REPLACE INTO last_post (id, wp_post_id, slug, title, caption, tags, meal_type, posted_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
                (post_id, slug, title, caption, tags_json, meal_type, pub_iso),
            )
            await db.commit()

        # Update in-memory index
        async with self._lock:
            try:
                pub = _parse_date(pub_iso)
            except ValueError:
                pub = published_at.astimezone(timezone.utc)
            self._index.append(PostIndex(
                wp_post_id=post_id,
                title=title,
                caption=caption_text,
                tags=tags,
                meal_type=meal_type,
                published_at=pub,
            ))

    async def load_last_post(self) -> Optional[dict]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM last_post WHERE id = 1") as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                return dict(row)

    async def update_last_post(self, **fields) -> None:
        """Update specific fields in last_post table."""
        if not fields:
            return
        set_clauses = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [1]
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                f"UPDATE last_post SET {set_clauses} WHERE id = 1",
                values,
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Quality checks
    # ------------------------------------------------------------------

    async def check(
        self,
        *,
        title: str,
        caption: str,
        meal_type: str,
        tags: list[str],
        vision_description: Optional[str],
        current_time: datetime,
    ) -> QualityReport:
        """Run all quality checks. TTL-guards index refresh."""
        if self._last_refresh is None or (
            (datetime.now(timezone.utc) - self._last_refresh).total_seconds() > self._ttl
        ):
            await self.refresh_index()

        report = QualityReport()

        self._check_banned_phrases(title, caption, report)
        self._check_english_leakage(title, caption, tags, report)
        self._check_tag_novelty(tags, report)
        self._check_caption_similarity(caption, title, report)
        if vision_description:
            self._check_vision_tag_coherence(tags, vision_description, report)
        self._check_meal_type_plausibility(meal_type, current_time, report)

        return report

    # --- Check 1: banned phrases ---

    def _check_banned_phrases(self, title: str, caption: str, report: QualityReport) -> None:
        combined = (title + " " + caption).lower()

        for phrase in BANNED_PHRASES:
            if phrase.lower() in combined:
                report.issues.append(CheckResult(
                    check_name="banned_phrases",
                    severity="warning",
                    message=f'Förbjuden fras: "{phrase}"',
                ))

        if WEEKDAY_IGEN_RE.search(combined):
            match = WEEKDAY_IGEN_RE.search(combined)
            report.issues.append(CheckResult(
                check_name="banned_phrases",
                severity="warning",
                message=f'Förbjuden fras: "{match.group(0)}"',
            ))

        if title.strip().lower() in BANNED_TITLE_EXACT:
            report.issues.append(CheckResult(
                check_name="banned_phrases",
                severity="warning",
                message=f'Förbjuden titel: "{title.strip()}"',
            ))

    # --- Check 2: English leakage ---

    def _check_english_leakage(self, title: str, caption: str, tags: list[str], report: QualityReport) -> None:
        combined = (title + " " + caption + " " + " ".join(tags)).lower()

        for fragment in ENGLISH_INGREDIENT_FRAGMENTS:
            if fragment in combined:
                report.issues.append(CheckResult(
                    check_name="english_leakage",
                    severity="warning",
                    message=f'Engelskt ord i innehåll: "{fragment}..."',
                ))
                return  # one report is enough

        # Heuristic: many long ASCII words without Swedish chars
        swedish_chars = set("åäöÅÄÖ")
        text = title + " " + caption
        ascii_words = ENGLISH_WORD_RE.findall(text)
        if len(ascii_words) > 3:
            non_swedish_words = [w for w in ascii_words if not any(c in w for c in swedish_chars)]
            if len(non_swedish_words) > 3:
                report.issues.append(CheckResult(
                    check_name="english_leakage",
                    severity="warning",
                    message=f"Möjlig engelska i text ({len(non_swedish_words)} misstänkta ord)",
                ))

    # ------------------------------------------------------------------
    # Ingredient novelty helper
    # ------------------------------------------------------------------

    def get_new_tags(self, tags: list[str]) -> list[str]:
        """Return tags that have never appeared in any indexed post."""
        freq: dict[str, int] = {}
        for post in self._index:
            for t in post.tags:
                freq[t.lower()] = freq.get(t.lower(), 0) + 1
        return [t for t in tags if freq.get(t.lower(), 0) == 0]

    # --- Check 3: tag novelty ---

    def _check_tag_novelty(self, tags: list[str], report: QualityReport) -> None:
        if not self._index:
            return

        total_posts = len(self._index)
        freq: dict[str, int] = {}
        for post in self._index:
            for t in post.tags:
                freq[t.lower()] = freq.get(t.lower(), 0) + 1

        for tag in tags:
            tl = tag.lower()
            count = freq.get(tl, 0)
            if count == 0:
                report.issues.append(CheckResult(
                    check_name="tag_novelty",
                    severity="warning",
                    message=f'Aldrig sett förut: "{tag}"',
                ))
            elif count / total_posts < 0.005:
                report.issues.append(CheckResult(
                    check_name="tag_novelty",
                    severity="warning",
                    message=f'Ovanlig ingrediens: "{tag}" ({count} tidigare inlägg)',
                ))

    # --- Check 4: caption similarity ---

    def _check_caption_similarity(self, caption: str, title: str, report: QualityReport) -> None:
        if not self._index:
            return

        recent = sorted(self._index, key=lambda p: p.published_at, reverse=True)[:30]

        cap_words = _word_set(caption)
        cap_trigrams = _trigram_set(caption)

        for post in recent:
            if not post.caption:
                continue
            j = _jaccard(cap_words, _word_set(post.caption))
            tg = _jaccard(cap_trigrams, _trigram_set(post.caption))
            score = max(j, tg)

            if score >= 0.75:
                date_str = post.published_at.strftime("%Y-%m-%d")
                report.issues.append(CheckResult(
                    check_name="caption_similarity",
                    severity="warning",
                    message=f'Nästan identisk bildtext med inlägg {date_str} "{post.title}"',
                ))
                return
            elif score >= 0.55:
                date_str = post.published_at.strftime("%Y-%m-%d")
                report.issues.append(CheckResult(
                    check_name="caption_similarity",
                    severity="warning",
                    message=f'Bildtext liknar inlägg {date_str} "{post.title}"',
                ))
                return

    # --- Check 5: vision-tag coherence ---

    def _check_vision_tag_coherence(self, tags: list[str], vision_description: str, report: QualityReport) -> None:
        desc_lower = vision_description.lower()
        for tag in tags:
            tl = tag.lower()
            keywords = TAG_VISION_KEYWORDS.get(tl)
            if keywords is None:
                continue  # tag not in table → skip
            if not any(kw in desc_lower for kw in keywords):
                report.issues.append(CheckResult(
                    check_name="vision_coherence",
                    severity="warning",
                    message=f'Taggen "{tag}" stöds inte av bildbeskrivningen',
                ))

    # --- Check 6: meal type plausibility ---

    def _check_meal_type_plausibility(self, meal_type: str, current_time: datetime, report: QualityReport) -> None:
        expected = MEAL_HOURS.get(meal_type)
        if expected is None:
            return

        from zoneinfo import ZoneInfo
        stockholm_hour = current_time.astimezone(ZoneInfo("Europe/Stockholm")).hour
        start, end = expected

        # Handle kvällsgröt spanning midnight (21-29 → 21-23 or 0-5)
        def in_range(h: int, s: int, e: int) -> bool:
            if e <= 24:
                return s <= h < e
            else:
                return h >= s or h < (e - 24)

        grace = 1
        in_window = in_range(stockholm_hour, max(0, start - grace), min(end + grace, 24)) or (
            end > 24 and (stockholm_hour >= start - grace or stockholm_hour < (end - 24) + grace)
        )

        if not in_window:
            end_display = end if end <= 24 else end - 24
            time_str = current_time.astimezone(ZoneInfo("Europe/Stockholm")).strftime("%H:%M")
            report.issues.append(CheckResult(
                check_name="meal_plausibility",
                severity="warning",
                message=f"meal_type '{meal_type}' genererades kl {time_str} (förväntat {start:02d}-{end_display:02d})",
            ))
