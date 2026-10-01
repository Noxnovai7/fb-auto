import os, json, glob, subprocess, random, time, asyncio, urllib.parse
import requests, edge_tts
from PIL import Image, ImageDraw, ImageFont, ImageOps
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

W, H, FPS = 1080, 1920, 30
USED = "used_topics.txt"
WORK = os.path.abspath("work")
MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
          "gemini-3.5-flash-lite", "gemini-3.5-flash"]
VOICES = ["en-US-AndrewNeural", "en-US-GuyNeural",
          "en-US-ChristopherNeural", "en-US-AriaNeural"]
VOICE = VOICES[int(time.time() // 86400) % len(VOICES)]
ORANGE = (255, 140, 30)
USED_LINKS = set()
os.makedirs(WORK, exist_ok=True)


def font_path():
    for pat in ["/usr/share/fonts/**/DejaVuSans-Bold.ttf", "/usr/share/fonts/**/*Bold*.ttf"]:
        f = glob.glob(pat, recursive=True)
        if f:
            return f[0]
    raise RuntimeError("No font found")


FONT = font_path()


def run(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        print("COMMAND FAILED:", " ".join(cmd[:8]), "...")
        print(r.stderr[-1500:])
        raise RuntimeError("command failed")
    return r


def probe_dur(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path])
    return float(r.stdout.strip())


def used_topics():
    if os.path.exists(USED):
        return open(USED, encoding="utf-8").read().splitlines()
    return []


# ---------------------------------------------------------------- Gemini
def gemini(prompt):
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"].strip()}
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}}
    last = None
    for rnd in range(3):
        for model in MODELS:
            url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   + model + ":generateContent")
            try:
                r = requests.post(url, headers=headers, json=body, timeout=120)
                if not r.ok:
                    print("Gemini error:", model, r.status_code, r.text[:200])
                    last = f"{model} {r.status_code}"
                    time.sleep(5)
                    continue
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            except Exception as e:
                last = f"{model} {e}"
                print("Gemini exception:", last)
                time.sleep(5)
        time.sleep(30)
    raise RuntimeError(f"Gemini failed: {last}")


def gen_script():
    used = used_topics()[-80:]
    prompt = f"""Write a YouTube Short script (English) about ONE mind-blowing, TRUE science fact (space, animals, or the human body).
Do NOT repeat these earlier topics: {used}
Rules:
- 9 to 11 scenes. Total narration 60 to 75 words.
- Scene 1 is a shocking question hook. Last scene invites viewers to follow for more facts.
- Each scene text is 5 to 10 words, one idea, simple spoken English.
- Be scientifically precise. No unqualified superlatives (biggest, fastest, oldest) unless exactly true; name the category. Real numbers only.
- For each scene give "keywords": 2 to 3 concrete visual words good for a stock-video search (example: "humpback whale ocean"), and "image": a detailed visual prompt for an AI image generator (no text in the image).
Return ONLY JSON:
{{"topic":"short topic name","title":"catchy accurate title under 60 characters","description":"two short lines then hashtags","tags":["tag1","tag2"],"scenes":[{{"text":"narration","keywords":"visual words","image":"image prompt"}}]}}"""
    data = json.loads(gemini(prompt))
    assert len(data["scenes"]) >= 5
    return data


def verify(meta):
    facts = " ".join(s["text"] for s in meta["scenes"])
    prompt = ("You are a strict science fact-checker. Check this short video narration for any false or "
              "misleading claim, wrong number, or unqualified superlative.\n"
              f'Narration: """{facts}"""\n'
              'Return ONLY JSON: {"ok": true or false, "issue": "short reason"}')
    try:
        d = json.loads(gemini(prompt))
        return bool(d.get("ok")), d.get("issue", "")
    except Exception as e:
        print("Fact-check unavailable:", e)
        return True, "checker unavailable"


# ---------------------------------------------------------------- Voice
async def _edge(text, path, voice):
    try:
        comm = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    except TypeError:
        comm = edge_tts.Communicate(text, voice)
    words = []
    with open(path, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["offset"] / 1e7, ch["duration"] / 1e7, ch["text"]))
    return words


def make_voice(text, path):
    try:
        words = asyncio.run(_edge(text, path, VOICE))
        if os.path.getsize(path) < 1000:
            raise RuntimeError("empty audio")
        return words
    except Exception as e:
        print("edge-tts failed, using gTTS:", e)
        from gtts import gTTS
        gTTS(text, lang="en").save(path)
        return []


def caption_timing(text, events, dur):
    toks = text.split()
    if events and abs(len(events) - len(toks)) <= 2:
        items = [(s, t) for s, d, t in events]
    else:
        total = sum(len(t) + 2 for t in toks)
        usable = max(dur - 0.3, 0.5)
        items, acc = [], 0.0
        for t in toks:
            items.append((acc / total * usable, t))
            acc += len(t) + 2
    out = []
    for i, (s, t) in enumerate(items):
        e = items[i + 1][0] if i + 1 < len(items) else dur
        out.append((s, e, t))
    return out


