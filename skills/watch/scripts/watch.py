#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["yt-dlp", "imageio-ffmpeg", "faster-whisper", "sherpa-onnx", "numpy"]
# ///
"""watch: turn a video (URL or local file) into frames + contact sheets + transcript for Claude.

Usage: uv run watch.py <url|file> [--out DIR] [--max-frames 36] [--model small] [--lang uk] [--no-transcript]
       uv run watch.py <call recording> --call [--speakers 2] [--no-diarize]
"""
import argparse, json, os, re, shutil, subprocess, sys, time, urllib.request
from pathlib import Path

import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
RUNS = Path.home() / ".claude" / "watch-runs"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36"


def log(msg):
    print(f"[watch] {msg}", file=sys.stderr, flush=True)


def slugify(s):
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-")[-60:] or "video"


# ---------- download ----------

def via_ytdlp(url, out):
    import yt_dlp
    opts = {
        "outtmpl": str(out / "video.%(ext)s"),
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "ffmpeg_location": FF,
        "quiet": True, "no_warnings": True, "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = sorted(out.glob("video.*"))
    if not files:
        raise RuntimeError("yt-dlp produced no file")
    meta = {k: info.get(k) for k in ("title", "description", "uploader", "channel", "upload_date",
                                     "view_count", "like_count", "comment_count", "repost_count", "duration", "webpage_url")}
    return files[0], meta


def apify_token():
    if os.environ.get("APIFY_TOKEN"):
        return os.environ["APIFY_TOKEN"]
    p = Path.home() / ".config" / "watch" / "apify_token"
    if p.exists():
        return p.read_text().strip()
    return None


def http(url, token=None, data=None, timeout=300):
    headers = {"User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def via_apify(url, out):
    token = apify_token()
    if not token:
        raise RuntimeError("no Apify token (set APIFY_TOKEN or ~/.config/watch/apify_token)")
    host = url.lower()
    if "tiktok" in host:
        actor, inp = "clockworks~tiktok-scraper", {"postURLs": [url], "shouldDownloadVideos": True, "resultsPerPage": 1}
    elif "instagram" in host:
        actor, inp = "apify~instagram-scraper", {"directUrls": [url], "resultsType": "posts", "resultsLimit": 1}
    else:
        raise RuntimeError("Apify fallback only covers TikTok and Instagram")
    log(f"yt-dlp failed, trying Apify {actor} (~$0.01-0.02)")
    items = json.loads(http(f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?timeout=240", token, inp))
    if not items:
        raise RuntimeError("Apify returned no items")
    it = items[0]
    vurl = (it.get("mediaUrls") or [None])[0] or it.get("videoUrl")
    if not vurl:
        raise RuntimeError(f"no video URL in Apify item (keys: {list(it)[:15]})")
    blob = http(vurl, token if "api.apify.com" in vurl else None)
    f = out / "video.mp4"
    f.write_bytes(blob)
    a = it.get("authorMeta") or {}
    meta = {
        "title": it.get("text") or it.get("caption"),
        "uploader": a.get("name") or it.get("ownerUsername"),
        "followers": a.get("fans"),
        "upload_date": it.get("createTimeISO") or it.get("timestamp"),
        "view_count": it.get("playCount") or it.get("videoPlayCount") or it.get("videoViewCount"),
        "like_count": it.get("diggCount") or it.get("likesCount"),
        "comment_count": it.get("commentCount") or it.get("commentsCount"),
        "save_count": it.get("collectCount"),
        "share_count": it.get("shareCount"),
        "webpage_url": it.get("webVideoUrl") or it.get("url") or url,
    }
    return f, meta


def fetch(src, out):
    p = Path(src).expanduser()
    if p.exists():
        dst = out / ("video" + p.suffix)
        dst.symlink_to(p.resolve())
        return dst, {"title": p.name, "source": "local"}
    try:
        return via_ytdlp(src, out)
    except Exception as e:
        log(f"yt-dlp: {str(e).splitlines()[0][:200]}")
        for f in out.glob("video.*"):
            f.unlink()
        return via_apify(src, out)


# ---------- frames ----------

def ff(*args):
    subprocess.run([FF, "-loglevel", "error", "-y", *args], check=True)


def probe(media):
    """-> (duration_s, has_real_video). Cover art in audio files is not video."""
    r = subprocess.run([FF, "-hide_banner", "-i", str(media)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    has_video = any("Video:" in l and "attached pic" not in l for l in r.stderr.splitlines())
    return dur, has_video


def frames(video, out, dur, max_frames):
    step = max(1.0, dur / max_frames)
    fdir = out / "frames"
    fdir.mkdir(exist_ok=True)
    ff("-i", str(video), "-vf", f"fps=1/{step:.3f},scale=720:-2", "-q:v", "3", str(fdir / "f_%03d.jpg"))
    files = sorted(fdir.glob("f_*.jpg"))
    stamped = []
    for i, f in enumerate(files):
        t = i * step
        new = fdir / f"f_{i:03d}_{int(t // 60):02d}m{int(t % 60):02d}s.jpg"
        f.rename(new)
        stamped.append({"file": str(new), "t": round(t, 1)})
    # contact sheets: 3x2 grid, 6 frames per sheet, same sampling
    sdir = out / "sheets"
    sdir.mkdir(exist_ok=True)
    ff("-i", str(video), "-vf", f"fps=1/{step:.3f},scale=480:-2,tile=3x2:padding=6:color=white",
       "-q:v", "3", str(sdir / "sheet_%02d.jpg"))
    sheets = []
    for k, s in enumerate(sorted(sdir.glob("sheet_*.jpg"))):
        ts = [x["t"] for x in stamped[k * 6:(k + 1) * 6]]
        sheets.append({"file": str(s), "frames_t": ts})
    return step, stamped, sheets


# ---------- transcript ----------

def extract_wav(video, out):
    wav = out / "audio.wav"
    try:
        ff("-i", str(video), "-vn", "-ar", "16000", "-ac", "1", str(wav))
        return wav
    except subprocess.CalledProcessError:
        return None


def transcribe(wav, model, lang, words=False):
    from faster_whisper import WhisperModel
    log(f"transcribing (whisper {model}, local, first run downloads the model)")
    m = WhisperModel(model, compute_type="int8")
    segs, info = m.transcribe(str(wav), language=lang, vad_filter=True, word_timestamps=words)
    rows = []
    for s in segs:
        r = {"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()}
        if words and s.words:
            r["words"] = [(round(w.start, 2), round(w.end, 2), w.word) for w in s.words]
        rows.append(r)
    return info.language, rows


# ---------- diarization (who speaks when; offline, no token) ----------

MODELS = Path.home() / ".cache" / "watch-models"
SEG_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
           "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
           "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")


def diar_models():
    import tarfile
    MODELS.mkdir(parents=True, exist_ok=True)
    seg = MODELS / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    if not seg.exists():
        log("downloading speaker-segmentation model (~6 MB, once)")
        tb = MODELS / "seg.tar.bz2"
        tb.write_bytes(http(SEG_URL))
        with tarfile.open(tb) as t:
            t.extractall(MODELS)
        tb.unlink()
    emb = MODELS / EMB_URL.rsplit("/", 1)[1]
    if not emb.exists():
        log("downloading speaker-embedding model (~40 MB, once)")
        emb.write_bytes(http(EMB_URL))
    return seg, emb


def diarize(wav, speakers, threshold=0.5):
    import wave
    import numpy as np
    import sherpa_onnx
    seg, emb = diar_models()
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg))),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb)),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=speakers, threshold=threshold),
        min_duration_on=0.3, min_duration_off=0.5)
    if not cfg.validate():
        raise RuntimeError("bad diarization config")
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    with wave.open(str(wav)) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    log(f"diarizing ({'auto' if speakers < 0 else speakers} speakers)")
    res = sd.process(audio).sort_by_start_time()
    return [(r.start, r.end, r.speaker) for r in res]


