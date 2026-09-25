import os
import re
import json
import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
OFFSET_FILE = ".telegram_offset"
GITHUB_OUTPUT = os.environ.get("GITHUB_OUTPUT", "")

def get_offset():
    if os.path.exists(OFFSET_FILE):
        try:
            return int(open(OFFSET_FILE).read().strip())
        except Exception:
            return 0
    return 0

def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))

def find_product_url(text):
    if not text:
        return None
    # bgtechlab / github.io / amazon / flipkart links
    patterns = [
        r"https?://bgtechlab\.github\.io/[^\s]+",
        r"https?://[^\s]*github\.io/[^\s]+",
        r"https?://(?:www\.)?(?:amazon\.in|amzn\.to|flipkart\.com|fktr\.in)/[^\s]+",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(0).rstrip(").,]>")
    return None

def main():
    article_url = ""
    if not TOKEN:
        print("No TELEGRAM_BOT_TOKEN")
        _write_output(article_url)
        return

    offset = get_offset()
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = {"offset": offset + 1, "timeout": 10}
    try:
        res = requests.get(url, params=params, timeout=30)
        data = res.json()
    except Exception as e:
        print(f"Telegram API error: {e}")
        _write_output("")
        return

    if not data.get("ok"):
        print(f"Telegram not ok: {data}")
        _write_output("")
        return

    results = data.get("result") or []
    max_update_id = offset
    found = None

    for upd in results:
        uid = upd.get("update_id", 0)
        if uid > max_update_id:
            max_update_id = uid

        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))

        # optional: sirf apne chat se
        if CHAT_ID and chat_id and chat_id != str(CHAT_ID):
            continue

        text = msg.get("text") or msg.get("caption") or ""
        link = find_product_url(text)
        if link:
            found = link
            print(f"Found URL: {link}")

    if max_update_id > offset:
        save_offset(max_update_id)

    _write_output(found or "")

def _write_output(article_url):
    print(f"article_url={article_url}")
    if GITHUB_OUTPUT:
        with open(GITHUB_OUTPUT, "a") as f:
            f.write(f"article_url={article_url}\n")

if __name__ == "__main__":
    main()
