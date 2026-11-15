#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLIENT BOT - Binance Square auto-poster. ONE file, standard library only (no pip install).

Content ("Coin Academy"): coin profiles, side-by-side comparisons, polls, "did you know", category
lists, beginner lessons and a live market snapshot. Every post has a generated picture and cashtags
($BTC ...) so readers can open the trading pair straight from the post.

Run:   python client_bot.py --loop         run forever
       python client_bot.py --duration 58  run 58 minutes then exit (GitHub Actions)
       python client_bot.py --dry          build one post + preview.png, publish nothing
       python client_bot.py --doctor       self-check        python client_bot.py --report   post history
Env:   SQUARE_API_KEY   (required, put it in GitHub Secrets - NEVER inside this file)
       BRAND / BRAND_EN / HASHTAGS      name shown in posts / on pictures / hashtags
       POSTS_PER_DAY (default 36)       EXCLUDE (e.g. TON,WLD: coins to never mention)
       REQUIRE_IMAGE=0                  allow text-only posts when a picture fails
Educational content only. Never gives trading advice and never trades."""
import email.utils
import hashlib
import html
import json
import os
import random
import re
import socket
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

socket.setdefaulttimeout(60)
SQUARE_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"
SQUARE_V2 = "https://www.binance.com/bapi/composite/v2/public/pgc/openApi"
SQUARE_URL = SQUARE_V1 + "/content/add"
STATE_FILE = "client_posted.json"
LOCK_FILE = "client.lock"
LOG_FILE = "client.log"
BRAND = os.getenv("BRAND", "").strip() or "أكاديمية العملات"
BRAND_EN = re.sub(r"[^A-Za-z0-9 ]", "", os.getenv("BRAND_EN", "")).strip() or "COIN ACADEMY"
HASHTAGS = os.getenv("HASHTAGS", "").strip() or ("#" + BRAND.replace(" ", "_") + " #Crypto #Binance")
POSTS_PER_DAY = max(4, int(os.getenv("POSTS_PER_DAY", "36")))
TARGET_GAP = max(300, int(86400 / POSTS_PER_DAY) - 15)
RETRY_SECONDS = 120
REQUIRE_IMAGE = os.getenv("REQUIRE_IMAGE", "1") != "0"
EXCLUDE = {x.strip().upper() for x in os.getenv("EXCLUDE", "").split(",") if x.strip()}
PHOTOS = os.getenv("PHOTOS", "1") != "0"             # real photo/logo first, generated card as fallback
NEWS = os.getenv("NEWS", "0") == "1"                  # optional news posts with the article's own photo
NEWS_PER_DAY = int(os.getenv("NEWS_PER_DAY", "6"))


def note(*parts):
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        if os.path.getsize(LOG_FILE) > 200000:
            with open(LOG_FILE, encoding="utf-8") as handle:
                tail = handle.read().splitlines()[-600:]
            with open(LOG_FILE, "w", encoding="utf-8") as handle:
                handle.write("\n".join(tail) + "\n")
    except OSError:
        pass


def clean_env(name):
    value = os.environ.get(name, "").strip()
    return "" if value.startswith("PASTE") else value


def limit_tags(text, keep=2):
    count = [0]

    def repl(match):
        count[0] += 1
        return match.group(0) if count[0] <= keep else match.group(1)
    return re.sub(r"(?<![\w.])\$([A-Za-z][A-Za-z0-9]{1,9})", repl, text)


def text_hash(text):
    return hashlib.md5(re.sub(r"\s+", " ", text)[:300].encode("utf-8")).hexdigest()[:16]


def post_square(text, key):
    keep = 5
    for _ in range(7):
        ambiguous = False
        req = urllib.request.Request(
            SQUARE_URL,
            data=json.dumps({"bodyTextOnly": limit_tags(text, keep)}).encode(),
            headers={"X-Square-OpenAPI-Key": key, "Content-Type": "application/json",
                     "clienttype": "binanceSkill"})
        try:
            with urllib.request.urlopen(req, timeout=35) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            ambiguous = exc.code >= 500
            result = {"code": f"HTTP{exc.code}", "message": exc.read()[:250].decode("utf-8", "ignore")}
        except Exception as exc:
            ambiguous = True
            result = {"code": "NETWORK", "message": str(exc)}
        code = str(result.get("code"))
        if code == "000000":
            data = result.get("data") or {}
            print("SQUARE OK", "https://www.binance.com/square/post/" + str(data.get("id", "")))
            return True
        note("SQUARE FAIL", result)
        if ambiguous:
            return None
        if code == "220095" and keep:
            keep -= 1
            continue
        return False
    return False


def sq_api(path, key, body, base=SQUARE_V2):
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"X-Square-OpenAPI-Key": key, "Content-Type": "application/json",
                                              "clienttype": "binanceSkill"})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:250].decode("utf-8", "ignore")
        if path == "/content/add" and exc.code >= 500:
            return {"code": "AMBIGUOUS", "message": f"HTTP{exc.code} {detail}"}
        return {"code": f"HTTP{exc.code}", "message": detail}
    except Exception as exc:
        if path == "/content/add":
            return {"code": "AMBIGUOUS", "message": str(exc)}
        return {"code": "NETWORK", "message": str(exc)}


def square_upload(image_path, key):
    name = os.path.basename(image_path)
    result = sq_api("/image/presignedUrl", key, {"imageName": name})
    if str(result.get("code")) != "000000":
        raise RuntimeError(f"image presign failed: {result}")
    data = result["data"]
    ext = name.rsplit(".", 1)[-1].lower()
    ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
             "webp": "image/webp"}.get(ext, "application/octet-stream")
    with open(image_path, "rb") as handle:
        payload = handle.read()
    request = urllib.request.Request(data["presignedUrl"], data=payload,
                                     method="PUT", headers={"Content-Type": ctype})
    urllib.request.urlopen(request, timeout=60).read()
    for _ in range(10):
        status = sq_api("/image/imageStatus", key, {"fileTicket": data["fileTicket"]})
        details = status.get("data") or {}
        if details.get("status") == 1:
            return details["imageUrl"]
        if details.get("status") == 2:
            raise RuntimeError(f"image processing failed: {details.get('failedReason')}")
        time.sleep(3)
    raise RuntimeError("image processing timeout")


def post_square_image(text, image_path, key):
    if not image_path or not os.path.exists(image_path):
        return False
    try:
        urls = [square_upload(image_path, key)]
    except Exception as exc:
        note("SQUARE image upload failed; NOT posting text-only:", exc)
        return False
    keep = 5
    for _ in range(7):
        result = sq_api("/content/add", key,
                        {"contentType": 1, "bodyTextOnly": limit_tags(text, keep), "imageList": urls}, SQUARE_V1)
        code = str(result.get("code"))
        if code == "000000":
            data = result.get("data") or {}
            print("SQUARE IMAGE OK", data.get("id", "(no id)"))
            return True
        note("SQUARE IMAGE FAIL", result)
        if code == "AMBIGUOUS":
            return None
        if code == "220095" and keep:
            keep -= 1
            continue
        return False
    return False


def sniff_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def download_image(url, stem="news_image"):
    """Download an image (jpg/png/webp, 5KB-5MB), detecting the type from the bytes."""
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=25) as response:
            data = response.read(5000001)
        ext = sniff_ext(data)
        if not ext or not (5000 <= len(data) <= 5000000):
            return None
        path = f"{stem}.{ext}"
        with open(path, "wb") as handle:
            handle.write(data)
        return path
    except Exception as exc:
        print("image download failed:", exc)
        return None


def strip_html(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def ascii_digits(text):
    return (text or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,"))


def _gtx(text):
    query = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "ar", "dt": "t", "q": text})
    request = urllib.request.Request("https://translate.googleapis.com/translate_a/single?" + query,
                                     headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    return "".join(part[0] for part in data[0] if part and part[0])


def _chrome_ext(text):
    query = urllib.parse.urlencode({"client": "dict-chrome-ex", "sl": "auto", "tl": "ar", "q": text})
    request = urllib.request.Request("https://clients5.google.com/translate_a/t?" + query,
                                     headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if isinstance(data, dict):
        return "".join(s.get("trans", "") for s in data.get("sentences", []))
    first = data[0]
    return first[0] if isinstance(first, list) else str(first)


def _mymemory(text):
    query = urllib.parse.urlencode({"q": text[:480], "langpair": "en|ar"})
    request = urllib.request.Request("https://api.mymemory.translated.net/get?" + query,
                                     headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    return (data.get("responseData") or {}).get("translatedText", "")


def translate_ar(text):
    if not text:
        return ""
    errors = []
    for provider in (_gtx, _chrome_ext, _mymemory):
        try:
            out = ascii_digits(provider(text)).strip()
            if len(re.findall(r"[؀-ۿ]", out)) >= 4:
                return out
            errors.append(f"{provider.__name__}: not Arabic")
        except Exception as exc:
            errors.append(f"{provider.__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


def rss_image(item):
    """Image published by the source itself (media:content / thumbnail / enclosure / <img>)."""
    for child in item.iter():
        tag = child.tag.rsplit("}", 1)[-1] if isinstance(child.tag, str) else ""
        if tag in ("content", "thumbnail", "enclosure"):
            url = (child.get("url") or "").strip()
            kind = (child.get("type") or child.get("medium") or "").lower()
            if url.startswith("http") and (kind.startswith("image") or tag == "thumbnail"
                                            or re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", url, re.I)):
                return url
    raw = (item.findtext("description") or "") + " " + " ".join(
        (c.text or "") for c in item if isinstance(c.tag, str) and c.tag.endswith("encoded"))
    match = re.search(r'<img[^>]+src=["\']([^"\']+)', html.unescape(raw))
    if match and match.group(1).startswith("http"):
        return match.group(1)
    return None


def page_image(url):
    """The article's own lead image (og:image / twitter:image). Skipped for Google News / Binance pages."""
    if not url or any(host in url for host in ("news.google.com", "binance.com")):
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            page = response.read(300000).decode("utf-8", "ignore")
        patterns = (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        )
        for pattern in patterns:
            match = re.search(pattern, page, re.I)
            if match and match.group(1).startswith("http"):
                return html.unescape(match.group(1))
    except Exception as exc:
        print("page image failed:", exc)
    return None

