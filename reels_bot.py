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
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
client = Client()

# ================= 1. CONFIGURATION =================
BUFFER_ACCESS_TOKEN = os.environ.get("BUFFER_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2", "").strip()
OUTPUT_DIR = "generated_reels"
os.makedirs(OUTPUT_DIR, exist_ok=True)
DEFAULT_FALLBACK_IMAGE = "https://via.placeholder.com/1080x1920.png?text=Product+Image"
MAX_CHUNK_CHARACTERS = 1000
MAX_RETRIES = 3

# ================= Telegram Notifier =================
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

# ================= 2. FONT SYSTEM =================
def get_system_font(font_size=55):
    font_paths = [
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\NirmalaB.ttf",
        "C:\\Windows\\Fonts\\seguiemj.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\segoeui.ttf",
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

# ================= 3. PRODUCT SCRAPER =================
def unshorten_amazon_url(url, session):
    if not any(domain in url for domain in ["amzn.to", "link.amazon", "earnkaro", "fktr.in", "linkredirect.in"]):
        return url
    try:
        logging.info(f"🔍 Tracing redirect for: {url}")
        res = session.get(url, allow_redirects=True, timeout=15)
        soup = BeautifulSoup(res.content, "html.parser")
        
        meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        if meta_refresh:
            content = meta_refresh.get("content", "")
            match = re.search(r"url=['\"]?(.*?)['\"]?$", content, re.I)
            if match:
                redirect_url = match.group(1).strip()
                res = session.get(redirect_url, allow_redirects=True, timeout=15)
                soup = BeautifulSoup(res.content, "html.parser")
        scripts = soup.find_all("script")
        for script in scripts:
            if script.string:
                target_match = re.search(r"['\"](https://(?:www\.|dl\.)?(?:flipkart\.com|amazon\.in)[^'\"]+)['\"]", script.string)
                if target_match:
                    redirect_url = target_match.group(1).strip()
                    res = session.get(redirect_url, allow_redirects=True, timeout=15)
                    soup = BeautifulSoup(res.content, "html.parser")
                    break
        if "amazon." in res.url or "flipkart." in res.url:
            logging.info(f"✅ Final Unshortened URL: {res.url[:70]}...")
            return res.url
    except Exception as e:
        logging.warning(f"⚠️ Redirect resolution failed: {e}")
    return url

def scrape_bgtechlab_page(url, session):
    data = {
        "title": "",
        "category": "",
        "price": "Special Offer",
        "has_real_price": False,
        "image": "",
        "extra_images": [],
        "features": []
    }
    try:
        res = session.get(url, timeout=20)
        soup = BeautifulSoup(res.content, "html.parser")
        page_text = soup.get_text(" ", strip=True)

        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""
        if not title:
            og_title = soup.find("meta", {"property": "og:title"})
            title = og_title.get("content", "") if og_title else ""
        title = re.sub(r"\s*Review\s*\(\d{4}\)\s*$", "", title, flags=re.I).strip()
        data["title"] = title[:80] or "Trending Product Deal"

        cat_match = re.search(r"([\w &]{2,25}?)\s*✓\s*Verified Expert Review", page_text)
        if cat_match:
            data["category"] = cat_match.group(1).strip()

        price_match = re.search(r"Current Best Deal Price[^\d₹]*₹\s*([\d,]+)", page_text)
        if price_match:
            data["price"] = f"₹{price_match.group(1)}"
            data["has_real_price"] = True

        image_urls = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if any(cdn in src for cdn in ["flixcart.com", "media-amazon.com"]) and not any(
                bad in src.lower() for bad in ["logo", "icon", "sprite"]
            ):
                clean_src = src.split("?")[0]
                if clean_src not in image_urls:
                    image_urls.append(clean_src)
        if not image_urls:
            og_image = soup.find("meta", {"property": "og:image"})
            if og_image and og_image.get("content"):
                image_urls = [og_image["content"]]
        if not image_urls:
            image_urls = [DEFAULT_FALLBACK_IMAGE]

        final_5 = []
        while len(final_5) < 5 and image_urls:
            for link in image_urls:
                final_5.append(link)
                if len(final_5) == 5:
                    break
        data["image"] = final_5[0]
        data["extra_images"] = final_5[:5]

        pros_heading = soup.find(string=re.compile(r"What We Like", re.I))
        if pros_heading:
            pros_list = pros_heading.find_parent().find_next("ul")
            if pros_list:
                for li in pros_list.find_all("li")[:5]:
                    txt = li.get_text(strip=True).lstrip("✓✔-• ").strip()
                    if txt:
                        data["features"].append(txt)

        logging.info(f"✅ BG-TechLab Page Extracted: {data['title']} | Category: {data['category'] or 'N/A'} | Price: {data['price']}")
    except Exception as e:
        logging.error(f"⚠️ BG-TechLab Scraping Error: {e}")
    return data

def scrape_product_details(url):
    logging.info(f"🔄 Fetching Real Product Data & Multiple Images: {url[:60]}...")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    })

    if "github.io" in url:
        return scrape_bgtechlab_page(url, session)

    data = {
        "title": "",
        "category": "",
        "price": "Special Offer",
        "has_real_price": False,
        "image": "",
        "extra_images": [],
        "features": []
    }
    res_url = unshorten_amazon_url(url, session)
    try:
        res = session.get(res_url, allow_redirects=True, timeout=20)
        soup = BeautifulSoup(res.content, "html.parser")

        title_elem = (
            soup.find("span", {"class": "VU-Tz5"}) 
            or soup.find("span", {"class": "B_NuCI"})
            or soup.find("h1", {"class": "_6ER3B5"})
            or soup.find("span", {"id": "productTitle"}) 
            or soup.find("h1", {"id": "title"})
            or soup.find("meta", {"property": "og:title"})
        )
        if title_elem:
            raw_title = title_elem.get("content") if title_elem.name == "meta" else title_elem.get_text()
            clean_title = raw_title.strip().replace("\n", " ")
            clean_title = re.sub(r"\s*:\s*(Amazon|Flipkart|Buy Online)\..*$", "", clean_title, flags=re.IGNORECASE)
            data["title"] = clean_title[:70].rstrip()

        if not data["title"]:
            match = re.search(r'flipkart\.com/([^/]+)/p/', res_url)
            if match:
                data["title"] = match.group(1).replace("-", " ").title()

        feature_elems = soup.find_all(["li", "div"], {"class": re.compile(r'(feature|bullet|description|_2416B)', re.I)})
        for fe in feature_elems[:4]:
            txt = fe.get_text().strip()
            if 15 < len(txt) < 120 and not any(x in txt.lower() for x in ["return", "delivery", "warranty", "cash on"]):
                data["features"].append(txt)

        image_urls = []
        all_imgs = soup.find_all("img", {"src": re.compile(r'rukminim[0-9]\.flixcart\.com')})
        for img in all_imgs:
            src = img.get('src') or img.get('data-src') or ""
            if src and not any(bad in src.lower() for bad in ["logo", "svg", "icon", "placeholder", "header"]):
                high_res = re.sub(r'/(?:\d+)/(?:\d+)/', '/832/832/', src)
                if high_res not in image_urls:
                    image_urls.append(high_res)

        if not image_urls:
            amz_imgs = soup.find_all("img", {"src": re.compile(r'm\.media-amazon\.com/images/I/')})
            for img in amz_imgs:
                src = img.get('src', '')
                if not any(bad in src.lower() for bad in ["logo", "icon", "sprite"]):
                    high_res = re.sub(r'\._AC_.*_\.', '.', src)
                    if high_res not in image_urls:
                        image_urls.append(high_res)

        if len(image_urls) == 0:
            image_urls = [DEFAULT_FALLBACK_IMAGE]

        final_5_images = []
        while len(final_5_images) < 5:
            for img_link in image_urls:
                final_5_images.append(img_link)
                if len(final_5_images) == 5:
                    break
        data["image"] = final_5_images[0]
        data["extra_images"] = final_5_images[:5]

        price_elem = soup.find("div", {"class": "Nx9bqj CxhGGd"}) or soup.find("span", {"class": "a-price-whole"})
        if price_elem:
            clean_price = re.sub(r"[^\d]", "", price_elem.get_text())
            if clean_price: 
                data["price"] = f"₹{clean_price}"
                data["has_real_price"] = True
    except Exception as e:
        logging.error(f"⚠️ Scraping Error: {e}")

    if not data["title"]:
        data["title"] = "Trending Gadget Deal"
    logging.info(f"✅ Product Extracted: {data['title']} | Price: {data['price']}")
    logging.info(f"🖼️ Images Prepared ({len(data['extra_images'])} images)")
    return data

