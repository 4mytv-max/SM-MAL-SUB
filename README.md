# SM OPEN SUBTITLES FOR MALAYALAM

Stremio subtitle addon that **auto-translates** movie & series subtitles to
Malayalam using OpenSubtitles + machine translation.

## How it works

1. Stremio asks for subtitles for a title (IMDb `tt` id).
2. The addon searches **OpenSubtitles** for that title (English preferred).
3. On first play, it downloads the source `.srt`, translates every subtitle
   cue to Malayalam (Google Translate → MyMemory fallback), caches the
   result for 24 hours, and serves the translated SRT.

## Setup (important)

### 1. Get an OpenSubtitles API key (free)

1. Create an account at https://www.opensubtitles.com
2. Go to your profile → API → generate a **free API key**.

### 2. Deploy on Render

1. Create a GitHub repo (e.g. `SM-OpenSubtitles-Malayalam`) and upload:
   - `app.py`
   - `requirements.txt`
   - `README.md` (optional)
2. Create a **Web Service** on https://render.com from that repo (Free plan).
3. In Render → **Environment**, add:
   - `OPENSUBTITLES_API_KEY` = your API key (**required**)
   - `OPENSUBTITLES_USER` = your opensubtitles username (optional, higher quota)
   - `OPENSUBTITLES_PASSWORD` = your password (optional)
4. Start command: `gunicorn app:app`
5. After deploy, install in Stremio/Nuvio:
   `https://<your-service>.onrender.com/trsub/manifest.json`

Without `OPENSUBTITLES_API_KEY` the addon returns no subtitles.

## Manifest

- **ID:** `com.smopensubtitles.malayalam`
- **Name:** SM OPEN SUBTITLES FOR MALAYALAM
- **Resources:** subtitles · **Types:** movie, series · **Prefixes:** tt

## Caveats

- **Machine translation quality varies.** It is a rough auto-translation —
  never as good as human-made Malayalam subtitles from Msone / Movie
  Mirror / Team GOAT. Use it only when no human subtitle exists.
- **Rate limits:** Google Translate's free endpoint may throttle. The addon
  translates in batches with delays, caches every file for 24h, and allows
  max one translation job per 5 seconds. If translation fails, the
  original (untranslated) lines are served so playback never breaks.
- **OpenSubtitles quota:** anonymous API use is limited; adding your own
  free account credentials raises the download quota.
- **Malayalam font:** some players show boxes/tofu for Malayalam glyphs —
  use VLC / MX Player (they bundle proper fonts).