# ---------------------------------------------------------------- pictures (no libraries)
FONT = {
    "A": (14, 17, 17, 31, 17, 17, 17), "B": (30, 17, 17, 30, 17, 17, 30), "C": (14, 17, 16, 16, 16, 17, 14),
    "D": (30, 17, 17, 17, 17, 17, 30), "E": (31, 16, 16, 30, 16, 16, 31), "F": (31, 16, 16, 30, 16, 16, 16),
    "G": (14, 17, 16, 23, 17, 17, 15), "H": (17, 17, 17, 31, 17, 17, 17), "I": (14, 4, 4, 4, 4, 4, 14),
    "J": (7, 2, 2, 2, 2, 18, 12), "K": (17, 18, 20, 24, 20, 18, 17), "L": (16, 16, 16, 16, 16, 16, 31),
    "M": (17, 27, 21, 21, 17, 17, 17), "N": (17, 17, 25, 21, 19, 17, 17), "O": (14, 17, 17, 17, 17, 17, 14),
    "P": (30, 17, 17, 30, 16, 16, 16), "Q": (14, 17, 17, 17, 21, 18, 13), "R": (30, 17, 17, 30, 20, 18, 17),
    "S": (15, 16, 16, 14, 1, 1, 30), "T": (31, 4, 4, 4, 4, 4, 4), "U": (17, 17, 17, 17, 17, 17, 14),
    "V": (17, 17, 17, 17, 17, 10, 4), "W": (17, 17, 17, 21, 21, 21, 10), "X": (17, 17, 10, 4, 10, 17, 17),
    "Y": (17, 17, 17, 10, 4, 4, 4), "Z": (31, 1, 2, 4, 8, 16, 31),
    "0": (14, 17, 19, 21, 25, 17, 14), "1": (4, 12, 4, 4, 4, 4, 14), "2": (14, 17, 1, 2, 4, 8, 31),
    "3": (31, 2, 4, 2, 1, 17, 14), "4": (2, 6, 10, 18, 31, 2, 2), "5": (31, 16, 30, 1, 1, 17, 14),
    "6": (6, 8, 16, 30, 17, 17, 14), "7": (31, 1, 2, 4, 8, 8, 8), "8": (14, 17, 17, 14, 17, 17, 14),
    "9": (14, 17, 17, 15, 1, 2, 12), ".": (0, 0, 0, 0, 0, 12, 12), "-": (0, 0, 0, 31, 0, 0, 0),
    "/": (1, 1, 2, 4, 8, 16, 16), "+": (0, 4, 4, 31, 4, 4, 0), "%": (24, 25, 2, 4, 8, 19, 3),
    ":": (0, 12, 12, 0, 12, 12, 0), "$": (4, 15, 20, 14, 5, 30, 4), "&": (12, 18, 20, 8, 21, 18, 13),
    "'": (4, 4, 8, 0, 0, 0, 0), "?": (14, 17, 1, 2, 4, 0, 4), "!": (4, 4, 4, 4, 4, 0, 4), " ": (0,) * 7,
}


