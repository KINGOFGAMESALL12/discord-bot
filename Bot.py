# bot.py
from flask import Flask
import threading
import os
import json
import asyncio
import hashlib
import aiohttp
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
import pytz
load_dotenv()
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    app.run(host='0.0.0.0', port=8080)

t = threading.Thread(target=run_web)
t.start()
# ==== CONFIG from .env ====
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", 0))
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", 0))
AUTO_NEWS_CHANNEL_ID = int(os.getenv("AUTO_NEWS_CHANNEL_ID", 0))

if not DISCORD_TOKEN or not OPENROUTER_API_KEY:
    raise RuntimeError("DISCORD_TOKEN и OPENROUTER_API_KEY должны быть заданы в .env")

# RSS sources
RSS_FEEDS = [
    ("РИА Новости", "https://ria.ru/export/rss2/index.xml"),
    ("ТАСС", "https://tass.ru/rss/v2.xml"),
    ("Интерфакс", "https://www.interfax.ru/rss.asp"),
    ("Lenta.ru", "https://lenta.ru/rss")
]

POSTED_DB = "posted_links.json"

# ==== Bot setup ====
intents = discord.Intents.all()  # ensure message content intent enabled in portal
bot = commands.Bot(command_prefix="/", intents=intents)

# ==== Persistence for posted links/keys ====
def load_posted():
    if os.path.exists(POSTED_DB):
        try:
            with open(POSTED_DB, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data)
        except Exception as e:
            print("Error reading posted DB:", e)
            return set()
    return set()

