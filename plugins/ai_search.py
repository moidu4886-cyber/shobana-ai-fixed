# ai_search.py — AI-assisted Search (REFACTORED by mn-bots)
#
# ╔══════════════════════════════════════════════════════════════╗
# ║  Architecture (FIXED):                                       ║
# ║                                                              ║
# ║  User Query                                                  ║
# ║    → Filler-word cleanup                                     ║
# ║    → AI Intent Parser  (extracts movie_name/lang/quality…)   ║
# ║    → Database Search   (MongoDB / PostgreSQL)                ║
# ║    → Show real DB results  (no hallucination)                ║
# ╚══════════════════════════════════════════════════════════════╝
#
# What the AI does NOT do any more:
#   ✗ Generate movie name suggestions from its own knowledge
#   ✗ Return a list of movies that "match" the query
#   ✗ Guess titles that may not be in the database
#
# What the AI ONLY does:
#   ✓ Parse the user's intent into a structured dict
#   ✓ Correct obvious spelling  (e.g. "kgf2" → "KGF Chapter 2")
#   ✓ Detect language / quality / year hints

import asyncio
import logging
import re

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from info import AI_SEARCH_ENABLED
from ai_client import ai_call
from database.ia_filterdb import get_search_results, get_search_results_by_intent
from database.search_logs_db import log_search

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Intent-parser system prompt ────────────────────────────────────────────────
# AI returns ONLY a JSON object — it must never list movie results itself.
_INTENT_SYSTEM = """You are a search-intent parser for a Telegram movie database bot.
Your ONLY job is to extract structured intent from the user's search query.

CRITICAL RULES:
- You must NEVER suggest, generate, or list movie names from your own knowledge.
- You must NEVER invent titles or guess what might be in the database.
- You ONLY parse what the user explicitly typed.
- If you cannot extract a clear movie name, leave movie_name as an empty string.

OUTPUT ONLY valid JSON — no markdown fences, no prose, no explanation.

{
  "movie_name": "cleaned title only (fix spelling, remove filler words) — empty string if unclear",
  "language": "one of: english / hindi / tamil / telugu / malayalam / kannada — or empty string",
  "year": "4-digit year if explicitly mentioned — or empty string",
  "quality": "one of: 480p / 720p / 1080p / 4k — or empty string",
  "tags": ["actor name", "genre", "keyword", "..."]
}

PARSING RULES:
movie_name:
  - Fix spelling errors (e.g. "avenjers" -> "Avengers", "kgf2" -> "KGF Chapter 2")
  - Remove filler words: full movie, download, watch, send, please, bro, pls, film, new, latest
  - Keep actor/character names only if they ARE the movie title
  - "super hero movie" -> movie_name="" tags=["superhero","action"]  (no specific title)
  - "vijay action movie" -> movie_name="" tags=["vijay","action"]

language:
  - "mal"/"mlm"/"mollywood" -> "malayalam"
  - "tam"/"kollywood" -> "tamil"
  - "hin"/"bolly"/"bollywood" -> "hindi"
  - "hindi dubbed"/"dubbed" -> "hindi"
  - "eng"/"english" -> "english"
  - "tel"/"tollywood" -> "telugu"

quality:
  - "hd" -> "720p"
  - "full hd"/"fhd" -> "1080p"
  - "4k"/"uhd"/"2160p" -> "4k"
  - "sd"/"low" -> "480p"

tags: extract actor names, director names, genre words (action, comedy, thriller, romance, horror, sci-fi, etc.)"""


# ── Filler words stripped BEFORE sending to AI (reduces noise) ────────────────
_FILLER_RE = re.compile(
    r"\b(please|pls|plz|send|give|gib|get|find|search|download|watch|"
    r"full\s*movie|full\s*film|full|movie|film|series|show|web\s*series|"
    r"new|latest|recent|bro|bhai|anna|che|kitto|tharuo|ayakum|"
    r"with\s*subtitle[s]?|subtitle[s]?|sub[s]?)\b",
    re.IGNORECASE,
)


def _clean_query_for_ai(query: str) -> str:
    """Strip filler words so AI receives a cleaner, denser signal."""
    cleaned = _FILLER_RE.sub(" ", query)
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned if cleaned else query  # never return empty string


# ── Core AI call: intent parsing only ─────────────────────────────────────────

async def ai_parse_intent(query: str) -> dict | None:
    """
    Call the configured AI provider and get a structured intent dict.
    Returns dict with keys: movie_name, language, year, quality, tags
    Returns None if the AI call fails or returns garbage.
    """
    cleaned = _clean_query_for_ai(query)
    if not cleaned or len(cleaned) < 2:
        return None

    result = await ai_call(_INTENT_SYSTEM, f'User search query: "{cleaned}"')

    if not isinstance(result, dict):
        logger.warning("ai_parse_intent: non-dict response for %r: %r", query, result)
        return None

    # Normalise all fields so downstream code never gets None
    intent = {
        "movie_name": str(result.get("movie_name") or "").strip(),
        "language":   str(result.get("language")   or "").strip().lower(),
        "year":       str(result.get("year")        or "").strip(),
        "quality":    str(result.get("quality")     or "").strip().lower(),
        "tags": [
            str(t).strip().lower()
            for t in (result.get("tags") or [])
            if t and len(str(t).strip()) > 1
        ],
    }
    logger.info("Intent parsed | query=%r -> %s", query, intent)
    return intent


# ── Main AI search pipeline ────────────────────────────────────────────────────