# ================= 4. SCRIPT GENERATOR =================
def generate_reel_script(product_data):
    logging.info("🤖 Generating Script (Gemini Free + Fallback)...")
    
    title = product_data["title"]
    price = product_data["price"]
    category = product_data.get("category", "") or "product"
    category_tag = re.sub(r'\W+', '', category) or "deals"
    features_str = " | ".join(product_data["features"]) if product_data["features"] else f"Great quality {category}, trusted brand, best value for money"

    prompt = f"""You are an expert viral Instagram Reel creator. Write a detailed, engaging 45 SECONDS long script in ROMAN ENGLISH / HINGLISH (English alphabets only) for:
PRODUCT CATEGORY: {category}
PRODUCT: {title}
PRICE: {price}
KEY FEATURES / PROS: {features_str}
STRICT RULES:
1. STRICT DURATION: The script MUST be 100 to 110 words long so speaking duration is EXACTLY 45 SECONDS!
2. SCRIPT LANGUAGE: Use ONLY Roman English / Hinglish script. Do NOT use Devanagari Hindi text!
3. NO GREETINGS: ABSOLUTELY NO 'Hello Guys', 'Namaskar', 'Hey Friends'.
4. Start IMMEDIATELY with a strong hook question in Hinglish.
5. Cover the actual KEY FEATURES / PROS listed above.
6. NO EMOJIS in hook_text, key_feature, or cta_text!
Return STRICTLY VALID JSON only (no markdown):
{{
    "script": "...",
    "caption": "🔥 {title} Deal! Check link in description #deals #{category_tag}",
    "hook_text": "VIRAL DEAL ALERT!",
    "key_feature": "Best Price: {price}",
    "cta_text": "Link in Description!"
}}"""

    # --- Gemini API (Free) - 2 keys support ---
    gemini_keys = [k for k in [GEMINI_API_KEY, GEMINI_API_KEY_2] if k]
    for idx, api_key in enumerate(gemini_keys, 1):
        try:
            logging.info(f"🤖 Trying Gemini API key #{idx}...")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024}
            }
            res = requests.post(url, json=payload, timeout=45)
            if res.status_code == 200:
                data = res.json()
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                clean_json = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE).strip()
                result = json.loads(clean_json)
                logging.info("✅ Gemini se script mil gaya!")
                return result
            else:
                logging.warning(f"⚠️ Gemini key #{idx} failed: {res.status_code} {res.text[:200]}")
        except Exception as e:
            logging.warning(f"⚠️ Gemini key #{idx} error: {e}")

    # --- g4f fallback ---
    try:
        logging.info("🤖 Gemini fail, g4f try kar rahe hain...")
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        raw_text = res.choices[0].message.content.strip()
        clean_json = re.sub(r'^```json\s*|\s*```$', '', raw_text, flags=re.MULTILINE)
        return json.loads(clean_json)
    except Exception as e:
        logging.error(f"⚠️ AI Script Error: {e}")
        notify_telegram(f"⚠️ AI script fail, fallback use.\n<code>{e}</code>")
        return {
            "script": f"Kya aap ek behtareen {category} dhoond rahe hain jisme achhi quality bhi ho aur price bhi sahi ho? Pesh hai {title}! Isme aapko milta hai {features_str}. Yeh dikhne me kafi premium hai aur use karna bhi bahut aasan hai. Is time is par bahut bada price drop offer chal raha hai. Aaj hi is special deal ka fayda uthane ke liye niche description me diye gaye link par visit karein aur apna order place karein!",
            "caption": f"Best Deal on {title}! Check link in description. #deals #{category_tag}",
            "hook_text": "VIRAL DEAL ALERT!",
            "key_feature": f"Best Price: {price}",
            "cta_text": "Link in Description!"
        }

