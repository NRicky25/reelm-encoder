# reelm-encoder

Turns a video file into HLS, the streaming format the Reelm player uses: 1080p,
720p and 480p versions cut into ~6-second chunks, so the player can switch
quality smoothly. Runs on your own PC and uses an NVIDIA graphics card (NVENC)
when there is one. Nothing is uploaded: you upload the result yourself.

```
movie.mp4 ──ffmpeg──▶ movie-3f9a2c/  ──you upload──▶ R2 (videos/) ──▶ Reelm player
                       ├ master.m3u8      ← the link the player uses
                       ├ v1080p/ v720p/ v480p/
```

## Requirements

- **Windows 10 or 11** (64-bit)
- **[uv](https://docs.astral.sh/uv/)**: installs Python 3.12 and all packages for you.
  Install it once with `winget install --id=astral-sh.uv -e`, then open a new terminal.
- **ffmpeg**: `winget install Gyan.FFmpeg` (found automatically, even when it isn't on
  PATH; or set `FFMPEG_DIR` in `.env`).
- **Optional: an NVIDIA graphics card** for fast encoding (NVENC). Without one it
  encodes on the processor, which is slower.

## Install

```
git clone https://github.com/NRicky25/reelm-encoder.git
cd reelm-encoder
.\setup.cmd
```

(Or download the ZIP from GitHub, extract it, and double-click **`setup.cmd`**.)

`setup.cmd` checks for uv, installs everything (`uv sync`), checks ffmpeg and the
graphics card (`reelm-encoder check`), and creates a **Reelm Encoder** shortcut in
the folder. Run it again after updating (`git pull`) to pick up new packages.

Optional: copy `.env.example` to `.env` and set `PUBLIC_URL` (where your videos are
hosted). The window then shows each video's full link, ready to copy.

## Use

Double-click the **Reelm Encoder** shortcut (or run `uv run reelm-encoder app`):

1. **Save to:** pick the folder the results go in (default `Videos\Reelm Encoded`).
2. **Encode on:** graphics card (fast) or processor.
3. Click **Choose videos**, or drop videos on the window. Each one shows its
   progress and the qualities it gets.
4. When a video is done, upload its folder into your R2 bucket under **`videos/`**
   (e.g. drag it into the R2 dashboard). Its link is then
   `<PUBLIC_URL>/videos/<folder>/master.m3u8`.
5. Reelm admin → **Videos → New**: provider **external**, status **ready**, paste the
   link and the duration (the window has Copy buttons), Save. Then choose that video
   on the episode or movie.

From a terminal instead:

```
uv run reelm-encoder encode "D:\videos\episode 1.mp4"              # into ./output
uv run reelm-encoder encode "D:\videos\episode 1.mp4" --out D:\enc  # into D:\enc
uv run reelm-encoder encode clip.mp4 --cpu                          # don't use the GPU
```

## Notes

- **Folder names** look like `episode-1-3f9a2c`: the video's name made safe for a link,
  plus a short code. Every encode gets a new code, because hosted files are cached
  "forever": re-uploading under an old name could keep serving the old video.
- **Never overwrites:** results always go into a new folder. A cancelled or failed
  encode removes only its own folder; one cut off by a crash is cleaned up the next
  time the window opens.
- **Never upscales:** a 720p source gives 720p + 480p.
- **Wide and portrait videos keep their shape.** A 2.40:1 movie's top level is 1920×800:
  full-HD width without the black bars. It's labelled **1080p** (as Netflix and YouTube
  do), and the Reelm player shows it as 1080p too.
- HDR is converted to normal (SDR) colour, surround sound is mixed to stereo, and with
  several audio tracks the default one is used.
- Subtitles: make a `.vtt` with the Subtitle Maker. The encoder doesn't include it yet.
- Only encode and stream videos you have the rights to.

## Troubleshooting

| Problem | Fix |
|---|---|
| `'uv' is not recognized` | Install uv (see Requirements), then open a **new** terminal. |
| Badge says "ffmpeg missing" | `winget install Gyan.FFmpeg`, then reopen the app. |
| Badge says "processor only" on a PC with an NVIDIA card | Update the NVIDIA driver, then reopen the app. |
| The window is blank | Install the [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) (built into Windows 11). |

## Development

```
uv run pytest      # includes real encodes of a generated clip (CPU, and GPU if present)
uv run ruff check .
uv run --with pillow assets/make_icon.py   # redraw the app icon
```
