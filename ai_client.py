# ai_client.py — Universal AI client for ShobanaFilterBot
# Supports Google Gemini (free), Groq (free), OpenRouter (free models), Anthropic (paid)
# Set AI_PROVIDER in your env to switch. Default: gemini
#
# ── Quick setup (pick ONE) ────────────────────────────────────────────────────
#
#  GEMINI (recommended — totally free, no card needed)
#    AI_PROVIDER   = gemini
#    AI_API_KEY    = <key from https://aistudio.google.com/app/apikey>
#
#  GROQ (free, very fast)
#    AI_PROVIDER   = groq
#    AI_API_KEY    = <key from https://console.groq.com>
#
#  OPENROUTER (free models available)
#    AI_PROVIDER   = openrouter
#    AI_API_KEY    = <key from https://openrouter.ai/keys>
#
#  ANTHROPIC (paid — only if you have a key)
#    AI_PROVIDER   = anthropic
#    AI_API_KEY    = <key from https://console.anthropic.com>
#
# ─────────────────────────────────────────────────────────────────────────────

import json
import logging
import re

import aiohttp

from info import AI_PROVIDER, AI_API_KEY, AI_SEARCH_ENABLED

logger = logging.getLogger(__name__)

# ── Provider configs ──────────────────────────────────────────────────────────

_PROVIDERS = {
    "gemini": {
        # Google Gemini — free tier: 15 RPM, 1500 RPD, no billing needed
        "url":     "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}",
        "headers": {"Content-Type": "application/json"},
        "build_body": lambda system, user: {
            "contents": [{"role": "user", "parts": [{"text": f"{system}\n\n{user}"}]}],
            "generationConfig": {"maxOutputTokens": 1000, "temperature": 0.2},
        },
        "parse": lambda d: d["candidates"][0]["content"]["parts"][0]["text"],
    },
    "groq": {
        # Groq — free tier: ~30 RPM on Llama 3.1 8B, extremely fast
        "url":     "https://api.groq.com/openai/v1/chat/completions",
        "headers": lambda key: {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {key}",
        },
        "build_body": lambda system, user: {
            "model":       "llama-3.1-8b-instant",
            "max_tokens":  1000,
            "temperature": 0.2,
            "messages": [
                {"role": "system",  "content": system},
                {"role": "user",    "content": user},
            ],
        },
        "parse": lambda d: d["choices"][0]["message"]["content"],
    },
    "openrouter": {
        # OpenRouter — several models are permanently free (no card)
        "url":     "https://openrouter.ai/api/v1/chat/completions",
        "headers": lambda key: {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer":  "https://github.com/MN-BOTS/ShobanaFilterBot",
        },
        "build_body": lambda system, user: {
            "model":       "mistralai/mistral-7b-instruct:free",
            "max_tokens":  1000,
            "temperature": 0.2,
            "messages": [
                {"role": "system",  "content": system},
                {"role": "user",    "content": user},
            ],
        },
        "parse": lambda d: d["choices"][0]["message"]["content"],
    },
    "anthropic": {
        # Anthropic — paid, kept for compatibility
        "url":     "https://api.anthropic.com/v1/messages",
        "headers": lambda key: {
            "Content-Type":      "application/json",
            "x-api-key":         key,
            "anthropic-version": "2023-06-01",
        },
        "build_body": lambda system, user: {
            "model":      "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "system":     system,
            "messages":   [{"role": "user", "content": user}],
        },
        "parse": lambda d: d["content"][0]["text"],
    },
}


# ── Core call ─────────────────────────────────────────────────────────────────

async def ai_call(system_prompt: str, user_content: str) -> dict | list | None:
    """
    Send a prompt to the configured AI provider and return parsed JSON,
    or None on any failure.
    """
    if not AI_SEARCH_ENABLED or not AI_API_KEY:
        return None

    provider = AI_PROVIDER.lower()
    cfg = _PROVIDERS.get(provider)
    if not cfg:
        logger.error("Unknown AI_PROVIDER: %r. Choose: gemini, groq, openrouter, anthropic", provider)
        return None

    # Build URL (Gemini embeds key in URL)
    url = cfg["url"]
    if "{key}" in url:
        url = url.format(key=AI_API_KEY)

    # Build headers
    raw_headers = cfg["headers"]
    headers = raw_headers(AI_API_KEY) if callable(raw_headers) else raw_headers

    # Build body
    body = cfg["build_body"](system_prompt, user_content)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("AI API [%s] HTTP %s: %s", provider, resp.status, text[:200])
                    return None
                data = await resp.json()

        raw_text = cfg["parse"](data).strip()
        # Strip accidental markdown fences
        raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text)
        raw_text = re.sub(r"\n?```$",        "", raw_text)
        return json.loads(raw_text)

    except json.JSONDecodeError as e:
        logger.warning("AI response was not valid JSON (%s): %s", provider, e)
        return None
    except Exception as e:
        logger.exception("ai_call [%s] failed: %s", provider, e)
        return None