def who(a, b, turns):
    best, top = None, 0.0
    for x, y, spk in turns:
        ov = min(b, y) - max(a, x)
        if ov > top:
            best, top = spk, ov
    if best is None:  # word in a gap: nearest turn
        mid = (a + b) / 2
        best = min(turns, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1])))[2] if turns else None
    return best


def label(rows, turns):
    """Speaker per segment, or per word when word timestamps exist (then rows are re-split into turns in place).
    Speakers are renamed S1, S2… by order of first appearance."""
    order = {}
    if rows and all("words" in r for r in rows):
        new = []
        for r in rows:
            for a, b, w in r["words"]:
                spk = who(a, b, turns)
                if spk is not None:
                    order.setdefault(spk, f"S{len(order) + 1}")
                lab = order.get(spk)
                if new and new[-1].get("speaker") == lab and a - new[-1]["end"] < 1.5:
                    new[-1]["text"] += w
                    new[-1]["end"] = b
                    new[-1]["words"].append((a, b, w))
                else:
                    new.append({"start": a, "end": b, "text": w, "speaker": lab, "words": [(a, b, w)]})
        # smoothing: a 1-2 word blip under 0.8 s between turns goes to the previous speaker
        merged = []
        for r in new:
            blip = len(r["words"]) <= 2 and r["end"] - r["start"] < 0.8
            if merged and blip:
                merged[-1]["text"] += r["text"]
                merged[-1]["end"] = r["end"]
                merged[-1]["words"] += r["words"]
            elif merged and merged[-1].get("speaker") == r.get("speaker") and r["start"] - merged[-1]["end"] < 1.5:
                merged[-1]["text"] += r["text"]
                merged[-1]["end"] = r["end"]
                merged[-1]["words"] += r["words"]
            else:
                merged.append(r)
        new = merged
        for r in new:
            r["text"] = r["text"].strip()
            if not r["speaker"]:
                r.pop("speaker")
        rows[:] = new
        return len({r["speaker"] for r in rows if r.get("speaker")})
    for r in rows:
        best, top = None, 0.0
        for a, b, spk in turns:
            ov = min(r["end"], b) - max(r["start"], a)
            if ov > top:
                best, top = spk, ov
        if best is not None:
            order.setdefault(best, f"S{len(order) + 1}")
            r["speaker"] = order[best]
    return len(order)


