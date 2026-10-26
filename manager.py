"""Download manager: the list of downloads, routing, settings and the Chrome extension listener.

No user-interface code lives here. The window (gui_downloader.py) plugs in by setting these
callbacks, which may be called from any thread:
    on_log(text)              a log line
    on_task_added(task)       a download was added to the list
    on_task_removed(task_id)  a download was removed
    on_ask(pending)           a download is ready; show the "Download File Info" dialog
    on_busy(text)             short status text ("Analyzing link...", "")
"""
import os
import re
import sys
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from engine import (
    yt_dlp, BROWSER_UA, DownloadTask, categorize, find_ffmpeg, ytdlp_runtime_opts, friendly_ytdlp_error,
    parse_vimeo_page_url, vimeo_player_url, get_vimeo_playlist_from_config,
)

# Link between this app and the Chrome extension. Both sides must use the same values
# (see extension/background.js). The port only listens on this computer.
EXTENSION_PORT = 9614
EXTENSION_TOKEN = "1922bb3123e141edd854398b611ea8ff"

DEFAULT_SETTINGS = {
    'save_dir': os.path.join(os.path.expanduser("~"), "Downloads"),
    'use_categories': True,         # save into Downloads/Video, Downloads/Music, ...
    'ask_before_download': True,    # show the "Download File Info" dialog
    'close_to_tray': True,
    'notify_complete': True,
    'connections': 16,
    'speed_limit_kb': 0,            # 0 = unlimited
    'cookie_browser': 'None',
    'logging': False,               # show and record the activity log
    'theme': 'system',              # system | light | dark
    'column_widths': [],
    'predownload': True,            # start downloading while the ask dialog is open (like IDM)
}

PROBE_CACHE_SECONDS = 600           # reuse a quality list for 10 minutes

DIRECT_EXTS = ('.mp4', '.mkv', '.webm', '.iso', '.zip', '.exe', '.msi', '.rar', '.mov', '.avi',
               '.7z', '.pdf', '.mp3', '.m4a', '.dmg', '.deb', '.apk', '.tar', '.gz', '.xz', '.img')


@dataclass
class Pending:
    """A download that has been analyzed but not added yet (shown in the ask dialog)."""
    url: str
    filename: str
    task_type: str                    # standard | ytdlp | vimeo_cdn
    size: int = 0
    extra: dict = field(default_factory=dict)
    source: str = ''                  # "Chrome", "Add URL", ...
    quality: str = ''                 # "1080p HD" for videos picked in the menu
    page_url: str = ''
    task: object = None               # the task already downloading in the background, if any


def _noop(*_args):
    pass


def safe_name(name):
    return re.sub(r'[\\/*?:"<>|\r\n\t]', "", name).strip().rstrip('.') or "download"


