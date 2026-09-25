"""
Telegram Link Checker
======================
Yeh script GitHub Actions ke scheduled run (har 5 min) me chalta hai.
Kaam: Telegram bot ko bheja gaya naya "URL" wala message dhoondhna,
      GITHUB_OUTPUT me article_url likhna, aur offset file update karna
      taaki wahi message dobara process na ho.

Zaroori Environment Variables:
  TELEGRAM_BOT_TOKEN  -> BotFather se mila token
  TELEGRAM_CHAT_ID    -> sirf isi chat/user ke message accept honge (security)
"""

import os
import re
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
OFFSET_FILE = ".telegram_offset"
GITHUB_OUTPUT = os.getenv("GITHUB_OUTPUT")

URL_REGEX = re.compile(r"https?://\S+")


def read_offset():
    if os.path.exists(OFFSET_FILE):
        try:
            with open(OFFSET_FILE, "r") as f:
                return int(f.read().strip() or 0)
        except Exception:
            return 0
    return 0


def write_offset(value):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(value))


def set_output(article_url):
    if GITHUB_OUTPUT:
        with open(GITHUB_OUTPUT, "a") as f:
            f.write(f"article_url={article_url}\n")
    print(f"article_url = '{article_url}'")


def reply(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"⚠️ Telegram reply failed: {e}")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set nahi hai. Skip.")
        set_output("")
        return

    offset = read_offset()
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": offset + 1, "timeout": 0},
            timeout=20,
        )
        data = resp.json()
    except Exception as e:
        print(f"⚠️ getUpdates fail: {e}")
        set_output("")
        return

    if not data.get("ok"):
        print(f"⚠️ Telegram API error: {data}")
        set_output("")
        return

    found_url = ""
    max_update_id = offset

    for update in data.get("result", []):
        max_update_id = max(max_update_id, update.get("update_id", 0))
        msg = update.get("message", {})
        sender_chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "") or ""

        # Sirf apne allowed chat id se hi message accept karo (security)
        if sender_chat_id != CHAT_ID:
            continue

        match = URL_REGEX.search(text)
        if match:
            found_url = match.group(0).strip()

    # Offset hamesha update karo, chahe URL mile ya na mile
    write_offset(max_update_id)

    if found_url:
        print(f"✅ Naya article link mila: {found_url}")
        reply(f"✅ Link mil gaya! Video banna shuru ho raha hai:\n{found_url}")
    else:
        print("ℹ️ Koi naya article link nahi mila is baar.")

    set_output(found_url)


if __name__ == "__main__":
    main()
