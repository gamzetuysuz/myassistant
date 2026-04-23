import os
import json
import logging
import base64
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

import anthropic
import requests
from dotenv import load_dotenv
from supabase import create_client
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv(override=True)

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# --- Google OAuth ---
oauth_flows = {}  # user_id -> Flow (temporary, during auth)


def get_google_flow():
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    return Flow.from_client_config(client_config, scopes=GOOGLE_SCOPES, redirect_uri="http://localhost")


def save_google_tokens(user_id: int, creds: Credentials):
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
    }
    existing = supabase.table("google_tokens").select("id").eq("user_id", str(user_id)).execute()
    if existing.data:
        supabase.table("google_tokens").update({"tokens": json.dumps(token_data)}).eq("user_id", str(user_id)).execute()
    else:
        supabase.table("google_tokens").insert({"user_id": str(user_id), "tokens": json.dumps(token_data)}).execute()


def get_google_creds(user_id: int):
    result = supabase.table("google_tokens").select("tokens").eq("user_id", str(user_id)).execute()
    if not result.data:
        return None
    token_data = json.loads(result.data[0]["tokens"])
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=GOOGLE_SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_google_tokens(user_id, creds)
    return creds


def get_gmail_service(user_id: int):
    creds = get_google_creds(user_id)
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)


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
    {
        "name": "gmail_oku",
        "description": "Gmail'deki son emailleri okur. Konu, gönderen ve özet bilgisi döndürür.",
        "input_schema": {
            "type": "object",
            "properties": {
                "adet": {"type": "integer", "description": "Kaç email okunacak (varsayılan 5, max 10)"}
            },
            "required": [],
        },
    },
    {
        "name": "gmail_ara",
        "description": "Gmail'de anahtar kelime ile email arar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sorgu": {"type": "string", "description": "Aranacak kelime (konu, gönderen, içerik)"}
            },
            "required": ["sorgu"],
        },
    },
    {
        "name": "gmail_gonder",
        "description": "Gmail üzerinden email gönderir.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kime": {"type": "string", "description": "Alıcı email adresi"},
                "konu": {"type": "string", "description": "Email konusu"},
                "icerik": {"type": "string", "description": "Email içeriği"},
            },
            "required": ["kime", "konu", "icerik"],
        },
    },
]

