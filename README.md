<p align="center">
 <img width="128" height="128" alt="icon128" src="https://github.com/user-attachments/assets/23bcc7c7-d8ed-4cf8-b226-d6b0b23cc4a0" />
</p>

<h1 align="center">MyDownloader</h1>

An IDM-style download manager for Windows, written in Python (PySide6), with a companion Chrome extension.

- Multi-connection downloads with **aria2c**, pause/resume, auto-resume when the network comes back
- Video downloads with **yt-dlp** (Vimeo, YouTube and more), quality picker, video + audio merged with **ffmpeg**
- Chrome extension: takes over Chrome downloads, download button on videos, detects streams
- "Download File Info" dialog that starts downloading in the background (like IDM)
- Categories, light/dark theme, tray icon, notifications

<p align="center">
- <img width="688" height="424" alt="image" src="https://github.com/user-attachments/assets/47f4f132-051e-42a6-910d-127ab47eb7d1" />
</p>


## Project layout

```
gui_downloader.py   window (PySide6) - start here
manager.py          download list, routing, settings, Chrome listener (127.0.0.1:9614)
engine.py           download engines: aria2c, yt-dlp, Vimeo, built-in fallback
icon.ico / icon.png app icon
extension/          Chrome extension (Manifest V3)
build.bat           builds the .exe with PyInstaller
```

## Run from source

```
pip install -r requirements.txt
python gui_downloader.py
```

Put these helper programs in the project folder (they are not in the repo):

| File | Needed for | Get it from |
|---|---|---|
| `aria2c.exe` | fast multi-connection downloads | https://github.com/aria2/aria2/releases |
| `ffmpeg.exe`, `ffprobe.exe` | merging video + audio | https://www.gyan.dev/ffmpeg/builds/ (essentials) |
| `deno.exe` | YouTube | https://github.com/denoland/deno/releases |

## Install the Chrome extension

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and select the `extension` folder.

The app must be running for the extension to work.

## Build the .exe

```
build.bat
```

The result is `dist\MyDownloader\` - zip and share that whole folder.

## Notes

- The app and the extension share a token (`EXTENSION_TOKEN` in `manager.py`, `TOKEN` in
  `extension/background.js`). If you change it, change both.
- Downloading from sites like YouTube may be against their terms; this project is for personal use.
- aria2 and FFmpeg are GPL/LGPL licensed; include their license files if you distribute builds.