def fmt_ts(x, long):
    h, m, sec = int(x // 3600), int(x % 3600 // 60), int(x % 60)
    return f"{h}:{m:02d}:{sec:02d}" if long else f"{m:02d}:{sec:02d}"


def rediarize(out, speakers, threshold):
    man = json.loads((out / "manifest.json").read_text())
    seg_file = out / "segments.json"
    words_file = out / "words.json"
    if words_file.exists():  # word-level: one pseudo-segment carrying every word, label() re-splits it
        ws = [tuple(w) for w in json.loads(words_file.read_text())]
        tr = [{"start": ws[0][0], "end": ws[-1][1], "text": "", "words": ws}] if ws else []
    elif seg_file.exists():
        tr = json.loads(seg_file.read_text())
    else:  # runs made before segments.json existed: rebuild from transcript.md
        tr = []
        for line in (out / "transcript.md").read_text().splitlines():
            m = re.match(r"\[(?:(\d+):)?(\d+):(\d+)\] (?:S\d+: )?(.*)", line)
            if m:
                t = int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])
                tr.append({"start": float(t), "text": m[4]})
        for i, r in enumerate(tr):
            r["end"] = tr[i + 1]["start"] if i + 1 < len(tr) else man["duration_s"]
    for r in tr:
        r.pop("speaker", None)
    wav = extract_wav(Path(man["video"]), out)
    t0 = time.time()
    n = label(tr, diarize(wav, speakers, threshold))
    wav.unlink(missing_ok=True)
    long = man["duration_s"] >= 3600
    lines = [f"[{fmt_ts(r['start'], long)}] " + (f"{r['speaker']}: " if r.get("speaker") else "") + r["text"] for r in tr]
    (out / "transcript.md").write_text("\n".join(lines) + "\n")
    seg_file.write_text(json.dumps(tr, ensure_ascii=False))
    share = {}
    for r in tr:
        if r.get("speaker"):
            share[r["speaker"]] = share.get(r["speaker"], 0) + r["end"] - r["start"]
    tot = sum(share.values()) or 1
    man.update(speakers_found=n, speaker_share={k: round(v / tot * 100) for k, v in sorted(share.items())})
    man.setdefault("timing", {})["diarize_s"] = round(time.time() - t0)
    (out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2))
    print(json.dumps({"speakers_found": n, "speaker_share_pct": man["speaker_share"], "diarize_s": man["timing"]["diarize_s"]}))
    print(f"transcript: {out / 'transcript.md'} ({len(lines)} lines)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--model", default="small")
    ap.add_argument("--lang", default=None)
    ap.add_argument("--no-transcript", action="store_true")
    ap.add_argument("--call", action="store_true", help="call recording: sparse frames, speaker labels, audio-only OK")
    ap.add_argument("--speakers", type=int, default=-1, help="known number of speakers (better accuracy); -1 = auto")
    ap.add_argument("--no-diarize", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.5, help="auto-clustering distance; higher = fewer speakers")
    ap.add_argument("--rediarize", metavar="RUN_DIR", help="redo speaker labels for an existing run (no re-transcription)")
    a = ap.parse_args()
    if a.rediarize:
        return rediarize(Path(a.rediarize), a.speakers, a.threshold)
    if not a.source:
        ap.error("source is required")
    if a.max_frames is None:
        a.max_frames = 24 if a.call else 36

    out = Path(a.out) if a.out else RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(a.source)}"
    out.mkdir(parents=True, exist_ok=True)
    video, meta = fetch(a.source, out)
    dur, has_video = probe(video)
    log(f"media ok: {dur / 60:.1f} min, video={'yes' if has_video else 'no (audio only)'}")
    step, fr, sheets = frames(video, out, dur, a.max_frames) if has_video else (0, [], [])

    timing, lang, tr, n_spk = {}, None, [], 0
    wav = None if a.no_transcript else extract_wav(video, out)
    if wav:
        t0 = time.time()
        lang, tr = transcribe(wav, a.model, a.lang, words=a.call)
        timing["transcribe_s"] = round(time.time() - t0)
        if a.call and not a.no_diarize and tr:
            t0 = time.time()
            try:
                n_spk = label(tr, diarize(wav, a.speakers, a.threshold))
            except Exception as e:
                log(f"diarization failed, transcript left without speakers: {e}")
            timing["diarize_s"] = round(time.time() - t0)
        wav.unlink(missing_ok=True)

    def ts(x):
        h, m, sec = int(x // 3600), int(x % 3600 // 60), int(x % 60)
        return f"{h}:{m:02d}:{sec:02d}" if dur >= 3600 else f"{m:02d}:{sec:02d}"

    lines = [f"[{ts(r['start'])}] " + (f"{r['speaker']}: " if r.get("speaker") else "") + r["text"] for r in tr]
    (out / "transcript.md").write_text("\n".join(lines) + "\n")
    (out / "segments.json").write_text(json.dumps(tr, ensure_ascii=False))
    if a.call and tr and "words" in tr[0]:
        (out / "words.json").write_text(json.dumps([w for r in tr for w in r["words"]], ensure_ascii=False))
    manifest = {"source": a.source, "dir": str(out), "video": str(video), "duration_s": round(dur, 1),
                "frame_step_s": round(step, 2), "meta": meta, "transcript_lang": lang,
                "transcript": str(out / "transcript.md"), "has_speech": bool(tr),
                "mode": "call" if a.call else "video", "has_video": has_video, "speakers_found": n_spk,
                "timing": timing, "whisper_model": a.model,
                "sheets": sheets, "frames": fr}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    print(json.dumps({k: manifest[k] for k in ("dir", "mode", "duration_s", "has_video", "frame_step_s", "meta", "transcript_lang", "has_speech", "speakers_found", "timing")},
                     ensure_ascii=False, indent=2))
    print(f"\nSHEETS ({len(sheets)}):")
    for s in sheets:
        print(f"  {s['file']}  t={s['frames_t']}")
    print(f"\nFRAMES: {len(fr)} in {out / 'frames'}")
    if len(lines) > 400:
        print(f"\nTRANSCRIPT: {len(lines)} lines, too long for stdout -> Read {out / 'transcript.md'} in chunks")
    else:
        print("\nTRANSCRIPT:")
        print("\n".join(lines) if lines else "  (no speech detected)")


if __name__ == "__main__":
    main()
