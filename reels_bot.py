import os
import re
import io
import sys
import json
import time
import glob
import asyncio
import logging
import requests
import subprocess
from bs4 import BeautifulSoup
from g4f.client import Client
import edge_tts
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
client = Client()

# ================= 1. CONFIG =================
BUFFER_ACCESS_TOKEN = os.environ.get("BUFFER_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2", "").strip()
OUTPUT_DIR = "generated_reels"
os.makedirs(OUTPUT_DIR, exist_ok=True)
DEFAULT_FALLBACK_IMAGE = "https://via.placeholder.com/1080x1920.png?text=Product+Image"
MAX_CHUNK_CHARACTERS = 1000
MAX_RETRIES = 3

# ---- Reel layout ----
MAX_IMAGES = 5            # reel me zyada se zyada itni images
IMG_SHOW_RATIO = 0.65     # har scene me image sirf itne hisse (60-70%) tak dikhti hai, baaki time sirf scroll
CARD_WIDTH = 648          # 1080 ka 60%
CARD_PAD = 56             # card ke charo taraf shadow ki jagah
CARD_CENTER_Y = 780       # image card ka center (caption se upar)
CAPTION_CENTER_Y = 1305   # caption ka center (image card ke neeche)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def notify_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }, timeout=20)
    except Exception as e:
        logging.warning(f"⚠️ Telegram notify failed: {e}")


# ================= 2. FONTS =================
def get_system_font(font_size=55):
    font_paths = [
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for p in font_paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, font_size)
            except Exception:
                pass
    return ImageFont.load_default()


def remove_emojis(text):
    return re.sub(r'[^\x00-\x7F]+', '', text).strip()


def sanitize_filename(name):
    clean = re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')
    return clean[:30] if clean else "product_reel"


# ================= 3. SCRAPER =================
def unshorten_amazon_url(url, session):
    if not any(d in url for d in ["amzn.to", "link.amazon", "earnkaro", "fktr.in", "linkredirect.in"]):
        return url
    try:
        res = session.get(url, allow_redirects=True, timeout=15)
        if "amazon." in res.url or "flipkart." in res.url:
            return res.url
    except Exception:
        pass
    return url


def scrape_bgtechlab_page(url, session):
    data = {
        "title": "", "category": "", "price": "Special Offer",
        "has_real_price": False, "image": "", "extra_images": [], "features": []
    }
    try:
        res = session.get(url, timeout=20)
        soup = BeautifulSoup(res.content, "html.parser")
        page_text = soup.get_text(" ", strip=True)

        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
        if not title:
            og = soup.find("meta", {"property": "og:title"})
            title = og.get("content", "") if og else ""
        title = re.sub(r"\s*Review\s*\(\d{4}\)\s*$", "", title, flags=re.I).strip()
        data["title"] = title[:80] or "Trending Product Deal"

        cat_match = re.search(r"([A-Za-z][\w &]{1,30}?)\s*✓\s*Verified Expert Review", page_text)
        if cat_match:
            data["category"] = cat_match.group(1).strip()

        price_match = re.search(r"Current Best Deal Price[^\d₹]*₹\s*([\d,]+)", page_text)
        if price_match:
            data["price"] = f"₹{price_match.group(1)}"
            data["has_real_price"] = True

        image_urls = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if any(c in src for c in ["flixcart.com", "media-amazon.com"]) and not any(
                b in src.lower() for b in ["logo", "icon", "sprite"]
            ):
                clean = src.split("?")[0]
                if clean not in image_urls:
                    image_urls.append(clean)
        if not image_urls:
            og_img = soup.find("meta", {"property": "og:image"})
            if og_img and og_img.get("content"):
                image_urls = [og_img["content"]]

        # sirf alag-alag (unique) images, max 5. Repeat karke 5 nahi banate; kam mili to utne hi scene me image aayegi
        unique_imgs = list(dict.fromkeys(u for u in image_urls if u and u != DEFAULT_FALLBACK_IMAGE))[:MAX_IMAGES]
        data["image"] = unique_imgs[0] if unique_imgs else DEFAULT_FALLBACK_IMAGE
        data["extra_images"] = unique_imgs

        pros = soup.find(string=re.compile(r"What We Like", re.I))
        if pros:
            ul = pros.find_parent().find_next("ul")
            if ul:
                for li in ul.find_all("li")[:5]:
                    txt = li.get_text(strip=True).lstrip("✓✔-• ").strip()
                    if txt:
                        data["features"].append(txt)

        logging.info(f"✅ Page: {data['title']} | {data['category']} | {data['price']}")
    except Exception as e:
        logging.error(f"⚠️ Scrape error: {e}")
    return data


