# watch

A **Claude Code skill** that lets Claude "watch" a video or a call recording.

Claude cannot take video as input. `watch` turns a video into what Claude can read:
timestamped frames packed into contact sheets, plus a transcript made **locally** on your
machine (faster-whisper, no OpenAI key, no per-minute fees). Claude then reads both and
writes a structured breakdown.

## What you get

**Creator / competitor video** (YouTube, Shorts, TikTok, Instagram Reels, LinkedIn, X, a local file):

→ TL;DR and metadata (views, likes, saves when the platform returns them)
→ the hook in the first 3 seconds: what is said, shown and written on screen
→ a beat-by-beat timeline with `[mm:ss]`
→ on-screen text, visual devices, CTA, fact check, takeaways you can reuse

**Call recording** (`--call`: Zoom, Meet, Teams, Loom, `.m4a`, `.mp3`, voice notes):

→ speakers labelled S1, S2… by offline diarization, no token
→ decisions, next steps with owner and date, objections, budget and timing quotes with timestamps
→ a local file never leaves your machine

## Install

Requires [Claude Code](https://claude.com/claude-code) and a terminal.

1. Install `uv` (it pulls Python and every other dependency on first run, ffmpeg included):

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Install the skill, either way works:

   ```bash
   npx skills add victor-shulga/watch -g
   ```

   or inside Claude Code:

   ```
   /plugin marketplace add victor-shulga/watch
   /plugin install watch@watch
   ```

3. Paste a link and say **"watch this"** / **"break down this video"** / **"analyze this call"**.

The first run downloads the whisper model (~460 MB) and takes a minute or two.

## Speed (Apple M4, measured)

| Input | Time |
|---|---|
| 19-second YouTube video | 25 s end to end |
| 26.6-min call, whisper `medium` | 16 min transcript + 4.5 min speaker labels |

`small` (the default) is about 3× faster than `medium`. Use `medium` for non-English or noisy audio.

## Requirements & integrations

| Integration | Used for | Required? | Auth/setup |
|---|---|---|---|
| Claude Code | runs the skill, reads frames and transcript | yes | — |
| uv | runs the script with its own Python ≥3.10 + deps | yes | one-line installer above |
| yt-dlp | download from YouTube, TikTok, Instagram, X, LinkedIn… | yes | installed by uv |
| imageio-ffmpeg | bundled ffmpeg: frames, contact sheets, audio | yes | installed by uv, no brew needed |
| faster-whisper | local speech-to-text | for transcript | model auto-downloads on first run |
| sherpa-onnx + k2-fsa models | speaker labels in `--call` | for calls | ~45 MB, auto-downloads, no token |
| Apify | fallback download when yt-dlp is blocked on TikTok / Instagram | no | `APIFY_TOKEN` env or `~/.config/watch/apify_token`; ~$0.01–0.02 per video |

## Structure

```
.claude-plugin/        plugin.json, marketplace.json
skills/watch/
  SKILL.md             how Claude runs it and the breakdown formats
  scripts/watch.py     downloader, frame sampler, transcriber, diarizer
```

## License

MIT. By [Victor Shulga](https://victorshulga.com).