async def ai_smart_search(client, msg, original_query: str) -> bool:
    """
    Full pipeline:
      1. Parse intent with AI  (AI does NOT generate results)
      2. Search database with structured intent  (strictest filter first)
      3. Fallback to plain movie_name search if intent search is empty
      4. Last-resort: cleaned query search
      5. Show real DB results — or an honest "not found" message

    Returns True  -> this function handled the query (caller should stop)
    Returns False -> nothing triggered (caller may try next handler)
    """
    if not AI_SEARCH_ENABLED:
        return False

    thinking_msg = await msg.reply_text("🤖 **AI Search** — analysing your query…")

    try:
        # ── 1. Parse intent ───────────────────────────────────────────────────
        intent = await ai_parse_intent(original_query)
        if not intent:
            await thinking_msg.delete()
            return False

        files, offset, total = [], "", 0

        # ── 2. Intent-based DB search (uses movie_name + language/quality/year) ─
        has_intent = any([intent["movie_name"], intent["language"],
                          intent["quality"], intent["year"], intent["tags"]])
        if has_intent:
            files, offset, total = await get_search_results_by_intent(intent)
            if files:
                logger.info("Intent search hit: %d results for %r", total, original_query)

        # ── 3. Fallback: plain movie_name regex search ────────────────────────
        if not files and intent["movie_name"]:
            files, offset, total = await get_search_results(
                intent["movie_name"].lower(), offset=0, filter=True
            )
            if files:
                logger.info("movie_name fallback hit: %d results for %r", total, original_query)

        # ── 4. Last-resort: cleaned raw query ─────────────────────────────────
        if not files:
            cleaned = _clean_query_for_ai(original_query)
            if cleaned.lower() != (intent.get("movie_name") or "").lower() and len(cleaned) > 2:
                files, offset, total = await get_search_results(
                    cleaned.lower(), offset=0, filter=True
                )
                if files:
                    logger.info("Cleaned-query fallback hit: %d results for %r", total, original_query)

        # ── 5. Log & display ──────────────────────────────────────────────────
        user_id = msg.from_user.id if msg.from_user else 0
        chat_id  = msg.chat.id

        if files:
            await log_search(user_id, chat_id, original_query, result_found=True)
            await thinking_msg.delete()
            display_label = intent["movie_name"] or _clean_query_for_ai(original_query) or original_query
            from plugins.pm_filter import auto_filter
            await auto_filter(client, msg, spoll=(display_label, files, offset, total))
            return True

        # ── No results in DB — honest message, zero hallucination ─────────────
        await log_search(user_id, chat_id, original_query, result_found=False)

        reqst_gle = original_query.replace(" ", "+")
        btn = [
            [InlineKeyboardButton(
                "🔍 Search on Google",
                url=f"https://www.google.com/search?q={reqst_gle}+telegram+movie"
            )],
            [InlineKeyboardButton("✘ Close", callback_data="aisearch#close")],
        ]

        # Show what was parsed so the user can understand the result
        parsed_info = []
        if intent["movie_name"]: parsed_info.append(f"🎬 Title: `{intent['movie_name']}`")
        if intent["language"]:   parsed_info.append(f"🌐 Language: `{intent['language'].title()}`")
        if intent["quality"]:    parsed_info.append(f"📺 Quality: `{intent['quality']}`")
        if intent["year"]:       parsed_info.append(f"📅 Year: `{intent['year']}`")
        if intent["tags"]:       parsed_info.append(f"🏷 Tags: `{', '.join(intent['tags'][:4])}`")

        parsed_block = "\n".join(parsed_info) if parsed_info else f"`{original_query}`"
        text = (
            "🤖 **AI Search — No Results Found**\n\n"
            f"**What I searched for:**\n{parsed_block}\n\n"
            "❌ This content is not in the database.\n"
            "_The bot only shows files that are actually stored._"
        )
        await thinking_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(btn))
        await asyncio.sleep(120)
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        return True  # We handled it (showed no-result message)

    except Exception as exc:
        logger.exception("ai_smart_search crashed: %s", exc)
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        return False


# ── Callback: "aisearch#close" or legacy inline buttons ───────────────────────

@Client.on_callback_query(filters.regex(r"^aisearch#"))
async def aisearch_callback(bot, query):
    payload = query.data.split("#", 1)[1]
    if payload == "close":
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer()
        return

    movie_name = payload.strip()
    await query.answer(f"🔍 Searching: {movie_name}", show_alert=False)

    files, offset, total = await get_search_results(movie_name.lower(), offset=0, filter=True)
    if not files:
        await query.answer(
            f"😔 No results for '{movie_name}' in the database.", show_alert=True
        )
        return

    from plugins.pm_filter import auto_filter
    await auto_filter(bot, query, spoll=(movie_name, files, offset, total))


# ── /aisearch command ──────────────────────────────────────────────────────────

@Client.on_message(filters.command("aisearch"))
async def aisearch_command(client, message):
    if len(message.command) < 2:
        return await message.reply_text(
            "**Usage:** `/aisearch <movie name>`\n"
            "**Example:** `/aisearch kgf2 hindi`\n\n"
            "_Tip: You can just type the movie name — "
            "AI search runs automatically when no results are found._",
            quote=True,
        )

    query_text = " ".join(message.command[1:])
    user_id = message.from_user.id if message.from_user else 0

    # Direct DB search first (fast path)
    files, offset, total = await get_search_results(query_text.lower(), offset=0, filter=True)
    if files:
        await log_search(user_id, message.chat.id, query_text, result_found=True)
        from plugins.pm_filter import auto_filter
        await auto_filter(client, message, spoll=(query_text, files, offset, total))
    else:
        await log_search(user_id, message.chat.id, query_text, result_found=False)
        await ai_smart_search(client, message, query_text)