# ================= 5. VOICE GENERATION =================
def split_script_into_chunks(script, max_chars=MAX_CHUNK_CHARACTERS):
    sentences = re.split(r'([.!?|\n])', script)
    chunks = []
    current_chunk = ""
    for i in range(0, len(sentences), 2):
        sentence = sentences[i]
        punct = sentences[i+1] if i+1 < len(sentences) else ""
        full_sentence = sentence + punct
        
        if len(current_chunk) + len(full_sentence) <= max_chars:
            current_chunk += full_sentence
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = full_sentence
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks

def generate_fable_voice_openai_fm(text_chunk, output_path):
    try:
        url = "https://www.openai.fm/api/generate"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Origin": "https://www.openai.fm",
            "Referer": "https://www.openai.fm/",
            "Accept": "*/*",
        }
        files = {
            "input": (None, text_chunk),
            "voice": (None, "fable"),
            "prompt": (None, "Speak clearly in an energetic, natural Hinglish male speaker tone at 1.25x speed. Moderate pace, energetic deal presenter tone."),
            "vibe": (None, "audio")
        }
        
        response = requests.post(url, files=files, headers=headers, timeout=90, stream=True)
        if response.status_code != 200:
            params = {
                "input": text_chunk,
                "voice": "fable",
                "prompt": "Speak clearly in an energetic, natural Hinglish male speaker tone."
            }
            response = requests.get(url, params=params, headers=headers, timeout=90, stream=True)
            
        response.raise_for_status()
        
        content_type = response.headers.get("content-type", "").lower()
        if not any(k in content_type for k in ["audio", "mpeg", "wav", "octet-stream"]):
            raise Exception(f"Unexpected content-type: {content_type}")
        
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        if os.path.getsize(output_path) < 8000:
            raise Exception("Downloaded audio too small")
            
        return True
    except Exception as e:
        logging.warning(f"⚠️ OpenAI.fm Direct API Error: {e}")
        return False