def scrape_product_details(url):
    logging.info(f"🔄 Fetching: {url[:60]}...")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    if "github.io" in url:
        return scrape_bgtechlab_page(url, session)

    data = {
        "title": "", "category": "", "price": "Special Offer",
        "has_real_price": False, "image": "", "extra_images": [], "features": []
    }
    res_url = unshorten_amazon_url(url, session)
    try:
        res = session.get(res_url, allow_redirects=True, timeout=20)
        soup = BeautifulSoup(res.content, "html.parser")
        title_elem = (
            soup.find("span", {"id": "productTitle"})
            or soup.find("span", {"class": "B_NuCI"})
            or soup.find("meta", {"property": "og:title"})
        )
        if title_elem:
            raw = title_elem.get("content") if title_elem.name == "meta" else title_elem.get_text()
            data["title"] = raw.strip().replace("\n", " ")[:70]
        if not data["title"]:
            data["title"] = "Trending Gadget Deal"

        image_urls = []
        og = soup.find("meta", {"property": "og:image"})
        if og and og.get("content"):
            image_urls = [og["content"]]
        data["image"] = image_urls[0] if image_urls else DEFAULT_FALLBACK_IMAGE
        data["extra_images"] = image_urls[:MAX_IMAGES]
    except Exception as e:
        logging.error(f"⚠️ Scrape error: {e}")
        data["title"] = data.get("title") or "Trending Gadget Deal"
        data["extra_images"] = []
        data["image"] = DEFAULT_FALLBACK_IMAGE
    return data


# ================= 4. SCRIPT (Gemini + fallback) =================
def generate_reel_script(product_data):
    logging.info("🤖 Generating Script (Gemini + Fallback)...")
    title = product_data["title"]
    price = product_data["price"]
    category = product_data.get("category") or "product"
    category_tag = re.sub(r"\W+", "", category) or "deals"
    features_str = " | ".join(product_data["features"]) if product_data["features"] else f"Great quality {category}"

    def _normalize(data):
        if not isinstance(data, dict):
            data = {}
        script = data.get("script") or data.get("Script") or data.get("text") or data.get("voiceover") or ""
        caption = data.get("caption") or data.get("Caption") or f"Best Deal on {title}! #deals #{category_tag}"
        hook = data.get("hook_text") or data.get("hook") or "VIRAL DEAL ALERT!"
        key_f = data.get("key_feature") or data.get("key_feature_text") or f"Best Price: {price}"
        cta = data.get("cta_text") or data.get("cta") or "Link in Description!"
        if not script or len(str(script)) < 30:
            script = (
                f"Kya aap ek behtareen {category} dhoond rahe hain jisme achhi quality bhi ho aur price bhi sahi ho? "
                f"Pesh hai {title}! Isme aapko milta hai {features_str}. Yeh dikhne me kafi premium hai. "
                f"Is time bada price drop offer chal raha hai. Description me diye link par visit karein!"
            )
        return {
            "script": str(script),
            "caption": str(caption),
            "hook_text": str(hook),
            "key_feature": str(key_f),
            "cta_text": str(cta),
        }

    prompt = f"""You are an expert viral Instagram Reel creator. Write a 45 SECONDS script in ROMAN ENGLISH / HINGLISH only.
PRODUCT CATEGORY: {category}
PRODUCT: {title}
PRICE: {price}
KEY FEATURES: {features_str}
RULES:
1. Script 100-110 words only.
2. Only Roman English/Hinglish, no Devanagari.
3. No greetings like Hello/Namaskar.
4. Start with strong hook question.
5. Cover features and price.
Return ONLY valid JSON:
{{
  "script": "...",
  "caption": "Deal on {title} #deals #{category_tag}",
  "hook_text": "VIRAL DEAL ALERT!",
  "key_feature": "Best Price: {price}",
  "cta_text": "Link in Description!"
}}"""

    gemini_keys = [k for k in [GEMINI_API_KEY, GEMINI_API_KEY_2] if k]
    for idx, api_key in enumerate(gemini_keys, 1):
        try:
            logging.info(f"🤖 Trying Gemini key #{idx}...")
            for model_name in ["gemini-3.6-flash", "gemini-3.8-flash", "gemini-flash-latest"]:
                for attempt in range(1, 4):
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
                    payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.7,
                            "maxOutputTokens": 2048,
                            "responseMimeType": "application/json",
                        },
                    }
                    res = requests.post(url, json=payload, timeout=60)
                    if res.status_code == 503:
                        logging.warning(f"⚠️ {model_name} 503, retry {attempt}/3")
                        time.sleep(3 * attempt)
                        continue
                    if res.status_code == 200:
                        data = res.json()
                        raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
                        start, end = clean.find("{"), clean.rfind("}")
                        if start != -1 and end != -1:
                            clean = clean[start : end + 1]
                        try:
                            parsed = json.loads(clean)
                            logging.info(f"✅ Gemini ({model_name}) OK")
                            return _normalize(parsed)
                        except json.JSONDecodeError as je:
                            logging.warning(f"⚠️ JSON parse fail: {je}")
                            break
                    else:
                        logging.warning(f"⚠️ {model_name}: {res.status_code} {res.text[:120]}")
                        break
        except Exception as e:
            logging.warning(f"⚠️ Gemini key #{idx}: {e}")

    try:
        logging.info("🤖 g4f fallback...")
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = res.choices[0].message.content.strip()
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        start, end = clean.find("{"), clean.rfind("}")
        if start != -1 and end != -1:
            clean = clean[start : end + 1]
        return _normalize(json.loads(clean))
    except Exception as e:
        logging.error(f"⚠️ AI Script Error: {e}")
        notify_telegram(f"⚠️ AI script fail, fallback use.\n<code>{e}</code>")
        return _normalize({})