def save_posted(s):
    try:
        with open(POSTED_DB, "w", encoding="utf-8") as f:
            json.dump(list(s), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Error saving posted DB:", e)

posted_links = load_posted()  # holds unique keys (title_lower|link or ai_hash)

# ==== Utilities ====
def now_utc_msk():
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc.astimezone(pytz.timezone("Europe/Moscow"))
    return now_utc, now_msk

def clean_html_to_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text().strip()

def make_ai_key(text: str) -> str:
    # create deterministic small key for AI-generated news to dedupe vs RSS
    h = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()
    return f"AI|{h}"

def make_rss_key(title: str, link: str) -> str:
    return f"RSS|{title.strip().lower()}|{link}"

async def fetch_og_image(url: str, session: aiohttp.ClientSession, timeout=8) -> str | None:
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                return og["content"]
            # fallback: first image tag
            img = soup.find("img")
            if img and img.get("src"):
                return img.get("src")
    except Exception:
        return None
    return None

def extract_text_from_message(message: discord.Message) -> str:
    """Extract text meaningfully from a message (content + embed title/description/fields)."""
    parts = []
    if getattr(message, "content", None):
        c = message.content.strip()
        if c:
            parts.append(c)
    if getattr(message, "embeds", None):
        for em in message.embeds:
            if getattr(em, "title", None):
                parts.append(str(em.title).strip())
            if getattr(em, "description", None):
                parts.append(str(em.description).strip())
            if getattr(em, "fields", None):
                for f in em.fields:
                    parts.append(f"{f.name}: {f.value}")
    return "\n\n".join([p for p in parts if p]).strip()

def extract_image_from_message(message: discord.Message) -> str | None:
    # attachments
    if getattr(message, "attachments", None):
        for att in message.attachments:
            if getattr(att, "content_type", None) and att.content_type.startswith("image"):
                return att.url
            if att.filename and att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                return att.url
    # embed image / thumbnail
    if getattr(message, "embeds", None):
        for em in message.embeds:
            img = getattr(em, "image", None)
            if img and getattr(img, "url", None):
                return img.url
            thumb = getattr(em, "thumbnail", None)
            if thumb and getattr(thumb, "url", None):
                return thumb.url
    return None

def get_image_from_rss_entry(entry) -> str | None:
    # media_content
    if "media_content" in entry and entry.media_content:
        mc = entry.media_content
        if isinstance(mc, list) and mc:
            return mc[0].get("url")
        if isinstance(mc, dict):
            return mc.get("url")
    # enclosures
    if "enclosures" in entry and entry.enclosures:
        for e in entry.enclosures:
            href = e.get("href")
            if href and any(href.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                return href
    # links
    if "links" in entry:
        for l in entry.links:
            t = l.get("type", "")
            href = l.get("href")
            if href and t and t.startswith("image"):
                return href
    # summary html <img>
    summary = entry.get("summary") or entry.get("description") or ""
    if summary:
        soup = BeautifulSoup(summary, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img.get("src")
    return None

# ==== AI via OpenRouter ====
async def process_with_ai_async(original_text: str) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    system_prompt = (
        "Ты — опытный новостной редактор. Перепиши этот текст аккуратно, сохрани факты и смысл, "
        "убери служебные теги (@everyone и т.п.), исправь очевидные опечатки. Не выдумывай события."
    )
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": original_text}],
        "temperature": 0.5,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=60) as resp:
                text = await resp.text()
                if resp.status != 200:
                    print("OpenRouter error:", resp.status, text)
                    return original_text
                j = await resp.json()
                return j.get("choices", [{}])[0].get("message", {}).get("content", original_text).strip()
    except Exception as e:
        print("AI request failed:", e)
        return original_text

# ==== AI handler: listen messages in NEWS_CHANNEL_ID ====
@bot.event
async def on_message(message: discord.Message):
    # ignore only self
    if message.author and message.author.id == bot.user.id:
        return

    # debug log (optional)
    # print(f"[MSG] channel={getattr(message.channel,'id',None)} author={message.author} type={message.type} embeds={len(message.embeds)}")

    if message.channel and message.channel.id == NEWS_CHANNEL_ID:
        original_text = extract_text_from_message(message)
        if not original_text:
            # nothing useful
            await bot.process_commands(message)
            return

        # Determine source: prefer webhook/bot name (for crosspost subscriptions), else guild name
        source_name = None
        try:
            if getattr(message.author, "name", None):
                source_name = str(message.author.name).split("#")[0].strip()
        except Exception:
            source_name = None
        if not source_name:
            source_name = (message.guild.name.split("#")[0].strip() if message.guild and message.guild.name else "Неизвестный источник")

        image_url = extract_image_from_message(message)

        print("[AI] Received message for AI processing...")

        rewritten = await process_with_ai_async(original_text)
        ai_key = make_ai_key(rewritten)
        if ai_key in posted_links:
            print("[AI] Already posted similar AI news — skipping.")
            await bot.process_commands(message)
            return

        # Build embed: title = first line, body = rest
        lines = [ln.strip() for ln in rewritten.splitlines() if ln.strip()]
        title_text = lines[0][:250] if lines else "Новость"
        body_text = "\n".join(lines[1:]) if len(lines) > 1 else rewritten
        if len(body_text) > 4096:
            body_text = body_text[:4090] + "..."

        embed = discord.Embed(title=f"📰 {title_text}", description=body_text or None, color=discord.Color.blue())
        if image_url:
            embed.set_image(url=image_url)

        now_utc, now_msk = now_utc_msk()
        footer_text = f"Источник: {source_name} • UTC {now_utc.strftime('%d.%m.%Y %H:%M')} | МСК {now_msk.strftime('%H:%M')}"
        # add small prompt in footer as requested
        footer_text += " • Хочешь видеть новости своей компании? Подай заявку в обратной связи."
        embed.set_footer(text=footer_text)

        target = bot.get_channel(TARGET_CHANNEL_ID)
        if not target:
            print("[AI] TARGET_CHANNEL_ID не найден. Проверь .env и права бота.")
        else:
            try:
                await target.send(embed=embed)
                posted_links.add(ai_key)
                save_posted(posted_links)
                print("[AI] Posted AI news to target channel.")
            except Exception as e:
                print("[AI] Error sending AI embed:", e)

    # let commands run as well
    await bot.process_commands(message)

# ==== RSS/autopost loop (1 minute) ====
@tasks.loop(minutes=2.0)
async def rss_loop():
    if not bot.is_ready():
        return
    channel = bot.get_channel(AUTO_NEWS_CHANNEL_ID)
    if not channel:
        print("[RSS] AUTO_NEWS_CHANNEL_ID not found")
        return

    print("[RSS] Checking feeds...")
    new_added = False
    async with aiohttp.ClientSession() as session:
        for source_name, feed_url in RSS_FEEDS:
            try:
                feed = feedparser.parse(feed_url)
            except Exception as e:
                print(f"[RSS] parse error {feed_url}: {e}")
                continue

            feed_title = getattr(feed, "feed", {}).get("title", source_name)
            entries = getattr(feed, "entries", []) or []
            for entry in entries[:6]:
                link = entry.get("link") or entry.get("id")
                if not link:
                    continue
                title = entry.get("title", "Без заголовка").strip()
                unique_key = make_rss_key(title, link)
                if unique_key in posted_links:
                    continue

                summary = entry.get("summary") or entry.get("description") or ""
                if not summary and "content" in entry and entry.content:
                    summary = entry.content[0].get("value", "") or ""
                description = clean_html_to_text(summary)

                # try images
                image = get_image_from_rss_entry(entry)
                if not image:
                    # fallback: try fetch og:image from page
                    image = await fetch_og_image(link, session)

                embed = discord.Embed(title=title[:256], url=link, description=(description[:2048] if description else ""), color=discord.Color.gold())
                embed.set_author(name=feed_title)
                now_u, now_m = now_utc_msk()
                embed.set_footer(text=f"{feed_title} • UTC {now_u.strftime('%d.%m.%Y %H:%M')} | МСК {now_m.strftime('%H:%M')}")
                if image:
                    embed.set_image(url=image)

                try:
                    await channel.send(embed=embed)
                    print(f"[RSS] Sent: {title} ({feed_title})")
                except Exception as e:
                    print("[RSS] Error sending embed:", e)
                    continue

                posted_links.add(unique_key)
                new_added = True
                await asyncio.sleep(1.0)

    if new_added:
        save_posted(posted_links)

@rss_loop.before_loop
async def before_rss_loop():
    await bot.wait_until_ready()

# ==== Interactive /news command (manual posting) ====
@bot.command(name="news")
@commands.has_permissions(manage_messages=True)
async def cmd_news(ctx: commands.Context):
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        await ctx.send("✏️ Введите заголовок новости (или 'отмена'):")
        title_msg = await bot.wait_for("message", timeout=120.0, check=check)
        if title_msg.content.lower() == "отмена":
            return await ctx.send("Отменено.")
        title = title_msg.content.strip()

        await ctx.send("📝 Введите текст новости (или 'отмена'):")
        text_msg = await bot.wait_for("message", timeout=600.0, check=check)
        if text_msg.content.lower() == "отмена":
            return await ctx.send("Отменено.")
        text = text_msg.content.strip()

        await ctx.send("🔗 Введите ссылку-источник или отправьте '-' если нет:")
        src_msg = await bot.wait_for("message", timeout=120.0, check=check)
        src = src_msg.content.strip()
        if src == "-":
            src = None

        await ctx.send("🖼 Укажите URL картинки или отправьте '-' если нет:")
        img_msg = await bot.wait_for("message", timeout=120.0, check=check)
        img = img_msg.content.strip()
        if img == "-":
            img = None

        embed = discord.Embed(title=title[:256], description=text[:4096], color=discord.Color.blue())
        if src:
            embed.url = src
        if img:
            embed.set_image(url=img)
        now_u, now_m = now_utc_msk()
        embed.set_footer(text=f"Автор: {ctx.author.display_name} • UTC {now_u.strftime('%d.%m.%Y %H:%M')} | МСК {now_m.strftime('%H:%M')}")
        target = bot.get_channel(TARGET_CHANNEL_ID)
        if not target:
            return await ctx.send("TARGET_CHANNEL_ID не настроен.")
        await target.send(embed=embed)
        await ctx.send("✅ Новость отправлена.")
    except asyncio.TimeoutError:
        await ctx.send("⏳ Время ожидания вышло. Попробуйте снова.")

@cmd_news.error
async def cmd_news_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Недостаточно прав (нужны Manage Messages).")
    else:
        await ctx.send(f"Ошибка: {error}")

# ==== Startup ====
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} (id={bot.user.id})")
    if not rss_loop.is_running():
        rss_loop.start()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

