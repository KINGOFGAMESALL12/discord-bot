from flask import Flask
import threading
import os
import json
import asyncio
import hashlib
import aiohttp
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone
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

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", 0))
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", 0))
AUTO_NEWS_CHANNEL_ID = int(os.getenv("AUTO_NEWS_CHANNEL_ID", 0))

if not DISCORD_TOKEN or not OPENROUTER_API_KEY:
    raise RuntimeError("DISCORD_TOKEN и OPENROUTER_API_KEY должны быть заданы в .env")

RSS_FEEDS = [
    ("РИА Новости", "https://ria.ru/export/rss2/index.xml"),
    ("ТАСС", "https://tass.ru/rss/v2.xml"),
    ("Интерфакс", "https://www.interfax.ru/rss.asp"),
    ("Lenta.ru", "https://lenta.ru/rss")
]

POSTED_DB = "posted_links.json"
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="/", intents=intents)

def load_posted():
    if os.path.exists(POSTED_DB):
        try:
            with open(POSTED_DB, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print("Error reading posted DB:", e)
    return set()

def save_posted(s):
    try:
        with open(POSTED_DB, "w", encoding="utf-8") as f:
            json.dump(list(s), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Error saving posted DB:", e)

posted_links = load_posted()

def now_utc_msk():
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc.astimezone(pytz.timezone("Europe/Moscow"))
    return now_utc, now_msk

def clean_html_to_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text().strip()

def make_ai_key(text: str) -> str:
    h = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()
    return f"AI|{h}"

def make_rss_key(title: str, link: str) -> str:
    return f"RSS|{title.strip().lower()}|{link}"

def extract_text_from_message(message: discord.Message) -> str:
    parts = []
    if message.content:
        parts.append(message.content.strip())
    for em in message.embeds:
        if em.title:
            parts.append(em.title.strip())
        if em.description:
            parts.append(em.description.strip())
        for f in em.fields:
            parts.append(f"{f.name}: {f.value}")
    return "\n\n".join([p for p in parts if p]).strip()

def extract_image_from_message(message: discord.Message) -> str | None:
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image"):
            return att.url
    for em in message.embeds:
        if em.image and em.image.url:
            return em.image.url
        if em.thumbnail and em.thumbnail.url:
            return em.thumbnail.url
    return None

async def process_with_ai_async(original_text: str) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    system_prompt = (
        "Ты — опытный новостной редактор. Перепиши этот текст аккуратно, сохрани факты и смысл, "
        "убери служебные теги (@everyone и т.п.), исправь очевидные опечатки. Не выдумывай события."
    )
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": original_text}
        ],
        "temperature": 0.5,
    }

    print(f"[AI DEBUG] Отправка в OpenRouter:\n{original_text}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=60) as resp:
                text_resp = await resp.text()
                print(f"[AI DEBUG] Ответ OpenRouter (raw): {text_resp}")
                if resp.status != 200:
                    print("OpenRouter error:", resp.status)
                    return original_text
                j = await resp.json()
                return j.get("choices", [{}])[0].get("message", {}).get("content", original_text).strip()
    except Exception as e:
        print("AI request failed:", e)
        return original_text

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return

    if message.channel.id == NEWS_CHANNEL_ID:
        print(f"[AI DEBUG] Получено сообщение в NEWS_CHANNEL_ID: {message.id}")
        original_text = extract_text_from_message(message)
        print(f"[AI DEBUG] Извлечённый текст:\n{original_text}")

        if not original_text:
            print("[AI DEBUG] Текст пустой, пропускаем.")
            return await bot.process_commands(message)

        rewritten = await process_with_ai_async(original_text)
        print(f"[AI DEBUG] Переписанный текст:\n{rewritten}")

        ai_key = make_ai_key(rewritten)
        if ai_key in posted_links:
            print("[AI DEBUG] Новость уже публиковалась, пропускаем.")
            return await bot.process_commands(message)

        embed = discord.Embed(
            title=f"📰 {rewritten.splitlines()[0][:250]}",
            description="\n".join(rewritten.splitlines()[1:]) or rewritten,
            color=discord.Color.blue()
        )
        img = extract_image_from_message(message)
        if img:
            embed.set_image(url=img)

        now_utc, now_msk = now_utc_msk()
        embed.set_footer(text=f"UTC {now_utc} | МСК {now_msk}")
        target = bot.get_channel(TARGET_CHANNEL_ID)

        if target:
            await target.send(embed=embed)
            posted_links.add(ai_key)
            save_posted(posted_links)
            print("[AI DEBUG] Новость отправлена в TARGET_CHANNEL_ID")
        else:
            print("[AI DEBUG] Не найден TARGET_CHANNEL_ID")

    await bot.process_commands(message)

@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} (id={bot.user.id})")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
