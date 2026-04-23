import os
import json
import logging
from datetime import datetime, timezone, timedelta

import anthropic
import requests
from dotenv import load_dotenv
from supabase import create_client
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

load_dotenv(override=True)

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Tools ---
TOOLS = [
    {
        "name": "simdi_ne",
        "description": "Şu anki tarih ve saati döndürür (Türkiye saati, UTC+3).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "not_ekle",
        "description": "Kullanıcının notunu Supabase veritabanına kaydeder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "icerik": {"type": "string", "description": "Kaydedilecek not içeriği"}
            },
            "required": ["icerik"],
        },
    },
    {
        "name": "not_ara",
        "description": "Supabase'teki notlarda anahtar kelime arar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sorgu": {"type": "string", "description": "Aranacak kelime veya ifade"}
            },
            "required": ["sorgu"],
        },
    },
    {
        "name": "web_ara",
        "description": "Tavily API ile internette arama yapar ve sonuçları döndürür.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sorgu": {"type": "string", "description": "Aranacak konu"}
            },
            "required": ["sorgu"],
        },
    },
]

SYSTEM_PROMPT = """Sen yardımsever bir Türkçe asistansın. Kullanıcıya kısa ve net cevaplar ver.
Elindeki araçlar:
- simdi_ne: Tarih ve saat öğrenmek için
- not_ekle: Kullanıcının notlarını kaydetmek için
- not_ara: Kayıtlı notlarda arama yapmak için
- web_ara: İnternette güncel bilgi aramak için

Araçları gerektiğinde kullan. Her zaman Türkçe cevap ver."""


def run_tool(name: str, input_data: dict, user_id: int) -> str:
    if name == "simdi_ne":
        tr_time = datetime.now(timezone(timedelta(hours=3)))
        return tr_time.strftime("%d %B %Y, %A — %H:%M:%S (Türkiye saati)")

    if name == "not_ekle":
        icerik = input_data["icerik"]
        supabase.table("notes").insert({
            "user_id": str(user_id),
            "content": icerik,
        }).execute()
        return f"Not kaydedildi: {icerik}"

    if name == "not_ara":
        sorgu = input_data["sorgu"]
        result = (
            supabase.table("notes")
            .select("content, created_at")
            .eq("user_id", str(user_id))
            .ilike("content", f"%{sorgu}%")
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        if not result.data:
            return f"'{sorgu}' ile eşleşen not bulunamadı."
        lines = []
        for row in result.data:
            lines.append(f"- {row['content']}  ({row['created_at'][:10]})")
        return "\n".join(lines)

    if name == "web_ara":
        if not TAVILY_API_KEY:
            return "Web arama yapılandırılmamış (TAVILY_API_KEY eksik)."
        sorgu = input_data["sorgu"]
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": TAVILY_API_KEY, "query": sorgu, "max_results": 3},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return "Sonuç bulunamadı."
        lines = []
        for r in results:
            lines.append(f"- {r['title']}: {r['content'][:200]}")
        return "\n".join(lines)

    return f"Bilinmeyen araç: {name}"


# --- Chat with Claude ---
def chat_with_claude(user_message: str, user_id: int) -> str:
    messages = [{"role": "user", "content": user_message}]

    # Claude may request multiple tool calls in a loop
    for _ in range(5):
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Check if Claude wants to use a tool
        if response.stop_reason == "tool_use":
            # Collect all text + tool results
            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    result = run_tool(block.name, block.input, user_id)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})
        else:
            # Final text response
            text_parts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_parts)

    return "Bir hata oluştu, lütfen tekrar dene."


# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba! Ben senin AI asistanınım. Sana nasıl yardımcı olabilirim?"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_id = update.effective_user.id

    await update.message.chat.send_action("typing")

    try:
        reply = chat_with_claude(user_message, user_id)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        reply = f"Hata: {e}"

    await update.message.reply_text(reply)


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot çalışıyor.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