SYSTEM_PROMPT = """SENİN ROLÜN
Sen, kullanıcının Telegram üzerinden çalışan kişisel e-mail asistanısın.
Ana görevin Gmail'deki e-mail ve thread'leri doğru şekilde okuyup:
1) günlük özet hazırlamak,
2) cevap bekleyenleri tespit etmek,
3) önemli aksiyonları ayıklamak,
4) kullanıcının sorması halinde detaylı açıklama yapmak,
5) aynı konuyu tekrar tekrar saymadan net ve güvenilir bilgi vermektir.

Elindeki araçlar:
- simdi_ne: Tarih ve saat öğrenmek için
- not_ekle: Kullanıcının notlarını kaydetmek için
- not_ara: Kayıtlı notlarda arama yapmak için
- web_ara: İnternette güncel bilgi aramak için
- gmail_oku: Son emailleri okumak için
- gmail_ara: Gmail'de email aramak için
- gmail_gonder: Email göndermek için

TEMEL DAVRANIŞ KURALLARI
- Gmail bağlantısı aktifse kullanıcıya tekrar "Gmail bağla" deme.
- Bir mesajı tek başına değil, mümkün olduğunda thread bütünlüğü içinde değerlendir.
- Aynı konuya ait birden fazla e-mail varsa bunları tek konu olarak ele al.
- Kullanıcı daha önce yanıt vermişse bunu thread içinde dikkate al.
- "Cevapsız" ile "henüz kapanmamış / takip gerektiren" kavramlarını karıştırma.
- Emin olmadığın durumda kesin hüküm verme; "muhtemelen", "görünene göre", "son görünen duruma göre" gibi ifadeler kullan.
- Önce durumu anla, sonra özetle, sonra aksiyon öner.
- Gereksiz uzun yazma; ama önemli ayrıntıları atlama.
- Aynı bilgiyi farklı cümlelerle tekrar etme.

GMAIL BAĞLANTI DURUMU KURALI
- Eğer Gmail aracından veri dönüyorsa, Gmail bağlı kabul edilir.
- Bu durumda kullanıcıya yeniden bağlamasını asla önerme.
- Yalnızca Gmail aracı gerçekten hata döndüğünde yeniden bağlantı iste.

THREAD BAZLI OKUMA KURALI
- Öncelik tek tek e-mail değil, thread'dir.
- Aynı subject veya reply zinciri olan iletileri aynı konu kabul et.
- Bir thread içinde son gelen mesaj, kullanıcının en son yanıtı, karşı tarafın kullanıcıdan aksiyon bekleyip beklemediği ve konunun kapanıp kapanmadığı birlikte değerlendirilmelidir.
- Eğer kullanıcı bir thread'e cevap verdiyse, bunu "cevap verildi" olarak işaretle; ancak konu hâlâ kullanıcı aksiyonu bekliyorsa "takip gerekebilir" olarak ayrıca belirt.
- "Cevapsız" sadece kullanıcının henüz yanıt vermediği ve yanıt beklenen thread'ler için kullanılmalı.

E-MAIL SINIFLANDIRMA
Her thread'i şu kategorilerden uygun olanlarla etiketle:
ACIL, BUGUN_TAKIP, BU_HAFTA_TAKIP, BILGI, BEKLEMEDE, CEVAP_VERILDI, KULLANICIDAN_AKSIYON_BEKLIYOR, KARSI_TARAFTAN_DONUS_BEKLENIYOR, ARSIVLIK

ÖNEMLİLİK KURALI
Önemli say: doğrudan kullanıcıdan yanıt bekleniyorsa, tarih/deadline/toplantı/ödeme/onay varsa, müşteri/iş/sözleşme/fatura/teklif/teslim/operasyon/teknik sorun içeriyorsa, son mesajda soru sorulmuşsa, risk/gecikme/sorun/hata/şikayet varsa, VIP/kilit kişi ile ilgiliyse.
Önemsiz say: otomatik bildirimler, pazarlama bültenleri, promosyonlar, sosyal ağ bildirimleri.

DETAYLI INCELEME KURALI
Kullanıcı "daha detaylı anlat" derse: Gmail bağlıysa tekrar bağlanmasını isteme, ilgili thread'i yeniden oku, son mesajı thread geçmişiyle birlikte açıkla.
Format: 1) konu özeti, 2) şu ana kadar olanlar, 3) en son mesajın ana noktası, 4) kullanıcıdan beklenen aksiyon, 5) önerilen kısa cevap taslağı (istenirse).

CEVAPSIZ MAIL TESPIT KURALI
Cevapsız say: karşı taraftan gelen son mesaj kullanıcıya hitap ediyor, içinde soru/talep/onay isteği var, kullanıcıdan sonra yanıt gönderilmemiş, konu kapanmamış.
Cevapsız SAYMA: kullanıcı zaten yanıt verdiyse, son adım karşı taraftaysa, otomatik kapanış mesajıysa, sistem bildirimiyse.

TON: Net, sakin, operasyonel, kullanıcıyı yormayan, gereksiz resmiyet olmadan profesyonel.

OTOMATİK FİLTRELE (kullanıcı özellikle sormadığı sürece dahil etme):
- Meta Ads / Facebook Ads ödeme bildirimleri
- Sosyal medya bildirimleri (LinkedIn, Instagram, Facebook, Twitter vb.)
- Otomatik sistem bildirimleri ve newsletter'lar
Bu tür mailler özetlere, listelere ve analizlere dahil edilmez. Sadece kullanıcı açıkça sorarsa gösterilir.

ASLA YAPMA: Gmail bağlıyken tekrar bağla deme, tek maili tüm konu sanma, aynı thread'i iki kez yazma, kullanıcının cevap verdiği bir maili cevapsız sayma, belirsiz bir şeyi kesinmiş gibi yazma, gereksiz uzun ve dağınık özet çıkarma."""


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

    if name == "gmail_oku":
        service = get_gmail_service(user_id)
        if not service:
            return "Gmail bağlı değil. Önce /gmail_bagla komutunu kullan."
        adet = min(input_data.get("adet", 5), 10)
        results = service.users().messages().list(userId="me", maxResults=adet).execute()
        messages = results.get("messages", [])
        if not messages:
            return "Gelen kutusunda email yok."
        return _format_emails(service, messages)

    if name == "gmail_ara":
        service = get_gmail_service(user_id)
        if not service:
            return "Gmail bağlı değil. Önce /gmail_bagla komutunu kullan."
        sorgu = input_data["sorgu"]
        results = service.users().messages().list(userId="me", q=sorgu, maxResults=5).execute()
        messages = results.get("messages", [])
        if not messages:
            return f"'{sorgu}' ile eşleşen email bulunamadı."
        return _format_emails(service, messages)

    if name == "gmail_gonder":
        service = get_gmail_service(user_id)
        if not service:
            return "Gmail bağlı değil. Önce /gmail_bagla komutunu kullan."
        message = MIMEText(input_data["icerik"])
        message["to"] = input_data["kime"]
        message["subject"] = input_data["konu"]
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email gönderildi: {input_data['kime']}"

    return f"Bilinmeyen araç: {name}"


def _get_email_body(payload):
    """Email gövdesini çıkar (plain text veya HTML'den)."""
    body = ""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    elif payload.get("parts"):
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                break
            elif part.get("parts"):
                for sub in part["parts"]:
                    if sub.get("mimeType") == "text/plain" and sub.get("body", {}).get("data"):
                        body = base64.urlsafe_b64decode(sub["body"]["data"]).decode("utf-8", errors="replace")
                        break
                if body:
                    break
    return body.strip()[:1500]  # max 1500 karakter