# ================= 5. VOICE =================
def split_script_into_chunks(script, max_chars=MAX_CHUNK_CHARACTERS):
    sentences = re.split(r"([.!?|\n])", script)
    chunks, current = [], ""
    for i in range(0, len(sentences), 2):
        sentence = sentences[i]
        punct = sentences[i + 1] if i + 1 < len(sentences) else ""
        full = sentence + punct
        if len(current) + len(full) <= max_chars:
            current += full
        else:
            if current:
                chunks.append(current.strip())
            current = full
    if current:
        chunks.append(current.strip())
    return chunks or [script]


def generate_edge_tts_voice(text, output_audio_path):
    async def _save():
        communicate = edge_tts.Communicate(text, "hi-IN-MadhurNeural", rate="+25%")
        await communicate.save(output_audio_path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_save())
    loop.close()


def generate_voiceover(text, output_audio_path):
    logging.info("🎙️ Generating voiceover (Edge-TTS)...")
    temp_dir = os.path.join(OUTPUT_DIR, "temp_voice")
    os.makedirs(temp_dir, exist_ok=True)
    chunks = split_script_into_chunks(text)
    audio_parts = []

    for idx, chunk in enumerate(chunks, 1):
        part = os.path.join(temp_dir, f"part_{idx:03d}.mp3")
        try:
            logging.info(f"🔊 Edge-TTS Part {idx}/{len(chunks)}...")
            generate_edge_tts_voice(chunk, part)
            if os.path.exists(part) and os.path.getsize(part) >= 4000:
                audio_parts.append(part)
                continue
            raise Exception("too small")
        except Exception as e:
            logging.warning(f"⚠️ Edge fail, gTTS: {e}")
            try:
                gTTS(text=chunk, lang="hi").save(part)
                if os.path.exists(part) and os.path.getsize(part) >= 4000:
                    audio_parts.append(part)
                else:
                    return False
            except Exception as e2:
                logging.error(f"❌ Voice fail: {e2}")
                return False

    list_file = os.path.join(temp_dir, "concat_list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in audio_parts:
            f.write(f"file '{os.path.abspath(p).replace(chr(92), '/')}'\n")
    try:
        cmd = [
            "ffmpeg", "-f", "concat", "-safe", "0", "-i", list_file,
            "-af", "atempo=1.25,loudnorm=I=-16:LRA=11:TP=-1.5",
            "-ar", "44100", "-ac", "2", "-b:a", "128k", "-c:a", "libmp3lame",
            "-y", output_audio_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        logging.info(f"🔊 Audio OK: {output_audio_path}")
        return True
    except Exception as e:
        logging.error(f"❌ Audio concat: {e}")
        return False


# ================= 6. CARDS + FFMPEG =================
def create_text_card_image(text, width=1000, height=150, bg_color=(220, 20, 50),
                           text_color=(255, 255, 255), font_size=55, save_name="card.png"):
    clean = remove_emojis(text.replace("₹", "Rs "))  # font me ₹ nahi hota, isliye Rs likhte hain
    img = Image.new("RGBA", (width, height), bg_color + (240,))
    draw = ImageDraw.Draw(img)
    font = get_system_font(font_size)
    bbox = draw.textbbox((0, 0), clean, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((width - tw) / 2, (height - th) / 2), clean, fill=text_color, font=font)
    path = os.path.join(OUTPUT_DIR, save_name)
    img.save(path)
    return path


# ----- Captions: bola ja raha word highlight hota hai -----
CAPTION_W = 980
CAPTION_H = 230
PAUSE_WEIGHTS = {",": 3, ";": 3, ":": 3, "\n": 4, ".": 6, "!": 6, "?": 6, "|": 6, "।": 6}


def build_caption_phrases(script, max_words=6):
    """Script ko chhote caption phrases me todta hai. Har phrase = [(word, pause_weight), ...]."""
    script = script.replace("₹", "Rs ")
    script = re.sub(r"(?<=\d),(?=\d)", "", script)  # 1,299 ko 1299 bana do taaki comma par na tute
    parts = re.split(r"([,.!?|।\n:;])", script)
    phrases = []
    for i in range(0, len(parts), 2):
        words = remove_emojis(parts[i]).upper().split()
        if not words:
            continue
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        end_pause = PAUSE_WEIGHTS.get(delim, 0)
        if len(words) <= max_words + 1:
            chunks = [words]
        else:
            n_chunks = -(-len(words) // max_words)
            size = -(-len(words) // n_chunks)
            chunks = [words[j:j + size] for j in range(0, len(words), size)]
        for ci, chunk in enumerate(chunks):
            pause = end_pause if ci == len(chunks) - 1 else 1
            phrases.append([(w, pause if wi == len(chunk) - 1 else 0) for wi, w in enumerate(chunk)])
    return phrases or [[(remove_emojis(script).upper() or "DEAL", 0)]]


def compute_word_timings(phrases, total_dur):
    """Har word ka time uski length (aur punctuation ke pause) ke hisaab se -> voice ke saath approx sync."""
    weights = []
    for ph in phrases:
        for w, pause in ph:
            weights.append(len(re.sub(r"\W", "", w)) + 2 + pause)
    total = float(sum(weights)) or 1.0
    timings, t = [], 0.0
    for wt in weights:
        d = total_dur * wt / total
        timings.append((t, t + d))
        t += d
    return timings


def layout_caption(words, max_size=58):
    """Words ko max 2 lines me baithata hai (zarurat ho to font chhota karke)."""
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    size = max_size
    while True:
        font = get_system_font(size)
        space = probe.textlength(" ", font=font)
        lines, cur, cur_w = [], [], 0.0
        for i, w in enumerate(words):
            ww = probe.textlength(w, font=font)
            add = ww if not cur else space + ww
            if cur and cur_w + add > CAPTION_W - 90:
                lines.append(cur)
                cur, cur_w, add = [], 0.0, ww
            cur.append((i, ww))
            cur_w += add
        if cur:
            lines.append(cur)
        if len(lines) <= 2 or size <= 34:
            return font, size, lines, space
        size -= 4


def render_caption_frame(words, layout, active_idx, save_path):
    """Poora phrase dikhata hai; sirf active_idx wala word peela (highlight) hota hai."""
    font, size, lines, space = layout
    line_h = int(size * 1.28)
    box_h = line_h * len(lines) + 56
    img = Image.new("RGBA", (CAPTION_W, CAPTION_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top = (CAPTION_H - box_h) // 2
    d.rounded_rectangle((0, top, CAPTION_W - 1, top + box_h - 1), radius=36, fill=(0, 0, 0, 190))
    y = top + 28
    for line in lines:
        line_w = sum(w for _, w in line) + space * (len(line) - 1)
        x = (CAPTION_W - line_w) / 2
        for i, ww in line:
            color = (255, 214, 0, 255) if i == active_idx else (255, 255, 255, 255)
            d.text((x, y), words[i], font=font, fill=color, stroke_width=4, stroke_fill=(0, 0, 0, 255))
            x += ww + space
        y += line_h
    img.save(save_path)


def build_caption_track(phrases, timings):
    """Har word ke liye ek PNG banata hai aur ffmpeg concat list file likhta hai (ek hi input me poora caption track)."""
    entries, k = [], 0
    for pi, ph in enumerate(phrases):
        words = [w for w, _ in ph]
        layout = layout_caption(words)
        for wi in range(len(words)):
            path = os.path.abspath(os.path.join(OUTPUT_DIR, f"cap_{pi:03d}_{wi:02d}.png")).replace("\\", "/")
            render_caption_frame(words, layout, wi, path)
            st, et = timings[k]
            k += 1
            entries.append((path, max(et - st, 0.04)))
    list_path = os.path.join(OUTPUT_DIR, "captions.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for path, dur in entries:
            f.write(f"file '{path}'\nduration {dur:.3f}\n")
        f.write(f"file '{entries[-1][0]}'\n")  # concat demuxer ka last frame quirk
    return list_path


def capture_page_for_scroll(url, out_path, max_css_height=7000):
    """Diye gaye page ka mobile-width (1080px) full screenshot leta hai, jise reel me scroll kiya jayega."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logging.warning("⚠️ playwright installed nahi hai, scroll background skip")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(
                viewport={"width": 540, "height": 960},
                device_scale_factor=2,
                is_mobile=True,
                user_agent=("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            height = min(int(page.evaluate("document.documentElement.scrollHeight")), max_css_height)
            # lazy images load karne ke liye neeche tak scroll
            y = 0
            while y < height:
                page.evaluate(f"window.scrollTo(0, {y})")
                page.wait_for_timeout(120)
                y += 500
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)
            page.screenshot(path=out_path, full_page=True,
                            clip={"x": 0, "y": 0, "width": 540, "height": height})
            browser.close()
        with Image.open(out_path) as im:
            im = im.convert("RGB")
            if im.width != 1080:
                im = im.resize((1080, int(im.height * 1080 / im.width)))
            if im.height < 1920:
                canvas = Image.new("RGB", (1080, 1920), (255, 255, 255))
                canvas.paste(im, (0, 0))
                im = canvas
            im.save(out_path)
            logging.info(f"🖼️ Scroll page captured: {im.width}x{im.height}")
        return out_path
    except Exception as e:
        logging.warning(f"⚠️ Page capture fail: {e}")
        return None


def get_audio_duration(audio_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(result.stdout.strip())


def create_image_card(im, save_path, width=CARD_WIDTH, pad=CARD_PAD, radius=40):
    """Product image ko gol kone + halki shadow wale (transparent) card me daalta hai."""
    im = ImageOps.contain(im.convert("RGBA"), (width - 48, width - 48), Image.LANCZOS)
    canvas_size = width + pad * 2

    shadow = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (pad, pad + 14, pad + width, pad + width + 14), radius=radius, fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))

    card = Image.new("RGBA", (width, width), (255, 255, 255, 255))
    card.paste(im, ((width - im.width) // 2, (width - im.height) // 2), im)
    mask = Image.new("L", (width, width), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, width - 1), radius=radius, fill=255)
    shadow.paste(card, (pad, pad), mask)
    shadow.save(save_path)
    return save_path


def download_card_images(urls):
    """Har scene ke liye ek entry: card ka path, ya None agar image nahi mili (us scene me sirf scroll chalega)."""
    cards = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for i, url in enumerate(urls[:MAX_IMAGES]):
        path = None
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200 and len(r.content) > 2000:
                with Image.open(io.BytesIO(r.content)) as im:
                    path = create_image_card(im, os.path.join(OUTPUT_DIR, f"card_{i}.png"))
            else:
                logging.warning(f"⚠️ Image {i + 1} nahi mili (status {r.status_code}), scene me sirf scroll")
        except Exception as e:
            logging.warning(f"⚠️ Image {i + 1} fail, scene me sirf scroll: {e}")
        cards.append(path)
    return cards


def create_vertical_reel_ffmpeg(product_data, ai_data, audio_path, output_video_path, scroll_image=None):
    logging.info("🎬 Rendering reel...")
    fps = 25
    audio_dur = get_audio_duration(audio_path)

    urls = [u for u in (product_data.get("extra_images") or []) if u and u != DEFAULT_FALLBACK_IMAGE][:MAX_IMAGES]
    cards = download_card_images(urls)
    if not scroll_image and not any(cards):
        logging.error("❌ Na scroll background bana, na koi image mili")
        return False

    hook_p = create_text_card_image(ai_data["hook_text"], 1000, 160, (220, 20, 50), (255, 255, 255), 60, "hook.png")
    price_tag = f"PRICE: {product_data['price']}" if product_data.get("has_real_price") else ai_data["key_feature"]
    price_p = create_text_card_image(price_tag, 920, 110, (0, 180, 80), (255, 255, 255), 45, "price.png")
    cta_p = create_text_card_image(ai_data["cta_text"], 980, 120, (20, 20, 20), (255, 215, 0), 45, "cta.png")

    phrases = build_caption_phrases(ai_data["script"])
    caption_list = build_caption_track(phrases, compute_word_timings(phrases, audio_dur))

    # ---- inputs: 0=background, 1=captions, 2/3/4=hook/price/cta, uske baad image cards, aakhir me audio ----
    if scroll_image:
        inputs = ["-loop", "1", "-framerate", str(fps), "-t", f"{audio_dur:.3f}", "-i", scroll_image]
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=0x15181f:s=1080x1920:r={fps}:d={audio_dur:.3f}"]
    inputs += ["-f", "concat", "-safe", "0", "-i", caption_list]
    inputs += ["-i", hook_p, "-i", price_p, "-i", cta_p]
    cap_i, hook_i, price_i, cta_i = 1, 2, 3, 4

    # har scene barabar lamba; image us scene ke sirf IMG_SHOW_RATIO hisse me dikhti hai (bacha hua time sirf scroll)
    # agar scroll background nahi bana to images hi content hain, isliye poore time dikhti hain
    scene_len = audio_dur / len(cards) if cards else 0.0
    show_ratio = IMG_SHOW_RATIO if scroll_image else 1.0
    scenes, next_i = [], 5
    for j, card in enumerate(cards):
        if not card:
            continue
        show = scene_len * show_ratio
        start = j * scene_len + (scene_len - show) * 0.3
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{show:.3f}", "-i", card]
        scenes.append((next_i, start, show))
        next_i += 1
    inputs += ["-i", audio_path]
    audio_i = next_i

    # ---- filter graph ----
    if scroll_image:
        # website ka lamba screenshot upar se neeche seedha (bina blur) scroll hota hai, poori voice ki length me
        fg = (f"[0:v]crop=1080:1920:0:'(ih-1920)*min(t/{audio_dur:.3f},1)',"
              f"setsar=1,fps={fps},format=yuv420p[bg];")
    else:
        fg = f"[0:v]setsar=1,fps={fps},format=yuv420p[bg];"
    last = "bg"

    base = CARD_WIDTH + 2 * CARD_PAD
    for n, (idx, start, show) in enumerate(scenes):
        fade_d = min(0.35, show / 4)
        zoom = f"{base}*(1+0.06*t/{show:.3f})"  # halka zoom-in (6%)
        fg += (
            f"[{idx}:v]format=rgba,scale=w='{zoom}':h='{zoom}':eval=frame,"
            f"fade=t=in:st=0:d={fade_d:.2f}:alpha=1,"
            f"fade=t=out:st={show - fade_d:.3f}:d={fade_d:.2f}:alpha=1,"
            f"setpts=PTS+{start:.3f}/TB[im{n}];"
            f"[{last}][im{n}]overlay=x='(W-w)/2':y='{CARD_CENTER_Y}-h/2':eof_action=pass:"
            f"enable='between(t,{start:.3f},{start + show:.3f})'[s{n}];"
        )
        last = f"s{n}"

    fg += f"[{last}][{hook_i}:v]overlay=(W-w)/2:140[h1];"
    fg += f"[h1][{price_i}:v]overlay=(W-w)/2:1420[h2];"
    fg += f"[h2][{cta_i}:v]overlay=(W-w)/2:1600[h3];"
    # caption sabse upar (image card ke neeche wali jagah par), taaki kabhi ढके nahi
    fg += f"[{cap_i}:v]fps={fps},format=rgba[cap];"
    fg += f"[h3][cap]overlay=(W-w)/2:{CAPTION_CENTER_Y}-h/2:eof_action=pass,format=yuv420p[outv]"

    cmd = [
        "ffmpeg", "-y", *inputs, "-filter_complex", fg,
        "-map", "[outv]", "-map", f"{audio_i}:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", output_video_path,
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode == 0 and os.path.exists(output_video_path) and os.path.getsize(output_video_path) > 500000:
        logging.info(f"✅ Reel OK: {output_video_path}")
        return True
    logging.error(f"❌ FFmpeg fail: {res.stderr[-1500:] if res.stderr else 'unknown'}")
    return False


# ================= 7. UPLOAD (GitHub Release + fallbacks) =================
def upload_video_for_direct_link(video_path):
    logging.info("☁️ Uploading video...")
    if not os.path.exists(video_path):
        return None
    size = os.path.getsize(video_path)
    if size < 500000:
        return None
    logging.info(f"📁 Size: {size/1024/1024:.2f} MB")
    filename = os.path.basename(video_path)

    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if gh_token and gh_repo:
        try:
            logging.info("📤 GitHub Release...")
            tag = f"reel-{int(time.time())}"
            headers = {
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            r = requests.post(
                f"https://api.github.com/repos/{gh_repo}/releases",
                json={"tag_name": tag, "name": f"Reel {tag}", "body": "Auto reel", "draft": False, "prerelease": True},
                headers=headers, timeout=60,
            )
            if r.status_code in (200, 201):
                upload_url = r.json().get("upload_url", "").split("{")[0]
                with open(video_path, "rb") as f:
                    up = requests.post(
                        f"{upload_url}?name={filename}",
                        headers={**headers, "Content-Type": "video/mp4"},
                        data=f, timeout=300,
                    )
                if up.status_code in (200, 201):
                    url = up.json().get("browser_download_url")
                    if url:
                        logging.info(f"🔗 GitHub: {url}")
                        return url
            else:
                logging.warning(f"⚠️ GH release: {r.status_code} {r.text[:150]}")
        except Exception as e:
            logging.warning(f"⚠️ GitHub upload: {e}")

    try:
        logging.info("📤 Tmpfiles...")
        with open(video_path, "rb") as f:
            res = requests.post("https://tmpfiles.org/api/v1/upload", files={"file": (filename, f)}, timeout=300)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "success":
                url = data["data"]["url"].replace("tmpfiles.org/", "tmpfiles.org/dl/")
                logging.info(f"🔗 Tmpfiles: {url}")
                return url
    except Exception as e:
        logging.warning(f"⚠️ Tmpfiles: {e}")

    try:
        logging.info("📤 Litterbox...")
        with open(video_path, "rb") as f:
            res = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "72h"},
                files={"fileToUpload": (filename, f, "video/mp4")},
                timeout=300,
            )
        text = (res.text or "").strip()
        if res.status_code == 200 and text.startswith("http"):
            logging.info(f"🔗 Litterbox: {text}")
            return text
    except Exception as e:
        logging.warning(f"⚠️ Litterbox: {e}")

    logging.error("❌ Upload fail")
    notify_telegram("❌ Video upload fail")
    return None


# ================= 8. BUFFER =================
def get_buffer_channels():
    url = "https://api.buffer.com"
    headers = {"Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        res = requests.post(url, json={"query": "query { account { organizations { id name } } }"}, headers=headers, timeout=20)
        orgs = res.json().get("data", {}).get("account", {}).get("organizations", [])
        if not orgs:
            logging.error("❌ No Buffer org")
            return []
        org_id = orgs[0]["id"]
        logging.info(f"🏢 Org: {orgs[0].get('name')}")

        q = """
        query GetChannels($input: ChannelsInput!) {
          channels(input: $input) {
            id service name
            metadata {
              ... on PinterestMetadata {
                boards { serviceId name }
              }
            }
          }
        }
        """
        res2 = requests.post(url, json={"query": q, "variables": {"input": {"organizationId": org_id}}}, headers=headers, timeout=20)
        channels = res2.json().get("data", {}).get("channels", [])
        out = []
        for ch in channels:
            service = (ch.get("service") or "").lower()
            board_id = None
            if service == "pinterest":
                boards = (ch.get("metadata") or {}).get("boards") or []
                if boards:
                    board_id = boards[0].get("serviceId")
                    logging.info(f"📌 Board: {boards[0].get('name')}")
            out.append({"id": ch["id"], "service": service, "name": ch.get("name", ""), "board_id": board_id})
            logging.info(f"📱 {ch.get('name')} ({service})")
        return out
    except Exception as e:
        logging.error(f"⚠️ Buffer channels: {e}")
        return []


def send_to_buffer(video_url, product_data, ai_data, buy_url):
    logging.info("🚀 Buffer publish...")
    channels = get_buffer_channels()
    if not channels:
        return

    url = "https://api.buffer.com"
    headers = {"Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}", "Content-Type": "application/json"}
    caption = f"{ai_data['caption']}\n\nBuy Here: {buy_url}"
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id } }
        ... on MutationError { message }
      }
    }
    """

    for ch in channels:
        c_id, service, ch_name, board_id = ch["id"], ch["service"], ch["name"], ch.get("board_id")
        metadata = {}

        if service == "instagram":
            metadata = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
        elif service == "facebook":
            metadata = {"facebook": {"type": "reel"}}
        elif service == "pinterest":
            if not board_id:
                logging.error(f"❌ Skip Pinterest {ch_name}: no board")
                continue
            metadata = {"pinterest": {"boardServiceId": board_id, "title": product_data.get("title", "Deal")[:100]}}

        variables = {
            "input": {
                "channelId": c_id,
                "text": caption,
                "mode": "addToQueue",
                "schedulingType": "automatic",
                "assets": [{"video": {"url": video_url}}],
            }
        }
        if metadata:
            variables["input"]["metadata"] = metadata

        try:
            res = requests.post(url, json={"query": mutation, "variables": variables}, headers=headers, timeout=30)
            res_json = res.json()
            if "errors" in res_json and "SchedulingType" in str(res_json):
                variables["input"]["schedulingType"] = "notification"
                res = requests.post(url, json={"query": mutation, "variables": variables}, headers=headers, timeout=30)
                res_json = res.json()

            result = res_json.get("data", {}).get("createPost", {})
            if result.get("post", {}).get("id"):
                logging.info(f"🎉 Sent → {ch_name} ({service}) ID={result['post']['id']}")
            elif result.get("message"):
                # IG/FB metadata fail ho to bina metadata try
                if service in ("instagram", "facebook") and metadata:
                    logging.warning(f"⚠️ {ch_name} metadata fail, retry plain: {result['message']}")
                    variables["input"].pop("metadata", None)
                    res2 = requests.post(url, json={"query": mutation, "variables": variables}, headers=headers, timeout=30)
                    r2 = res2.json().get("data", {}).get("createPost", {})
                    if r2.get("post", {}).get("id"):
                        logging.info(f"🎉 Sent (plain) → {ch_name} ({service})")
                    else:
                        logging.error(f"❌ {ch_name}: {r2.get('message') or res2.json()}")
                else:
                    logging.error(f"❌ {ch_name}: {result['message']}")
            else:
                logging.error(f"❌ {ch_name}: {res_json}")
        except Exception as e:
            logging.error(f"⚠️ Post {ch_name}: {e}")


# ================= 9. MAIN =================
async def process_and_publish(buy_url):
    logging.info("=" * 60)
    logging.info("🚀 STARTING REEL PROCESS")
    logging.info("=" * 60)
    notify_telegram(f"🚀 Reel start\n🔗 {buy_url}")

    product = scrape_product_details(buy_url)
    if not product.get("title"):
        notify_telegram("❌ Scrape fail")
        return

    ai_data = generate_reel_script(product)
    if not isinstance(ai_data, dict) or not ai_data.get("script"):
        logging.error(f"⚠️ Bad ai_data: {ai_data}")
        ai_data = {
            "script": f"Kya aap yeh deal dekhna chahte hain? {product.get('title')} best price par. Link description me!",
            "caption": f"Deal! {product.get('title')} #deals",
            "hook_text": "VIRAL DEAL ALERT!",
            "key_feature": f"Best Price: {product.get('price')}",
            "cta_text": "Link in Description!",
        }

    ts = int(time.time())
    audio_path = os.path.join(OUTPUT_DIR, f"voice_{ts}.mp3")
    video_path = os.path.join(OUTPUT_DIR, f"reel_{sanitize_filename(product['title'])}_{ts}.mp4")

    if not generate_voiceover(ai_data["script"], audio_path):
        notify_telegram("❌ Voice fail")
        return

    # Jo link diya gaya hai, wahi page background me scroll hoga (SCROLL_BACKGROUND=0 se band kar sakte hain)
    scroll_image = None
    if os.getenv("SCROLL_BACKGROUND", "1").strip() != "0":
        scroll_image = capture_page_for_scroll(buy_url, os.path.join(OUTPUT_DIR, f"page_{ts}.png"))
        if not scroll_image:
            logging.warning("⚠️ Scroll background nahi bana, product images wala reel banega")

    if not create_vertical_reel_ffmpeg(product, ai_data, audio_path, video_path, scroll_image=scroll_image):
        notify_telegram("❌ Video fail")
        return

    link = upload_video_for_direct_link(video_path)
    if link and BUFFER_ACCESS_TOKEN:
        send_to_buffer(link, product, ai_data, buy_url)
        notify_telegram(f"✅ Buffer ko bhej diya\n🎬 {product['title']}")
    else:
        logging.error("❌ Upload/token fail")
        notify_telegram("❌ Upload fail ya token missing")


async def main():
    print("\n🎬 MULTI-PLATFORM REEL GENERATOR\n")
    buy_url = os.getenv("PRODUCT_URL", "").strip()
    if not buy_url and len(sys.argv) > 1:
        buy_url = sys.argv[1].strip()
    if not buy_url:
        buy_url = input("🔗 Product link: ").strip()
    if not buy_url:
        print("❌ No link")
        return
    await process_and_publish(buy_url)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⚠️ Stopped")
    except Exception as e:
        logging.error(f"Fatal: {e}", exc_info=True)
        notify_telegram(f"❌ CRASH\n<code>{e}</code>")
        raise