# ---------------------------------------------------------------- Visuals
def get_image(prompt, path):
    url = ("https://image.pollinations.ai/prompt/"
           + urllib.parse.quote(prompt + ", vertical composition, cinematic, realistic, no text, no watermark")
           + f"?width={W}&height={H}&nologo=true&seed={random.randint(1, 99999)}")
    for _ in range(3):
        try:
            r = requests.get(url, timeout=120)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                tmp = os.path.join(WORK, "tmp_dl.img")
                open(tmp, "wb").write(r.content)
                im = Image.open(tmp).convert("RGB")
                w, h = im.size
                im = im.crop((0, 0, w, int(h * 0.92)))
                ImageOps.fit(im, (W, H)).save(path, quality=92)
                return True
        except Exception:
            pass
        time.sleep(6)
    Image.new("RGB", (W, H), (14, 22, 48)).save(path)
    return False


def pexels_videos(query, n):
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key or not query:
        return []
    try:
        r = requests.get("https://api.pexels.com/videos/search",
                         headers={"Authorization": key},
                         params={"query": query, "orientation": "portrait", "per_page": 10},
                         timeout=30)
        if not r.ok:
            print("Pexels error:", r.status_code, r.text[:150])
            return []
        links = []
        for v in r.json().get("videos", []):
            files = [f for f in v.get("video_files", [])
                     if f.get("file_type") == "video/mp4" and f.get("width")]
            if not files:
                continue
            ok = [f for f in files if f["width"] <= 1080]
            f = max(ok, key=lambda x: x["width"]) if ok else min(files, key=lambda x: x["width"])
            links.append(f["link"])
        random.shuffle(links)
        return links[:n]
    except Exception as e:
        print("Pexels exception:", e)
        return []


def pixabay_videos(query, n):
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key or not query:
        return []
    queries = [query]
    if len(query.split()) > 2:
        queries.append(" ".join(query.split()[:2]))
    for q in queries:
        try:
            r = requests.get("https://pixabay.com/api/videos/",
                             params={"key": key, "q": q[:90], "per_page": 15,
                                     "safesearch": "true"},
                             timeout=30)
            if not r.ok:
                print("Pixabay error:", r.status_code, r.text[:150])
                continue
            links = []
            for h in r.json().get("hits", []):
                vs = h.get("videos", {})
                for size in ("large", "medium", "small"):
                    u = (vs.get(size) or {}).get("url")
                    if u:
                        links.append(u)
                        break
            if links:
                random.shuffle(links)
                return links[:n]
        except Exception as e:
            print("Pixabay exception:", e)
    return []


def stock_videos(query, n):
    links = pixabay_videos(query, n)
    if len(links) < n:
        links += pexels_videos(query, n - len(links))
    return links


def download(link, path):
    try:
        with requests.get(link, stream=True, timeout=120,
                          headers={"User-Agent": "Mozilla/5.0"}) as r:
            if not r.ok:
                return False
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        return os.path.getsize(path) > 10000
    except Exception as e:
        print("Download failed:", e)
        return False


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
       "-pix_fmt", "yuv420p", "-r", str(FPS), "-an"]


def cut_from_video(src, dur, out):
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
          f"crop={W}:{H},fps={FPS},format=yuv420p")
    run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-t", f"{dur:.3f}",
         "-vf", vf] + ENC + [out])


def cut_from_image(img, dur, out, idx):
    frames = int(dur * FPS) + 1
    if idx % 2 == 0:
        z = f"min(1+0.22*on/{frames},1.22)"
    else:
        z = f"max(1.22-0.22*on/{frames},1.0)"
    vf = (f"scale=1620:2880,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
    run(["ffmpeg", "-y", "-i", img, "-vf", vf, "-t", f"{dur:.3f}"] + ENC + [out])


def concat(files, out):
    lst = out + ".txt"
    open(lst, "w").write("".join(f"file '{f}'\n" for f in files))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])


def build_scene(i, sc, dur):
    n = max(1, round(dur / 2.6))
    seg = dur / n
    links = stock_videos(sc.get("keywords") or sc["text"], n + 3)
    cuts = []
    for c in range(n):
        out = os.path.join(WORK, f"sc{i}_{c}.mp4")
        done = False
        while links and not done:
            link = links.pop(0)
            if link in USED_LINKS:
                continue
            src = os.path.join(WORK, f"sc{i}_{c}_src.mp4")
            if download(link, src):
                try:
                    cut_from_video(src, seg, out)
                    USED_LINKS.add(link)
                    done = True
                except Exception as e:
                    print("Video cut failed:", e)
        if not done:
            img = os.path.join(WORK, f"sc{i}_{c}.jpg")
            get_image(sc.get("image") or sc["text"], img)
            cut_from_image(img, seg, out, i + c)
        cuts.append(out)
    scene = os.path.join(WORK, f"scene{i}.mp4")
    concat(cuts, scene)
    return scene