def _format_emails(service, messages):
    """Email detaylarını thread bilgisi ve tam içerikle formatla."""
    lines = []
    for msg_ref in messages:
        msg = service.users().messages().get(userId="me", id=msg_ref["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        thread_id = msg.get("threadId", "")
        labels = msg.get("labelIds", [])

        # Thread bilgisi — tüm mesajları al
        thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        thread_msgs = thread.get("messages", [])
        thread_count = len(thread_msgs)

        # Kim cevap vermiş
        senders = set()
        for m in thread_msgs:
            for h in m["payload"]["headers"]:
                if h["name"] == "From":
                    senders.add(h["value"])

        # Email gövdesini oku
        body = _get_email_body(msg["payload"])
        if not body:
            body = msg.get("snippet", "")

        # Thread'deki tüm cevapları da oku
        thread_summary = ""
        if thread_count > 1:
            thread_parts = []
            for tm in thread_msgs:
                tm_headers = {h["name"]: h["value"] for h in tm["payload"]["headers"]}
                tm_body = _get_email_body(tm["payload"])
                if not tm_body:
                    tm_body = tm.get("snippet", "")
                thread_parts.append(f"  > {tm_headers.get('From', '?')} ({tm_headers.get('Date', '?')[:16]}):\n  {tm_body[:500]}")
            thread_summary = "\nKonuşma geçmişi:\n" + "\n".join(thread_parts)

        # Durum
        if thread_count == 1:
            durum = "Cevap yok"
        else:
            durum = f"{thread_count} mesaj (yazanlar: {', '.join(senders)})"

        okundu = "Okunmadı" if "UNREAD" in labels else "Okundu"

        lines.append(
            f"---\n"
            f"Konu: {headers.get('Subject', '(konu yok)')}\n"
            f"Kimden: {headers.get('From', '?')}\n"
            f"Kime: {headers.get('To', '?')}\n"
            f"Tarih: {headers.get('Date', '?')}\n"
            f"Durum: {okundu} | {durum}\n"
            f"İçerik:\n{body}"
            f"{thread_summary}"
        )
    return "\n".join(lines)


# --- Conversation History ---
conversation_history = {}  # user_id -> list of messages
MAX_HISTORY = 20  # son 20 mesaj tutulur


def get_history(user_id: int) -> list:
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    return conversation_history[user_id]


def add_to_history(user_id: int, role: str, content):
    history = get_history(user_id)
    history.append({"role": role, "content": content})
    # Geçmişi sınırla (tool_result çiftlerini koruyarak)
    while len(history) > MAX_HISTORY:
        history.pop(0)


# --- Chat with Claude ---
def chat_with_claude(user_message: str, user_id: int) -> str:
    history = get_history(user_id)
    history.append({"role": "user", "content": user_message})

    messages = list(history)

    for _ in range(5):
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
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
            text_parts = [b.text for b in response.content if b.type == "text"]
            reply = "\n".join(text_parts)
            # Tüm mesajları (tool çağrıları dahil) geçmişe kaydet
            conversation_history[user_id] = messages.copy()
            conversation_history[user_id].append({"role": "assistant", "content": reply})
            # Geçmişi sınırla
            while len(conversation_history[user_id]) > MAX_HISTORY:
                conversation_history[user_id].pop(0)
            return reply

    return "Bir hata oluştu, lütfen tekrar dene."


# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Merhaba! Ben senin AI asistanınım.\n\n"
        "Yapabileceklerim:\n"
        "- Soru cevapla\n"
        "- Not al ve ara\n"
        "- Web'de ara\n"
        "- Gmail oku, ara, gönder\n\n"
        "Gmail için: /gmail_bagla"
    )


async def gmail_bagla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GOOGLE_CLIENT_ID:
        await update.message.reply_text("Google OAuth yapılandırılmamış.")
        return
    user_id = update.effective_user.id
    flow = get_google_flow()
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    oauth_flows[user_id] = flow
    await update.message.reply_text(
        f"Gmail'ini bağlamak için:\n\n"
        f"1. Bu linki aç:\n{auth_url}\n\n"
        f"2. Google hesabınla giriş yap ve izin ver\n"
        f"3. Sayfa yüklenemez hatası verecek — bu normal!\n"
        f"4. Tarayıcının adres çubuğundaki URL'den code= sonrasını kopyala\n"
        f"   (http://localhost?code=BURASI&scope=... → sadece BURASI kısmı)\n"
        f"5. /gmail_kod KODUNUZ yazarak gönder"
    )


async def gmail_kod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in oauth_flows:
        await update.message.reply_text("Önce /gmail_bagla komutunu kullan.")
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /gmail_kod KODUNUZ")
        return
    code = context.args[0]
    flow = oauth_flows[user_id]
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        save_google_tokens(user_id, creds)
        del oauth_flows[user_id]
        await update.message.reply_text("Gmail başarıyla bağlandı! Artık email okuyabilir, arayabilir ve gönderebilirsin.")
    except Exception as e:
        logger.error(f"Gmail OAuth error: {e}", exc_info=True)
        await update.message.reply_text(f"Hata: {e}\n\nTekrar dene: /gmail_bagla")


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
    app.add_handler(CommandHandler("gmail_bagla", gmail_bagla))
    app.add_handler(CommandHandler("gmail_kod", gmail_kod))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot çalışıyor.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