def generate_edge_tts_voice(text, output_audio_path):
    voice = "hi-IN-MadhurNeural"
    async def _save():
        communicate = edge_tts.Communicate(text, voice, rate="+25%")
        await communicate.save(output_audio_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_save())
    loop.close()

def generate_voiceover(text, output_audio_path):
    logging.info("🎙️ Generating AI Voiceover (OpenAI.fm Fable Voice + Edge-TTS Fallback)...")
    temp_dir = os.path.join(OUTPUT_DIR, "temp_voice")
    os.makedirs(temp_dir, exist_ok=True)
    chunks = split_script_into_chunks(text, max_chars=MAX_CHUNK_CHARACTERS)
    audio_parts = []
    fable_failed = False

    for idx, chunk in enumerate(chunks, start=1):
        part_filename = os.path.join(temp_dir, f"part_{idx:03d}.mp3")
        success = False

        if not fable_failed:
            for attempt in range(1, MAX_RETRIES + 1):
                logging.info(f"🎙️ Generating Part {idx}/{len(chunks)} with OpenAI.fm (Attempt {attempt})...")
                if generate_fable_voice_openai_fm(chunk, part_filename):
                    success = True
                    break
                time.sleep(2)
            if not success:
                logging.warning("⚠️ OpenAI.fm failed, switching to Edge-TTS Fallback.")
                fable_failed = True

        if fable_failed or not success:
            try:
                logging.info(f"🔊 Generating Part {idx}/{len(chunks)} via Edge-TTS Fallback...")
                generate_edge_tts_voice(chunk, part_filename)
                if os.path.exists(part_filename) and os.path.getsize(part_filename) >= 4000:
                    success = True
                else:
                    raise Exception("Edge TTS ne khali/chhoti audio di")
            except Exception as e:
                logging.warning(f"⚠️ Edge TTS Part {idx} fail ({e}). Ab gTTS try kar rahe hain...")
                try:
                    tts = gTTS(text=chunk, lang="hi")
                    tts.save(part_filename)
                    if os.path.exists(part_filename) and os.path.getsize(part_filename) >= 4000:
                        success = True
                    else:
                        raise Exception("gTTS ne bhi khali/chhoti audio di")
                except Exception as e2:
                    logging.error(f"❌ Voice part {idx} failed completely: {e2}")
                    notify_telegram(f"❌ Awaaz (TTS) fail ho gayi — teeno tarike fail (Part {idx}).")
                    return False
        audio_parts.append(part_filename)

    if audio_parts:
        list_file = os.path.join(temp_dir, "concat_list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for p in audio_parts:
                clean_p = os.path.abspath(p).replace('\\', '/')
                f.write(f"file '{clean_p}'\n")
        try:
            cmd = [
                'ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_file,
                '-af', 'atempo=1.25,loudnorm=I=-16:LRA=11:TP=-1.5',
                '-ar', '44100', '-ac', '2', '-b:a', '128k',
                '-c:a', 'libmp3lame', '-y', output_audio_path
            ]
            subprocess.run(cmd, capture_output=True, check=True)
            logging.info(f"🔊 Audio generated successfully: {output_audio_path}")
            return True
        except Exception as e:
            logging.error(f"❌ Audio Concatenation Error: {e}")
            return False
        finally:
            for f in audio_parts + [list_file]:
                if os.path.exists(f):
                    try: os.remove(f)
                    except: pass
    return False

# ================= 6. TEXT CARD GENERATOR =================
def create_text_card_image(text, width=1000, height=150, bg_color=(220, 20, 50), text_color=(255, 255, 255), font_size=55, save_name="card.png"):
    clean_txt = remove_emojis(text)
    
    img = Image.new("RGBA", (width, height), bg_color + (240,))
    draw = ImageDraw.Draw(img)
    
    font = get_system_font(font_size)
    bbox = draw.textbbox((0, 0), clean_txt, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    
    x = (width - text_w) / 2
    y = (height - text_h) / 2
    draw.text((x, y), clean_txt, fill=text_color, font=font)
    
    path = os.path.join(OUTPUT_DIR, save_name)
    img.save(path)
    return path

# ================= 7. FFMPEG HELPERS =================
def get_audio_duration(audio_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(result.stdout.strip())

def download_temp_images(urls):
    local_paths = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for i, url in enumerate(urls):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                p = os.path.join(OUTPUT_DIR, f"temp_img_{i}.png")
                with Image.open(io.BytesIO(r.content)) as img:
                    img = img.convert("RGBA")
                    canvas = Image.new("RGBA", (1020, 1020), (0, 0, 0, 0))
                    img.thumbnail((1000, 1000))
                    offset = ((1020 - img.width) // 2, (1020 - img.height) // 2)
                    canvas.paste(img, offset)
                    canvas.save(p)
                local_paths.append(p)
        except Exception as e:
            logging.error(f"❌ Image Download Error: {e}")
    return local_paths

# ================= 8. REEL BUILDER =================
def create_vertical_reel_ffmpeg(product_data, ai_data, audio_path, output_video_path):
    logging.info("🎬 Rendering 9:16 Reel with Fixed Image Timing...")
    local_imgs = download_temp_images(product_data["extra_images"])
    if not local_imgs:
        logging.error("❌ No product images found!")
        return False

    audio_dur = get_audio_duration(audio_path)
    num_imgs = len(local_imgs)
    per_img_dur = audio_dur / num_imgs

    hook_p = create_text_card_image(ai_data["hook_text"], 1000, 160, (220, 20, 50), (255, 255, 255), 60, "hook_card.png")
    price_tag = f"PRICE: {product_data['price']}" if product_data['has_real_price'] else ai_data['key_feature']
    price_p = create_text_card_image(price_tag, 920, 110, (0, 180, 80), (255, 255, 255), 45, "price_card.png")
    cta_p = create_text_card_image(ai_data["cta_text"], 980, 120, (20, 20, 20), (255, 215, 0), 45, "cta_card.png")

    phrases = [p.strip() for p in re.split(r'[,.!?|।\n]', ai_data["script"]) if len(p.strip()) > 2]
    if not phrases:
        phrases = [ai_data["script"]]
    sub_dur = audio_dur / len(phrases)

    bg_files = glob.glob("BAground Video*") + glob.glob("baground video*") + glob.glob("background*")
    bg_video = bg_files[0] if bg_files else None

    ffmpeg_cmd_inputs = []
    
    if bg_video:
        ffmpeg_cmd_inputs.extend(["-stream_loop", "-1", "-i", bg_video])
    else:
        ffmpeg_cmd_inputs.extend(["-f", "lavfi", "-i", f"color=c=black:s=1080x1920:d={audio_dur}"])

    for img in local_imgs:
        ffmpeg_cmd_inputs.extend(["-loop", "1", "-i", img])

    ffmpeg_cmd_inputs.extend(["-i", hook_p, "-i", price_p, "-i", cta_p])

    sub_paths = []
    for i, phrase in enumerate(phrases):
        sub_path = create_text_card_image(phrase, 960, 130, (10, 10, 10), (255, 230, 0), 40, f"sub_{i}.png")
        sub_paths.append(sub_path)
        ffmpeg_cmd_inputs.extend(["-i", sub_path])

    ffmpeg_cmd_inputs.extend(["-i", audio_path])

    bg_idx = 0
    img_start_idx = 1
    img_end_idx = num_imgs
    hook_idx = img_end_idx + 1
    price_idx = hook_idx + 1
    cta_idx = price_idx + 1
    sub_start_idx = cta_idx + 1
    sub_end_idx = sub_start_idx + len(phrases) - 1
    audio_idx = sub_end_idx + 1

    fps = 25
    filter_graph = f"[{bg_idx}:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,trim=0:{audio_dur},setpts=PTS-STARTPTS[bg];"

    last_v = "bg"
    for i in range(1, num_imgs + 1):
        st = (i - 1) * per_img_dur
        et = i * per_img_dur
        frames = int(per_img_dur * fps)
        
        if i % 2 != 0:
            zoom_expr = f"min(zoom+0.0015,1.25)"
        else:
            zoom_expr = f"max(1.25-0.0015*on,1.0)"
        
        next_v = f"v_img_{i}"
        filter_graph += (
            f"[{img_start_idx + i - 1}:v]trim=0:{per_img_dur},setpts=PTS-STARTPTS,"
            f"scale=1020:1020,zoompan=z='{zoom_expr}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1020x1020,fps={fps},setpts=PTS-STARTPTS[img_{i}];"
            f"[{last_v}][img_{i}]overlay=(W-w)/2:(H-h)/2:enable='between(t,{st},{et})'[{next_v}];"
        )
        last_v = next_v

    filter_graph += f"[{last_v}][{hook_idx}:v]overlay=(W-w)/2:140[v1];"
    filter_graph += f"[v1][{price_idx}:v]overlay=(W-w)/2:1420[v2];"
    filter_graph += f"[v2][{cta_idx}:v]overlay=(W-w)/2:1600[v3];"

    curr_v = "v3"
    for i in range(len(phrases)):
        s_idx = sub_start_idx + i
        st = i * sub_dur
        et = (i + 1) * sub_dur
        next_v = f"v_sub_{i}"
        filter_graph += f"[{curr_v}][{s_idx}:v]overlay=(W-w)/2:1180:enable='between(t,{st},{et})'[{next_v}];"
        curr_v = next_v

    filter_graph = filter_graph.rstrip(";")

    cmd = [
        "ffmpeg", "-y",
        *ffmpeg_cmd_inputs,
        "-filter_complex", filter_graph,
        "-map", f"[{curr_v}]",
        "-map", f"{audio_idx}:a",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        output_video_path
    ]

    logging.info("⚡ Executing FFmpeg Pipeline with Fixed Timing...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if res.returncode == 0:
        if os.path.exists(output_video_path):
            file_size = os.path.getsize(output_video_path)
            if file_size < 500000:
                logging.error(f"❌ Video too small ({file_size} bytes). Rendering failed.")
                return False
            logging.info(f"✅ Reel Generated Successfully: {output_video_path} ({file_size/1024/1024:.2f} MB)")
            return True
        else:
            logging.error("❌ Output video file not found!")
            return False
    else:
        logging.error(f"❌ FFmpeg Error:\n{res.stderr}")
        return False

# ================= 9. FREE VIDEO UPLOAD =================
def upload_video_for_direct_link(video_path):
    logging.info("☁️ Uploading Video to Cloud for Direct Link...")
    
    if not os.path.exists(video_path):
        logging.error("❌ Video file not found!")
        return None
    
    file_size = os.path.getsize(video_path)
    if file_size < 500000:
        logging.error(f"❌ Video too small ({file_size} bytes). Not uploading.")
        return None
    
    logging.info(f"📁 Video size: {file_size/1024/1024:.2f} MB")
    filename = os.path.basename(video_path)

    def _is_direct_video(url):
        """Buffer ke liye URL check — direct video hona chahiye"""
        try:
            h = requests.head(url, timeout=20, allow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0"
            })
            ct = h.headers.get("content-type", "").lower()
            if h.status_code == 200 and ("video" in ct or "octet-stream" in ct or "mp4" in ct):
                return True
            # kuch hosts HEAD block karte hain — GET range try
            g = requests.get(url, headers={"Range": "bytes=0-1000", "User-Agent": "Mozilla/5.0"}, timeout=20, stream=True)
            if g.status_code in (200, 206):
                return True
        except Exception as e:
            logging.warning(f"⚠️ URL verify fail ({url[:50]}...): {e}")
        return False

    # ===== HOST 1: pixeldrain (free, reliable) =====
    try:
        logging.info("📤 Uploading via pixeldrain.com...")
        with open(video_path, "rb") as f:
            res = requests.post(
                "https://pixeldrain.com/api/file/" + filename,
                data=f,
                timeout=300
            )
        if res.status_code in (200, 201):
            data = res.json()
            file_id = data.get("id")
            if file_id:
                direct_url = f"https://pixeldrain.com/api/file/{file_id}?download"
                if _is_direct_video(direct_url):
                    logging.info(f"🔗 Direct Video URL (pixeldrain): {direct_url}")
                    return direct_url
                logging.warning("⚠️ pixeldrain URL verify fail")
    except Exception as e:
        logging.warning(f"⚠️ pixeldrain failed: {e}")

    # ===== HOST 2: gofile.io (free) =====
    try:
        logging.info("📤 Uploading via gofile.io...")
        # server nikaalo
        srv = requests.get("https://api.gofile.io/servers", timeout=20).json()
        server = srv["data"]["servers"][0]["name"]
        with open(video_path, "rb") as f:
            res = requests.post(
                f"https://{server}.gofile.io/uploadFile",
                files={"file": (filename, f)},
                timeout=300
            )
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "ok":
                direct_url = data["data"]["downloadPage"]
                # gofile direct link try
                file_id = data["data"].get("fileId") or data["data"].get("id")
                if file_id:
                    # content link better for Buffer
                    content_url = data["data"].get("downloadPage", "")
                    logging.info(f"🔗 Gofile page: {content_url}")
                    # gofile ka direct content URL
                    direct = f"https://{server}.gofile.io/download/web/{file_id}/{filename}"
                    if _is_direct_video(direct):
                        logging.info(f"🔗 Direct Video URL (gofile): {direct}")
                        return direct
    except Exception as e:
        logging.warning(f"⚠️ gofile failed: {e}")

    # ===== HOST 3: Catbox =====
    try:
        logging.info("📤 Uploading via Catbox.moe...")
        with open(video_path, "rb") as f:
            res = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": f},
                timeout=300
            )
        if res.status_code == 200 and "files.catbox.moe" in res.text:
            direct_url = res.text.strip()
            if _is_direct_video(direct_url):
                logging.info(f"🔗 Direct Video URL (Catbox): {direct_url}")
                return direct_url
    except Exception as e:
        logging.warning(f"⚠️ Catbox failed: {e}")

    # ===== HOST 4: 0x0.st =====
    try:
        logging.info("📤 Uploading via 0x0.st...")
        with open(video_path, "rb") as f:
            res = requests.post("https://0x0.st", files={"file": f}, timeout=300)
        if res.status_code == 200 and res.text.startswith("http"):
            direct_url = res.text.strip()
            if _is_direct_video(direct_url):
                logging.info(f"🔗 Direct Video URL (0x0.st): {direct_url}")
                return direct_url
    except Exception as e:
        logging.warning(f"⚠️ 0x0.st failed: {e}")

    # ===== HOST 5: Litterbox 72h =====
    try:
        logging.info("📤 Uploading via Litterbox...")
        with open(video_path, "rb") as f:
            res = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "72h"},
                files={"fileToUpload": f},
                timeout=300
            )
        if res.status_code == 200 and res.text.startswith("http"):
            direct_url = res.text.strip()
            if _is_direct_video(direct_url):
                logging.info(f"🔗 Direct Video URL (Litterbox): {direct_url}")
                return direct_url
    except Exception as e:
        logging.warning(f"⚠️ Litterbox failed: {e}")

    logging.error("❌ Koi bhi free host se direct video URL nahi mila!")
    notify_telegram("❌ Video upload fail — koi free host Buffer-compatible URL nahi de paya.")
    return None

# ================= 10. BUFFER CHANNELS + PINTEREST BOARD =================
def get_buffer_channels():
    url = "https://api.buffer.com"
    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    
    org_query = """
    query {
      account {
        id
        organizations {
          id
          name
        }
      }
    }
    """
    
    try:
        res = requests.post(url, json={"query": org_query}, headers=headers, timeout=20)
        res_data = res.json()
        
        if "errors" in res_data:
            logging.error(f"❌ Buffer GraphQL Org Error: {res_data['errors']}")
            return []
            
        orgs = res_data.get("data", {}).get("account", {}).get("organizations", [])
        if not orgs:
            logging.error("❌ No Organization found in your Buffer Account.")
            return []
            
        org_id = orgs[0]["id"]
        logging.info(f"🏢 Found Organization: {orgs[0].get('name')} (ID: {org_id})")
        
        channels_query = """
        query GetChannels($input: ChannelsInput!) {
          channels(input: $input) {
            id
            service
            name
            metadata {
              ... on PinterestMetadata {
                boards {
                  serviceId
                  name
                }
              }
            }
          }
        }
        """
        
        variables = {
            "input": {
                "organizationId": org_id
            }
        }
        
        res_ch = requests.post(url, json={"query": channels_query, "variables": variables}, headers=headers, timeout=20)
        ch_data = res_ch.json()
        
        if "errors" in ch_data:
            logging.error(f"❌ Buffer Channels Error: {ch_data['errors']}")
            return []
            
        channels = ch_data.get("data", {}).get("channels", [])
        channel_details = []
        
        for ch in channels:
            service = ch.get("service", "").lower()
            board_id = None
            
            if service == "pinterest":
                boards = []
                meta = ch.get("metadata")
                if meta and isinstance(meta, dict):
                    boards = meta.get("boards", [])
                if boards:
                    board_id = boards[0].get("serviceId")
                    logging.info(f"📌 Pinterest Board selected: {boards[0].get('name')} (ID: {board_id})")
                else:
                    logging.warning(f"⚠️ Pinterest channel '{ch.get('name')}' me koi board nahi mila!")
            
            channel_details.append({
                "id": ch["id"],
                "service": service,
                "name": ch.get("name", "Unknown Channel"),
                "board_id": board_id
            })
            logging.info(f"📱 Channel Found: {ch.get('name')} ({service})")
        
        return channel_details
        
    except Exception as e:
        logging.error(f"⚠️ Buffer Profiles Exception: {e}")
        return []

# ================= 11. SEND TO BUFFER =================
def send_to_buffer(video_url, product_data, ai_data, buy_url):
    logging.info("🚀 Publishing Reel via Buffer GraphQL API...")
    
    channels = get_buffer_channels()
    if not channels:
        logging.error("❌ No Buffer channels found! Check your BUFFER_ACCESS_TOKEN.")
        return
        
    url = "https://api.buffer.com"
    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    
    caption_text = f"{ai_data['caption']}\n\nBuy Here: {buy_url}"
    
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """
    
    for ch in channels:
        c_id = ch["id"]
        service = ch["service"]
        ch_name = ch["name"]
        board_id = ch.get("board_id")
        
        metadata = {}
        
        if service == "instagram":
            metadata = {
                "instagram": {
                    "type": "reel",
                    "shouldShareToFeed": True
                }
            }
        elif service == "facebook":
            metadata = {
                "facebook": {
                    "type": "reel"
                }
            }
        elif service == "pinterest":
            if not board_id:
                logging.error(f"❌ Skipping Pinterest ({ch_name}) — No board selected")
                continue
            metadata = {
                "pinterest": {
                    "boardServiceId": board_id,
                    "title": product_data.get("title", "Deal")[:100]
                }
            }
        elif service == "youtube":
            metadata = {
                "youtube": {
                    "title": product_data.get("title", "Trending Tech Reel")[:90],
                    "categoryId": "28"
                }
            }
        elif service == "tiktok":
            metadata = {
                "tiktok": {
                    "isAiGenerated": False
                }
            }
        
        variables = {
            "input": {
                "channelId": c_id,
                "text": caption_text,
                "mode": "addToQueue",
                "schedulingType": "automatic",
                "assets": [
                    {
                        "video": {
                            "url": video_url
                        }
                    }
                ]
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
            
            create_result = res_json.get("data", {}).get("createPost", {})
            
            if create_result.get("post", {}).get("id"):
                logging.info(f"🎉 Reel Successfully Sent to {ch_name} ({service}) | Post ID: {create_result['post']['id']}")
            elif create_result.get("message"):
                logging.error(f"❌ Buffer Post Failed for {ch_name} ({service}): {create_result['message']}")
            else:
                logging.error(f"❌ Buffer Post Failed for {ch_name} ({service}): Unexpected → {res_json}")
                
        except Exception as e:
            logging.error(f"⚠️ Buffer GraphQL Post Exception for {ch_name}: {e}")

# ================= 12. MAIN WORKFLOW =================
async def process_and_publish(buy_url):
    logging.info("=" * 60)
    logging.info("🚀 STARTING MULTI-PLATFORM REEL GENERATION PROCESS (BUFFER)")
    logging.info("=" * 60)
    notify_telegram(f"🚀 <b>Reel banna shuru hua</b>\n🔗 {buy_url}")

    product = scrape_product_details(buy_url)
    if not product.get("title"):
        notify_telegram(f"❌ Product scrape fail ho gaya.\n🔗 {buy_url}")
        return

    ai_data = generate_reel_script(product)
    
    clean_title = sanitize_filename(product["title"])
    timestamp = int(time.time())
    unique_filename = f"reel_{clean_title}_{timestamp}.mp4"
    
    audio_path = os.path.join(OUTPUT_DIR, f"voice_{timestamp}.mp3")
    video_path = os.path.join(OUTPUT_DIR, unique_filename)
    
    if not generate_voiceover(ai_data["script"], audio_path):
        notify_telegram("❌ Awaaz (voiceover) nahi ban payi, reel skip ho gaya.")
        return
    
    if not create_vertical_reel_ffmpeg(product, ai_data, audio_path, video_path):
        notify_telegram("❌ Video (ffmpeg) build fail ho gaya, reel skip ho gaya.")
        return
    
    direct_mp4_link = upload_video_for_direct_link(video_path)
    
    if direct_mp4_link and BUFFER_ACCESS_TOKEN:
        send_to_buffer(
            video_url=direct_mp4_link, 
            product_data=product, 
            ai_data=ai_data, 
            buy_url=buy_url
        )
        notify_telegram(f"✅ <b>Reel Buffer ko bhej diya gaya</b>!\n🎬 {product['title']}")
    else:
        logging.error("❌ Video Upload failed or Buffer Access Token missing!")
        notify_telegram("❌ Video upload fail hua ya BUFFER_ACCESS_TOKEN missing hai.")

async def main():
    print("\n" + "=" * 60)
    print("🎬 MULTI-PLATFORM REEL GENERATOR (CONNECTED TO BUFFER)")
    print("=" * 60 + "\n")

    buy_url = os.getenv("PRODUCT_URL", "").strip()
    if not buy_url and len(sys.argv) > 1:
        buy_url = sys.argv[1].strip()
    if not buy_url:
        buy_url = input("🔗 Enter Product Link: ").strip()
    if not buy_url:
        print("❌ No product link provided. Exiting...")
        notify_telegram("⚠️ Koi product link provide nahi hui.")
        return
    
    await process_and_publish(buy_url)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️ Process interrupted by user. Exiting...")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        logging.error(f"Fatal error: {e}", exc_info=True)
        notify_telegram(f"❌ <b>Automation CRASH ho gaya</b>\n<code>{e}</code>")
        raise
