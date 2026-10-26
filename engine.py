"""Download engine: tool lookup, Vimeo parsing and the DownloadTask that runs one download.

No user-interface code lives here. A task reports through its manager (manager.py):
    mgr.log(msg), mgr.save_state(), mgr.session, mgr.settings,
    mgr.get_headers(url, extra), mgr.ytdlp_extra_opts(url, extra)
"""
import os
import re
import base64
import threading
import time
import shutil
import sys
import tempfile
import subprocess
import urllib.parse

try:
    import yt_dlp
except ImportError:
    yt_dlp = None
    print("Please install yt-dlp using: pip install yt-dlp")

BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')


# ============================================================
# TOOLS (ffmpeg, aria2c) AND VIMEO HELPERS
# ============================================================
def app_folders():
    """Folders where bundled tools (ffmpeg.exe, aria2c.exe) may live, most specific first."""
    folders = []
    if getattr(sys, 'frozen', False):                      # running as a PyInstaller .exe
        if hasattr(sys, '_MEIPASS'):
            folders.append(sys._MEIPASS)                    # --onefile temp folder
        folders.append(os.path.dirname(sys.executable))     # folder the .exe sits in
    folders.append(os.path.dirname(os.path.abspath(__file__)))
    return folders


def find_tool(name, extra_paths=()):
    """Find a bundled tool next to the app first, then on PATH, then in extra locations."""
    exe = name + ('.exe' if os.name == 'nt' else '')
    for folder in app_folders():
        for c in (os.path.join(folder, exe), os.path.join(folder, name, 'bin', exe)):
            if os.path.isfile(c):
                return c
    found = shutil.which(name)
    if found:
        return found
    for c in extra_paths:
        c = os.path.expandvars(c)
        if os.path.isfile(c):
            return c
    return None


def find_ffmpeg():
    return find_tool('ffmpeg', (r'C:\ffmpeg\bin\ffmpeg.exe',
                                r'%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe'))


def find_aria2c():
    return find_tool('aria2c', (r'%LOCALAPPDATA%\Microsoft\WinGet\Links\aria2c.exe',))


def find_deno():
    return find_tool('deno', (r'%USERPROFILE%\.deno\bin\deno.exe',
                              r'%LOCALAPPDATA%\Microsoft\WinGet\Links\deno.exe'))


def ytdlp_runtime_opts():
    """Since 2026 YouTube makes yt-dlp solve JavaScript challenges. That needs a JS runtime
    (Deno, bundled next to the app like ffmpeg) plus the yt-dlp-ejs scripts. Without them
    YouTube only returns thumbnails and yt-dlp says 'Requested format is not available'."""
    opts = {}
    deno = find_deno()
    node = None if deno else find_tool('node')
    if deno:
        opts['js_runtimes'] = {'deno': {'path': deno}}
    elif node:
        opts['js_runtimes'] = {'node': {'path': node}}
    try:
        import yt_dlp_ejs  # noqa: F401  (pip install "yt-dlp[default]")
    except ImportError:
        opts['remote_components'] = ['ejs:github']   # let yt-dlp fetch the scripts itself
    return opts


YOUTUBE_RUNTIME_HINT = ("YouTube needs Deno to work. Download deno.exe (deno.com) and put it next to "
                        "MyDownloader, like ffmpeg.exe, then try again.")


def friendly_ytdlp_error(err):
    """Turn yt-dlp's YouTube JS-runtime failure into something actionable."""
    if ('Requested format is not available' in err or 'Only images are available' in err) \
            and not (find_deno() or find_tool('node')):
        return YOUTUBE_RUNTIME_HINT
    return err


NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)   # hide console popups on Windows


class SegmentError(Exception):
    """Raised when a Vimeo segment can't be downloaded; message goes to the log."""