# ---------------------------------------------------------------- Logo watermark
def make_watermark(path):
    S = 4
    im = Image.new("RGBA", (520 * S, 150 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([10 * S, 20 * S, 120 * S, 130 * S], fill=ORANGE + (255,))
    d.ellipse([78 * S, 8 * S, 138 * S, 68 * S], fill=(0, 0, 0, 0))
    d.ellipse([40 * S, 52 * S, 54 * S, 66 * S], fill=(255, 255, 255, 255))
    d.ellipse([60 * S, 84 * S, 70 * S, 94 * S], fill=(255, 255, 255, 255))
    f = ImageFont.truetype(FONT, 50 * S)
    d.text((150 * S, 18 * S), "FACT", font=f, fill=(255, 255, 255, 255),
           stroke_width=3 * S, stroke_fill=(0, 0, 0, 255))
    d.text((150 * S, 76 * S), "BITES", font=f, fill=ORANGE + (255,),
           stroke_width=3 * S, stroke_fill=(0, 0, 0, 255))
    im.resize((520, 150), Image.LANCZOS).save(path)


# ---------------------------------------------------------------- Final render
def build_video(scenes):
    scene_files, wavs, caps = [], [], []
    t0 = 0.0
    for i, sc in enumerate(scenes):
        mp3 = os.path.join(WORK, f"v{i}.mp3")
        wav = os.path.join(WORK, f"v{i}.wav")
        events = make_voice(sc["text"], mp3)
        run(["ffmpeg", "-y", "-i", mp3, "-ar", "44100", "-ac", "2",
             "-af", "apad=pad_dur=0.25", wav])
        dur = probe_dur(wav)
        for s, e, t in caption_timing(sc["text"], events, dur):
            caps.append((t0 + s, t0 + e, t))
        scene_files.append(build_scene(i, sc, dur))
        wavs.append(wav)
        t0 += dur
    silent = os.path.join(WORK, "silent.mp4")
    narr = os.path.join(WORK, "narr.wav")
    concat(scene_files, silent)
    concat(wavs, narr)
    total = probe_dur(narr)

    wm = os.path.join(WORK, "wm.png")
    make_watermark(wm)

    draws = []
    for k, (a, b, t) in enumerate(caps):
        word = "".join(ch for ch in t if ch not in "\"`").upper().strip()
        if not word:
            continue
        open(os.path.join(WORK, f"w{k}.txt"), "w", encoding="utf-8").write(word)
        size = 104 if len(word) <= 9 else (86 if len(word) <= 13 else 64)
        color = "yellow" if k % 2 else "white"
        draws.append(
            f"drawtext=fontfile={FONT}:textfile=w{k}.txt:expansion=none:fontsize={size}"
            f":fontcolor={color}:borderw=8:bordercolor=black"
            f":x=(w-text_w)/2:y=h*0.64:enable='between(t,{a:.3f},{b:.3f})'")
    music = "music.mp3"
    has_music = os.path.exists(music)
    graph = ("[0:v]" + ",".join(draws) + "[v0];"
             "[1:v]scale=230:-1,format=rgba,colorchannelmixer=aa=0.92[lg];"
             "[v0][lg]overlay=36:140[v]")
    cmd = ["ffmpeg", "-y", "-i", silent, "-i", wm, "-i", narr]
    if has_music:
        cmd += ["-stream_loop", "-1", "-i", os.path.abspath(music)]
        graph += (";[3:a]volume=0.10[m];[2:a][m]amix=inputs=2:duration=first:"
                  "dropout_transition=0,volume=2[a]")
        amap = "[a]"
    else:
        amap = "2:a"
    open(os.path.join(WORK, "graph.txt"), "w").write(graph)
    final = os.path.join(WORK, "final.mp4")
    cmd += ["-filter_complex_script", "graph.txt", "-map", "[v]", "-map", amap,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}",
            "-movflags", "+faststart", final]
    run(cmd, cwd=WORK)
    print(f"Video ready: {total:.1f}s, {len(caps)} caption words")
    return final


# ---------------------------------------------------------------- Upload
def upload(path, meta):
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"].strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"].strip(),
        client_secret=os.environ["YT_CLIENT_SECRET"].strip(),
    )
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": meta["title"][:88] + " #Shorts",
            "description": meta["description"] + "\n\n#Shorts #Facts #Science",
            "tags": meta.get("tags", [])[:15],
            "categoryId": "28",
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }
    media = MediaFileUpload(path, mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("Uploaded: https://youtube.com/shorts/" + resp["id"])


if __name__ == "__main__":
    meta = None
    for attempt in range(3):
        m = gen_script()
        ok, why = verify(m)
        print(f"Topic: {m['topic']} | fact-check ok={ok} {why}")
        if ok:
            meta = m
            break
    if meta is None:
        raise SystemExit("Fact-check failed 3 times, skipping today")
    video = build_video(meta["scenes"])
    upload(video, meta)
    with open(USED, "a", encoding="utf-8") as f:
        f.write(meta["topic"].replace("\n", " ") + "\n")