def make_card(path, title, subtitle="", accent=(240, 185, 11), seed=0):
    """1100x580 PNG: dark card, faint price line, big English title (built-in pixel font)."""
    import struct
    import zlib
    w, h = 1100, 580
    rnd = random.Random(seed)
    rows = [bytearray(bytes((24, 26, 32)) * w) for _ in range(h)]

    def rect(x0, y0, x1, y1, color):
        x0, x1 = max(0, x0), min(w, x1)
        if x1 <= x0:
            return
        chunk = bytes(color) * (x1 - x0)
        for y in range(max(0, y0), min(h, y1)):
            rows[y][x0 * 3:x1 * 3] = chunk

    def width(s, scale):
        return max(0, len(s) * 6 * scale - scale)

    def text(x, y, s, scale, color):
        for ch in s:
            for r, bits in enumerate(FONT.get(ch, FONT[" "])):
                for c in range(5):
                    if bits & (1 << (4 - c)):
                        rect(x + c * scale, y + r * scale, x + (c + 1) * scale, y + (r + 1) * scale, color)
            x += 6 * scale

    clean = lambda s: re.sub(r"[^A-Z0-9 /.+%$&:'?!-]", "", (s or "").upper()).strip()
    dim = tuple(c // 7 for c in accent)
    line = tuple(c // 3 for c in accent)
    value, points = 0.0, []
    for _ in range(70):
        value += rnd.uniform(-1, 1.08)
        points.append(value)
    lo, hi = min(points), max(points)
    ys = [int(540 - 190 * (p - lo) / ((hi - lo) or 1)) for p in points]
    step = w / (len(points) - 1)
    for i in range(len(points) - 1):
        x0, x1 = int(i * step), int((i + 1) * step)
        for x in range(x0, x1 + 1):
            y = int(ys[i] + (ys[i + 1] - ys[i]) * ((x - x0) / max(1, x1 - x0)))
            rect(x, y + 3, x + 1, 580, dim)
            rect(x, y - 1, x + 1, y + 2, line)
    rect(0, 0, w, 14, accent)
    text(50, 44, clean(BRAND_EN)[:28], 4, accent)
    lines = textwrap.wrap(clean(title) or "CRYPTO", 15)[:3]
    scale = max(5, min(18, 1000 // (6 * max(len(x) for x in lines))))
    y = 130
    for ln in lines:
        text(50, y, ln, scale, (255, 255, 255))
        y += 9 * scale
    for ln in textwrap.wrap(clean(subtitle), 32)[:2]:
        text(50, y + 14, ln, 5, (150, 158, 170))
        y += 60
    text(50, 536, "EDUCATIONAL CONTENT - NOT FINANCIAL ADVICE", 3, (110, 118, 130))
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as handle:
        handle.write(png)
    return path


# ---------------------------------------------------------------- content
CATS = {
    "L1": ("بلوكتشين أساسي (Layer 1)", "LAYER 1", (240, 185, 11),
           "تتأثر عادةً بنمو التطبيقات وعدد المستخدمين وأخبار الترقيات التقنية."),
    "L2": ("حلول توسعة (Layer 2)", "LAYER 2", (98, 126, 234),
           "يتابعون نشاط الشبكة، وحجم الأموال المودعة فيها، وعلاقتها بالشبكة الأم."),
    "DEFI": ("التمويل اللامركزي (DeFi)", "DEFI", (14, 203, 129),
             "يراقبون حجم الأموال المقفلة في البروتوكول والرسوم والإيرادات وأخبار الأمان."),
    "MEME": ("عملات الميم", "MEME", (246, 70, 93),
             "تتحرك غالبًا بالمزاج الجماعي ونشاط مواقع التواصل، وتقلبها عالٍ جدًا."),
    "AI": ("الذكاء الاصطناعي", "AI", (170, 120, 255),
           "ترتبط أحيانًا بأخبار قطاع الذكاء الاصطناعي عمومًا، وتتحرك معه بقوة."),
    "PRIV": ("عملات الخصوصية", "PRIVACY", (130, 140, 150),
             "تتأثر بالتنظيمات وقرارات الإدراج على المنصات إضافةً إلى الطلب عليها."),
    "INFRA": ("البنية التحتية", "INFRA", (60, 190, 220),
              "يتابعون مدى استخدام الخدمة فعليًا من قبل المشاريع والمطورين."),
    "GAME": ("الألعاب والميتافيرس", "GAMING", (255, 140, 60),
             "تتأثر بعدد اللاعبين النشطين وإصدارات الألعاب وسلوك السوق العام."),
    "PAY": ("المدفوعات والتحويلات", "PAYMENTS", (90, 170, 255),
            "يراقبون الاعتماد من شركات الدفع، والتنظيمات، وحجم التحويلات على الشبكة."),
}


def C(ar, en, year, cat, what, *facts):
    return dict(ar=ar, en=en, year=year, cat=cat, what=what, facts=list(facts))


COINS = {
    "BTC": C("بيتكوين", "Bitcoin", 2009, "L1", "أول عملة رقمية وأكبرها قيمة سوقية، تُعرف بـ«الذهب الرقمي».",
             "أُطلقت في يناير 2009 على يد شخص أو مجموعة باسم ساتوشي ناكاموتو.",
             "الحد الأقصى للعرض 21 مليون عملة فقط.",
             "تُخفَّض مكافأة التعدين إلى النصف تقريبًا كل 4 سنوات فيما يُعرف بالتنصيف."),
    "ETH": C("إيثريوم", "Ethereum", 2015, "L1", "منصة العقود الذكية الأشهر، وعليها أغلب تطبيقات DeFi.",
             "اقترحها فيتاليك بوتيرين وانطلقت شبكتها في يوليو 2015.",
             "بُنيت عليها العقود الذكية والرموز غير القابلة للاستبدال (NFT).",
             "انتقلت من التعدين إلى إثبات الحصة في سبتمبر 2022."),
    "BNB": C("بي إن بي", "BNB", 2017, "L1", "العملة الأصلية لشبكة BNB Chain ومنظومة Binance.",
             "ظهرت عام 2017 عبر عرض أولي.",
             "تُستخدم لدفع رسوم الشبكة (الغاز) على BNB Chain.",
             "تُنفَّذ لها عمليات حرق دورية لتقليل المعروض."),
    "SOL": C("سولانا", "Solana", 2020, "L1", "شبكة سريعة برسوم منخفضة تستضيف تطبيقات كثيرة وعملات الميم.",
             "أسسها أناتولي ياكوفينكو وانطلقت شبكتها الرئيسية عام 2020.",
             "تجمع بين إثبات الحصة وآلية «إثبات التاريخ» (Proof of History).",
             "شهدت في بعض الفترات توقفًا مؤقتًا للشبكة."),
    "ADA": C("كاردانو", "Cardano", 2017, "L1", "بلوكتشين يُبنى بنهج أكاديمي ومراجعات علمية للأبحاث.",
             "أسسها تشارلز هوسكينسون وهو أحد مؤسسي إيثريوم.",
             "تعمل بآلية إثبات الحصة المسماة Ouroboros."),
    "AVAX": C("أفالانش", "Avalanche", 2020, "L1", "شبكة تتيح إنشاء سلاسل فرعية مخصصة تسمى Subnets.",
              "أُطلقت من شركة Ava Labs عام 2020.",
              "تشتهر بسرعة التأكيد النهائي للمعاملات."),
    "DOT": C("بولكادوت", "Polkadot", 2020, "L1", "شبكة تربط سلاسل مستقلة تُسمى Parachains بسلسلة مركزية.",
             "أسسها غافين وود، أحد مؤسسي إيثريوم.",
             "انطلقت الشبكة الرئيسية عام 2020."),
    "ATOM": C("كوزموس", "Cosmos", 2019, "L1", "منظومة تُعرف بشعار «إنترنت البلوكتشين» لربط السلاسل.",
              "تعتمد بروتوكول IBC لنقل البيانات والأصول بين السلاسل.",
              "تتيح لكل تطبيق بناء سلسلته الخاصة عبر Cosmos SDK."),
    "NEAR": C("نير", "NEAR Protocol", 2020, "L1", "شبكة تركّز على سهولة الاستخدام للمطورين والمستخدمين.",
              "تعتمد تقنية التجزئة (Sharding) لزيادة السعة.",
              "تتيح حسابات بأسماء مقروءة بدل العناوين الطويلة."),
    "APT": C("أبتوس", "Aptos", 2022, "L1", "سلسلة سريعة تعتمد لغة Move للعقود الذكية.",
             "أُطلقت عام 2022 من فريق عمل سابقين في مشروع Diem لدى Meta.",
             "تركّز على السرعة والأمان للتطبيقات."),
    "SUI": C("سوي", "Sui", 2023, "L1", "سلسلة جديدة تعالج كثيرًا من المعاملات بالتوازي.",
             "طوّرتها شركة Mysten Labs وانطلقت شبكتها الرئيسية عام 2023.",
             "تستخدم لغة Move أيضًا لكتابة العقود الذكية."),
    "TON": C("تون كوين", "Toncoin", 2018, "L1", "شبكة ارتبطت بتطبيقات داخل منصة تيليجرام.",
             "بدأت كمشروع من تيليجرام ثم تولّى تطويرها مجتمع مستقل.",
             "تُستخدم لدفع الرسوم وتشغيل تطبيقات الشبكة."),
    "TRX": C("ترون", "TRON", 2018, "L1", "شبكة تُستخدم كثيرًا في تحويل العملات المستقرة.",
             "أسسها جاستن صن وأُطلقت شبكتها الرئيسية عام 2018.",
             "يُنقل عليها حجم كبير من تحويلات USDT برسوم منخفضة نسبيًا."),
    "ICP": C("إنترنت كمبيوتر", "Internet Computer", 2021, "L1", "شبكة تهدف لتشغيل تطبيقات كاملة على السلسلة نفسها.",
             "طوّرتها مؤسسة DFINITY وأُطلقت عام 2021."),
    "ALGO": C("ألغوراند", "Algorand", 2019, "L1", "بلوكتشين يعتمد إثبات حصة نقيًا بتأكيد سريع.",
              "أسسها سيلفيو ميكالي الحائز على جائزة تورينغ."),
    "HBAR": C("هيديرا", "Hedera", 2019, "L1", "شبكة تستخدم تقنية Hashgraph بدل سلسلة الكتل التقليدية.",
              "تُدار عبر مجلس حوكمة يضم شركات كبرى."),
    "ETC": C("إيثريوم كلاسيك", "Ethereum Classic", 2016, "L1", "السلسلة الأصلية التي استمرت بعد انقسام إيثريوم.",
             "ظهرت بعد انقسام الشبكة إثر حادثة The DAO عام 2016.",
             "ما زالت تعتمد التعدين بإثبات العمل."),
    "FIL": C("فايلكوين", "Filecoin", 2020, "INFRA", "شبكة لامركزية لتخزين البيانات.",
             "تدفع للمستخدمين الذين يوفرون مساحة تخزين للشبكة."),
    "LINK": C("تشين لينك", "Chainlink", 2017, "INFRA", "شبكة أوراكل تنقل بيانات العالم الحقيقي إلى العقود الذكية.",
              "أسسها سيرجي نازاروف.",
              "تعتمد عليها تطبيقات DeFi كثيرة لجلب الأسعار."),
    "GRT": C("ذا غراف", "The Graph", 2020, "INFRA", "بروتوكول يفهرس بيانات البلوكتشين لتسهيل الاستعلام عنها.",
             "يُسمى أحيانًا «غوغل البلوكتشين»."),
    "VET": C("في تشين", "VeChain", 2018, "INFRA", "بلوكتشين يركّز على تتبع سلاسل الإمداد للشركات.",
             "يعتمد نظام عملتين: VET للقيمة وVTHO للغاز."),
    "TIA": C("سيلستيا", "Celestia", 2023, "INFRA", "أول شبكة مبنية على فكرة البلوكتشين المعياري (Modular).",
             "تتخصص في توفير البيانات (Data Availability) للسلاسل الأخرى.",
             "أُطلقت شبكتها عام 2023."),
    "ARB": C("أربيترم", "Arbitrum", 2021, "L2", "حل توسعة لإيثريوم يعتمد تقنية Optimistic Rollups.",
             "يقلل الرسوم ويسرّع المعاملات مقارنة بالشبكة الرئيسية.",
             "وُزّع رمز ARB على المجتمع عام 2023."),
    "OP": C("أوبتيمزم", "Optimism", 2021, "L2", "طبقة ثانية لإيثريوم تعتمد Optimistic Rollups.",
            "تبني منظومة «Superchain» من سلاسل مترابطة."),
    "POL": C("بوليغون", "Polygon", 2017, "L2", "منظومة لتوسعة إيثريوم برسوم أقل وسرعة أعلى.",
             "رمز POL خلف رمز MATIC في منظومة Polygon."),
    "IMX": C("إيميوتابل", "Immutable", 2021, "GAME", "شبكة توسعة لألعاب وNFT مبنية فوق إيثريوم.",
             "تركّز على تقليل رسوم الألعاب والمقتنيات الرقمية."),
    "UNI": C("يونيسواب", "Uniswap", 2018, "DEFI", "بورصة لامركزية تعتمد نموذج صانع السوق الآلي (AMM).",
             "أُطلقت عام 2018 وأصدرت رمز UNI عام 2020.",
             "تتيح تبادل الرموز دون وسيط مركزي."),
    "AAVE": C("آف", "Aave", 2020, "DEFI", "بروتوكول إقراض واقتراض لامركزي.",
              "بدأ باسم ETHLend قبل أن يتحول إلى Aave.",
              "اشتهر بخدمة «القروض السريعة» (Flash Loans)."),
    "CRV": C("كيرف", "Curve", 2020, "DEFI", "بورصة لامركزية متخصصة في العملات المستقرة والأصول المتقاربة.",
             "يعتمد رمز CRV نظام قفل veCRV لتوجيه المكافآت."),
    "LDO": C("ليدو", "Lido DAO", 2020, "DEFI", "أكبر بروتوكولات التخزين السائل (Liquid Staking).",
             "يمنح من يودع ETH رمزًا مقابلًا (stETH) يمكن استخدامه في DeFi."),
    "INJ": C("إنجكتيف", "Injective", 2021, "DEFI", "سلسلة موجهة لتطبيقات المال اللامركزي مثل التداول.",
             "مبنية على إطار Cosmos."),
    "RUNE": C("ثورتشين", "THORChain", 2021, "DEFI", "بروتوكول لتبادل الأصول بين سلاسل مختلفة دون وسيط.",
              "يتيح التبادل الأصلي دون الحاجة إلى عملات مغلّفة."),
    "DOGE": C("دوجكوين", "Dogecoin", 2013, "MEME", "عملة بدأت كمزحة مستوحاة من ميم كلب الشيبا إينو.",
              "أُطلقت في ديسمبر 2013 على يد بيلي ماركوس وجاكسون بالمر.",
              "ليس لها حد أقصى ثابت للعرض."),
    "SHIB": C("شيبا إينو", "Shiba Inu", 2020, "MEME", "عملة ميم على إيثريوم وصفت بأنها «قاتل دوجكوين».",
              "ظهرت عام 2020 باسم مستعار «Ryoshi».",
              "تعمل كرمز ERC-20 على إيثريوم."),
    "PEPE": C("بيبي", "Pepe", 2023, "MEME", "عملة ميم مستوحاة من ميم الضفدع الشهير.",
              "ظهرت عام 2023.",
              "ترتبط حركتها غالبًا بمزاج المضاربين ونشاط مواقع التواصل."),
    "FET": C("فيتش إيه آي", "Fetch.ai", 2019, "AI", "مشروع يركّز على وكلاء الذكاء الاصطناعي المستقلين.",
             "انضم عام 2024 إلى تحالف Artificial Superintelligence Alliance مع مشاريع أخرى."),
    "RENDER": C("رندر", "Render", 2020, "AI", "شبكة لامركزية لتأجير قدرة معالجة الرسوميات (GPU).",
                "تستفيد منها أعمال الرسوم والذكاء الاصطناعي."),
    "WLD": C("وورلد كوين", "Worldcoin", 2023, "AI", "مشروع للتحقق من الهوية البشرية بمسح قزحية العين.",
             "شارك في تأسيسه سام ألتمان.",
             "أثار جدلًا حول الخصوصية بسبب مسح القزحية عبر جهاز Orb."),
    "XRP": C("ريبل", "XRP", 2012, "PAY", "عملة مرتبطة بشبكة XRP Ledger وتركّز على التحويل عبر الحدود.",
             "أُطلقت شبكتها عام 2012 وارتبطت بشركة Ripple.",
             "العرض الكلي 100 مليار عملة."),
    "XLM": C("ستيلار", "Stellar", 2014, "PAY", "شبكة تركّز على التحويلات منخفضة التكلفة بين الدول.",
             "أُطلقت عام 2014 وشارك في تأسيسها جيد مكالب."),
    "LTC": C("لايتكوين", "Litecoin", 2011, "PAY", "عملة أُطلقت لتكون أسرع وأخف من بيتكوين.",
             "أطلقها تشارلي لي عام 2011.",
             "يُشار إليها بـ«الفضة مقابل ذهب بيتكوين».",
             "الحد الأقصى لعرضها 84 مليون عملة."),
    "BCH": C("بيتكوين كاش", "Bitcoin Cash", 2017, "PAY", "عملة انشقت عن بيتكوين لتسريع المعاملات.",
             "ظهرت في أغسطس 2017 بعد خلاف حول حجم الكتلة."),
    "XMR": C("مونيرو", "Monero", 2014, "PRIV", "عملة تركّز على الخصوصية الكاملة للمعاملات.",
             "تخفي المرسل والمستلم والمبلغ افتراضيًا.",
             "قد تواجه قيودًا على بعض المنصات بسبب خصوصيتها."),
    "ZEC": C("زي كاش", "Zcash", 2016, "PRIV", "عملة تتيح معاملات محمية بتقنية zk-SNARKs.",
             "أُطلقت عام 2016.",
             "الحماية اختيارية وليست إجبارية."),
    "SAND": C("ذا ساندبوكس", "The Sandbox", 2020, "GAME", "عالم افتراضي يبني فيه المستخدمون ألعابهم ويملكون أراضي رقمية."),
    "MANA": C("ديسنترالاند", "Decentraland", 2017, "GAME", "عالم افتراضي ثلاثي الأبعاد يملك فيه المستخدمون أراضي رقمية."),
    "AXS": C("أكسي إنفينيتي", "Axie Infinity", 2018, "GAME", "لعبة اشتهرت بنموذج «العب لتربح».",
             "تعمل على شبكة Ronin الخاصة بها."),
}
for _s in list(COINS):
    if _s in EXCLUDE:
        del COINS[_s]
MAJORS = [s for s in ("BTC", "ETH", "BNB", "SOL", "XRP", "DOGE") if s in COINS] or list(COINS)[:4]

L = lambda en, ar, body: dict(en=en, ar=ar, body=body)
LESSONS = [
    L("WHAT IS STAKING", "التخزين (Staking)", "في الشبكات التي تعتمد إثبات الحصة يمكنك قفل جزء من عملاتك لدعم الشبكة مقابل مكافأة دورية. العائد ليس مضمونًا، وقد تُقفل العملات لفترة، وقد تنخفض قيمتها أثناء القفل."),
    L("BITCOIN HALVING", "التنصيف (Halving)", "كل نحو 4 سنوات تنخفض مكافأة التعدين في بيتكوين إلى النصف، فيتباطأ إصدار العملات الجديدة. كان آخر تنصيف في أبريل 2024. هو حدث مبرمج ومعروف مسبقًا، لكنه لا يضمن اتجاه السعر."),
    L("MARKET CAP", "القيمة السوقية", "القيمة السوقية = السعر × العرض المتداول. هي المقياس الأدق لحجم المشروع، وليس سعر العملة وحده. عملة سعرها 0.001$ قد تكون أكبر من عملة سعرها 100$."),
    L("LIQUIDITY", "السيولة", "السيولة هي سهولة الشراء والبيع دون تحريك السعر كثيرًا. العملات ذات الحجم الكبير تنفّذ أوامرك بسعر قريب مما رأيته، أما قليلة السيولة فقد تتحرك بشدة بمجرد أمر كبير."),
    L("SLIPPAGE", "الانزلاق السعري", "الانزلاق هو الفرق بين السعر المتوقع وسعر التنفيذ الفعلي. يزداد مع الأوامر السوقية الكبيرة في الأسواق قليلة السيولة. الأوامر المحددة (Limit) تحميك من تنفيذ سعر لا تريده."),
    L("GAS FEES", "رسوم الشبكة (Gas)", "كل معاملة على البلوكتشين تتطلب رسومًا للمعدّنين أو المدقّقين، وتزداد عند الازدحام. اختر الوقت والشبكة المناسبين، وتأكد من الرسوم قبل تأكيد أي تحويل."),
    L("HOT VS COLD WALLET", "المحفظة الساخنة والباردة", "المحفظة الساخنة متصلة بالإنترنت وسهلة الاستخدام لكنها أكثر عرضة للاختراق. المحفظة الباردة تحفظ المفاتيح بعيدًا عن الإنترنت وهي أنسب للمبالغ الكبيرة طويلة الأجل."),
    L("SEED PHRASE", "عبارة الاسترداد", "هي 12 أو 24 كلمة تمنح من يملكها السيطرة الكاملة على محفظتك. لا تصوّرها ولا تخزنها على الهاتف أو السحابة، ولا تدخلها في أي موقع. لن يطلبها منك دعم فني حقيقي أبدًا."),
    L("PHISHING", "التصيد الاحتيالي", "روابط ورسائل مزيفة تنتحل صفة المنصات لسرقة بياناتك. تحقق من عنوان الموقع بدقة، لا تضغط روابط من مصادر مجهولة، ولا تشارك رموز التحقق مع أحد."),
    L("TWO FACTOR AUTH", "المصادقة الثنائية", "فعّل المصادقة الثنائية على حسابك بتطبيق مصادقة بدل الرسائل النصية متى أمكن. هي خط الدفاع الثاني إذا تسربت كلمة السر."),
    L("DOLLAR COST AVERAGING", "التجميع الدوري DCA", "شراء مبلغ ثابت على فترات منتظمة يقلل أثر التوقيت السيئ لدخول واحد كبير. لا يمنع الخسارة إذا هبط الأصل طويلًا، لكنه يخفف التوتر ويضبط الانضباط."),
    L("STOP LOSS", "وقف الخسارة", "وقف الخسارة أمر يغلق صفقتك تلقائيًا عند سعر تحدده لتحدّ من الضرر. ضعه عند مستوى يُبطل فكرتك (تحت دعم أو فوق مقاومة)، لا عند رقم عشوائي."),
    L("POSITION SIZING", "حجم الصفقة", "كثير من المحترفين يخاطرون بنسبة صغيرة (مثل 1–2%) من رأس المال في الصفقة الواحدة. بهذه الطريقة لا تدمّر سلسلة خسائر حسابك وتبقى قادرًا على الاستمرار."),
    L("SUPPORT AND RESISTANCE", "الدعم والمقاومة", "الدعم منطقة يتوقف عندها الهبوط غالبًا، والمقاومة منطقة يتوقف عندها الصعود. كلما اختُبر المستوى أكثر زادت أهميته، وعند كسره بحجم جيد قد ينقلب دوره."),
    L("CANDLESTICKS", "الشموع اليابانية", "كل شمعة تلخص حركة السعر في فترة: الافتتاح والإغلاق والأعلى والأدنى. جسم الشمعة يبيّن الفرق بين الافتتاح والإغلاق، والظلال تبيّن المدى الذي وصل إليه السعر قبل أن يتراجع."),
    L("TRADING VOLUME", "حجم التداول", "ارتفاع السعر مع حجم مرتفع أقوى عادةً من ارتفاعه مع حجم ضعيف. الحجم المنخفض يعني مشاركين أقل، فقد تنعكس الحركة بسهولة."),
    L("LIMIT VS MARKET ORDER", "الأمر المحدد والسوقي", "الأمر السوقي ينفّذ فورًا بأفضل سعر متاح لكنه قد ينزلق. الأمر المحدد ينفّذ بسعرك أو أفضل لكنه قد لا ينفّذ إطلاقًا. اختر حسب أولويتك: السرعة أم السعر."),
    L("LEVERAGE", "الرافعة المالية", "الرافعة تضاعف حجم صفقتك بمال مقترض، فتضاعف الربح والخسارة معًا. كلما زادت الرافعة اقترب سعر التصفية من سعر دخولك. ابدأ برافعة منخفضة جدًا أو دونها."),
    L("LIQUIDATION", "التصفية", "تُغلق الصفقة المرفوعة قسرًا عندما يقترب رصيدك من تغطية الخسارة. تعرّف على سعر التصفية قبل فتح أي صفقة، وتجنب استخدام كل رصيدك كهامش."),
    L("FUNDING RATE", "معدل التمويل", "في العقود الدائمة يدفع أحد الطرفين الآخر دوريًا لإبقاء سعر العقد قريبًا من السعر الفوري. معدل مرتفع جدًا قد يدل على ازدحام في جهة واحدة، وهو مؤشر مساعد وليس إشارة تداول."),
    L("FEAR AND GREED", "الخوف والطمع", "مؤشر يلخص مشاعر السوق من التقلب والزخم وغيرهما. القراءات المتطرفة تدل على المزاج السائد، لكنها وحدها لا تحدد القمم والقيعان."),
    L("STABLECOINS", "العملات المستقرة", "عملات تحاول الحفاظ على سعر ثابت مقابل الدولار. استقرارها ليس مطلقًا: قد تفقد ارتباطها بسبب نقص الاحتياطي أو أزمة ثقة. اعرف من يصدرها وكيف تُدعم."),
    L("CEX VS DEX", "المنصة المركزية واللامركزية", "المنصة المركزية (CEX) تحتفظ بأموالك وتدير الحساب، وهي أسهل. المنصة اللامركزية (DEX) تبقى المفاتيح معك لكنك تتحمل مسؤولية الأمان والرسوم والأخطاء."),
    L("CROSS CHAIN BRIDGES", "الجسور بين السلاسل", "الجسر ينقل أصولك بين بلوكتشينين مختلفين. هو من أكثر نقاط الضعف استهدافًا بالاختراق، فلا تنقل مبالغ كبيرة عبر جسور لا تعرفها."),
    L("AIRDROPS", "الإيردروب", "توزيع مجاني لرموز على مستخدمي مشروع. كثير من الرسائل التي تعد بإيردروب احتيالية، فلا توقّع أي صلاحية على محفظتك من رابط لا تثق به."),
    L("TOKENOMICS", "اقتصاديات الرمز", "تشمل العرض الكلي والمتداول وكيف يوزَّع بين الفريق والمستثمرين والمجتمع وجدول فتحه. افهمها قبل الدخول، فتوزيع مركّز أو إصدار كبير مرتقب قد يضغط على السعر."),
    L("TOKEN UNLOCKS", "فتح الرموز", "كثير من المشاريع تقفل رموز الفريق والمستثمرين ثم تفتحها تدريجيًا. فتح كمية كبيرة قد يزيد المعروض في السوق دفعة واحدة، فتابع جداول الفتح."),
    L("TOKEN BURN", "الحرق", "حرق الرموز يعني إرسالها إلى عنوان لا يمكن استعادتها منه، فيقل العرض. الحرق وحده لا يضمن ارتفاع السعر، فالمهم هو الطلب الحقيقي."),
    L("LAYER TWO", "الطبقة الثانية Layer 2", "شبكات تعمل فوق بلوكتشين رئيسي مثل إيثريوم لتقليل الرسوم وزيادة السرعة، ثم تُثبّت نتائجها على الشبكة الرئيسية."),
    L("ORACLES", "الأوراكل", "العقد الذكي لا يرى العالم الخارجي بنفسه. الأوراكل خدمة تنقل إليه بيانات مثل الأسعار. دقة الأوراكل وأمانه مهمان لأن خطأه قد يؤثر في التطبيقات التي تعتمد عليه."),
    L("RSI INDICATOR", "مؤشر RSI", "مؤشر زخم بين 0 و100. فوق 70 يُقرأ عادةً كتشبع شرائي وتحت 30 كتشبع بيعي. في الاتجاهات القوية قد يبقى متطرفًا طويلًا، فلا تقرّر من RSI وحده."),
    L("MOVING AVERAGES", "المتوسطات المتحركة", "المتوسط المتحرك يسلّس حركة السعر ويبيّن الاتجاه. السعر فوق المتوسطات يُقرأ عادةً كاتجاه صاعد قصير، وتحتها كهابط. وقد يعمل المتوسط كدعم أو مقاومة ديناميكية."),
    L("BEAT FOMO", "التغلب على FOMO", "الشعور بفوات الفرصة يدفع لمطاردة السعر بعد صعود كبير. قبل أي دخول اسأل: أين وقف الخسارة؟ وما العائد مقابل المخاطرة؟ السوق لا يتوقف والفرص تتكرر لمن يملك خطة."),
    L("TRADING JOURNAL", "دفتر التداول", "دوّن سبب كل صفقة وسعر الدخول والخروج وشعورك. بعد أسابيع ستكتشف أخطاءك المتكررة بالأرقام لا بالانطباع."),
    L("DIVERSIFICATION", "التنويع", "توزيع رأس المال على أصول مختلفة يقلل أثر فشل مشروع واحد، لكنه لا يلغي مخاطر السوق كله، فمعظم العملات تهبط مع البيتكوين في الهبوط الحاد."),
]

ENDINGS = [
    "ما رأيك؟ شاركنا في التعليقات 👇", "هل كنت تعرف هذا؟ 👇", "ما العملة التي تراقبها هذا الأسبوع؟ 👇",
    "اكتب رأيك في تعليق 👇", "هل تتابع هذا المجال؟ 👇", "ما أكثر ما يهمك عند اختيار عملة للمتابعة؟ 👇",
    "شاركنا تجربتك 👇", "أي نقطة تودّ أن نشرحها بتفصيل أكبر؟ 👇", "هل تضيفها إلى قائمة مراقبتك؟ 👇",
]
_LAST_END = [None]


def ending():
    pool = [e for e in ENDINGS if e != _LAST_END[0]] or ENDINGS
    _LAST_END[0] = random.choice(pool)
    return _LAST_END[0]


def cat_of(sym):
    return CATS[COINS[sym]["cat"]]


def finish(body, coins, footer="⚠️ محتوى تعليمي للتوعية وليس نصيحة مالية. الأسعار والمعلومات قد تتغير."):
    tags = HASHTAGS
    return f"{body}\n\n{footer}\n{tags}"


def used_pick(state, kind, keys):
    used = state.setdefault("used", {}).setdefault(kind, [])
    fresh = [k for k in keys if k not in used]
    if not fresh:
        used.clear()
        fresh = list(keys)
    key = random.choice(fresh)
    used.append(key)
    del used[:-400]
    return key


# ---------------------------------------------------------------- post builders
# each returns dict(text, title, sub, accent, coin, kind) or None
def k_profile(state):
    sym = used_pick(state, "profile", list(COINS))
    c, cat = COINS[sym], cat_of(sym)
    peers = [s for s in COINS if COINS[s]["cat"] == c["cat"] and s != sym]
    random.shuffle(peers)
    peers = peers[:2]
    lines = [f"📘 ملف عملة | ${sym} — {c['ar']}", "", c["what"], ""]
    lines += [f"• {f}" for f in c["facts"]]
    lines += ["", f"🏷️ الفئة: {cat[0]}", f"💡 ما الذي يراقبه المتابعون عادةً؟ {cat[3]}"]
    if peers:
        lines += ["", "🔎 من الفئة نفسها: " + "  ".join(f"${p}" for p in peers)]
    lines += ["", ending()]
    return dict(text=finish("\n".join(lines), [sym] + peers), title=sym,
                sub=f"{c['en']} - {cat[1]} - SINCE {c['year']}", accent=cat[2], coin=sym, kind="profile",
                wiki=c["en"])


def k_compare(state):
    syms = list(COINS)
    for _ in range(30):
        a, b = random.sample(syms, 2)
        key = "-".join(sorted((a, b)))
        if key not in state.setdefault("used", {}).setdefault("compare", []):
            break
    state["used"]["compare"].append(key)
    del state["used"]["compare"][:-400]
    ca, cb = COINS[a], COINS[b]
    body = (f"⚖️ مقارنة سريعة: ${a} مقابل ${b}\n\n"
            f"🔹 ${a} ({ca['ar']})\n{ca['what']}\nسنة الإطلاق: {ca['year']} | الفئة: {cat_of(a)[0]}\n\n"
            f"🔸 ${b} ({cb['ar']})\n{cb['what']}\nسنة الإطلاق: {cb['year']} | الفئة: {cat_of(b)[0]}\n\n"
            "لا توجد عملة «أفضل» بإطلاق؛ لكل مشروع فكرة ومخاطر مختلفة.\n\n"
            f"أيهما أقرب لاهتمامك: ${a} أم ${b}؟ 👇")
    return dict(text=finish(body, [a, b]), title=f"{a} VS {b}", sub=f"{ca['en']} AND {cb['en']}",
                accent=cat_of(a)[2], coin=a, kind="compare")


def k_poll(state):
    cats = [c for c in CATS if len([s for s in COINS if COINS[s]["cat"] == c]) >= 3]
    cat = used_pick(state, "poll", cats)
    pool = [s for s in COINS if COINS[s]["cat"] == cat]
    pick = random.sample(pool, 3)
    nums = ["1️⃣", "2️⃣", "3️⃣"]
    lines = [f"🗳️ استطلاع: {CATS[cat][0]}", "",
             "لو قررت متابعة عملة واحدة من هذه الفئة هذا الأسبوع، فأيها تختار؟", ""]
    lines += [f"{n} ${s} — {COINS[s]['what']}" for n, s in zip(nums, pick)]
    lines += ["", "اكتب رقم اختيارك مع السبب في تعليق 👇"]
    return dict(text=finish("\n".join(lines), pick), title="YOUR PICK",
                sub=" - ".join(pick), accent=CATS[cat][2], coin=pick[0], kind="poll")


def k_dyk(state):
    sym = used_pick(state, "dyk", list(COINS))
    c = COINS[sym]
    fact = random.choice(c["facts"])
    body = (f"🤔 هل تعلم؟\n\n{fact}\n\nالعملة: ${sym} — {c['ar']}\n"
            f"الفئة: {cat_of(sym)[0]}\n\n{ending()}")
    return dict(text=finish(body, [sym]), title="DID YOU KNOW", sub=f"{sym} - {c['en']}",
                accent=cat_of(sym)[2], coin=sym, kind="dyk", wiki=c["en"])


def k_category(state):
    cats = [c for c in CATS if len([s for s in COINS if COINS[s]["cat"] == c]) >= 3]
    cat = used_pick(state, "category", cats)
    pool = [s for s in COINS if COINS[s]["cat"] == cat]
    random.shuffle(pool)
    pool = pool[:6]
    lines = [f"🗂️ أشهر عملات: {CATS[cat][0]}", ""]
    lines += [f"• ${s} — {COINS[s]['ar']}: {COINS[s]['what']}" for s in pool]
    lines += ["", f"💡 {CATS[cat][3]}", "", "أي منها تتابع؟ وأي عملة تضيفها للقائمة؟ 👇"]
    return dict(text=finish("\n".join(lines), pool), title=CATS[cat][1] + " COINS",
                sub=f"{len(pool)} NAMES TO KNOW", accent=CATS[cat][2], coin=pool[0], kind="category")


def k_lesson(state):
    i = used_pick(state, "lesson", list(range(len(LESSONS))))
    les = LESSONS[i]
    tags = random.sample(MAJORS, min(2, len(MAJORS)))
    body = (f"🎓 درس اليوم: {les['ar']}\n\n{les['body']}\n\n"
            "🔎 طبّق الفكرة على سوق: " + "  ".join(f"${t}" for t in tags) + f"\n\n{ending()}")
    return dict(text=finish(body, tags, "🎓 محتوى تعليمي عام وليس نصيحة مالية."), title=les["en"],
                sub="BEGINNER LESSON", accent=(14, 203, 129), coin=tags[0], kind="lesson")


def k_live(state):
    """Live movers among our coins (Binance public market-data host). Returns None if unreachable."""
    try:
        req = urllib.request.Request("https://data-api.binance.vision/api/v3/ticker/24hr",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            rows = json.load(response)
        want = {s + "USDT": s for s in COINS}
        got = []
        for row in rows:
            sym = want.get(row.get("symbol"))
            if sym and float(row["quoteVolume"]) >= 3e6:
                got.append((sym, float(row["priceChangePercent"]), float(row["lastPrice"])))
    except Exception as exc:
        note("live snapshot unavailable:", exc)
        return None
    if len(got) < 8:
        return None
    got.sort(key=lambda x: x[1], reverse=True)
    up, down = got[:3], got[-3:][::-1]

    def fmt(s, chg, price):
        text = f"{price:,.2f}" if price >= 1 else f"{price:.6f}".rstrip("0")
        return f"• ${s}: {chg:+.2f}% (السعر المرجعي {text}$)"
    body = ("📊 لقطة من قائمة المتابعة (آخر 24 ساعة)\n\n🚀 الأعلى صعودًا:\n"
            + "\n".join(fmt(*x) for x in up) + "\n\n🩸 الأكثر تراجعًا:\n" + "\n".join(fmt(*x) for x in down)
            + "\n\nالتحرك السريع ليس دليلًا على استمراره؛ تحقق من السيولة والأخبار قبل أي قرار.\n\n"
            "ℹ️ المصدر: بيانات Binance للسوق الفوري.\n\nأي منها تراقب؟ 👇")
    syms = [x[0] for x in up + down]
    return dict(text=finish(body, syms), title="MARKET SNAPSHOT", sub="TOP MOVERS - 24H",
                accent=(240, 185, 11), coin=up[0][0], kind="live")


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def wiki_image(name):
    """Lead picture (usually the logo) of the Wikipedia article about this coin; None when unsure."""
    query = urllib.parse.urlencode({
        "action": "query", "generator": "search", "gsrsearch": f"{name} cryptocurrency", "gsrlimit": 6,
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 900, "format": "json"})
    try:
        pages = (get_json("https://en.wikipedia.org/w/api.php?" + query, 15).get("query") or {}).get("pages") or {}
    except Exception as exc:
        note("wikipedia failed:", exc)
        return None
    word = name.split()[0].lower()
    for page in sorted(pages.values(), key=lambda p: p.get("index", 99)):
        source = (page.get("thumbnail") or {}).get("source")
        if source and word in (page.get("title") or "").lower():     # the article must really be about this coin
            return source
    return None


NEWS_FEEDS = [("Cointelegraph", "https://cointelegraph.com/rss"), ("Decrypt", "https://decrypt.co/feed"),
              ("CryptoSlate", "https://cryptoslate.com/feed/"), ("The Block", "https://www.theblock.co/rss.xml"),
              ("BeInCrypto", "https://beincrypto.com/feed/"),
              ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/")]


def fetch_news(now_ts):
    items = []
    for name, url in random.sample(NEWS_FEEDS, 3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as response:
                root = ET.fromstring(response.read())
        except Exception as exc:
            note("feed failed", name, exc)
            continue
        for it in root.iter("item"):
            title = strip_html(it.findtext("title"))
            link = (it.findtext("link") or "").strip()
            try:
                ts = email.utils.parsedate_to_datetime(it.findtext("pubDate")).timestamp()
            except Exception:
                continue
            if title and 0 <= now_ts - ts <= 12 * 3600:
                items.append(dict(id=hashlib.md5((link or title).encode()).hexdigest()[:16], title=title,
                                  link=link, desc=strip_html(it.findtext("description"))[:400],
                                  source=name, image=rss_image(it)))
    return items


def k_news(state):
    """News with the article's OWN photo. Skips stories without a photo or without an Arabic translation."""
    now = time.time()
    state["news_log"] = [t for t in state.get("news_log", []) if t > now - 86400]
    if len(state["news_log"]) >= NEWS_PER_DAY:
        return None
    seen = state.setdefault("news_seen", {})
    for item in sorted(fetch_news(now), key=lambda i: bool(i["image"]), reverse=True):
        if item["id"] in seen:
            continue
        image = item["image"] or page_image(item["link"])
        if not image:
            continue
        try:
            title_ar = translate_ar(item["title"])
            desc_ar = translate_ar(item["desc"][:300]) if item["desc"] else ""
        except Exception as exc:
            note("translation failed:", exc)
            continue
        seen[item["id"]] = now
        for key in [k for k, v in seen.items() if v < now - 3 * 86400]:
            del seen[key]
        found = [s for s, c in COINS.items()                      # symbol (exact case) or the full coin name
                 if re.search(r"\b" + re.escape(s) + r"\b", item["title"])
                 or re.search(r"\b" + re.escape(c["en"]) + r"\b", item["title"], re.I)][:3]
        coins = found + [m for m in MAJORS if m not in found][:max(0, 2 - len(found))]
        lines = [f"📰 خبر | {title_ar}"]
        if desc_ar and desc_ar != title_ar:
            lines += ["", desc_ar[:350]]
        lines += ["", "🔎 عملات ذات صلة: " + "  ".join(f"${c}" for c in coins), "",
                  f"📰 المصدر: {item['source']}", f"🔗 {item['link']}", "", "ما رأيك في الخبر؟ 👇"]
        state["news_log"].append(now)
        return dict(text=finish("\n".join(lines), coins, "⚠️ خبر للمعلومات فقط وليس نصيحة مالية."),
                    title="CRYPTO NEWS", sub=item["source"], accent=(246, 70, 93), coin=coins[0],
                    kind="news", image_url=image)
    return None


KINDS = {"profile": k_profile, "compare": k_compare, "lesson": k_lesson, "poll": k_poll,
         "dyk": k_dyk, "category": k_category, "live": k_live}
if NEWS:
    KINDS["news"] = k_news
ROTATION = ["profile", "compare", "lesson", "profile", "live", "poll", "lesson", "dyk", "profile", "category",
            "compare", "lesson", "profile", "poll", "live", "dyk", "lesson", "profile", "compare", "lesson"]


def build_post(state, seq):
    first = "news" if (NEWS and seq % 5 == 2) else ROTATION[seq % len(ROTATION)]
    for kind in [first] + [k for k in KINDS if k != first]:
        try:
            post = KINDS[kind](state)
        except Exception as exc:
            note(f"builder {kind} failed:", exc)
            post = None
        if post:
            return post
    return None


# ---------------------------------------------------------------- state / publishing / loop
def online(timeout=4):
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53), ("www.binance.com", 443)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


_LOCK_FH = None


def acquire_lock():
    global _LOCK_FH
    try:
        import fcntl
    except ImportError:
        return True
    try:
        _LOCK_FH = open(LOCK_FILE, "w")
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def load_state():
    state = {}
    for path in (STATE_FILE, STATE_FILE + ".bak"):
        try:
            with open(path, encoding="utf-8") as handle:
                state = json.load(handle)
            if isinstance(state, dict):
                break
        except Exception:
            state = {}
    for key, default in (("last_post_ts", 0), ("last_attempt_ts", 0), ("post_log", []), ("sent_hashes", {}),
                         ("used", {}), ("seq", 0), ("post_history", [])):
        state.setdefault(key, default)
    return state


def save_state(state):
    now = time.time()
    state["post_log"] = [t for t in state.get("post_log", []) if t > now - 86400]
    state["sent_hashes"] = {k: v for k, v in state.get("sent_hashes", {}).items() if v > now - 86400}
    del state["post_history"][:-400]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)
    try:
        import shutil
        shutil.copyfile(STATE_FILE, STATE_FILE + ".bak")
    except OSError:
        pass


def publish(post, image, state):
    """True = published, "maybe" = unknown outcome (never retried), "dup" = same text in 24h, False = failed."""
    digest = text_hash(post["text"])
    if time.time() - state["sent_hashes"].get(digest, 0) < 86400:
        note("same text already sent in the last 24h; skipping")
        return "dup"
    key = clean_env("SQUARE_API_KEY")
    if not key:
        note("❌ SQUARE_API_KEY is missing (add it to GitHub Secrets)")
        return False
    if image and os.path.exists(image):
        ok = post_square_image(post["text"], image, key)
    elif REQUIRE_IMAGE:
        note("no image -> not posting text-only")
        return False
    else:
        ok = post_square(post["text"], key)
    if ok is None:
        note("publish outcome unknown (timeout); counted as posted to avoid duplicates")
        ok = "maybe"
    if ok:
        state["sent_hashes"][digest] = time.time()
    return ok


def run_once(dry=False):
    now = time.time()
    state = load_state()
    gap = now - state["last_post_ts"]
    day = len([t for t in state["post_log"] if t > now - 86400])
    due = dry or (gap >= TARGET_GAP and day < POSTS_PER_DAY and now - state["last_attempt_ts"] >= RETRY_SECONDS)
    if not due:
        print(f"Nothing due: {day}/{POSTS_PER_DAY} in 24h; next in ~{max(0, int(TARGET_GAP - gap))}s")
        return 0
    state["last_attempt_ts"] = now
    seq = state["seq"]
    if not dry:
        state["seq"] = seq + 1
        save_state(state)
    post = build_post(state, seq)
    if not post:
        note("no post could be built")
        return 1
    print(post["text"], "\n")
    image = None
    if PHOTOS:                                   # 1) a real picture: the article's photo, or the coin's logo
        try:
            url = post.get("image_url") or (wiki_image(post["wiki"]) if post.get("wiki") else None)
            image = download_image(url, "preview_photo" if dry else "post_photo") if url else None
            if image:
                note("📷 real picture:", url[:90])
        except Exception as exc:
            note("photo failed:", exc)
    if not image:                                # 2) generated card (never fails, no libraries)
        try:
            image = make_card("preview.png" if dry else "post_img.png", post["title"], post["sub"],
                              post["accent"], seed=seq)
        except Exception as exc:
            note("picture failed:", exc)
    if dry:
        print("DRY RUN: nothing published. Picture:", image)
        return 0
    ok = publish(post, image, state)
    if image and os.path.exists(image):
        os.remove(image)
    if ok and ok != "dup":
        t = time.time()
        state["last_post_ts"] = t
        state["post_log"].append(t)
        state["post_history"].append(dict(ts=int(t), kind=post["kind"], coin=post["coin"]))
        note(f"✅ post #{day + 1} in 24h ({post['kind']} {post['coin']})")
    save_state(state)
    return 0 if ok else 1


def health_exit():
    """End of a timed run: RED when nothing was posted for 4 hours, so a green tick means 'posting'."""
    state = load_state()
    now = time.time()
    recent = [t for t in state["post_log"] if t > now - 4 * 3600]
    note(f"📊 run summary: {len(recent)} posts in the last 4h | "
         f"{len([t for t in state['post_log'] if t > now - 86400])} in 24h")
    if recent or not clean_env("SQUARE_API_KEY"):
        return 0
    note("❌ nothing posted for 4 hours - look above for: SQUARE FAIL / SQUARE IMAGE FAIL / key missing")
    return 1


def loop_forever(wait, deadline=None):
    errors = 0
    offline_since = None
    while True:
        if deadline and time.time() > deadline:
            return health_exit()
        if not online():
            if offline_since is None:
                offline_since = time.time()
                note("📴 offline - waiting (nothing is lost; posting resumes by itself)")
            time.sleep(20)
            continue
        if offline_since is not None:
            note(f"🌐 back online after {int(time.time() - offline_since)}s")
            offline_since = None
        try:
            run_once()
            errors = 0
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            errors += 1
            note(f"RUN ERROR #{errors}:", exc)
        pause = wait if errors == 0 else min(300, wait * (2 ** min(errors, 4)))
        if deadline and time.time() + pause > deadline:
            return health_exit()
        time.sleep(pause)


def report():
    from collections import Counter
    hist = load_state().get("post_history", [])
    if not hist:
        print("no history yet")
        return 0
    print(f"last {min(40, len(hist))} posts (UTC) - match them with the Write to Earn page:\n")
    for h in hist[-40:]:
        when = datetime.fromtimestamp(h["ts"], timezone.utc).strftime("%m-%d %H:%M")
        print(f'{when} | {str(h.get("kind", "?")):9} | {h.get("coin") or "-"}')
    print("\ncount by kind:")
    for kind, count in Counter(h.get("kind", "?") for h in hist).most_common():
        print(f"  {kind}: {count}")
    return 0


def doctor():
    ok_all = True

    def line(ok, name, detail=""):
        nonlocal ok_all
        ok_all = ok_all and ok
        print("✅" if ok else "❌", name, detail)
    line(online(), "internet")
    line(bool(clean_env("SQUARE_API_KEY")), "SQUARE_API_KEY")
    print(f"ℹ️ {POSTS_PER_DAY} posts/day -> one every ~{TARGET_GAP // 60} min | coins: {len(COINS)} | lessons: {len(LESSONS)}")
    try:
        path = make_card("doctor.png", "DOCTOR TEST", "picture works", (240, 185, 11))
        line(os.path.getsize(path) > 3000, "picture generator", f"{os.path.getsize(path)} bytes")
        os.remove(path)
    except Exception as exc:
        line(False, "picture generator", str(exc)[:100])
    url = wiki_image("Bitcoin")
    line(bool(url) or not PHOTOS, "real photos (Wikipedia)", "OK" if url else "unreachable -> generated cards are used")
    state = load_state()
    for kind in KINDS:
        try:
            post = KINDS[kind](state)
            line(bool(post) or kind == "live", f"builder {kind}", "" if post else "(skipped: no live data)")
        except Exception as exc:
            line(False, f"builder {kind}", str(exc)[:100])
    return 0 if ok_all else 1


def main():
    if "--doctor" in sys.argv:
        return doctor()
    if "--report" in sys.argv:
        return report()
    if "--dry" in sys.argv:
        return run_once(dry=True)
    if not acquire_lock():
        print("another copy is already running here; exiting")
        return 0
    if any(f in sys.argv for f in ("--loop", "--forever", "--duration")):
        wait = max(20, int(os.getenv("LOOP_SECONDS", "45")))
        deadline = None
        if "--duration" in sys.argv:
            try:
                deadline = time.time() + float(sys.argv[sys.argv.index("--duration") + 1]) * 60
            except (ValueError, IndexError):
                raise SystemExit("usage: --duration MINUTES")
        note("🔧 CLIENT BOT start | key:", "yes" if clean_env("SQUARE_API_KEY") else "NO!",
             f"| posts/day {POSTS_PER_DAY} | coins {len(COINS)} | brand {BRAND} | photos {int(PHOTOS)} | news {int(NEWS)}")
        return loop_forever(wait, deadline)
    return run_once()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(130)
    except Exception as exc:
        note("FATAL:", exc)
        raise SystemExit(1)