# ============================================================
# VIMEO HELPERS
# ============================================================
def parse_vimeo_page_url(url):
    """Return (video_id, unlisted_hash) for vimeo.com / player.vimeo.com URLs, else (None, None)."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if 'vimeocdn.com' in host or 'vimeo.com' not in host:
        return None, None
    m = re.search(r'/(?:video/)?(\d+)(?:/([0-9a-f]+))?', parsed.path)
    if not m:
        return None, None
    video_id, vhash = m.group(1), m.group(2)
    if not vhash:
        vhash = urllib.parse.parse_qs(parsed.query).get('h', [None])[0]
    return video_id, vhash


def vimeo_player_url(video_id, vhash=None):
    url = f"https://player.vimeo.com/video/{video_id}"
    return f"{url}?h={vhash}" if vhash else url


def get_vimeo_playlist_from_config(video_id, vhash, session, referer, log_callback):
    """Ask the Vimeo player config for the DASH playlist.json URL (what the browser player does)."""
    cfg_url = f"https://player.vimeo.com/video/{video_id}/config"
    if vhash:
        cfg_url += f"?h={vhash}"
    headers = {'User-Agent': BROWSER_UA, 'Referer': referer or 'https://vimeo.com/'}
    log_callback(f"[VIMEO CONFIG] Fetching player config for {video_id}...")
    try:
        r = session.get(cfg_url, headers=headers, timeout=15)
    except Exception as e:
        log_callback(f"[VIMEO CONFIG] Network error: {e}")
        return None, None
    if r.status_code != 200:
        log_callback(f"[VIMEO CONFIG] HTTP {r.status_code} (private/embed-only video? "
                     f"set Custom Referer to the page that embeds it)")
        return None, None
    try:
        cfg = r.json()
    except Exception:
        log_callback("[VIMEO CONFIG] Config was not JSON.")
        return None, None

    title = cfg.get('video', {}).get('title') or f"vimeo_{video_id}"
    dash = cfg.get('request', {}).get('files', {}).get('dash', {})
    cdns = dash.get('cdns', {})
    default = dash.get('default_cdn')
    order = ([default] if default in cdns else []) + [c for c in cdns if c != default]
    for cdn in order:
        u = cdns[cdn].get('url')
        if u:
            log_callback(f"[VIMEO CONFIG] Got DASH playlist from CDN '{cdn}'.")
            return u, title
    log_callback("[VIMEO CONFIG] No DASH playlist in config.")
    return None, title


# ============================================================
# VIMEO CDN MANIFEST PARSER
# ============================================================
def parse_vimeo_manifest(url, session, log_callback):
    """
    Parse a Vimeo playlist.json and return (video_track, audio_track, headers).
    Each track is {'init': bytes | url | None, 'segments': [absolute urls], 'label': str}.

    URL resolution chain (this is what was broken before):
        manifest URL  ->  data['base_url']  ->  stream['base_url']  ->  segment['url']
    Each step is relative to the previous one, so everything is resolved with urljoin
    starting from the FULL manifest URL. The auth token (exp=~acl=~hmac=) lives in the
    path, so it must not be thrown away.
    """
    log_callback(f"[VIMEO PARSER] Fetching manifest: {url[:100]}...")

    headers = {
        'User-Agent': BROWSER_UA,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://player.vimeo.com/',
        'Origin': 'https://player.vimeo.com',
    }

    try:
        res = session.get(url, headers=headers, timeout=15)
    except Exception as e:
        log_callback(f"[VIMEO PARSER] Network error: {e}")
        return None, None, headers

    log_callback(f"[VIMEO PARSER] Manifest response: HTTP {res.status_code}, {len(res.content)} bytes")
    if res.status_code != 200:
        log_callback(f"[VIMEO PARSER] ERROR: Manifest returned HTTP {res.status_code}")
        return None, None, headers

    try:
        data = res.json()
    except Exception as e:
        log_callback(f"[VIMEO PARSER] JSON parse error: {e}")
        return None, None, headers

    # FIX 1: resolve against the full manifest URL, not scheme://host
    master_base = urllib.parse.urljoin(url, data.get('base_url', ''))

    # FIX 2: pick ONE rendition per track instead of mixing every quality together
    def pick_best(streams, key):
        streams = [s for s in (streams or []) if s.get('segments')]
        return max(streams, key=key) if streams else None

    v = pick_best(data.get('video'), lambda s: (s.get('height') or 0, s.get('bitrate') or 0))
    a = pick_best(data.get('audio'), lambda s: s.get('bitrate') or 0)

    if not v:
        log_callback("[VIMEO PARSER] ERROR: No video stream with segments in manifest")
        return None, None, headers

    # FIX 3: keep the init segment; fMP4 segments are unplayable without it
    def build_track(stream, kind):
        stream_base = urllib.parse.urljoin(master_base, stream.get('base_url', ''))
        init = None
        if stream.get('init_segment'):
            init = base64.b64decode(stream['init_segment'])
        elif stream.get('init_segment_url'):
            init = urllib.parse.urljoin(stream_base, stream['init_segment_url'])
        segments = [urllib.parse.urljoin(stream_base, s['url'])
                    for s in stream['segments'] if s.get('url')]
        if kind == 'video':
            label = f"{stream.get('height', '?')}p @ {(stream.get('bitrate') or 0) // 1000} kbps"
        else:
            label = f"{(stream.get('bitrate') or 0) // 1000} kbps"
        return {'init': init, 'segments': segments, 'label': label}

    video_track = build_track(v, 'video')
    audio_track = build_track(a, 'audio') if a else None

    log_callback(f"[VIMEO PARSER] Video {video_track['label']}: {len(video_track['segments'])} segments")
    if audio_track:
        log_callback(f"[VIMEO PARSER] Audio {audio_track['label']}: {len(audio_track['segments'])} segments")
    else:
        log_callback("[VIMEO PARSER] No separate audio track (video may be muxed or silent)")
    log_callback(f"[VIMEO PARSER] First segment URL: {video_track['segments'][0][:160]}")

    return video_track, audio_track, headers



# ============================================================
# SMALL HELPERS
# ============================================================
CATEGORIES = {
    'Compressed': ('zip', 'rar', '7z', 'gz', 'tgz', 'bz2', 'xz', 'tar', 'cab', 'arj', 'lzh', 'zst'),
    'Documents': ('pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'rtf', 'odt', 'ods',
                  'odp', 'epub', 'csv', 'md'),
    'Music': ('mp3', 'm4a', 'aac', 'flac', 'wav', 'ogg', 'opus', 'wma', 'aif', 'aiff'),
    'Programs': ('exe', 'msi', 'iso', 'img', 'dmg', 'deb', 'rpm', 'apk', 'appimage', 'msix', 'jar'),
    'Video': ('mp4', 'mkv', 'webm', 'avi', 'mov', 'm4v', 'flv', 'wmv', 'ts', 'mpg', 'mpeg', '3gp'),
}


NETWORK_HINTS = ('connection refused', 'timed out', 'timeout', 'connection reset', 'name resolution',
                 'network is unreachable', 'temporary failure', 'getaddrinfo', 'remote end closed',
                 'incompleteread', 'connection aborted', 'no route to host', 'connection broken',
                 'failed to establish', 'max retries exceeded', 'unable to connect', 'nameresolution',
                 'connectionerror', 'eof occurred', 'network problem', 'read error')


def categorize(filename):
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return 'General'


def human_size(n):
    if not n or n <= 0:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f"{n:.0f} {unit}" if unit in ('B', 'KB') else f"{n:.2f} {unit}"
        n /= 1024


def human_time(sec):
    if sec is None or sec < 0 or sec > 360000:
        return ''
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} hr {m} min"
    if m:
        return f"{m} min {s} sec"
    return f"{s} sec"


_UNITS = {'b': 1, 'kib': 1024, 'mib': 1024 ** 2, 'gib': 1024 ** 3, 'tib': 1024 ** 4,
          'kb': 1000, 'mb': 1000 ** 2, 'gb': 1000 ** 3}


def parse_size(text):
    """'12.5MiB' -> bytes (aria2c readout)."""
    m = re.match(r'([\d.]+)\s*([A-Za-z]+)', text or '')
    if not m:
        return 0
    return int(float(m.group(1)) * _UNITS.get(m.group(2).lower(), 1))


def parse_eta(text):
    """'1h2m3s' -> seconds (aria2c readout)."""
    total = 0
    for num, unit in re.findall(r'(\d+)([hms])', text or ''):
        total += int(num) * {'h': 3600, 'm': 60, 's': 1}[unit]
    return total if text else None


# ============================================================
# DOWNLOAD TASK
# ============================================================
class DownloadTask:
    """One download. Status is one of:
    Queued, Downloading, Paused, Completed, Error, Cancelled."""

    def __init__(self, mgr, url, save_dir, filename, task_id, task_type="standard",
                 total_size=0, extra=None, state=None):
        self.mgr = mgr
        self.url = url
        self.save_dir = save_dir
        self.filename = filename
        self.task_id = str(task_id)
        self.task_type = task_type
        # Per-download settings: referer, cookie (from the extension), format (chosen quality)
        self.extra = dict(extra or {})

        self.status = "Queued"
        self.current_progress = 0
        self.total_size = total_size or 0
        self.downloaded = 0
        self.speed = 0.0          # bytes per second
        self.eta = None           # seconds left
        self.error = ''
        self.date_added = time.time()
        self.date_finished = None
        # Pre-download (like IDM): the file downloads into a temp folder while the
        # "Download File Info" dialog is open. awaiting = dialog not answered yet;
        # final_path = where the user chose to save it (moved there when complete).
        self.awaiting = False
        self.final_path = None
        # Auto-resume: set when a download failed because the network dropped
        self.retryable = False
        self.retry_count = 0
        self.next_retry = 0.0

        self.is_paused = False
        self.is_cancelled = False
        self.process = None
        self.threads = []
        self.worker = None
        self.file_lock = threading.Lock()
        self.move_lock = threading.Lock()
        self._meter_t = None
        self._meter_b = 0

        if state:
            self.status = state.get("status", "Queued")
            self.current_progress = state.get("current_progress", 0)
            self.total_size = state.get("total_size") or 0
            self.downloaded = state.get("downloaded") or 0
            self.date_added = state.get("date_added") or self.date_added
            self.date_finished = state.get("date_finished")
            self.error = state.get("error", '')
            self.extra = dict(state.get("extra") or {})
            self.awaiting = bool(state.get("awaiting"))
            self.final_path = state.get("final_path")
            self.retryable = bool(state.get("retryable"))
            self.retry_count = int(state.get("retry_count") or 0)
            if self.status == "Downloading":     # app was closed mid-download
                self.status = "Paused"
            if self.status == "Completed":
                self.current_progress = 100

    # ---------------- Info ----------------
    @property
    def output_path(self):
        return os.path.join(self.save_dir, self.filename)

    @property
    def display_name(self):
        """The name the user sees (the chosen name, even while it's still in the temp folder)."""
        return os.path.basename(self.final_path) if self.final_path else self.filename

    @property
    def display_path(self):
        return self.final_path or self.output_path

    @property
    def category(self):
        return categorize(self.display_name)

    @property
    def is_active(self):
        return self.status == "Downloading"

    def get_state(self):
        return {
            "task_id": self.task_id, "url": self.url, "save_dir": self.save_dir,
            "filename": self.filename, "total_size": self.total_size,
            "downloaded": self.downloaded, "status": self.status, "task_type": self.task_type,
            "current_progress": self.current_progress, "date_added": self.date_added,
            "date_finished": self.date_finished, "error": self.error,
            "awaiting": self.awaiting, "final_path": self.final_path,
            "retryable": self.retryable, "retry_count": self.retry_count,
            # cookies are login data, so they are never written to disk
            "extra": {k: v for k, v in self.extra.items() if k != 'cookie'},
        }

    def _log(self, msg):
        self.mgr.log(msg)

    def _meter(self, downloaded, total=None):
        """Track bytes, speed (smoothed) and time left for engines that don't report them."""
        now = time.time()
        self.downloaded = downloaded
        if total:
            self.total_size = total
        if self._meter_t is None:
            self._meter_t, self._meter_b = now, downloaded
            return
        dt = now - self._meter_t
        if dt >= 1.0:
            inst = max(0.0, (downloaded - self._meter_b) / dt)
            self.speed = inst if not self.speed else 0.7 * self.speed + 0.3 * inst
            self._meter_t, self._meter_b = now, downloaded
        if self.total_size and self.speed > 0:
            self.eta = max(0, (self.total_size - downloaded) / self.speed)
        if self.total_size:
            self.current_progress = max(0, min(99, int(downloaded / self.total_size * 100)))

    def _fail(self, raw, code=None):
        """Finish with an error, explaining it in plain words and deciding whether the
        app should retry by itself (network dropped) or the link itself is the problem."""
        text = str(raw)
        low = text.lower()
        m = re.search(r'(?:status=|http error |http )(\d{3})', low)
        status = m.group(1) if m else None
        pct = self.current_progress
        if status in ('401', '403', '404', '410'):
            self.retryable = False
            msg = (f"The link expired or was refused (HTTP {status}). Download it again from the website"
                   + (f" and it will continue from {pct}%." if pct else "."))
        elif code in (2, 6, 19) or any(h in low for h in NETWORK_HINTS):
            self.retryable = True
            self.next_retry = time.time() + min(300, 10 * 2 ** min(self.retry_count, 5))
            msg = "Connection lost. It will continue automatically when you're back online."
        else:
            self.retryable = False
            msg = text[:300]
        self._log(f"[ERROR] {self.display_name}: {text[:300]}")
        self._finish("Error", msg)

    def _finish(self, status, error=''):
        self.speed = 0.0
        self.eta = None
        self._meter_t = None
        # Locked so a "Start Download" click at the same moment can't miss the move
        with self.move_lock:
            if status == "Completed":
                self.current_progress = 100
                self.date_finished = time.time()
                if self.total_size:
                    self.downloaded = self.total_size
                self.retryable = False
                self.retry_count = 0
                if self.final_path and not self.move_to_final():
                    status, error = "Error", self.error
            self.status = status
            self.error = error
        self.mgr.save_state()

    def confirm(self, final_path, start=True):
        """The user answered the dialog: remember the chosen path, move now if already done."""
        with self.move_lock:
            self.awaiting = False
            self.final_path = final_path
            if self.status == "Completed":
                self.move_to_final()
        if not start and self.status in ("Downloading", "Queued"):
            self.pause()                     # "Download Later" keeps what's downloaded so far
        self.mgr.save_state()

    def move_to_final(self):
        """Move a finished file from the temp folder to where the user chose to save it."""
        if not self.final_path:
            return True
        src = self.output_path
        dst = self.final_path
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(src):
                base, ext = os.path.splitext(dst)
                n = 1
                while os.path.exists(f"{base} ({n}){ext}"):
                    n += 1
                dst = f"{base} ({n}){ext}"
            if os.path.abspath(dst) != os.path.abspath(src):
                shutil.move(src, dst)
        except OSError as e:
            self.error = f"Downloaded, but could not move it to {dst}: {e}. It is in {src}"
            self._log(f"[MOVE] {self.error}")
            return False
        self.save_dir, self.filename = os.path.split(dst)
        self.final_path = None
        self._log(f"[MOVE] Saved to {dst}")
        return True

    # ---------------- Controls ----------------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.status = "Downloading"
        self.error = ''
        self.is_paused = False
        self.is_cancelled = False
        self._meter_t = None
        os.makedirs(self.save_dir, exist_ok=True)
        self.mgr.save_state()

        if self.task_type == "ytdlp":
            target = self._run_ytdlp
        elif self.task_type == "vimeo_cdn":
            target = self._run_vimeo_cdn
        else:
            target = self._run_http
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def pause(self):
        if self.status not in ("Downloading", "Queued"):
            return
        self.is_paused = True
        self.status = "Paused"
        self.speed = 0.0
        self.eta = None
        if self.process:
            try: self.process.terminate()
            except Exception: pass
        self.mgr.save_state()

    def resume(self):
        if self.status not in ("Paused", "Queued", "Error"):
            return
        self.is_paused = False
        if self.worker and self.worker.is_alive():
            # The worker is waiting in its pause loop; don't start a second one.
            self.status = "Downloading"
            self.mgr.save_state()
        else:
            self.start()

    def cancel(self):
        self.is_cancelled = True
        self.is_paused = False
        self.status = "Cancelled"
        if self.process:
            try: self.process.terminate()
            except Exception: pass
        self.mgr.save_state()

    def remove_files(self):
        """Delete the downloaded file and any leftovers (partial data, aria2/yt-dlp state files).
        Returns the paths that could not be deleted (e.g. the file is open in a player)."""
        base = self.output_path
        candidates = [base, base + '.aria2', base + '.part', base + '.ytdl', base + '.part.aria2']
        folder, name = os.path.split(base)
        stem = os.path.splitext(name)[0]
        try:   # yt-dlp keeps separate video/audio parts like "name.f137.mp4.part"
            for f in os.listdir(folder):
                if f.startswith(stem + '.f') and (f.endswith('.part') or f.endswith('.ytdl')
                                                  or re.search(r'\.f\d+\.\w+$', f)):
                    candidates.append(os.path.join(folder, f))
                elif f.startswith(name + '.part-Frag'):
                    candidates.append(os.path.join(folder, f))
        except OSError:
            pass
        failed = []
        for p in candidates:
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                failed.append(p)
        return failed

    def _wait_if_paused(self):
        while self.is_paused and not self.is_cancelled:
            time.sleep(0.3)

    # ---------------- Vimeo CDN ----------------
    def _link_expired(self):
        m = re.search(r'exp=(\d+)', self.url)
        return bool(m) and time.time() > int(m.group(1))

    def _fetch_segment(self, seg_url, headers, what, retries=3):
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                r = self.mgr.session.get(seg_url, headers=headers, timeout=30)
            except Exception as e:
                last_err = f"network error: {e}"
                time.sleep(attempt)
                continue
            if r.status_code == 200 and r.content:
                return r.content
            if r.status_code == 403:
                if self._link_expired():
                    raise SegmentError(f"{what}: HTTP 403 - the link's exp= time has passed. Grab a fresh link.")
                raise SegmentError(f"{what}: HTTP 403 but the link is NOT expired, so the segment URL "
                                   f"was built wrong or a header is missing. URL: {seg_url[:200]}")
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}"
                time.sleep(attempt * 2)
                continue
            raise SegmentError(f"{what}: HTTP {r.status_code}" + ("" if r.content else " (empty body)"))
        raise SegmentError(f"{what}: failed after {retries} attempts ({last_err})")

    def _run_vimeo_cdn(self):
        """Native Vimeo DASH downloader: one file per track, then mux with ffmpeg."""
        log = self._log
        log(f"[VIMEO CDN] Starting download: {self.filename}")
        tmp_dir = None
        try:
            video, audio, headers = parse_vimeo_manifest(self.url, self.mgr.session, log)
            if not video:
                raise SegmentError("No usable segments in manifest.")

            tracks = [('video', video)] + ([('audio', audio)] if audio else [])
            total_segs = sum(len(t['segments']) for _, t in tracks)
            log(f"[VIMEO CDN] Downloading {total_segs} segments...")

            tmp_dir = tempfile.mkdtemp(prefix="vimeo_")
            paths = {}
            done = 0
            written = 0
            for name, track in tracks:
                path = os.path.join(tmp_dir, f"{name}.mp4")
                paths[name] = path
                with open(path, 'wb') as f:
                    init = track['init']
                    if isinstance(init, bytes):
                        f.write(init)
                    elif init:
                        f.write(self._fetch_segment(init, headers, f"{name} init segment"))
                    for i, seg_url in enumerate(track['segments']):
                        self._wait_if_paused()
                        if self.is_cancelled:
                            break
                        data = self._fetch_segment(seg_url, headers, f"{name} segment {i + 1}")
                        f.write(data)
                        written += len(data)
                        done += 1
                        # Size isn't known up front; estimate it from the average segment so far
                        self._meter(written, int(written / done * total_segs))
                        self.current_progress = min(99, int(done / total_segs * 100))
                if self.is_cancelled:
                    break

            if self.is_cancelled:
                self._finish("Cancelled")
            else:
                self._finalize_vimeo(paths)
                self.total_size = written
                self._finish("Completed")
                log(f"[VIMEO CDN] SUCCESS: {self.filename} ({human_size(written)})")
        except Exception as e:
            self._fail(e)
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _finalize_vimeo(self, paths):
        """Mux video + audio into the final file (stream copy, no re-encode)."""
        log = self._log
        if 'audio' not in paths:
            shutil.move(paths['video'], self.output_path)
            return
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            log("[VIMEO CDN] Merging video + audio with ffmpeg...")
            cmd = [ffmpeg, '-y', '-loglevel', 'error',
                   '-i', paths['video'], '-i', paths['audio'],
                   '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy', self.output_path]
            p = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
            if p.returncode != 0:
                raise SegmentError(f"ffmpeg merge failed: {p.stderr.strip()[-300:]}")
            return
        # No ffmpeg: keep both tracks so nothing is lost
        base, _ = os.path.splitext(self.output_path)
        shutil.move(paths['video'], base + ".video.mp4")
        shutil.move(paths['audio'], base + ".audio.m4a")
        self.filename = os.path.basename(base + ".video.mp4")
        log("[VIMEO CDN] ffmpeg not found, so video and audio were saved as two files. "
            "Put ffmpeg.exe next to the app to get a single merged .mp4.")

    # ---------------- Standard HTTP ----------------
    def _run_http(self):
        try:
            headers = self.mgr.get_headers(self.url, self.extra)
            res = self.mgr.session.get(self.url, headers=headers, stream=True, timeout=15)
            if res.status_code not in (200, 206):
                res.close()
                self._log(f"[HTTP] HTTP {res.status_code} for {self.filename}")
                self._fail(f"HTTP {res.status_code}")
                return
            total_size = int(res.headers.get('content-length', 0))
            accept_ranges = res.headers.get('accept-ranges', '').lower()
            content_type = res.headers.get('content-type', '')
            if total_size:
                self.total_size = total_size
            if 'text/html' in content_type.lower():
                res.close()
                self._log(f"[HTTP] {self.url[:100]} returned a web page, not a file.")
                self._finish("Error", "The link returned a web page, not a file")
                return

            aria2c = find_aria2c()
            if aria2c:
                res.close()
                self._run_aria2c(aria2c, headers)
                return
            self._log("[HTTP] aria2c not found, using built-in 4-thread downloader.")

            if total_size <= 0 or 'bytes' not in accept_ranges:
                self._single_stream_download(res)
                return
            res.close()
            self._chunked_download(total_size, headers)
        except Exception as e:
            self._fail(e)

    # ---------------- aria2c engine ----------------
    def _run_aria2c(self, aria2c, headers):
        """Multi-connection download with aria2c. Pause = stop process; resume = restart with --continue."""
        log = self._log
        conns = max(1, min(16, int(self.mgr.settings.get('connections', 16))))
        limit = int(self.mgr.settings.get('speed_limit_kb', 0))
        log(f"[ARIA2C] Starting {conns}-connection download: {self.filename}")

        cmd = [aria2c, self.url,
               f'--dir={self.save_dir}', f'--out={self.filename}',
               '--continue=true',                  # resume from the .aria2 control file
               f'--max-connection-per-server={conns}', f'--split={conns}', '--min-split-size=1M',
               '--file-allocation=none', '--auto-file-renaming=false', '--allow-overwrite=true',
               '--max-tries=10', '--retry-wait=2', '--timeout=30', '--connect-timeout=15',
               '--summary-interval=1', '--console-log-level=warn',
               '--download-result=hide', '--enable-color=false', '--show-console-readout=true']
        if limit > 0:
            cmd.append(f'--max-download-limit={limit}K')
        for k, v in headers.items():
            kl = k.lower()
            if kl == 'user-agent':
                cmd.append(f'--user-agent={v}')
            elif kl == 'referer':
                cmd.append(f'--referer={v}')
            elif kl != 'connection':
                cmd.append(f'--header={k}: {v}')

        tail = []
        last_log = 0.0
        try:
            # text=True turns aria2c's \r progress updates into separate lines
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, bufsize=1, creationflags=NO_WINDOW)
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                m = re.search(r'\((\d+)%\)', line)
                if m:
                    self.current_progress = min(99, int(m.group(1)))
                    sz = re.search(r'([\d.]+\w+)/([\d.]+\w+)', line)
                    dl = re.search(r'DL:([\d.]+\w+)', line)
                    eta = re.search(r'ETA:(\w+)', line)
                    if sz:
                        self.downloaded = parse_size(sz.group(1))
                        self.total_size = parse_size(sz.group(2)) or self.total_size
                    self.speed = parse_size(dl.group(1)) if dl else 0.0
                    self.eta = parse_eta(eta.group(1)) if eta else None
                    if time.time() - last_log >= 10:
                        last_log = time.time()
                        cn = re.search(r'CN:(\d+)', line)
                        log("[ARIA2C] " + " ".join(filter(None, [
                            self.filename[:40],
                            f"{human_size(self.downloaded)} of {human_size(self.total_size)}",
                            f"({m.group(1)}%)",
                            f"@ {human_size(self.speed)}/s" if self.speed else None,
                            f"{cn.group(1)} connections" if cn else None])))
                elif not line.startswith('[#'):
                    tail = (tail + [line])[-5:]      # keep last messages for error reporting
            code = self.process.wait()
        except Exception as e:
            log(f"[ARIA2C] Could not run aria2c: {e}")
            self._finish("Error", f"Could not run aria2c: {e}")
            return
        finally:
            self.process = None

        if self.is_cancelled:
            for p in (self.output_path, self.output_path + '.aria2'):
                try: os.remove(p)
                except OSError: pass
            self._finish("Cancelled")
        elif self.is_paused:
            log(f"[ARIA2C] Paused: {self.filename} (progress kept)")
            self.speed, self.eta = 0.0, None
            self.status = "Paused"
            self.mgr.save_state()
        elif code == 0:
            log(f"[ARIA2C] SUCCESS: {self.filename}")
            self._finish("Completed")
        else:
            self._fail(' | '.join(tail) or f'aria2c exit code {code}', code)

    # ---------------- Built-in fallback (no aria2c) ----------------
    def _chunked_download(self, total_size, headers):
        with open(self.output_path, 'wb') as f:
            f.truncate(total_size)
        num_threads = 4
        chunk_size = total_size // num_threads
        self.threads = []
        self._bytes = 0
        self._chunk_failed = False
        for i in range(num_threads):
            start = i * chunk_size
            end = total_size - 1 if i == num_threads - 1 else (start + chunk_size - 1)
            t = threading.Thread(target=self._download_chunk, args=(start, end, headers), daemon=True)
            self.threads.append(t)
            t.start()
        while any(t.is_alive() for t in self.threads):
            if self.is_cancelled:
                break
            self._meter(self._bytes, total_size)
            time.sleep(0.3)
        if self.is_cancelled:
            self._finish("Cancelled")
        elif self._chunk_failed:
            self._fail("connection broken while downloading")
        else:
            self._finish("Completed")

    def _download_chunk(self, start, end, headers):
        local_headers = dict(headers, Range=f"bytes={start}-{end}")
        try:
            res = self.mgr.session.get(self.url, headers=local_headers, stream=True, timeout=15)
            if res.status_code not in (200, 206):
                self._chunk_failed = True
                return
            with open(self.output_path, 'r+b') as f:
                f.seek(start)
                for chunk in res.iter_content(chunk_size=65536):
                    if self.is_cancelled:
                        return
                    self._wait_if_paused()
                    if chunk:
                        f.write(chunk)
                        with self.file_lock:
                            self._bytes += len(chunk)
        except Exception:
            self._chunk_failed = True

    def _single_stream_download(self, res):
        try:
            done = 0
            with open(self.output_path, 'wb') as f:
                for chunk in res.iter_content(chunk_size=65536):
                    if self.is_cancelled:
                        break
                    self._wait_if_paused()
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        self._meter(done)
            self._finish("Cancelled" if self.is_cancelled else "Completed")
        except Exception as e:
            self._fail(e)

    # ---------------- yt-dlp ----------------
    def _run_ytdlp(self):
        if yt_dlp is None:
            self._finish("Error", "yt-dlp is not installed (pip install yt-dlp)")
            return
        last_log = [0.0]
        parts_done = [0]          # video + audio come as separate files; count finished ones

        def progress_hook(d):
            if self.is_cancelled:
                raise Exception("Cancelled")
            self._wait_if_paused()
            if self.is_cancelled:
                raise Exception("Cancelled")
            if d['status'] == 'downloading':
                done = d.get('downloaded_bytes') or 0
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                frag_i, frag_n = d.get('fragment_index'), d.get('fragment_count')
                self.downloaded = done
                if total:
                    self.total_size = total
                    pct = done / total * 100
                elif frag_i and frag_n:
                    pct = frag_i / frag_n * 100
                else:
                    pct = None
                if pct is not None:
                    self.current_progress = max(0, min(99, int(pct)))
                self.speed = d.get('speed') or 0.0
                self.eta = d.get('eta')
                if time.time() - last_log[0] >= 10:
                    last_log[0] = time.time()
                    self._log("[YT-DLP] " + " ".join(filter(None, [
                        self.filename[:40],
                        f"{human_size(done)}" + (f" of ~{human_size(total)}" if total else ""),
                        f"(fragment {frag_i}/{frag_n})" if frag_i and frag_n else None,
                        f"@ {human_size(self.speed)}/s" if self.speed else None])))
            elif d['status'] == 'finished':
                parts_done[0] += 1
                self._log(f"[YT-DLP] Part {parts_done[0]} finished, processing...")

        # '%' has a meaning in yt-dlp templates, so escape it in real file names
        outtmpl = os.path.join(self.save_dir, self.filename).replace('%', '%%')

        ffmpeg = find_ffmpeg()
        chosen = self.extra.get('format')            # quality picked in the extension menu
        if chosen and ('+' not in chosen or ffmpeg):
            fmt = chosen
            self._log(f"[YT-DLP] Using chosen quality (format {chosen})")
        elif ffmpeg:
            fmt = 'bestvideo+bestaudio/best'
        else:
            # Without ffmpeg, yt-dlp can't merge separate video + audio streams
            fmt = 'best[vcodec!=none][acodec!=none]/best'
            self._log("[YT-DLP] ffmpeg not found: downloading best single file with audio. "
                      "Put ffmpeg.exe next to the app for best quality.")

        ydl_opts = {
            'outtmpl': outtmpl,
            'format': fmt,
            'progress_hooks': [progress_hook],
            'quiet': True, 'no_warnings': True, 'noprogress': True,
            'concurrent_fragment_downloads': 8,   # parallel fragments, like IDM's connections
            'socket_timeout': 30,                 # never hang forever on a stalled connection
            'retries': 10, 'fragment_retries': 10,
        }
        limit = int(self.mgr.settings.get('speed_limit_kb', 0))
        if limit > 0:
            ydl_opts['ratelimit'] = limit * 1024
        if ffmpeg:
            ydl_opts['ffmpeg_location'] = ffmpeg
            if self.filename.lower().endswith('.mp4'):
                ydl_opts['merge_output_format'] = 'mp4'
        ydl_opts.update(self.mgr.ytdlp_extra_opts(self.url, self.extra))

        self._log(f"[YT-DLP] Starting: {self.filename}")
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self.url])
            if os.path.isfile(self.output_path):
                self.total_size = os.path.getsize(self.output_path)
            self._log(f"[YT-DLP] SUCCESS: {self.filename}")
            self._finish("Completed")
        except Exception as e:
            if self.is_cancelled:
                self._finish("Cancelled")
            else:
                clean_err = friendly_ytdlp_error(re.sub(r'\x1b\[[0-9;]*m', '', str(e)).replace('ERROR: ', ''))
                self._fail(clean_err)
