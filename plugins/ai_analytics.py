# AI Analytics — by mn-bots
# Admin dashboard using free AI (Gemini / Groq / OpenRouter / Anthropic).
# Commands: /aistats  /aiinsights  /aihelp

import json
import logging
from collections import Counter
from datetime import datetime, timezone, timedelta

from pyrogram import Client, filters

from info import ADMINS, AI_SEARCH_ENABLED, AI_PROVIDER, AI_API_KEY
from ai_client import ai_call
from database.search_logs_db import get_logs

logger = logging.getLogger(__name__)

_ANALYTICS_SYSTEM = """You are an analytics engine for a Telegram bot admin dashboard.
You receive user activity logs. Compute and return ONLY valid JSON:
{
  "total_users": 0,
  "active_users_24h": 0,
  "total_searches": 0,
  "success_rate": 0.0,
  "top_searches": [],
  "failed_searches": [],
  "trending": [],
  "peak_time": "HH:00-HH:00"
}
No markdown fences. No explanation. Only JSON."""

_INSIGHTS_SYSTEM = """You are a growth consultant for a Telegram movie-file bot.
Based on the analytics JSON provided, generate actionable admin insights.
Consider: which movies to upload urgently, highest-demand language, most valuable users,
and engagement improvement suggestions.
OUTPUT ONLY valid JSON:
{
  "insights": [],
  "recommendations": []
}
No markdown fences. No explanation. Only JSON."""


def _parse_ts(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except Exception:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _compute_local_stats(logs: list) -> dict:
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    total_users      = len({r["user_id"] for r in logs})
    active_users_24h = len({r["user_id"] for r in logs if _parse_ts(r.get("timestamp")) >= cutoff})
    total_searches   = len(logs)
    found            = sum(1 for r in logs if r.get("result_found"))
    success_rate     = round(found / total_searches * 100, 1) if total_searches else 0.0
    q_counter        = Counter(r["query"] for r in logs)
    fail_counter     = Counter(r["query"] for r in logs if not r.get("result_found"))
    hours = [_parse_ts(r.get("timestamp")).hour for r in logs if _parse_ts(r.get("timestamp")) != datetime.min.replace(tzinfo=timezone.utc)]
    peak_time = "N/A"
    if hours:
        ph = Counter(hours).most_common(1)[0][0]
        peak_time = f"{ph:02d}:00-{(ph+2)%24:02d}:00"
    return {
        "total_users":      total_users,
        "active_users_24h": active_users_24h,
        "total_searches":   total_searches,
        "success_rate":     success_rate,
        "top_searches":     [q for q, _ in q_counter.most_common(10)],
        "failed_searches":  [q for q, _ in fail_counter.most_common(10)],
        "trending":         [],
        "peak_time":        peak_time,
    }


def _format_stats(s: dict) -> str:
    lines = [
        "📊 **AI Analytics Report**\n",
        f"👥 Total users:    `{s['total_users']}`",
        f"🟢 Active (24 h):  `{s['active_users_24h']}`",
        f"🔍 Total searches: `{s['total_searches']}`",
        f"✅ Success rate:   `{s['success_rate']}%`",
        f"⏰ Peak time:      `{s['peak_time']}`\n",
    ]
    if s.get("top_searches"):
        lines.append("🏆 **Top searches:**")
        lines += [f"  {i+1}. {q}" for i, q in enumerate(s["top_searches"][:10])]
    if s.get("failed_searches"):
        lines.append("\n🚫 **Top failed searches** _(upload these urgently)_:")
        lines += [f"  • {q}" for q in s["failed_searches"][:5]]
    if s.get("trending"):
        lines.append("\n🔥 **Trending:**")
        lines += [f"  • {t}" for t in s["trending"][:5]]
    return "\n".join(lines)


def _format_insights(d: dict) -> str:
    lines = ["💡 **AI Insights & Recommendations**\n"]
    for item in d.get("insights", []):
        lines.append(f"📌 {item}")
    if d.get("recommendations"):
        lines.append("\n🚀 **Recommendations:**")
        for item in d["recommendations"]:
            lines.append(f"  ➤ {item}")
    return "\n".join(lines) if len(lines) > 1 else "No insights generated."


@Client.on_message(filters.command("aistats") & filters.user(ADMINS))
async def aistats_command(client, message):
    wait = await message.reply_text("⏳ Fetching logs and computing stats…")
    logs = await get_logs(limit=1000)
    if not logs:
        return await wait.edit_text("📭 No search logs found yet.")

    stats = _compute_local_stats(logs)

    if AI_SEARCH_ENABLED and AI_API_KEY:
        ai_stats = await ai_call(
            _ANALYTICS_SYSTEM,
            f"Here are {len(logs)} recent search log entries:\n{json.dumps(logs[:500], default=str)}"
        )
        if isinstance(ai_stats, dict):
            stats["trending"]        = ai_stats.get("trending",        stats["trending"])
            stats["peak_time"]       = ai_stats.get("peak_time",       stats["peak_time"])
            stats["top_searches"]    = ai_stats.get("top_searches",    stats["top_searches"])
            stats["failed_searches"] = ai_stats.get("failed_searches", stats["failed_searches"])

    await wait.edit_text(_format_stats(stats))


@Client.on_message(filters.command("aiinsights") & filters.user(ADMINS))
async def aiinsights_command(client, message):
    if not AI_SEARCH_ENABLED or not AI_API_KEY:
        return await message.reply_text(
            "⚠️ AI features are disabled.\nSet `AI_API_KEY` and `AI_SEARCH_ENABLED=true` in your env vars."
        )
    wait = await message.reply_text("🤖 Generating AI insights…")
    logs = await get_logs(limit=1000)
    if not logs:
        return await wait.edit_text("📭 No search logs found yet.")

    stats  = _compute_local_stats(logs)
    result = await ai_call(
        _INSIGHTS_SYSTEM,
        f"Analytics summary:\n{json.dumps(stats, default=str)}\n\n"
        f"Recent log sample (50 entries):\n{json.dumps(logs[:50], default=str)}"
    )
    if not result:
        return await wait.edit_text("❌ AI insights failed. Check your API key or try again later.")
    await wait.edit_text(_format_insights(result))


@Client.on_message(filters.command("aihelp") & filters.user(ADMINS))
async def aihelp_command(client, message):
    await message.reply_text(
        "🤖 **AI Commands**\n\n"
        "**Any user:**\n"
        "`/aisearch <query>` — smart AI-powered search\n\n"
        "**Admin only:**\n"
        "`/aistats`    — analytics report\n"
        "`/aiinsights` — AI growth recommendations\n\n"
        "**Config vars** (set in your deployment):\n"
        "`AI_PROVIDER`      — `gemini` _(free, default)_, `groq` _(free)_,\n"
        "                     `openrouter` _(free)_, `anthropic` _(paid)_\n"
        "`AI_API_KEY`       — API key for chosen provider\n"
        "`AI_SEARCH_ENABLED`— set `true` to activate\n\n"
        "**Free API key links:**\n"
        "• Gemini → aistudio.google.com/app/apikey\n"
        "• Groq   → console.groq.com\n"
        "• OpenRouter → openrouter.ai/keys"
    )
