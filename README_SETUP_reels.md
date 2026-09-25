# BG Reels Bot — सभी Category के लिए Multi-Platform Reel Automation

## क्या बदला है
पहले यह bot सिर्फ "sound/bass/Dolby audio" वाला script बनाता था चाहे product कोई भी हो — क्योंकि:
1. आपकी अपनी साइट (`bgtechlab.github.io`) की product review pages को यह Amazon/Flipkart के सीधे पेज जैसा स्क्रैप करने की कोशिश करता था, जो गलत तरीका था। अब एक नया स्क्रैपर जोड़ा है जो **आपकी अपनी साइट का structure** (title, category badge, price, pros/cons, images) सीधे पढ़ता है — यह हर category (TV, mobile, audio, kitchen, fashion, travel — सब) में एक जैसे तरीके से काम करेगा, क्योंकि आपकी साइट का template हर category में समान है।
2. AI स्क्रिप्ट prompt में "sound/bass" hardcode था — अब वो हटाकर असली scrape किए गए Pros/Features और Category के हिसाब से dynamic बनाया है।

अब यह **हर category** (Mobiles, TV, Audio, Laptops, Printers, Smartwatches, Gadgets, Kitchen, Home & Kitchen, Accessories, Women wear, Men, Travel, Car & Motorbike, Books) के लिए सही तरीके से काम करेगा — बस उस product की `bgtechlab.github.io/bgtech/products/...` वाली लिंक डालनी है।

## नई फाइलें रिपो में डालनी हैं
```
reels_bot.py
requirements.txt
telegram_check.py
.github/workflows/reels-bot.yml
```

## स्टेप 1 — Secrets जोड़ें (Settings → Secrets and variables → Actions)

| Secret Name              | Value                                      |
|---------------------------|----------------------------------------------|
| `BUFFER_ACCESS_TOKEN`      | आपका Buffer API token                        |
| `TELEGRAM_BOT_TOKEN`       | Telegram बॉट token                           |
| `TELEGRAM_CHAT_ID`         | आपकी chat id                                 |

(Buffer में पहले से Instagram, Facebook, Pinterest channels **connect** होने चाहिए — bot वहीं पोस्ट भेजेगा।)

## स्टेप 2 — इस्तेमाल कैसे करें (2 तरीके)

**GitHub पेज से:** Actions → "BG Reels Bot - Multi-Platform Auto Publisher" → Run workflow → `product_url` में किसी भी category के product की bgtechlab लिंक डालें → Run।

**Telegram से:** अपने बॉट को कोई भी product लिंक भेज दें (चाहे किसी भी category का हो) — 5 मिनट के अंदर अपने आप reel बनकर Buffer में queue हो जाएगा और Instagram/Facebook/Pinterest पर पोस्ट होगा।

## Schedule/Automatic Posting के बारे में
- GitHub का schedule सिर्फ यह चेक करता है कि Telegram पर **नया लिंक आया है या नहीं** — इसलिए असल में जब भी आप कोई लिंक बॉट को भेजेंगे, वही process होगा। अगर आप चाहें कि रोज खुद-ब-खुद कोई ना कोई प्रोडक्ट बिना आपके भेजे उठाकर reel बने (जैसे आपकी साइट की latest products list से), तो वो एक अलग फीचर होगा — बताएं तो वो भी जोड़ सकता हूँ (आपकी साइट के `data/products.json` से रोज एक नया प्रोडक्ट अपने आप उठाकर reel बनाना)।
- Buffer में पोस्ट का असली schedule/समय (queue timing) Buffer के अपने app में सेट होता है — यह bot सिर्फ पोस्ट को queue में भेजता है (`mode: addToQueue`), असली publish समय Buffer अपने queue schedule अनुसार तय करता है।

## ध्यान रखने वाली बात
- **Pinterest:** Buffer कभी-कभी Pinterest पोस्ट के लिए board ID जैसी extra जानकारी माँगता है। अगर Pinterest पर पोस्ट fail हो (Telegram पर पता चल जाएगा), तो बताएं — उसका अलग से metadata जोड़ देंगे।
- आवाज़ (TTS) के लिए अब 3 fallback हैं: OpenAI.fm → Edge TTS → gTTS।