class DownloadManager:
    def __init__(self, data_dir=None):
        # Settings and the download list live next to the app (the .exe once packaged)
        if data_dir is None:
            data_dir = (os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)
                        else os.path.dirname(os.path.abspath(__file__)))
        self.data_dir = data_dir
        self.state_file = os.path.join(self.data_dir, "downloads_state.json")
        self.settings_file = os.path.join(self.data_dir, "settings.json")
        self.session = requests.Session()
        self.tasks = {}                         # task_id -> DownloadTask (insertion ordered)
        self.task_counter = 0
        self._state_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self.server_ok = False
        self._probe_cache = {}                  # page url -> (time, result)
        self._probe_inflight = {}               # page url -> Event, so duplicate lookups wait
        self._probe_lock = threading.Lock()

        self.on_log = _noop
        self.on_task_added = _noop
        self.on_task_removed = _noop
        self.on_ask = None                      # None = start without asking
        self.on_busy = _noop

        self.settings = dict(DEFAULT_SETTINGS)
        self._load_settings()
        self._load_state()

    # ======================================================================
    # Logging, settings, state
    # ======================================================================
    def log(self, message):
        if not self.settings.get('logging', False):
            return                              # logging switched off in Options
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line)
        self.on_log(line)

    def _load_settings(self):
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            self.settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass

    def save_settings(self, new=None):
        if new:
            self.settings.update({k: v for k, v in new.items() if k in DEFAULT_SETTINGS})
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=2)
        except OSError as e:
            self.log(f"[SETTINGS] Could not save settings: {e}")

    def _load_state(self):
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for tid, st in data.items():
            try:
                task = DownloadTask(self, st['url'], st['save_dir'], st['filename'], tid,
                                    task_type=st.get('task_type', 'standard'), state=st)
            except (KeyError, TypeError):
                continue
            if task.status == "Cancelled":
                continue
            if task.awaiting:
                # The app closed while its dialog was still open: throw the partial file away
                task.remove_files()
                continue
            self.tasks[task.task_id] = task
            try:
                self.task_counter = max(self.task_counter, int(tid))
            except ValueError:
                pass

    def save_state(self):
        with self._state_lock:
            try:
                data = {tid: t.get_state() for tid, t in list(self.tasks.items())}
                tmp = self.state_file + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, self.state_file)
            except OSError:
                pass

    # ======================================================================
    # Where files go
    # ======================================================================
    def folder_for(self, filename, category=None):
        base = self.settings['save_dir']
        if self.settings.get('use_categories'):
            return os.path.join(base, category or categorize(filename))
        return base

    @staticmethod
    def unique_path(path):
        """'file.zip' -> 'file (1).zip' if the name is already taken."""
        if not os.path.exists(path) and not os.path.exists(path + '.aria2'):
            return path
        base, ext = os.path.splitext(path)
        n = 1
        while os.path.exists(f"{base} ({n}){ext}"):
            n += 1
        return f"{base} ({n}){ext}"

    # ======================================================================
    # Adding downloads
    # ======================================================================
    def submit(self, url, extra=None, kind='auto', meta=None, source='', ask=None):
        """Analyze a link in the background, then ask the user (or start right away)."""
        threading.Thread(target=self._submit, args=(url, dict(extra or {}), kind, meta or {}, source, ask),
                         daemon=True).start()

    def _submit(self, url, extra, kind, meta, source, ask):
        self.on_busy(f"Analyzing {url[:60]}...")
        try:
            pending = self.resolve(url, extra, kind, meta)
        except Exception as e:
            self.log(f"[ROUTER] Could not analyze link: {e}")
            pending = None
        self.on_busy("")
        if not pending:
            return
        pending.source = source
        old = self._find_unfinished_copy(pending)
        if old is not None:
            # Same file again (e.g. its link expired and the user downloaded it again in Chrome):
            # give the old download the fresh link and continue from where it stopped.
            old.url, old.extra = pending.url, pending.extra
            old.retryable, old.retry_count = False, 0
            self.log(f"[QUEUE] Continuing {old.display_name} from {old.current_progress}% with the new link")
            self.on_busy(f"Continuing {old.display_name} from {old.current_progress}%")
            old.resume()
            return
        should_ask = self.settings.get('ask_before_download', True) if ask is None else ask
        if should_ask and self.on_ask:
            if self.settings.get('predownload', True):
                # Like IDM: start downloading into a temp folder while the dialog is open
                pending.task = self._create_task(pending, None, awaiting=True)
                pending.task.start()
            self.on_ask(pending)
        else:
            path = self.unique_path(os.path.join(self.folder_for(pending.filename), pending.filename))
            self.add_pending(pending, path, start=True)

    def _find_unfinished_copy(self, pending):
        if pending.task_type != 'standard' or not pending.size:
            return None
        for t in self.tasks.values():
            if (not t.awaiting and t.task_type == 'standard' and t.status in ('Paused', 'Error', 'Queued')
                    and t.total_size == pending.size and t.display_name == pending.filename
                    and t.current_progress > 0):
                return t
        return None

    # ======================================================================
    # Auto-resume after the network drops
    # ======================================================================
    def start_watchdog(self):
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self):
        while True:
            time.sleep(5)
            now = time.time()
            for t in list(self.tasks.values()):
                if t.status != 'Error' or not t.retryable or now < t.next_retry:
                    continue
                if self._reachable(t.url):
                    t.retry_count += 1
                    self.log(f"[RETRY] Back online, continuing {t.display_name} (attempt {t.retry_count})")
                    t.resume()
                else:
                    t.next_retry = now + min(60, 10 * (t.retry_count + 1))   # check again soon

    @staticmethod
    def _reachable(url):
        """Can we open a connection to the download's server right now?"""
        import socket
        p = urllib.parse.urlparse(url)
        port = p.port or (443 if p.scheme == 'https' else 80)
        try:
            with socket.create_connection((p.hostname, port), timeout=4):
                return True
        except OSError:
            return False

    def temp_dir(self):
        """Hidden folder inside the download folder (same drive, so the final move is instant)."""
        d = os.path.join(self.settings['save_dir'], '.MyDownloader-temp')
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            if sys.platform.startswith('win'):
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetFileAttributesW(d, 0x02)   # hidden
                except Exception:
                    pass
        return d

    def _create_task(self, pending, path, awaiting=False):
        # Several downloads can arrive at the same moment (from different threads),
        # so hand out IDs under a lock; otherwise two downloads could share one ID.
        with self._id_lock:
            self.task_counter += 1
            task_id = self.task_counter
        if awaiting:
            save_dir, filename = self.temp_dir(), f"{task_id}_{pending.filename}"
        else:
            save_dir, filename = os.path.split(path)
        task = DownloadTask(self, pending.url, save_dir, filename, task_id,
                            task_type=pending.task_type, total_size=pending.size, extra=pending.extra)
        task.awaiting = awaiting
        self.tasks[task.task_id] = task
        return task

    def add_pending(self, pending, path, start=True):
        """The user approved the download (or asking is off): put it in the list."""
        task = pending.task
        if task is not None and task.task_id in self.tasks and start:
            task.confirm(path, start=True)           # already downloading in the temp folder
            if task.status in ("Paused", "Queued", "Error"):
                task.resume()
        else:
            if task is not None:
                # "Download Later": delete what was pre-downloaded and add it fresh, not started
                self.discard_pending(pending, quiet=True)
            task = self._create_task(pending, path)
            if start:
                task.start()
        self.on_task_added(task)
        self.log(f"[QUEUE] Added: {os.path.basename(path)}" + ("" if start else " (download later)"))
        self.save_state()
        return task

    def discard_pending(self, pending, quiet=False):
        """Stop the background (pre-)download and delete its data (Cancel or Download Later)."""
        task = pending.task
        if task is None:
            return
        pending.task = None
        self.tasks.pop(task.task_id, None)
        task.cancel()

        def remove(tries=[0]):
            left = task.remove_files()
            tries[0] += 1
            if left and tries[0] < 5:
                threading.Timer(1.0, remove).start()
        threading.Timer(0.5, remove).start()
        if not quiet:
            self.log(f"[QUEUE] Cancelled: {pending.filename}")
        self.save_state()

    def resolve(self, url, extra, kind='auto', meta=None):
        """Work out how to download a link. Returns a Pending, or None (reason is logged)."""
        meta = meta or {}
        if kind == 'ytdlp':                    # quality picked in the video menu
            fmt = str(meta.get('format') or '')
            if fmt:
                extra['format'] = fmt
            ext = re.sub(r'[^a-z0-9]', '', str(meta.get('ext') or 'mp4').lower()) or 'mp4'
            title = safe_name(str(meta.get('title') or 'video'))
            quality = re.sub(r'[\\/*?:"<>|()]', '', str(meta.get('suffix') or '')).strip()
            filename = f"{title} ({quality}).{ext}" if quality else f"{title}.{ext}"
            return Pending(url, filename, 'ytdlp', extra=extra,
                           quality=str(meta.get('label') or quality or 'Best quality'),
                           page_url=extra.get('referer', ''))
        if kind == 'file':                     # a download Chrome started
            extra['filename'] = os.path.basename(str(meta.get('filename') or ''))
            return self._resolve_file(url, extra)
        return self._route(url, extra)

    def _route(self, url, extra):
        low = url.lower()
        # 1. Vimeo CDN playlist links: native parser
        if 'vimeocdn.com' in low or 'playlist.json' in low:
            self.log("[ROUTER] Vimeo CDN link detected. Using native parser.")
            return Pending(url, f"Vimeo video {self.task_counter + 1}.mp4", 'vimeo_cdn', extra=extra)

        # 2. Direct file links
        if urllib.parse.urlparse(url).path.lower().endswith(DIRECT_EXTS):
            return self._resolve_file(url, extra)

        # 3. Vimeo pages
        video_id, vhash = parse_vimeo_page_url(url)
        if video_id:
            self.log(f"[ROUTER] Vimeo page detected (id {video_id}).")
            p = self._try_ytdlp(vimeo_player_url(video_id, vhash), extra)
            if p:
                return p
            playlist_url, title = get_vimeo_playlist_from_config(
                video_id, vhash, self.session, extra.get('referer', ''), self.log)
            if playlist_url:
                return Pending(playlist_url, safe_name(title) + ".mp4", 'vimeo_cdn', extra=extra)
            self.log("[ROUTER] Could not get this Vimeo video. Play it in Chrome and use the "
                     "Download this video button instead.")
            return None

        # 4. Other pages yt-dlp understands (YouTube, etc.)
        p = self._try_ytdlp(url, extra)
        if p:
            return p

        # 5. Treat it as a plain file
        return self._resolve_file(url, extra)

    def _try_ytdlp(self, url, extra):
        if yt_dlp is None:
            return None
        self.log(f"[ROUTER] Checking with yt-dlp: {url[:100]}")
        opts = {'quiet': True, 'no_warnings': True, 'socket_timeout': 20, 'noplaylist': True}
        opts.update(self.ytdlp_extra_opts(url, extra))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            err = friendly_ytdlp_error(re.sub(r'\x1b\[[0-9;]*m', '', str(e)).replace('ERROR: ', ''))
            if 'Unsupported URL' not in err:
                self.log(f"[ROUTER] yt-dlp: {err[:200]}")
            if 'cookie database' in err.lower():
                self.log("[ROUTER] Hint: Chrome locks its cookie file while open. In Options, set "
                         "'Cookies from browser' to None (the extension sends cookies itself).")
            return None
        if info.get('entries'):
            entries = [e for e in info['entries'] if e]
            if not entries:
                return None
            info = entries[0]
        size = info.get('filesize') or info.get('filesize_approx') or 0
        return Pending(url, safe_name(info.get('title') or 'video') + '.mp4', 'ytdlp',
                       size=size, extra=extra, quality='Best quality')

    def _resolve_file(self, url, extra):
        extra = dict(extra)
        hint = extra.pop('filename', '')           # name Chrome suggested, if any
        try:
            res = self.session.get(url, headers=self.get_headers(url, extra), stream=True, timeout=15)
            res.close()
        except Exception as e:
            self.log(f"[HTTP] Could not reach {url[:100]}: {e}")
            return None
        if res.status_code >= 400:
            self.log(f"[HTTP] Server returned HTTP {res.status_code} for {url[:100]}")
            return None
        final_url = res.url or url
        total_size = int(res.headers.get('content-length', 0) or 0)
        content_type = res.headers.get('content-type', '')
        if 'text/html' in content_type.lower():
            self.log(f"[HTTP] {url[:100]} is a web page, not a file. Nothing to download.")
            return None
        if final_url != url:
            self.log(f"[HTTP] Redirected to: {final_url[:120]}")

        filename = None
        cd = res.headers.get('content-disposition', '')
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
        if m:
            filename = urllib.parse.unquote(m.group(1).strip().strip('"'))
        elif 'filename=' in cd:
            fm = re.findall(r'filename=["\']?([^"\';]+)["\']?', cd)
            if fm:
                filename = fm[0]
        # Script names (download.php, get.aspx) are never the real file name
        script = re.compile(r'\.(php\d?|aspx?|jsp|cgi|pl|html?)$', re.I)
        path_name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(final_url).path))
        if not filename and path_name and '.' in path_name and not script.search(path_name):
            filename = path_name
        if not filename and hint and '.' in hint and not script.search(hint):
            filename = hint
        if not filename:
            filename = path_name if path_name and '.' in path_name else 'downloaded_file.bin'
        return Pending(final_url, safe_name(filename), 'standard', size=total_size, extra=extra)

    # ======================================================================
    # Controls
    # ======================================================================
    def pause(self, task_id):
        t = self.tasks.get(task_id)
        if t:
            t.pause()

    def resume(self, task_id):
        t = self.tasks.get(task_id)
        if t:
            t.resume()

    def pause_all(self):
        for t in list(self.tasks.values()):
            if t.status in ("Downloading", "Queued"):
                t.pause()

    def delete(self, task_id, delete_file=False):
        """Remove a download from the list; with delete_file, also delete it from disk.
        Returns the paths that could not be deleted."""
        t = self.tasks.pop(task_id, None)
        if not t:
            return []
        was_running = t.status in ("Downloading", "Queued")
        if t.status in ("Downloading", "Paused", "Queued", "Error"):
            t.cancel()
        failed = []
        if delete_file:
            if was_running:
                # aria2c / yt-dlp need a moment to let go of the file; try a few times
                def later(tries=[0]):
                    left = t.remove_files()
                    tries[0] += 1
                    if left and tries[0] < 5:
                        threading.Timer(1.0, later).start()
                    elif left:
                        self.log(f"[DELETE] Could not delete: {left}")
                threading.Timer(1.0, later).start()
            else:
                failed = t.remove_files()
            self.log(f"[DELETE] Deleted {t.display_name} from disk")
        else:
            self.log(f"[DELETE] Removed {t.display_name} from the list (file kept)")
        self.on_task_removed(task_id)
        self.save_state()
        return failed

    def shutdown(self):
        """Stop running downloads cleanly; they show as Paused next time and resume."""
        for t in list(self.tasks.values()):
            if t.status == "Downloading":
                t.pause()
        self.save_state()

    # ======================================================================
    # HTTP headers / yt-dlp options
    # ======================================================================
    def get_headers(self, url, extra=None):
        """HTTP headers for a download. Values sent by the extension come first."""
        extra = extra or {}
        parsed = urllib.parse.urlparse(url)
        headers = {
            'User-Agent': BROWSER_UA,
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
            'Referer': extra.get('referer') or f"{parsed.scheme}://{parsed.netloc}/",
        }
        if extra.get('cookie'):
            headers['Cookie'] = extra['cookie']
        return headers

    def ytdlp_extra_opts(self, url, extra=None):
        """Options shared by extraction and download: JS runtime (YouTube), cookies, referer."""
        extra = extra or {}
        opts = ytdlp_runtime_opts()
        cookie = extra.get('cookie')
        browser = self.settings.get('cookie_browser', 'None')
        if browser and browser != 'None' and not cookie:
            opts['cookiesfrombrowser'] = (browser.lower(),)
        referer = extra.get('referer') or ''
        if 'vimeo.com' in url.lower():
            referer = referer or 'https://vimeo.com/'
        headers = {}
        if referer:
            headers['Referer'] = referer
        if cookie:
            headers['Cookie'] = cookie
        if headers:
            opts['http_headers'] = headers
        return opts

    # ======================================================================
    # Quality list for the extension's video menu
    # ======================================================================
    def probe_formats(self, data):
        """Quality list for a page, cached for 10 minutes. The extension asks early (when the
        video starts playing), so by the time the user clicks the pill it is usually ready.
        If a lookup for the same page is already running, wait for it instead of starting another."""
        key = str(data.get('page_url', '')).split('#')[0]
        with self._probe_lock:
            hit = self._probe_cache.get(key)
            if hit and time.time() - hit[0] < PROBE_CACHE_SECONDS:
                return hit[1]
            waiter = self._probe_inflight.get(key)
            if waiter is None:
                waiter = threading.Event()
                self._probe_inflight[key] = waiter
                owner = True
            else:
                owner = False
        if not owner:
            waiter.wait(60)
            hit = self._probe_cache.get(key)
            return hit[1] if hit else {'ok': False, 'error': 'Lookup timed out', 'formats': []}
        try:
            result = self._probe_formats(data)
            if result.get('ok'):
                with self._probe_lock:
                    self._probe_cache[key] = (time.time(), result)
                    if len(self._probe_cache) > 200:          # keep memory small
                        oldest = min(self._probe_cache, key=lambda k: self._probe_cache[k][0])
                        self._probe_cache.pop(oldest, None)
            else:
                with self._probe_lock:
                    self._probe_cache[key] = (time.time() - PROBE_CACHE_SECONDS + 5, result)  # short-lived
            return result
        finally:
            with self._probe_lock:
                self._probe_inflight.pop(key, None)
            waiter.set()

    def _probe_formats(self, data):
        """Ask yt-dlp which qualities exist. Tries the page first, then streams Chrome detected."""
        if yt_dlp is None:
            return {'ok': False, 'error': 'yt-dlp is not installed', 'formats': []}
        page = str(data.get('page_url', '')).strip()
        extra = {'referer': str(data.get('referer') or page), 'cookie': str(data.get('cookie') or '')}
        candidates = []
        vid, vhash = parse_vimeo_page_url(page)
        if vid:
            candidates.append(vimeo_player_url(vid, vhash))   # vimeo.com pages need login; player doesn't
        elif page.startswith(('http://', 'https://')):
            candidates.append(page)
        for s in data.get('streams') or []:
            if isinstance(s, str) and s.startswith(('http://', 'https://')) and s not in candidates:
                candidates.append(s)

        last_error = "No video found on this page."
        for url in candidates[:4]:
            self.log(f"[EXTENSION] Looking up qualities: {url[:100]}")
            opts = {'quiet': True, 'no_warnings': True, 'socket_timeout': 20, 'noplaylist': True}
            opts.update(self.ytdlp_extra_opts(url, extra))
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e:
                last_error = friendly_ytdlp_error(
                    re.sub(r'\x1b\[[0-9;]*m', '', str(e)).replace('ERROR: ', ''))[:200]
                continue
            if info.get('entries'):
                entries = [e for e in info['entries'] if e]
                if not entries:
                    continue
                info = entries[0]
            formats = self._summarize_formats(info)
            if formats:
                title = safe_name(info.get('title') or 'video')
                self.log(f"[EXTENSION] Found {len(formats)} options for '{title}'")
                return {'ok': True, 'title': title, 'source_url': url,
                        'duration': info.get('duration'), 'formats': formats}
        return {'ok': False, 'error': last_error, 'formats': []}

    @staticmethod
    def _summarize_formats(info):
        """Turn yt-dlp's raw format list into one clean menu entry per resolution."""
        has_ffmpeg = bool(find_ffmpeg())
        duration = info.get('duration') or 0
        best = {}
        for f in info.get('formats') or [info]:
            if f.get('vcodec') == 'none' or not f.get('height'):
                continue
            has_audio = f.get('acodec') != 'none'               # None = unknown, usually muxed
            if not has_audio and not has_ffmpeg:
                continue
            h = f['height']
            key = (has_audio and not has_ffmpeg, f.get('tbr') or 0)
            if h not in best or key > best[h][0]:
                best[h] = (key, f, has_audio)

        def size_text(f):
            size = f.get('filesize') or f.get('filesize_approx')
            if not size and f.get('tbr') and duration:
                size = f['tbr'] * 125 * duration
            return f"~{size / 1048576:.0f} MB" if size else ""

        out = [{'label': 'Best quality (auto)', 'format': '', 'ext': 'mp4', 'suffix': '',
                'detail': 'Highest video + audio'}]
        for h in sorted(best, reverse=True):
            _, f, has_audio = best[h]
            fid = f['format_id']
            sel = fid if has_audio else f"{fid}+bestaudio[ext=m4a]/{fid}+bestaudio"
            ext = 'mp4' if not has_audio else (f.get('ext') or 'mp4')
            if ext in ('m3u8', 'mpd', 'unknown_video'):
                ext = 'mp4'
            fps = f.get('fps')
            label = f"{h}p" + (f"{int(fps)}" if fps and fps > 30 else "") + (" HD" if h >= 720 else "")
            out.append({'label': label, 'format': sel, 'ext': ext, 'suffix': f"{h}p",
                        'detail': " · ".join(filter(None, [ext.upper(), size_text(f)]))})
        audio = [f for f in info.get('formats') or [] if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
        if audio:
            out.append({'label': 'Audio only', 'format': 'bestaudio[ext=m4a]/bestaudio', 'ext': 'm4a',
                        'suffix': 'audio', 'detail': 'M4A'})
        return out if len(out) > 1 else []

    # ======================================================================
    # Chrome extension listener (127.0.0.1 only)
    # ======================================================================
    def start_extension_server(self):
        mgr = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                return self.headers.get('X-Token') == EXTENSION_TOKEN

            def do_GET(self):
                if self.path == '/ping' and self._authorized():
                    self._reply(200, {'ok': True, 'app': 'MyDownloader'})
                else:
                    self._reply(403, {'ok': False})

            def do_POST(self):
                if self.path not in ('/download', '/formats') or not self._authorized():
                    self._reply(403, {'ok': False, 'error': 'forbidden'})
                    return
                try:
                    length = min(int(self.headers.get('Content-Length', 0)), 1_000_000)
                    data = json.loads(self.rfile.read(length) or b'{}')
                    if not isinstance(data, dict):
                        raise ValueError
                except Exception:
                    self._reply(400, {'ok': False, 'error': 'bad json'})
                    return

                if self.path == '/formats':
                    try:
                        self._reply(200, mgr.probe_formats(data))
                    except Exception as e:
                        self._reply(200, {'ok': False, 'error': str(e)[:200], 'formats': []})
                    return

                url = str(data.get('url', '')).strip()
                if not url.startswith(('http://', 'https://')):
                    self._reply(400, {'ok': False, 'error': 'url must be http(s)'})
                    return
                kind = str(data.get('kind') or 'auto')
                extra = {'referer': str(data.get('referer') or '').strip(),
                         'cookie': str(data.get('cookie') or '').strip()}
                what = {'file': 'captured download', 'ytdlp': 'video'}.get(kind, 'link')
                mgr.log(f"[EXTENSION] Received {what} from Chrome: {url[:120]}")
                mgr.submit(url, extra, kind=kind, meta=data, source='Chrome')
                self._reply(200, {'ok': True})

            def log_message(self, *args):
                pass

        try:
            server = ThreadingHTTPServer(('127.0.0.1', EXTENSION_PORT), Handler)
        except OSError as e:
            self.log(f"[EXTENSION] Could not listen on port {EXTENSION_PORT} (is the app already open?): {e}")
            self.server_ok = False
            return False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.server_ok = True
        self.log(f"[EXTENSION] Listening for Chrome on 127.0.0.1:{EXTENSION_PORT}")
        return True
