#!/usr/bin/env python3
"""sonora - download songs/albums with lyrics, then play them in a customizable TUI.

Needs: python 3.11+, mpv, ffmpeg, and `pip install textual yt-dlp`.
Config: ~/.config/sonora/config.toml (created on first run).
"""
import array, html as htmllib, json, math, threading, os, random, re, shutil, socket, subprocess, sys, tempfile, time, tomllib
import urllib.parse, urllib.request
from pathlib import Path
from string import Template

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, OptionList, RichLog, Static, Tab, TabbedContent, TabPane, Tabs
from textual.widgets.option_list import Option

# ───────────────────────────── config ─────────────────────────────
IS_WIN = os.name == "nt"
CONFIG_BASE = Path(os.environ.get("APPDATA", Path.home())) if IS_WIN else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_FILE = CONFIG_BASE / "sonora" / "config.toml"

DEFAULT_TOML = """\
music_dir = "~/Music/Sonora"
audio_format = "mp3"          # mp3, m4a, opus, flac
lyrics_sources = ["lrclib", "azlyrics", "genius"]   # tried in order, first hit wins

[theme]                         # edit here, or live in the Settings tab (F3)
background = "#130b22"          # deep violet
panel = "#1b1130"
text = "#e8dcff"                # soft lilac
accent = "#b57bff"              # electric violet
accent2 = "#f0abfc"             # orchid highlight (gradients, lyrics, twinkles)
line = "#3a2763"                # idle panel borders
dim = "#7a679e"                 # muted purple
cover_chars = " .:-=+*#%@"      # ASCII cover ramp, dark -> light

[player]
lyric_window = 3               # lyric lines shown at once
volume = 80
cover_mode = "album"           # "album" = the song cover, "star" = rotating ASCII star (key: c)
cover_style = "braille"        # braille (sharpest) | blocks (color) | ascii
cover_cols = 120               # max cover width in terminal cells
stars = true                   # twinkling star decorations
splash = true                  # rotating star -> SONORA logo when opening, and the same in reverse when quitting
lift = 1                       # lines to raise the song name + lyrics from the bottom

[keys]                         # Textual key names
play_pause = "p"
next = "n"
previous = "b"
seek_back = "comma"
seek_forward = "full_stop"
volume_down = "1"
volume_up = "2"
shuffle = "m"
repeat = "r"
tab_player = "f1"
tab_download = "f2"
tab_settings = "f3"           # colour settings tab
lyrics = "l"                   # re-fetch lyrics for the selected song
filter = "slash"               # filter the song list
cover_toggle = "c"             # album cover <-> rotating star
view_next = "v"                # collection view: all songs -> favorites -> playlists -> queue
queue_add = "w"                # add song / playlist to "up next" (plays after the current song)
play_all = "P"                 # play everything shown (or the selected playlist) from the top
move_up = "shift+up"           # reorder a song inside a playlist / favorites
move_down = "shift+down"
favorite = "f"                 # heart / un-heart the selected (or playing) song
sort = "s"                     # cycle song sorting: file order, title, artist, album, recently added
delete_file = "D"              # delete the selected song FROM DISK (asks first)
update_ytdlp = "U"            # download page: update yt-dlp (fixes "downloads fail")
lyrics_menu = "L"              # pick another lyrics source, search again, load a file, remove
new_playlist = "ctrl+n"
add_to_playlist = "a"          # add the selected song (or whole album) to a playlist
edit = "e"                     # fix album/artist of a song or album; rename a playlist
delete = "d"                   # remove song from playlist / delete playlist (never deletes files)
back = "backspace"             # leave a playlist/album (Esc works too)
help = "question_mark"          # open / close the Help tab (lists every key)
quit = "q"
"""


def merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(DEFAULT_TOML, encoding="utf-8")
    cfg = tomllib.loads(DEFAULT_TOML)
    return merge(cfg, tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8")))


CFG = load_config()
THEME = CFG["theme"]

COLOR_KEYS = ("background", "panel", "text", "accent", "accent2", "line", "dim")
SLOT_INFO = {"background": "page background", "panel": "bars / hover", "text": "main text", "accent": "highlights",
             "accent2": "lyrics, gradients", "line": "idle borders", "dim": "muted text"}
PRESETS = {
    "Sonora Purple": dict(background="#130b22", panel="#1b1130", text="#e8dcff", accent="#b57bff", accent2="#f0abfc", line="#3a2763", dim="#7a679e"),
    "Midnight Blue": dict(background="#0b1220", panel="#111a2e", text="#dbe7ff", accent="#6ea8ff", accent2="#9be7ff", line="#1f3358", dim="#5f7aa8"),
    "Rose":          dict(background="#1f0d17", panel="#2a1220", text="#ffe4ee", accent="#ff7eb6", accent2="#ffc2dd", line="#4d2339", dim="#a06883"),
    "Emerald":       dict(background="#08170f", panel="#0e2217", text="#dcfce9", accent="#4ade80", accent2="#a7f3d0", line="#1c4630", dim="#5c9477"),
    "Sunset":        dict(background="#1c0f0a", panel="#26150e", text="#ffeadb", accent="#ff9a4d", accent2="#ffd27a", line="#4a2a1a", dim="#a8785c"),
    "Grape Soda":    dict(background="#14091f", panel="#1d0f2e", text="#f3e8ff", accent="#a855f7", accent2="#22d3ee", line="#3b1d5e", dim="#8b6bb0"),
    "Mono":          dict(background="#0e0e10", panel="#161618", text="#e6e6e6", accent="#bdbdbd", accent2="#ffffff", line="#2e2e32", dim="#777777"),
}


def norm_hex(s):
    """'#B57BFF' / 'b57bff' / '#fa0' -> '#b57bff' / '#ffaa00'; None if not a colour."""
    s = s.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", s):
        s = "".join(c * 2 for c in s)
    return "#" + s.lower() if re.fullmatch(r"[0-9a-fA-F]{6}", s) else None


def save_theme(theme):
    """Write the [theme] block into config.toml, leaving every other setting untouched."""
    block = "[theme]\n" + "".join(f'{k} = "{theme[k]}"\n' for k in COLOR_KEYS) + f"cover_chars = {json.dumps(theme['cover_chars'])}\n"
    text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    if re.search(r"(?m)^\[theme\]", text):
        text = re.sub(r"(?ms)^\[theme\][^\n]*\n.*?(?=^\[|\Z)", lambda m: block + "\n", text, count=1)
    else:
        text = text.rstrip() + "\n\n" + block
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(text, encoding="utf-8")

CSS = Template("""
Screen, TabbedContent, TabPane, Tabs, Tab, Input, DataTable, OptionList, RichLog, Button,
#listpanel, #rightpanel, #progress, #searchpanel, #linkpanel, #logpanel, #presetpanel, #slotpanel { background: ${background}; }
* { scrollbar-background: ${panel}; scrollbar-background-hover: ${panel}; scrollbar-background-active: ${panel};
    scrollbar-color: ${line}; scrollbar-color-hover: ${accent}; scrollbar-color-active: ${accent2}; scrollbar-corner-color: ${panel}; }
OptionList { color: ${text}; }
Screen { color: ${text}; }
Tab.-active { color: ${accent2}; text-style: bold; }
Tabs { border-bottom: none; }
Tabs > #tabs-active-underline, Underline > .underline--bar { color: ${accent}; background: ${line}; }
TabPane { padding: 0; }
Input { border: tall ${line}; }
Input:focus { border: tall ${accent}; }
#main { height: 1fr; }
#listpanel { width: 55%; height: 1fr; border: round ${line}; }
#listpanel:focus-within { border: round ${accent}; }
DataTable { height: 1fr; border: none; }
DataTable > .datatable--header { background: ${background}; color: ${accent}; text-style: bold; }
DataTable > .datatable--cursor { background: ${accent}; color: ${background}; text-style: bold; }
DataTable > .datatable--hover { background: ${panel}; }
OptionList > .option-list--option-highlighted { background: ${accent}; color: ${background}; }
OptionList > .option-list--option-hover { background: ${panel}; }
#rightpanel { width: 45%; height: 1fr; border: round ${line}; padding: 0 1; }
#stars_top { width: 100%; height: 2; }
#stars_bot { width: 100%; height: 1; }
#cover { width: 100%; height: 1fr; content-align: center middle; text-align: center; }
#title { width: 100%; height: 1; text-align: center; text-style: bold; }
#lyrics { width: 100%; height: 3; margin-top: 1; margin-bottom: ${lift}; text-align: center; }
#bottom { height: 5; }
#progress { width: 1fr; height: 5; border: round ${line}; padding: 0 1; }
#seek { width: 100%; height: 2; }
#times { width: 100%; height: 1; color: ${dim}; }
#controls { width: 40; height: 5; }
.btnrow { height: 3; }
Button { width: 1fr; min-width: 9; height: 3; color: ${text}; border: round ${line}; }
Button:hover { border: round ${accent2}; color: ${accent2}; background: ${panel}; }
Button:focus { border: round ${accent}; text-style: bold; }
#volume { width: 100%; height: 1; margin-left: 1; }
OptionList { height: 1fr; border: none; }
RichLog { height: 1fr; border: none; }
#dl_top { height: 2fr; }
#searchpanel { width: 50%; height: 1fr; border: round ${line}; }
#linkpanel { width: 50%; height: 1fr; border: round ${line}; padding: 0 1; }
#logpanel { height: 1fr; border: round ${line}; padding: 0 1; }
#searchpanel:focus-within, #linkpanel:focus-within, #logpanel:focus-within { border: round ${accent}; }
#stars_dl { width: 100%; height: 1; }
#dl_hint { width: 100%; height: 1; color: ${dim}; }
#dl_status { width: 100%; height: 1; margin-top: 1; }
#dl_bar { width: 100%; height: 1; }
#dlstar { width: 100%; height: 1fr; content-align: center middle; text-align: center; }
#set_top { height: 1fr; }
#presetpanel { width: 40%; height: 1fr; border: round ${line}; }
#slotpanel { width: 60%; height: 1fr; border: round ${line}; padding: 0 1; overflow-y: auto; }
#presetpanel:focus-within, #slotpanel:focus-within { border: round ${accent}; }
#slots { height: 7; }
#hexlabel { width: 100%; height: 1; margin-top: 1; color: ${dim}; }
#set_status { width: 100%; height: 1; margin-top: 1; color: ${dim}; }
PromptScreen, PickScreen { align: center middle; background: ${background} 70%; }
#dlg { width: 64; max-width: 90%; height: auto; max-height: 90%; border: round ${accent}; background: ${panel}; padding: 1 2; }
#dlg_title { width: 100%; color: ${accent2}; text-style: bold; }
.dlg_label { width: 100%; color: ${dim}; margin-top: 1; }
#pick_list { height: auto; max-height: 16; margin-top: 1; background: ${panel}; }
SplashScreen { background: ${background}; }
#splash { width: 100%; height: 100%; background: ${background}; }
#tools_label { width: 100%; height: 1; margin-top: 1; color: ${dim}; }
#tool_status { width: 100%; height: 1; color: ${dim}; }
#helppanel { height: 1fr; border: round ${line}; padding: 1 2; }
#helptext { width: 100%; height: auto; }
""").substitute(**{k: f"$sn-{k}" for k in COLOR_KEYS}, lift=CFG["player"]["lift"])

# ───────────────────────────── lyrics ─────────────────────────────
UA = {"User-Agent": "sonora/0.1"}


def http_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
        return json.load(r)


def clean_title(t):
    return re.sub(r"\s*[\(\[][^)\]]*(official|video|audio|lyric|visuali[sz]er|hd|4k)[^)\]]*[\)\]]", "", t, flags=re.I).strip()


def fetch_lrclib(artist, title, album=None, duration=None):
    """Synced lyrics from LRCLIB (falls back to plain lyrics)."""
    q = {"artist_name": artist, "track_name": title}
    if album:
        q["album_name"] = album
    if duration:
        q["duration"] = int(duration)
    try:
        d = http_json("https://lrclib.net/api/get?" + urllib.parse.urlencode(q))
    except Exception:
        d = None
    if not d:
        try:
            res = http_json("https://lrclib.net/api/search?" + urllib.parse.urlencode(
                {"artist_name": artist, "track_name": title}))
            d = next((r for r in res if r.get("syncedLyrics")), res[0] if res else None)
        except Exception:
            d = None
    return (d.get("syncedLyrics") or d.get("plainLyrics")) if d else None


BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
              "Accept-Language": "en-US,en;q=0.9"}


def fetch_azlyrics(artist, title, album=None, duration=None):
    """Plain lyrics from AZLyrics. Builds the song URL directly: /lyrics/<artist>/<title>.html"""
    title = re.sub(r"^" + re.escape(artist) + r"\s*-\s*", "", title, flags=re.I)   # "Artist - Song"
    title = re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s", title, flags=re.I)[0]
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title)                             # (feat. x), [remix]
    a = re.sub(r"[^a-z0-9]", "", re.sub(r"^the\s+", "", artist.lower()))
    t = re.sub(r"[^a-z0-9]", "", title.lower())
    if not a or not t:
        return None
    time.sleep(2)  # be polite: AZLyrics blocks fast scrapers
    try:
        req = urllib.request.Request(f"https://www.azlyrics.com/lyrics/{a}/{t}.html", headers=BROWSER_UA)
        page = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
    except Exception:
        return None
    m = re.search(r"Usage of azlyrics\.com content.*?-->\s*(.*?)</div>", page, re.S)
    if not m:
        return None
    text = re.sub(r"<br\s*/?>", "\n", m.group(1))
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)  # collapse blank-line runs
    return text or None


def _div_end(page, start):
    """Index just past the </div> that closes the <div> opening at `start`."""
    depth = 0
    for m in re.finditer(r"<div\b|</div>", page[start:]):
        depth += 1 if m.group(0) != "</div>" else -1
        if depth == 0:
            return start + m.end()
    return len(page)


def _genius_slug(artist, title):
    """'Hideki Naganuma', "Ain't Nothing Like a Funky Beat" -> Hideki-naganuma-aint-nothing-like-a-funky-beat"""
    raw = f"{artist} {title}".replace("&", " and ")
    raw = re.sub(r"[\'\u2019\"]", "", raw)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-")
    return slug[:1].upper() + slug[1:].lower()


def _genius_page_lyrics(url):
    """Download a genius.com song page and pull the lyrics out of its lyrics containers."""
    req = urllib.request.Request(url, headers=BROWSER_UA)
    page = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
    parts = []
    for m in re.finditer(r'<div[^>]*data-lyrics-container="true"[^>]*>', page):
        block = page[m.start():_div_end(page, m.start())]
        block = block[m.end() - m.start():-len("</div>")]
        while True:                                   # drop "contributors / read more" boxes
            x = re.search(r'<div[^>]*data-exclude-from-selection="true"[^>]*>', block)
            if not x:
                break
            block = block[:x.start()] + block[_div_end(block, x.start()):]
        block = re.sub(r"<br\s*/?>", "\n", block)
        parts.append(htmllib.unescape(re.sub(r"<[^>]+>", "", block)).strip())
    text = "\n\n".join(p for p in parts if p)
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text).strip() or None


def fetch_genius(artist, title, album=None, duration=None):
    """Plain lyrics from Genius: tries /Artist-Song-lyrics directly, then Genius' search."""
    title = re.sub(r"^" + re.escape(artist) + r"\s*-\s*", "", title, flags=re.I)
    title = re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s", title, flags=re.I)[0]
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()
    slug = _genius_slug(artist, title)
    if slug:
        try:
            text = _genius_page_lyrics(f"https://genius.com/{slug}-lyrics")
            if text:
                return text
        except Exception:
            pass
    try:                                              # fallback: ask Genius' own search for the right page
        res = http_json("https://genius.com/api/search/multi?" + urllib.parse.urlencode({"q": f"{artist} {title}"}))
        for sec in res["response"]["sections"]:
            for hit in sec.get("hits", []):
                if hit.get("type") == "song":
                    return _genius_page_lyrics(hit["result"]["url"])
    except Exception:
        pass
    return None


SOURCES = {"lrclib": fetch_lrclib, "azlyrics": fetch_azlyrics, "genius": fetch_genius}


def fetch_lyrics(artist, title, album=None, duration=None, sources=("lrclib", "azlyrics", "genius")):
    """Try each source in order. Returns (text, source_name) or (None, None)."""
    for name in sources:
        fn = SOURCES.get(name)
        if fn:
            text = fn(artist, title, album, duration)
            if text:
                return text, name
    return None, None


def save_lyrics(path, text):
    """Timestamped lyrics -> .lrc (synced in the player), plain -> .txt. Returns the file extension."""
    ext = ".lrc" if re.search(r"^\[\d+:\d+", text, re.M) else ".txt"
    for old in (".lrc", ".txt"):
        path.with_suffix(old).unlink(missing_ok=True)
    path.with_suffix(ext).write_text(text, encoding="utf-8")
    return ext


def parse_lrc(text):
    out = []
    for line in text.splitlines():
        words = re.sub(r"\[[^\]]*\]", "", line).strip()
        for m, s in re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line):
            out.append((int(m) * 60 + float(s), words))
    return sorted(out)


# ───────────────────────────── downloading ─────────────────────────────
def search(query, n=8):
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True}) as y:
        r = y.extract_info(f"ytsearch{n}:{query}", download=False)
    return [(e["title"], e.get("uploader") or e.get("channel") or "", e["url"]) for e in r["entries"] if e]


class DLState:
    """Progress of the current download, written by the worker thread and read by the UI timer."""
    def __init__(self):
        self.done, self.total, self.now, self.pct, self.note = 0, 0, "", 0.0, ""
        self.skipped = 0
        self.cancel = threading.Event()


def run_download(target, cfg, log, state, playlist=False, have=None):
    """Download a song / album / playlist URL, embed tags + cover, save lyrics next to each file."""
    import yt_dlp
    root = Path(cfg["music_dir"]).expanduser()

    def on_pp(d):
        if d["status"] != "finished" or d["postprocessor"] != "MoveFiles":
            return
        info = d["info_dict"]
        path = Path(info.get("filepath") or info.get("_filename") or "")
        artists = info.get("artists") or [info.get("artist") or info.get("uploader") or ""]
        artist = artists[0].replace(" - Topic", "").strip()
        title = clean_title(info.get("track") or info.get("title") or path.stem)
        text, src = fetch_lyrics(artist, title, info.get("album"), info.get("duration"), cfg["lyrics_sources"])
        tag = f"lyrics{save_lyrics(path, text)} via {src}" if text else "no lyrics"
        log(f"✓ {artist} — {title}  [{tag}]")
        state.done += 1
        state.pct = 0.0

    def on_prog(d):
        if state.cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        info = d.get("info_dict") or {}
        state.now = info.get("title") or state.now
        state.total = info.get("n_entries") or info.get("playlist_count") or state.total or 1
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate")
            state.pct = d.get("downloaded_bytes", 0) / tot * 100 if tot else 0.0

    def skip_known(info, *, incomplete=False):
        """match_filter: leave out songs we already have (albums/playlists only)."""
        if incomplete or not have or info.get("_type") == "playlist":
            return None
        title = info.get("track") or info.get("title") or ""
        who = (info.get("artists") or [info.get("artist") or info.get("uploader") or ""])[0]
        if have(title, who) or have(f"{who} - {title}", who):
            state.skipped += 1
            log(f"· already have: {title}")
            return "already in library"
        return None

    opts = {
        "noplaylist": not playlist,
        "match_filter": skip_known if (playlist and have) else None,
        "progress_hooks": [on_prog],
        "format": "bestaudio/best",
        "outtmpl": str(root / "%(artist,uploader|Unknown)s" / "%(album,playlist_title|Singles)s"
                       / "%(playlist_index&{} - |)s%(title)s.%(ext)s"),
        "writethumbnail": True,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": cfg["audio_format"], "preferredquality": "0"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ],
        "postprocessor_hooks": [on_pp],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([target])
        state.note = f"finished: {state.done} downloaded" + (f", {state.skipped} already in library" if state.skipped else "")
    except yt_dlp.utils.DownloadCancelled:
        state.note = "cancelled"
    except Exception as e:
        state.note = "failed"
        log(f"✗ {e}")
    state.now = ""
    log(f"— {state.note}")


# ───────────────────────────── playback (mpv over IPC) ─────────────────────────────
class MPV:
    """Talks to mpv over JSON IPC: a named pipe on Windows, a unix socket elsewhere."""

    def __init__(self, volume):
        self.win = os.name == "nt"
        if self.win:
            self.path = rf"\\.\pipe\sonora-{os.getpid()}"
        else:
            self.path = os.path.join(tempfile.gettempdir(), f"sonora-{os.getpid()}.sock")
        flags = subprocess.CREATE_NO_WINDOW if self.win else 0
        self.proc = subprocess.Popen(
            ["mpv", "--no-video", "--idle=yes", "--really-quiet", f"--volume={volume}",
             f"--input-ipc-server={self.path}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags)
        self.sock, self.buf, self.rid = None, b"", 0
        for _ in range(50):
            try:
                if self.win:
                    self.sock = open(self.path, "r+b", buffering=0)
                else:
                    s = socket.socket(socket.AF_UNIX)
                    s.connect(self.path)
                    s.settimeout(0.3)
                    self.sock = s
                break
            except OSError:
                time.sleep(0.1)

    def _send(self, data):
        self.sock.write(data) if self.win else self.sock.sendall(data)

    def _recv(self):
        return self.sock.read(4096) if self.win else self.sock.recv(4096)

    def cmd(self, *args):
        if not self.sock or self.proc.poll() is not None:
            return None
        self.rid += 1
        try:
            self._send(json.dumps({"command": list(args), "request_id": self.rid}).encode() + b"\n")
            while True:
                while b"\n" not in self.buf:
                    chunk = self._recv()
                    if not chunk:
                        return None
                    self.buf += chunk
                line, self.buf = self.buf.split(b"\n", 1)
                msg = json.loads(line)
                if msg.get("request_id") == self.rid:
                    return msg.get("data")
        except (OSError, ValueError):
            return None

    def close(self):
        self.proc.terminate()
        if not self.win:
            try:
                os.unlink(self.path)
            except OSError:
                pass


# ───────────────────────────── library ─────────────────────────────
AUDIO = {".mp3", ".m4a", ".opus", ".flac", ".ogg", ".wav"}


def scan(root):
    tracks = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in AUDIO:
            rel = p.relative_to(root).parts
            artist = rel[0] if len(rel) >= 2 else "Unknown"
            album = rel[1] if len(rel) >= 3 else "Singles"
            tracks.append((artist, album, p.stem, p))
    return tracks


def fmt(s):
    s = int(s or 0)
    return f"{s // 60:02d}:{s % 60:02d}"


# ───────────────────────────── collection: playlists, album fixes ─────────────────────────────
# Extra data lives in its own file (never touches your music files):
#   playlists: name -> [song path relative to music_dir]
#   overrides: song path -> {"album", "artist"}   your manual fixes, always win over auto-organize
#   tags:      song path -> {"album", "artist"}   album tag read once (ffprobe) for songs not in an album folder
COLLECTION_FILE = CONFIG_BASE / "sonora" / "collection.json"


def load_collection():
    try:
        d = json.loads(COLLECTION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    for k in ("playlists", "overrides", "tags"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    for k in ("favorites", "no_lyrics"):
        if not isinstance(d.get(k), list):
            d[k] = []
    return d


def save_collection(d):
    COLLECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COLLECTION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(COLLECTION_FILE)


STATE_FILE = CONFIG_BASE / "sonora" / "state.json"
MODES = ["songs", "favorites", "playlists", "queue"]
SORTS = ["file", "title", "artist", "album", "recent"]
SORT_NAMES = {"file": "file order", "title": "by title", "artist": "by artist", "album": "by album", "recent": "recently added"}


def load_state():
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_state(d):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def sort_tracks(tracks, mode):
    if mode == "title":
        return sorted(tracks, key=lambda t: display_title(t[2]).casefold())
    if mode == "artist":
        return sorted(tracks, key=lambda t: (t[0].casefold(), t[1].casefold(), t[2].casefold()))
    if mode == "album":
        return sorted(tracks, key=lambda t: (t[1].casefold(), t[0].casefold(), t[2].casefold()))
    if mode == "recent":
        def mtime(t):
            try:
                return t[3].stat().st_mtime
            except OSError:
                return 0
        return sorted(tracks, key=mtime, reverse=True)
    return list(tracks)


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def make_have(tracks):
    """-> have(title, uploader): is this YouTube result already in the library? Title must match AND the artist must agree."""
    pairs = []
    for artist, _, stem, _ in tracks:
        t = display_title(stem)
        pairs.append((norm(clean_title(t)), norm(artist), norm(t)))

    def have(title, uploader=""):
        raw, clean, up = norm(title), norm(clean_title(title)), norm(uploader)
        for lt, la, lraw in pairs:
            if not lt:
                continue
            if la in ("", "unknown"):                      # no artist known for that file: title alone has to match
                if clean == lt or raw == lraw:
                    return True
            elif (la in up or la in clean or la in raw) and (clean == lt or raw == lraw or clean.replace(la, "") == lt):
                return True
        return False
    return have


def lyric_title(artist, stem):
    """Song title for a lyrics search: drop '(Official Video)' and a leading 'Artist - '."""
    t = clean_title(display_title(stem))
    if " - " in t:
        a, b = t.split(" - ", 1)
        if norm(a) and (norm(a) in norm(artist) or norm(artist) in norm(a)):
            t = b.strip()
    return t


def probe_tags(path):
    """Album/artist tag of one file via ffprobe ('' when missing)."""
    out = {"album": "", "artist": ""}
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries", "format_tags", "-i", str(path)],
                           stdout=subprocess.PIPE, timeout=15, **QUIET)
        tags = {k.lower(): str(v) for k, v in (json.loads(r.stdout or b"{}").get("format", {}).get("tags") or {}).items()}
    except Exception:
        return out
    out["album"] = tags.get("album", "").strip()
    out["artist"] = (tags.get("album_artist") or tags.get("albumartist") or tags.get("artist") or "").strip()
    return out


def organize_tracks(raw, root, col):
    """raw scan -> (tracks, auto, by_key). Album = folder, else album tag, else 'Singles'; manual overrides win."""
    tracks, auto, by_key = [], {}, {}
    for artist, album, stem, path in raw:
        key = path.relative_to(root).as_posix()
        if album == "Singles":
            tg = col["tags"].get(key) or {}
            if tg.get("album"):
                album = tg["album"]
                if artist == "Unknown" and tg.get("artist"):
                    artist = tg["artist"]
        auto[key] = (artist, album)
        ov = col["overrides"].get(key)
        if ov:
            artist, album = ov.get("artist") or artist, ov.get("album") or album
        by_key[key] = (artist, album, stem, path)
        tracks.append(by_key[key])
    return tracks, auto, by_key


def group_albums(tracks):
    """{(album, artist): [tracks]} — songs keep their file order inside an album."""
    g = {}
    for t in tracks:
        g.setdefault((t[1], t[0]), []).append(t)
    return g


def sorted_albums(groups):
    return sorted(groups, key=lambda k: (k[0].casefold(), k[1].casefold()))


class SplashScreen(ModalScreen):
    """Opening: star -> logo -> program.  Closing: the same animation backwards, then the app exits."""

    def __init__(self, mode):
        super().__init__()
        self.smode, self.t0, self.sdone, self.stimer, self._vol = mode, None, False, None, None

    def compose(self) -> ComposeResult:
        yield Static(id="splash")

    def on_mount(self):
        self.t0 = time.time()
        self.stimer = self.set_interval(1 / 30, self.step)
        self.step()

    def step(self):
        if self.sdone:
            return
        e = time.time() - self.t0
        if e >= SPLASH_TOTAL:
            return self.finish()
        box = self.query_one("#splash", Static).size
        if box.width < 4 or box.height < 3:
            return
        tt = e if self.smode == "intro" else SPLASH_TOTAL - e
        self.query_one("#splash", Static).update(splash_frame(tt, box.width, box.height, THEME, int(time.time() / 0.25)))
        if self.smode == "outro" and self.app.mpv:                 # the music fades out with the animation
            v = int(self.app.volume * max(0.0, 1 - e / SPLASH_TOTAL))
            if v != self._vol:
                self._vol = v
                self.app.mpv.cmd("set_property", "volume", v)

    def finish(self):
        if self.sdone:
            return
        self.sdone = True
        if self.stimer:
            self.stimer.stop()
        self.dismiss(True)

    def on_key(self, e):
        e.stop()
        self.finish()                                              # any key skips the animation

    def on_click(self, e):
        self.finish()


class PromptScreen(ModalScreen):
    """Small text form: one Input per field. Dismisses with [values], "reset" or None."""
    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(self, heading, fields, has_reset=False):
        super().__init__()
        self.heading, self.fields, self.has_reset = heading, fields, has_reset

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Static(self.heading, id="dlg_title")
            for i, (label, value) in enumerate(self.fields):
                yield Static(label, classes="dlg_label")
                yield Input(value=value, id=f"f{i}")
            with Horizontal(classes="btnrow"):
                yield Button("SAVE", id="dlg_ok")
                if self.has_reset:
                    yield Button("RESET", id="dlg_reset")
                yield Button("CANCEL", id="dlg_cancel")

    def on_mount(self):
        self.query_one("#f0", Input).focus()

    def read(self):
        return [self.query_one(f"#f{i}", Input).value.strip() for i in range(len(self.fields))]

    def on_input_submitted(self, e: Input.Submitted):
        e.stop()
        i = int(e.input.id[1:])
        if i + 1 < len(self.fields):
            self.query_one(f"#f{i + 1}", Input).focus()
        else:
            self.dismiss(self.read())

    def on_button_pressed(self, e: Button.Pressed):
        e.stop()
        self.dismiss({"dlg_ok": self.read(), "dlg_reset": "reset"}.get(e.button.id))

    def action_cancel(self):
        self.dismiss(None)


class PickScreen(ModalScreen):
    """Pick one entry from a list. Dismisses with the index or None."""
    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(self, heading, options):
        super().__init__()
        self.heading, self.options = heading, options

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Static(self.heading, id="dlg_title")
            yield OptionList(*[Option(Text(o, no_wrap=True, overflow="ellipsis")) for o in self.options], id="pick_list")

    def on_mount(self):
        ol = self.query_one("#pick_list", OptionList)
        ol.highlighted = 0
        ol.focus()

    def on_option_list_option_selected(self, e: OptionList.OptionSelected):
        e.stop()
        self.dismiss(e.option_index)

    def action_cancel(self):
        self.dismiss(None)


# ───────────────────────────── key help ─────────────────────────────
KEY_NAMES = {"comma": ",", "full_stop": ".", "slash": "/", "question_mark": "?", "backspace": "⌫", "escape": "Esc",
             "enter": "↵", "space": "Space", "shift+up": "⇧↑", "shift+down": "⇧↓", "up": "↑", "down": "↓"}


def pretty_key(k):
    if k in KEY_NAMES:
        return KEY_NAMES[k]
    if k.startswith("ctrl+"):
        return "^" + k[5:].upper()
    if re.fullmatch(r"f\d+", k):
        return k.upper()
    return k


def help_groups(k):
    """(section, [(keys, what)]) - used by the ? popup."""
    pk = pretty_key
    return [
        ("PLAYBACK", [(pk(k["play_pause"]), "play / pause"), (f"{pk(k['previous'])} {pk(k['next'])}", "previous / next"),
                      (f"{pk(k['seek_back'])} {pk(k['seek_forward'])}", "seek back / forward"),
                      (f"{pk(k['volume_down'])} {pk(k['volume_up'])}", "volume down / up"),
                      (pk(k["shuffle"]), "shuffle"), (pk(k["repeat"]), "repeat")]),
        ("NAVIGATE", [(f"{pk(k['tab_player'])} {pk(k['tab_download'])} {pk(k['tab_settings'])}", "player / download / settings"),
                      (pk(k["view_next"]), "next view (songs, favorites…)"), (pk(k["filter"]), "filter the list"),
                      (pk(k["sort"]), "cycle sorting"), (pk(k["back"]), "go back (Esc too)"),
                      (pk(k["cover_toggle"]), "cover / spinning star")]),
        ("LIBRARY", [(pk(k["favorite"]), "favorite"), (pk(k["add_to_playlist"]), "add to playlist"),
                     (pk(k["new_playlist"]), "new playlist"), (pk(k["queue_add"]), "add to up next"),
                     (pk(k["play_all"]), "play everything shown"), (f"{pk(k['move_up'])} {pk(k['move_down'])}", "reorder"),
                     (pk(k["edit"]), "fix album / rename"), (pk(k["delete"]), "remove from list"),
                     (pk(k["delete_file"]), "delete file from disk")]),
        ("LYRICS & MORE", [(pk(k["lyrics"]), "re-fetch lyrics"), (pk(k["lyrics_menu"]), "lyrics menu"),
                           (pk(k["update_ytdlp"]), "update yt-dlp (download tab)"),
                           (pk(k["help"]), "open / close Help"), (pk(k["quit"]), "quit")]),
    ]


def build_help(keys):
    """The Help page: every key, grouped in two columns."""
    from rich.table import Table
    grid = Table.grid(padding=(0, 6), expand=False)
    grid.add_column()
    grid.add_column()
    groups = help_groups(keys)
    for row in range(0, len(groups), 2):
        cells = []
        for title, items in groups[row:row + 2]:
            t = Text(title + "\n", style=f"bold {THEME['accent']}")
            for ks, what in items:
                t.append(f"{ks:<10}", style=f"bold {THEME['accent2']}")
                t.append(what + "\n", style=THEME["text"])
            cells.append(t)
        while len(cells) < 2:
            cells.append(Text(""))
        grid.add_row(*cells)
    return grid


# ───────────────────────────── cover + waveform helpers ─────────────────────────────
NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
BLOCKS = " ▁▂▃▄▅▆▇█"
QUIET = dict(stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NOWIN)


def display_title(stem):
    return re.sub(r"^\d+\s*-\s*", "", stem)


def compute_wave(path, n=320):
    """Loudness envelope (n values, 0..1) of the whole track, decoded by ffmpeg."""
    try:
        out = subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(path), "-vn", "-ac", "1", "-ar", "4000",
                              "-f", "s16le", "-"], stdout=subprocess.PIPE, timeout=90, **QUIET).stdout
    except Exception:
        return None
    a = array.array("h")
    a.frombytes(out[: len(out) // 2 * 2])
    if len(a) < n:
        return None
    step = len(a) // n
    peaks = [max(map(abs, a[i * step:(i + 1) * step])) for i in range(n)]
    m = max(peaks) or 1
    return [(p / m) ** 0.6 for p in peaks]


def load_cover(path):
    """Embedded cover art as a square RGB PIL image ('pillow' if Pillow is missing, None if no cover)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return "pillow"
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-i", str(path), "-an", "-frames:v", "1", tmp.name],
                       stdout=subprocess.DEVNULL, timeout=20, **QUIET)
        img = Image.open(tmp.name).convert("RGB")
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return ImageOps.autocontrast(img.crop((left, top, left + side, top + side)))


DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))   # braille dot bits [row][col]
BAYER = (0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5)
STARS = ("·", "✦", "⋆", "✧", "˙", "+")        # twinkling stars
DECO = "✦ {} ✦"                              # ornament around panel titles


def lerp_hex(a, b, t):
    """Blend two #rrggbb colours (t = 0..1)."""
    pa, pb = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


def braille_from(pixels, W, cols, rows, thr, style):
    """pixels: W*(rows*4) bytes; a dot is lit when value > thr. 8 pixels per terminal cell."""
    lines = []
    for r in range(rows):
        line = []
        for c in range(cols):
            bits = 0
            for dy in range(4):
                base = (r * 4 + dy) * W + c * 2
                if pixels[base] > thr:
                    bits |= DOTS[dy][0]
                if pixels[base + 1] > thr:
                    bits |= DOTS[dy][1]
            line.append(chr(0x2800 + bits))
        lines.append("".join(line))
    return Text("\n".join(lines), style=style, no_wrap=True)


def cover_text(img, cols, style):
    rows = max(1, cols // 2)                      # a cell is ~2x taller than wide
    if style == "blocks":                         # colour, 2 pixels per cell via half blocks
        small = img.resize((cols, rows * 2))
        px = small.load()
        out = Text(no_wrap=True)
        for r in range(rows):
            for c in range(cols):
                t, b = px[c, 2 * r], px[c, 2 * r + 1]
                out.append("▀", f"rgb({t[0]},{t[1]},{t[2]}) on rgb({b[0]},{b[1]},{b[2]})")
            if r < rows - 1:
                out.append("\n")
        return out
    gray = img.convert("L")
    if style == "ascii":
        small = gray.resize((cols, rows))
        data, chars = small.tobytes(), THEME["cover_chars"]
        n = len(chars) - 1
        return Text("\n".join("".join(chars[data[y * cols + x] * n // 255] for x in range(cols))
                              for y in range(rows)), style=THEME["text"], no_wrap=True)
    # braille: 2x4 dots per cell + Floyd-Steinberg dithering = by far the sharpest
    lut = [0 if v < 30 else int(255 * ((v - 30) / 225) ** 1.3) for v in range(256)]   # crush dark noise, keep highlights
    bw = gray.point(lut).resize((cols * 2, rows * 4)).convert("1").convert("L").tobytes()
    return braille_from(bw, cols * 2, cols, rows, 127, THEME["text"])


STAR_N, _star_mask, _star_grid = 96, None, {}


def star_mask():
    global _star_mask
    if _star_mask is None:
        pts = [((0.92 if i % 2 == 0 else 0.36) * math.cos(-math.pi / 2 + i * math.pi / 5),
                (0.92 if i % 2 == 0 else 0.36) * math.sin(-math.pi / 2 + i * math.pi / 5)) for i in range(10)]
        m = bytearray(STAR_N * STAR_N)
        for y in range(STAR_N):
            py = (y + 0.5) / STAR_N * 2 - 1
            for x in range(STAR_N):
                px, inside, j = (x + 0.5) / STAR_N * 2 - 1, False, 9
                for i in range(10):
                    (xi, yi), (xj, yj) = pts[i], pts[j]
                    if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                        inside = not inside
                    j = i
                m[y * STAR_N + x] = inside
        _star_mask = m
    return _star_mask


def star_text(cols, angle, color):
    """One frame of a rotating star, drawn with dithered braille dots."""
    rows = max(1, cols // 2)
    W, H = cols * 2, rows * 4
    grid = _star_grid.get((W, H))
    if grid is None:
        grid = [((x + 0.5) / W * 2 - 1, (y + 0.5) / H * 2 - 1, (BAYER[(y & 3) * 4 + (x & 3)] + 0.5) / 16)
                for y in range(H) for x in range(W)]
        _star_grid.clear()
        _star_grid[(W, H)] = grid
    mask, N, ca, sa = star_mask(), STAR_N, math.cos(angle), math.sin(angle)
    px = bytearray(W * H)
    for i, (u, v, th) in enumerate(grid):
        su, sv = ca * u + sa * v, ca * v - sa * u
        ix, iy = int((su + 1) * 0.5 * N), int((sv + 1) * 0.5 * N)
        if 0 <= ix < N and 0 <= iy < N and mask[iy * N + ix]:
            if 1.0 - 0.55 * math.sqrt(su * su + sv * sv) / 0.92 > th:
                px[i] = 1
    return braille_from(px, W, cols, rows, 0, color)


def star_row(width, frame, seed, accent, dim):
    """A row of sparse twinkling stars."""
    out = Text(no_wrap=True)
    for c in range(width):
        h = ((c + 1) * 2654435761 ^ (seed + 1) * 40503) & 0xFFFFFFFF
        if h % 13 == 0:
            out.append(STARS[(h // 13 + frame * (1 + h % 2)) % len(STARS)], accent if (h >> 5) % 3 == 0 else dim)
        else:
            out.append(" ")
    return out


# ── splash: star -> SONORA logo (opening) and the same thing played backwards (closing) ──
LOGO_FONT = {
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "N": ["#...#", "##..#", "##..#", "#.#.#", "#..##", "#..##", "#...#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
}
LOGO_ROWS = [".".join(LOGO_FONT[c][r] for c in "SONORA") for r in range(7)]      # 35 x 7 pixels
SPLASH_TOTAL = 3.5
_logo_cache = {}


def hash01(x, y, seed=0):
    h = (x * 374761393 + y * 668265263 + seed * 2246822519) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((h ^ (h >> 16)) & 0xFFFF) / 65536.0


def logo_cells(W, H):
    """{(x, y): (char, 0..1 gradient position)} for the SONORA logo centred in a W x H area, plus its box."""
    key = (W, H)
    if key in _logo_cache:
        return _logo_cache[key]
    cells = {}
    if W >= 72:                                   # big: every pixel is 2 columns x 1 row
        w, h = 70, 7
        x0, y0 = (W - w) // 2, (H - h) // 2
        for y, row in enumerate(LOGO_ROWS):
            for x, ch in enumerate(row):
                if ch == "#":
                    for dx in (0, 1):
                        cells[(x0 + 2 * x + dx, y0 + y)] = ("█", x / 34)
    elif W >= 38:                                 # small: half blocks, 1 column per pixel
        w, h = 35, 4
        x0, y0 = (W - w) // 2, (H - h) // 2
        for i in range(4):
            top = LOGO_ROWS[2 * i]
            bot = LOGO_ROWS[2 * i + 1] if 2 * i + 1 < 7 else "." * 35
            for x in range(35):
                t, b = top[x] == "#", bot[x] == "#"
                if t or b:
                    cells[(x0 + x, y0 + i)] = ("█" if t and b else "▀" if t else "▄", x / 34)
    else:                                         # tiny terminal: plain letters
        word = " ".join("SONORA")
        w, h = len(word), 1
        x0, y0 = max(0, (W - w) // 2), H // 2
        for i, ch in enumerate(word):
            if ch != " ":
                cells[(x0 + i, y0)] = (ch, i / max(1, w - 1))
    _logo_cache.clear()
    _logo_cache[key] = (cells, (x0, y0, w, h))
    return _logo_cache[key]


def splash_frame(tt, W, H, th, frame):
    """One frame of the opening animation at time tt (0..SPLASH_TOTAL). The closing plays it with tt running backwards."""
    clamp = lambda v: max(0.0, min(1.0, v))
    T_IN, T_D0, T_D1, T_F0 = 0.9, 1.2, 2.2, 3.1
    bg, acc, acc2, dim, txt = th["background"], th["accent"], th["accent2"], th["dim"], th["text"]
    logo, (lx, ly, lw, lh) = logo_cells(W, H)
    p, fade = clamp((tt - T_D0) / (T_D1 - T_D0)), clamp((tt - T_F0) / (SPLASH_TOTAL - T_F0))
    star = {}
    if tt < T_D1:
        full = max(8, min(W, H * 2 - 4, 44))
        cols = max(6, int(full * (0.12 + 0.88 * (1 - (1 - clamp(tt / T_IN)) ** 3))) // 2 * 2)
        angle = 11.0 * (1 - (1 - min(tt, 2.2) / 2.2) ** 2)
        lines = star_text(cols, angle, acc).plain.split("\n")
        top, left = (H - len(lines)) // 2, (W - cols) // 2
        for j, ln in enumerate(lines):
            for i, ch in enumerate(ln):
                if ch not in " \u2800":
                    star[(left + i, top + j)] = ch
    star_col = lerp_hex(bg, acc, clamp(tt / 0.45))
    spark_rows = {ly - 2, ly + lh + 1} if p >= 1 else set()
    out = Text(no_wrap=True)
    for y in range(H):
        run, run_col = [], None

        def flush():
            if run:
                out.append("".join(run), style=run_col)
                run.clear()
        for x in range(W):
            ch, col = " ", None
            lc, sc = logo.get((x, y)), star.get((x, y))
            if lc and p >= 0.5 * (x / W) + 0.5 * hash01(x, y, 1):
                ch, col = lc[0], lerp_hex(lerp_hex(acc, acc2, lc[1]), bg, fade)
            elif sc:
                gone = 0.1 + 0.9 * hash01(x, y, 2)
                if p < gone:
                    ch = sc
                    col = txt if (p > 0 and gone - p < 0.08) else star_col
            elif y in spark_rows and hash01(x, y, 3) < 0.07:
                ch = STARS[int(hash01(x, y, 4) * len(STARS) + frame) % len(STARS)]
                col = lerp_hex(acc if hash01(x, y, 5) > 0.5 else dim, bg, fade)
            if col != run_col:
                flush()
                run_col = col
            run.append(ch)
        flush()
        if y < H - 1:
            out.append("\n")
    return out


def seek_cells(w, wave, frac, head, hover, paused, th):
    """Mirrored waveform, 2 rows tall: the top row grows up from the centre line, the bottom row hangs down
    from it (inverted blocks). Played part = violet->orchid gradient with a glowing comet tail, a bright
    playhead, a ghost marker under the mouse, and a soft idle colour for what's still to come."""
    if wave:
        raw = [max(wave[c * len(wave) // w: max(c * len(wave) // w + 1, (c + 1) * len(wave) // w)]) for c in range(w)]
    else:                                                    # still decoding: gentle placeholder ripple
        raw = [0.2 + 0.1 * math.sin(c * 0.9) + 0.05 * math.sin(c * 0.37) for c in range(w)]
    vals = [(raw[max(0, c - 1)] + 2 * raw[c] + raw[min(w - 1, c + 1)]) / 4 for c in range(w)]   # light smoothing
    bg, upcoming = th["background"], lerp_hex(th["line"], th["dim"], 0.3)
    top, bot = [], []
    for c, v in enumerate(vals):
        k = max(1, min(8, round(v * 8)))
        if c == head:
            col, k = (th["dim"] if paused else th["text"]), 8
        elif head is not None and c < head:
            col = lerp_hex(th["accent"], th["accent2"], c / max(1, w - 1))
            d = head - c
            if d <= 6:
                col = lerp_hex(col, th["text"], 0.55 * (1 - d / 7))     # comet tail
        else:
            col = upcoming
        if c == hover and c != head:
            col, k = lerp_hex(th["accent2"], bg, 0.25), max(k, 6)
        top.append((BLOCKS[k], col))
        bot.append((BLOCKS[8 - k], f"{bg} on {col}"))
    return top, bot


class Seekbar(Static):
    """Click anywhere on the waveform to jump there; hovering previews the time."""
    hover = None

    def on_click(self, e):
        self.app.seek_to(e.x / max(1, self.size.width))

    def on_mouse_move(self, e):
        self.hover = max(0, min(self.size.width - 1, e.x))
        self.app.seek_redraw()

    def on_leave(self, e):
        self.hover = None
        self.app.seek_redraw()


class Volbar(Static):
    PREFIX, WIDTH = 6, 18        # "VOL  ▕" then 18 cells

    def on_click(self, e):
        self.app.set_volume(abs=round(max(0, min(1, (e.x - self.PREFIX) / self.WIDTH)) * 100))


# ───────────────────────────── app ─────────────────────────────
class Sonora(App):
    CSS = CSS

    def get_css_variables(self):
        v = super().get_css_variables()
        v.update({f"sn-{k}": THEME[k] for k in COLOR_KEYS})     # our palette, changeable at runtime
        return v

    def __init__(self):
        super().__init__()
        self._slot = None
        self._seek_last = None
        self.cfg = CFG
        self.keymap = {v: k for k, v in CFG["keys"].items()}
        self.mpv = None
        self.tracks, self.view, self.found = [], [], []
        self.queue, self.raw, self.auto, self.by_key, self.rows = [], [], {}, {}, []   # queue = what is playing; view = what is shown
        self.mode, self.drill, self.root = "songs", None, None                   # collection view: songs | playlists | albums
        self.col = load_collection()
        self.idx, self.cur_path = None, None
        self.lyrics, self.plain_lines, self._cur = [], [], None
        self.wave, self.cover_img, self.cover_state, self._cover_key = None, None, "loading", None
        self.paused = self.shuffle = self.repeat = False
        self.started = 0.0
        self.volume = min(100, CFG["player"]["volume"])
        self.cover_mode = CFG["player"]["cover_mode"]
        self.busy, self.dl, self.angle, self._t = False, None, 0.0, time.time()
        self.stars_on = CFG["player"]["stars"]
        self.upnext, self.cur_track, self.sort = [], None, "file"
        self.quitting = False
        self.dl_queue, self.updating, self.lyr_stop, self.lyr_running = [], False, threading.Event(), False
        self.state = load_state()                                   # last session: volume, shuffle, repeat, sort, song
        if isinstance(self.state.get("volume"), (int, float)):
            self.volume = max(0, min(100, int(self.state["volume"])))
        self.shuffle = bool(self.state.get("shuffle", False))
        self.repeat = bool(self.state.get("repeat", False))
        if self.state.get("sort") in SORTS:
            self.sort = self.state["sort"]

    def query_one(self, selector, expect_type=None):
        """Always look in the main screen: timers keep running while a dialog/splash is on top of it."""
        stack = self.screen_stack
        return (stack[0] if stack else super()).query_one(selector, expect_type)

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="player"):
            with TabPane("Player", id="player"):
                with Horizontal(id="main"):
                    with Vertical(id="listpanel"):
                        yield Tabs(Tab("ALL SONGS", id="v_songs"), Tab("FAVORITES", id="v_favorites"), Tab("PLAYLISTS", id="v_playlists"), Tab("QUEUE", id="v_queue"), id="views")
                        yield Input(placeholder="filter…", id="filter")
                        yield DataTable(id="lib", cursor_type="row")
                    with Vertical(id="rightpanel"):
                        yield Static(id="stars_top")
                        yield Static(id="cover")
                        yield Static(id="title")
                        yield Static(id="lyrics")
                        yield Static(id="stars_bot")
                with Horizontal(id="bottom"):
                    with Vertical(id="progress"):
                        yield Seekbar(id="seek")
                        yield Static(id="times")
                    with Vertical(id="controls"):
                        with Horizontal(classes="btnrow"):
                            yield Button("‹‹ PREV", id="prev")
                            yield Button("PAUSE", id="pp")
                            yield Button("NEXT ››", id="next")
                        yield Volbar(id="volume")
            with TabPane("Download", id="download"):
                with Horizontal(id="dl_top"):
                    with Vertical(id="searchpanel"):
                        yield Input(placeholder="search a song, or paste a link to one song", id="query")
                        yield OptionList(id="results")
                    with Vertical(id="linkpanel"):
                        yield Static(id="stars_dl")
                        yield Input(placeholder="paste a playlist or album link…", id="playlist")
                        with Horizontal(classes="btnrow"):
                            yield Button("DOWNLOAD", id="dl_go")
                            yield Button("CANCEL", id="dl_cancel")
                            yield Button("UPDATE", id="dl_update")
                        yield Static("playlist/album links · songs you already have are skipped · UPDATE = yt-dlp", id="dl_hint")
                        yield Static(id="dl_status")
                        yield Static(id="dl_bar")
                        yield Static(id="dlstar")
                with Vertical(id="logpanel"):
                    yield RichLog(id="log", wrap=True)
            with TabPane("Settings", id="settings"):
                with Horizontal(id="set_top"):
                    with Vertical(id="presetpanel"):
                        yield OptionList(id="presets")
                    with Vertical(id="slotpanel"):
                        yield OptionList(id="slots")
                        yield Static(id="hexlabel")
                        yield Input(placeholder="#rrggbb", id="hex", max_length=7)
                        with Horizontal(classes="btnrow"):
                            yield Button("SAVE", id="set_save")
                            yield Button("RESET", id="set_reset")
                        yield Static(id="set_status")
                        yield Static("LIBRARY TOOLS", id="tools_label")
                        with Horizontal(classes="btnrow"):
                            yield Button("MISSING LYRICS", id="tool_lyrics")
                            yield Button("RESCAN", id="tool_rescan")
                        yield Static(id="tool_status")
            with TabPane("Help", id="help"):
                with VerticalScroll(id="helppanel"):
                    yield Static(id="helptext")

    def do_help(self):
        tabs = self.query_one(TabbedContent)
        if tabs.active == "help":
            return self.leave_help()
        self._before_help = tabs.active
        tabs.active = "help"

    def leave_help(self):
        back = getattr(self, "_before_help", "player")
        {"download": self.do_tab_download, "settings": self.do_tab_settings}.get(back, self.do_tab_player)()

    def on_mount(self):
        self.mpv = MPV(self.volume)
        self.query_one("#lib", DataTable).add_columns("#", "Title", "Artist")
        deco = DECO if self.stars_on else "{}"
        self.update_title()
        self.query_one("#progress").border_title = deco.format("NOW PLAYING")
        self.query_one("#searchpanel").border_title = deco.format("SEARCH")
        self.query_one("#linkpanel").border_title = deco.format("ALBUMS & PLAYLISTS")
        self.query_one("#logpanel").border_title = deco.format("DOWNLOADS")
        self.query_one("#presetpanel").border_title = deco.format("THEMES")
        self.query_one("#slotpanel").border_title = deco.format("COLORS")
        self.query_one("#helppanel").border_title = deco.format("HELP")
        self.query_one("#helptext", Static).update(build_help(CFG["keys"]))
        self.refresh_presets()
        self.refresh_slots()
        self.show_slot(COLOR_KEYS[0])
        for w in ("#stars_top", "#stars_bot", "#stars_dl", "#dlstar"):
            self.query_one(w).display = self.stars_on
        self.query_one("#filter").display = False
        self.query_one("#cover", Static).update(Text("♪", style=THEME["dim"]))
        self.render_volume()
        self.render_seek(0, 0)                      # idle waveform before anything plays
        self.reload_library()
        self.call_after_refresh(self.restore_session)
        self.query_one("#lib").focus()
        self.set_interval(15, self.save_session)
        if CFG["player"].get("splash", True):
            self.push_screen(SplashScreen("intro"))
        self.set_interval(0.25, self.tick)
        self.set_interval(0.125, self.spin)
        self.set_interval(0.6, self.twinkle)
        self.set_interval(0.3, self.dl_tick)
        self.twinkle()

    def on_unmount(self):
        self.save_session()
        if self.mpv:
            self.mpv.close()

    # ── library ──
    def reload_library(self):
        self.root = Path(self.cfg["music_dir"]).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw = scan(self.root)
        self.organize()
        self.apply_filter(self.query_one("#filter", Input).value)
        self.read_missing_tags()

    def organize(self):
        self.tracks, self.auto, self.by_key = organize_tracks(self.raw, self.root, self.col)

    def tkey(self, t):
        return t[3].relative_to(self.root).as_posix()

    def save_col(self):
        try:
            save_collection(self.col)
        except OSError as ex:
            self.notify(f"couldn't save collection: {ex}", severity="error")

    def read_missing_tags(self):
        """Songs that aren't inside an Artist/Album folder: look up their album tag once, in the background."""
        if not shutil.which("ffprobe"):
            return
        items = [(k, p) for (_, album, _, p) in self.raw if album == "Singles"
                 for k in [p.relative_to(self.root).as_posix()] if k not in self.col["tags"]]
        if items:
            self.read_tags(items)

    @work(thread=True, exclusive=True, group="tags")
    def read_tags(self, items):
        got = {k: probe_tags(p) for k, p in items}
        self.call_from_thread(self.tags_done, got)

    def tags_done(self, got):
        self.col["tags"].update(got)
        self.save_col()
        if any(v["album"] for v in got.values()):
            self.organize()
            self.refresh_view()

    def album_groups(self):
        return group_albums(self.tracks)

    def current_tracks(self):
        if self.drill:
            return [self.by_key[k] for k in self.col["playlists"].get(self.drill[1], []) if k in self.by_key]
        if self.mode == "favorites":
            return [self.by_key[k] for k in self.col["favorites"] if k in self.by_key]
        if self.mode == "queue":
            return self.upnext + (self.queue[self.idx + 1:] if self.idx is not None else self.queue)
        return sort_tracks(self.tracks, self.sort)

    def refresh_view(self):
        self.apply_filter(self.query_one("#filter", Input).value, keep=True)

    def is_list_view(self):
        return self.mode == "playlists" and not self.drill

    def apply_filter(self, q, keep=False):
        q = "" if self.mode == "queue" else q.lower()
        table = self.query_one("#lib", DataTable)
        row = table.cursor_row
        table.clear(columns=True)
        if self.drill and self.drill[1] not in self.col["playlists"]:
            self.drill = None                       # the playlist we were inside is gone -> back to the list
        if self.is_list_view():
            self.view = []
            self.rows = [("new", None)] + [("playlist", n) for n in self.col["playlists"] if q in n.lower()]
            table.add_columns("#", "Playlist", "Songs")
            for i, (kind, n) in enumerate(self.rows):
                if kind == "new":
                    table.add_row(Text(""), Text("＋ New playlist", style=THEME["accent2"]), Text(""), key=str(i))
                else:
                    cnt = sum(1 for k in self.col["playlists"][n] if k in self.by_key)
                    table.add_row(Text(str(i)), Text(n), Text(str(cnt)), key=str(i))
        else:
            self.rows = []
            self.view = [t for t in self.current_tracks() if q in " ".join(map(str, t[:3])).lower()]
            fav = set(self.col["favorites"])
            queue_mode = self.mode == "queue"
            table.add_columns("#", "Title", "Artist", "Album", *(["Where"] if queue_mode else []))
            for i, t in enumerate(self.view):
                cell = Text()
                if self.tkey(t) in fav:
                    cell.append("♥ ", style=THEME["accent2"])
                cell.append(display_title(t[2]))
                extra = [Text("up next" if i < len(self.upnext) else "queue", style=THEME["dim"])] if queue_mode else []
                table.add_row(Text(str(i + 1)), cell, Text(t[0]), Text(t[1]), *extra, key=str(i))
        if keep and table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))
        self.update_title()

    def update_title(self):
        label = "COLLECTION"
        if self.drill:
            label += " › " + self.drill[1]
        elif self.mode == "songs" and self.sort != "file":
            label += " · " + SORT_NAMES[self.sort]
        self.query_one("#listpanel").border_title = (DECO if self.stars_on else "{}").format(label)

    # ── collection: views, playlists, albums, fixes ──
    def reset_filter(self):
        f = self.query_one("#filter", Input)
        f.value = ""
        f.display = False

    def on_tabs_tab_activated(self, e: Tabs.TabActivated):
        if e.tabs.id != "views" or not e.tab.id:
            return
        e.stop()
        mode = e.tab.id.removeprefix("v_")
        if mode != self.mode:
            self.mode, self.drill = mode, None
            self.reset_filter()
            self.apply_filter("")

    def do_view_next(self):
        self.query_one("#views", Tabs).active = "v_" + MODES[(MODES.index(self.mode) + 1) % len(MODES)]
        self.query_one("#lib").focus()

    def do_back(self):
        if self.drill:
            self.drill = None
            self.reset_filter()
            self.apply_filter("")

    def cursor_index(self):
        t = self.query_one("#lib", DataTable)
        return t.cursor_row if t.row_count else None

    def selected_tracks(self):
        r = self.cursor_index()
        if r is None:
            return []
        if self.is_list_view():
            if r < len(self.rows) and self.rows[r][0] == "playlist":
                return [self.by_key[k] for k in self.col["playlists"].get(self.rows[r][1], []) if k in self.by_key]
            return []
        return [self.view[r]] if r < len(self.view) else []

    def open_row(self, r):
        kind, val = self.rows[r]
        if kind == "new":
            return self.prompt_new_playlist()
        self.drill = (kind, val)
        self.reset_filter()
        self.apply_filter("")

    def do_new_playlist(self):
        self.prompt_new_playlist()

    def prompt_new_playlist(self, tracks=None):
        def done(res):
            name = res[0] if isinstance(res, list) else ""
            if not name:
                return
            exists = next((n for n in self.col["playlists"] if n.casefold() == name.casefold()), None)
            name = exists or name
            self.col["playlists"].setdefault(name, [])
            if tracks:
                return self.add_to_playlist(name, tracks)
            self.save_col()
            self.refresh_view()
            self.notify(f"playlist “{name}” {'already exists' if exists else 'created'}")
        self.push_screen(PromptScreen("New playlist", [("Name", "")]), done)

    def add_to_playlist(self, name, tracks):
        lst = self.col["playlists"].setdefault(name, [])
        new = [k for k in dict.fromkeys(self.tkey(t) for t in tracks) if k not in lst]
        lst.extend(new)
        self.save_col()
        self.refresh_view()
        skipped = len(tracks) - len(new)
        self.notify(f"added {len(new)} to “{name}”" + (f" ({skipped} already there)" if skipped else ""))

    def do_add_to_playlist(self):
        tracks = self.selected_tracks()
        if not tracks:
            return self.notify("select a song first (ALL SONGS tab)", severity="warning")
        names = list(self.col["playlists"])
        opts = ["＋ New playlist…"] + [f"{n}  ({len(self.col['playlists'][n])})" for n in names]
        what = display_title(tracks[0][2]) if len(tracks) == 1 else f"{len(tracks)} songs"

        def chosen(i):
            if i == 0:
                self.prompt_new_playlist(tracks)
            elif i is not None:
                self.add_to_playlist(names[i - 1], tracks)
        self.push_screen(PickScreen(f"Add “{what}” to…", opts), chosen)

    def set_override(self, key, album, artist):
        if (artist, album) == self.auto.get(key):
            self.col["overrides"].pop(key, None)         # same as automatic -> no override needed
        else:
            self.col["overrides"][key] = {"artist": artist, "album": album}

    def apply_fix(self, tracks, album=None, artist=None, reset=False):
        for t in tracks:
            key = self.tkey(t)
            if reset:
                self.col["overrides"].pop(key, None)
            else:
                self.set_override(key, album or t[1], artist or t[0])
        self.save_col()
        self.organize()
        self.refresh_view()
        self.notify("album fix reset" if reset else f"moved {len(tracks)} song(s) to “{album}”")

    def do_edit(self):
        r = self.cursor_index()
        if r is None:
            return
        if self.is_list_view():
            kind, name = self.rows[r]
            return self.rename_playlist(name) if kind == "playlist" else None
        tracks = self.selected_tracks()
        if tracks:
            self.fix_song_picker(tracks[0])

    def rename_playlist(self, old):
        def done(res):
            new = res[0] if isinstance(res, list) else ""
            if not new or new == old:
                return
            if any(n.casefold() == new.casefold() and n != old for n in self.col["playlists"]):
                return self.notify(f"“{new}” already exists", severity="warning")
            self.col["playlists"] = {(new if n == old else n): v for n, v in self.col["playlists"].items()}
            if self.drill == ("playlist", old):
                self.drill = ("playlist", new)
            self.save_col()
            self.refresh_view()
        self.push_screen(PromptScreen("Rename playlist", [("Name", old)]), done)

    def fix_album_dialog(self, tracks, single=False):
        t = tracks[0]
        def done(res):
            if res == "reset":
                self.apply_fix(tracks, reset=True)
            elif isinstance(res, list) and res[0]:
                self.apply_fix(tracks, album=res[0], artist=res[1] or t[0])
        head = f"Fix album of “{display_title(t[2])}”" if single else f"Fix album ({len(tracks)} songs)"
        self.push_screen(PromptScreen(head, [("Album", t[1]), ("Artist", t[0])], has_reset=True), done)

    def fix_song_picker(self, t):
        albums = [k for k in sorted_albums(self.album_groups()) if k != (t[1], t[0])]
        opts = ["✎ Type album / artist…"] + [f"{a} — {ar}" for a, ar in albums] + ["↺ Reset to automatic"]

        def chosen(i):
            if i is None:
                return
            if i == 0:
                self.fix_album_dialog([t], single=True)
            elif i == len(opts) - 1:
                self.apply_fix([t], reset=True)
            else:
                self.apply_fix([t], album=albums[i - 1][0], artist=albums[i - 1][1])
        self.push_screen(PickScreen(f"Wrong album? Move “{display_title(t[2])}” to…", opts), chosen)

    def do_delete(self):
        r = self.cursor_index()
        if r is None:
            return
        if self.mode == "queue":
            return self.remove_queue_row(r)
        if self.mode == "favorites":
            if r < len(self.view):
                key = self.tkey(self.view[r])
                if key in self.col["favorites"]:
                    self.col["favorites"].remove(key)
                    self.save_col()
                    self.refresh_view()
                    self.notify("removed from favorites")
        elif self.drill:
            if r < len(self.view):
                lst = self.col["playlists"][self.drill[1]]
                key = self.tkey(self.view[r])
                if key in lst:
                    lst.remove(key)
                    self.save_col()
                    self.refresh_view()
                    self.notify(f"removed from “{self.drill[1]}”")
        elif self.is_list_view() and r < len(self.rows) and self.rows[r][0] == "playlist":
            name = self.rows[r][1]
            def done(i):
                if i == 0:
                    self.col["playlists"].pop(name, None)
                    self.save_col()
                    self.refresh_view()
            self.push_screen(PickScreen(f"Delete playlist “{name}”? (songs stay on disk)", ["Delete", "Cancel"]), done)
        else:
            self.notify("d removes a song from favorites/playlist/queue or deletes a playlist — D deletes the file")

    # ── favorites, queue, reorder, sort, delete file, lyrics menu ──
    def do_favorite(self):
        tracks = self.selected_tracks() if not self.is_list_view() else []
        t = tracks[0] if tracks else self.cur_track
        if not t:
            return self.notify("select a song first", severity="warning")
        key, fav = self.tkey(t), self.col["favorites"]
        if key in fav:
            fav.remove(key)
            self.notify(f"♡ removed “{display_title(t[2])}” from favorites")
        else:
            fav.append(key)
            self.notify(f"♥ added “{display_title(t[2])}” to favorites")
        self.save_col()
        self.refresh_view()

    def do_queue_add(self):
        tracks = self.selected_tracks()
        if not tracks:
            return self.notify("select a song or a playlist first", severity="warning")
        self.upnext.extend(tracks)
        self.notify(f"{len(tracks)} song(s) added to up next ({len(self.upnext)} waiting)")
        if self.mode == "queue":
            self.refresh_view()

    def remove_queue_row(self, r):
        if r < len(self.upnext):
            self.upnext.pop(r)
        else:
            k = (self.idx + 1 if self.idx is not None else 0) + r - len(self.upnext)
            if 0 <= k < len(self.queue):
                del self.queue[k]
        self.refresh_view()

    def play_queue_row(self, r):
        if r < len(self.upnext):
            self.play_upnext(self.upnext.pop(r))
        else:
            self.play((self.idx + 1 if self.idx is not None else 0) + r - len(self.upnext))

    def do_play_all(self):
        tracks = self.selected_tracks() if self.is_list_view() else list(self.view)
        if not tracks:
            return self.notify("nothing to play here", severity="warning")
        self.play(random.randrange(len(tracks)) if self.shuffle else 0, queue=list(tracks))

    def order_list(self):
        if self.drill:
            return self.col["playlists"].get(self.drill[1])
        if self.mode == "favorites":
            return self.col["favorites"]
        return None

    def do_move_up(self):
        self.move_song(-1)

    def do_move_down(self):
        self.move_song(1)

    def move_song(self, delta):
        lst, r = self.order_list(), self.cursor_index()
        if lst is None or r is None or r >= len(self.view):
            return self.notify("reordering works inside a playlist or in favorites")
        if self.query_one("#filter", Input).value:
            return self.notify("clear the filter first to reorder")
        key = self.tkey(self.view[r])
        if key not in lst:
            return
        i = lst.index(key)
        j = i + delta
        while 0 <= j < len(lst) and lst[j] not in self.by_key:
            j += delta
        if not 0 <= j < len(lst):
            return
        lst[i], lst[j] = lst[j], lst[i]
        self.save_col()
        self.refresh_view()
        row = next((n for n, t in enumerate(self.view) if self.tkey(t) == key), r)
        self.query_one("#lib", DataTable).move_cursor(row=row)

    def do_sort(self):
        self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
        self.refresh_view()
        self.notify(f"sort: {SORT_NAMES[self.sort]}" + ("" if self.mode == "songs" else " (applies to ALL SONGS)"))

    def purge_key(self, key, path):
        self.col["favorites"] = [k for k in self.col["favorites"] if k != key]
        for lst in self.col["playlists"].values():
            lst[:] = [k for k in lst if k != key]
        self.col["overrides"].pop(key, None)
        self.col["tags"].pop(key, None)
        self.upnext = [x for x in self.upnext if x[3] != path]
        if self.idx is not None:
            self.idx -= sum(1 for x in self.queue[:self.idx + 1] if x[3] == path)
        self.queue = [x for x in self.queue if x[3] != path]

    def do_delete_file(self):
        tracks = [] if self.is_list_view() else self.selected_tracks()
        if not tracks:
            return self.notify("select a song first", severity="warning")
        t = tracks[0]
        if t[3] == self.cur_path:
            return self.notify("that song is playing — start another one first", severity="warning")

        def done(i):
            if i != 0:
                return
            try:
                t[3].unlink()
                for ext in (".lrc", ".txt"):
                    t[3].with_suffix(ext).unlink(missing_ok=True)
            except OSError as ex:
                return self.notify(f"couldn't delete: {ex}", severity="error")
            self.purge_key(self.tkey(t), t[3])
            self.save_col()
            self.reload_library()
            self.notify(f"deleted “{display_title(t[2])}”")
        self.push_screen(PickScreen(f"Delete “{display_title(t[2])}” from disk? This can't be undone.",
                                    ["Delete file (and its lyrics)", "Cancel"]), done)

    def do_lyrics_menu(self):
        tracks = [] if self.is_list_view() else self.selected_tracks()
        t = tracks[0] if tracks else self.cur_track
        if not t:
            return self.notify("select a song first", severity="warning")
        artist, title, path = t[0], display_title(t[2]), t[3]
        opts = ["Search again (all sources)", "Only LRCLIB (synced)", "Only AZLyrics", "Only Genius",
                "Search with a different artist / title…", "Use lyrics from a file…", "Remove lyrics"]

        def chosen(i):
            if i is None:
                return
            if i <= 3:
                src = self.cfg["lyrics_sources"] if i == 0 else [("lrclib", "azlyrics", "genius")[i - 1]]
                self.notify("searching lyrics…")
                self.lyrics_job(path, artist, title, src)
            elif i == 4:
                def got(res):
                    if isinstance(res, list) and res[0] and res[1]:
                        self.notify("searching lyrics…")
                        self.lyrics_job(path, res[0], res[1], self.cfg["lyrics_sources"])
                self.push_screen(PromptScreen("Search lyrics as…", [("Artist", artist), ("Title", title)]), got)
            elif i == 5:
                def got(res):
                    if not (isinstance(res, list) and res[0]):
                        return
                    try:
                        text = Path(res[0].strip("'\"")).expanduser().read_text(encoding="utf-8", errors="ignore")
                        save_lyrics(path, text)
                    except OSError as ex:
                        return self.notify(f"couldn't read that file: {ex}", severity="error")
                    self.lyrics_done(path, "your file")
                self.push_screen(PromptScreen("Lyrics from a file", [("Path to a .lrc or .txt file", "")]), got)
            else:
                for ext in (".lrc", ".txt"):
                    path.with_suffix(ext).unlink(missing_ok=True)
                self.lyrics_done(path, None, removed=True)
        self.push_screen(PickScreen(f"Lyrics for “{title}”", opts), chosen)

    # ── session memory ──
    def save_session(self):
        d = dict(self.state)
        d.update(volume=self.volume, shuffle=self.shuffle, repeat=self.repeat, sort=self.sort)
        if self.cur_track is not None and self.root is not None:
            try:
                d["track"] = self.tkey(self.cur_track)
                d["pos"] = (self.mpv.cmd("get_property", "time-pos") or 0) if self.mpv else d.get("pos", 0)
            except ValueError:
                pass
        self.state = d
        try:
            save_state(d)
        except OSError:
            pass

    def restore_session(self):
        t = self.by_key.get(self.state.get("track") or "")
        if not t:
            return
        self.queue = list(self.tracks)
        self.idx = self.queue.index(t)
        self.start(t, paused=True)                  # ready to go, press play to resume where you left off
        try:
            pos = float(self.state.get("pos") or 0)
        except (TypeError, ValueError):
            pos = 0
        if pos > 2:
            self.set_timer(0.8, lambda: self.mpv.cmd("seek", pos, "absolute"))

    # ── events ──
    def on_input_changed(self, e: Input.Changed):
        if e.input.id == "filter":
            self.apply_filter(e.value)
        elif e.input.id == "hex" and self._slot:
            raw = e.value.strip().lstrip("#")
            c = norm_hex(raw) if len(raw) == 6 else None          # live preview once 6 digits are typed
            if c and c != THEME[self._slot]:
                self.apply_theme({self._slot: c})

    def on_input_submitted(self, e: Input.Submitted):
        if e.input.id == "filter":
            self.query_one("#lib").focus()
            if not e.value:
                e.input.display = False
        elif e.input.id == "hex" and self._slot:
            c = norm_hex(e.value)
            if c:
                e.input.value = c
                if c != THEME[self._slot]:
                    self.apply_theme({self._slot: c})
                self.query_one("#slots").focus()
            else:
                self.notify("not a colour — use #rrggbb", severity="warning")
        elif e.input.id == "playlist":
            self.do_dl_go()
        elif e.input.id == "query" and e.value.strip():
            q = e.value.strip()
            if q.startswith("http"):
                self.start_download(q, playlist="/playlist" in q)
            else:
                self.log_line(f"searching “{q}”…")
                self.do_search(q)

    def on_data_table_row_selected(self, e: DataTable.RowSelected):
        i = int(e.row_key.value)
        if self.mode == "queue":
            self.play_queue_row(i)
        elif not self.is_list_view():
            self.play(i, queue=list(self.view))
        elif i < len(self.rows):
            self.open_row(i)

    def on_option_list_option_selected(self, e: OptionList.OptionSelected):
        oid = e.option_list.id
        if oid == "presets":
            self.apply_theme(PRESETS[list(PRESETS)[e.option_index]])
            self.show_slot(self._slot or COLOR_KEYS[0], force=True)
        elif oid == "slots":
            self.query_one("#hex").focus()
        elif oid == "results":
            title, _, url = self.found[e.option_index]
            self.start_download(url, label=title)

    def on_option_list_option_highlighted(self, e: OptionList.OptionHighlighted):
        if e.option_list.id == "slots" and e.option.id and e.option.id != self._slot:
            self.show_slot(e.option.id)

    def on_button_pressed(self, e: Button.Pressed):
        {"prev": self.do_previous, "pp": self.do_play_pause, "next": self.do_next,
         "dl_go": self.do_dl_go, "dl_cancel": self.do_dl_cancel, "dl_update": self.do_update_ytdlp,
         "tool_lyrics": self.do_lyrics_missing, "tool_rescan": self.do_rescan,
         "set_save": self.do_set_save, "set_reset": self.do_set_reset}[e.button.id]()

    def on_key(self, e):
        if isinstance(self.screen, ModalScreen):
            return                                   # dialogs handle their own keys
        if e.key == "escape" and self.focused is not None and self.focused.id == "filter":
            self.query_one("#filter", Input).value = ""
            self.query_one("#filter").display = False
            self.query_one("#lib").focus()
            return e.stop()
        if e.key == "escape" and self.query_one(TabbedContent).active == "help":
            self.leave_help()
            return e.stop()
        if e.key == "escape" and self.drill:
            self.do_back()
            return e.stop()
        if e.is_printable and isinstance(self.focused, Input):
            return
        name = self.keymap.get(e.key)
        if name:
            getattr(self, "do_" + name)()
            e.stop()

    # ── download tab ──
    def log_line(self, s):
        self.query_one("#log", RichLog).write(s)

    @work(thread=True)
    def do_search(self, q):
        try:
            res = search(q)
        except Exception as ex:
            self.call_from_thread(self.log_line, f"✗ {ex}")
            return
        self.call_from_thread(self.show_results, res)

    def show_results(self, res):
        self.found = res
        have = make_have(self.tracks)
        ol = self.query_one("#results", OptionList)
        ol.clear_options()
        opts = []
        for t, u, _ in res:
            line = Text()
            line.append("✓ " if have(t, u) else "  ", style=THEME["accent2"])
            line.append(f"{t} — {u}")
            opts.append(Option(line))
        ol.add_options(opts)
        ol.focus()

    def start_download(self, url, playlist=False, label=None):
        if any(q[0] == url for q in self.dl_queue):
            return self.notify("already waiting in the queue", severity="warning")
        waiting = self.busy or self.updating
        self.dl_queue.append((url, playlist, label))
        if waiting:
            self.log_line(f"queued: {label or url}")
            self.notify(f"queued — {len(self.dl_queue)} waiting")
        self.pump_queue()

    def pump_queue(self):
        """Start the next waiting download when nothing else is running."""
        if self.busy or self.updating or not self.dl_queue:
            return
        url, playlist, label = self.dl_queue.pop(0)
        self.busy, self.dl = True, DLState()
        self.log_line(f"{'playlist' if playlist else 'downloading'}: {label or url}")
        self.fetch(url, playlist, make_have(self.tracks))

    @work(thread=True)
    def fetch(self, url, playlist, have=None):
        try:
            run_download(url, self.cfg, lambda s: self.call_from_thread(self.log_line, s), self.dl, playlist, have)
        finally:
            self.call_from_thread(self.finish_download)

    def finish_download(self):
        self.busy = False
        self.reload_library()
        self.pump_queue()

    def do_dl_go(self):
        url = self.query_one("#playlist", Input).value.strip()
        if not url.startswith("http"):
            return self.notify("paste a playlist or album link first", severity="warning")
        self.start_download(url, playlist=True)

    def do_dl_cancel(self):
        if self.busy and self.dl:
            self.dl.cancel.set()
            self.notify("cancelling…" + (f" ({len(self.dl_queue)} more waiting — press again when idle to clear them)" if self.dl_queue else ""))
        elif self.dl_queue:
            n = len(self.dl_queue)
            self.dl_queue.clear()
            self.notify(f"cleared {n} waiting download(s)")

    def do_update_ytdlp(self):
        if self.busy or self.updating:
            return self.notify("wait for the current download/update to finish", severity="warning")
        self.updating = True
        self.log_line("updating yt-dlp…")
        self.notify("updating yt-dlp…")
        self.update_ytdlp_job()

    @work(thread=True)
    def update_ytdlp_job(self):
        def version():
            r = subprocess.run([sys.executable, "-c", "import importlib.metadata as m; print(m.version('yt-dlp'))"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True, timeout=60, **NOWIN)
            return r.stdout.strip() or "unknown"
        log = lambda m: self.call_from_thread(self.log_line, m)
        try:
            before = version()
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, timeout=300, **NOWIN)
            if r.returncode != 0:
                log("✗ update failed: " + (r.stdout.strip().splitlines() or ["pip error"])[-1])
                msg = "yt-dlp update failed (see the log)"
            else:
                after = version()
                if after == before:
                    msg = f"yt-dlp is already up to date ({after})"
                else:
                    msg = f"yt-dlp updated {before} → {after}"
                    if "yt_dlp" in sys.modules:
                        msg += " — restart Sonora to use it"
                log("✓ " + msg)
        except Exception as ex:
            msg = f"yt-dlp update failed: {ex}"
            log("✗ " + msg)
        self.call_from_thread(self.update_done, msg)

    def update_done(self, msg):
        self.updating = False
        self.notify(msg)
        self.pump_queue()

    # ── library tools (settings page) ──
    def tool_say(self, text):
        self.query_one("#tool_status", Static).update(Text(text, style=THEME["dim"], no_wrap=True, overflow="ellipsis"))

    def do_rescan(self):
        self.reload_library()
        self.tool_say(f"rescanned: {len(self.tracks)} songs")
        self.notify(f"library rescanned — {len(self.tracks)} songs")

    def do_lyrics_missing(self):
        if self.lyr_running:
            self.lyr_stop.set()
            return self.notify("stopping after the current song…")
        skip = set(self.col["no_lyrics"])
        todo = [t for t in self.tracks
                if not t[3].with_suffix(".lrc").exists() and not t[3].with_suffix(".txt").exists() and self.tkey(t) not in skip]
        if not todo:
            msg = "every song already has lyrics" + (f" ({len(skip)} not found earlier — use L on a song to retry)" if skip else "")
            self.tool_say(msg)
            return self.notify(msg)
        self.lyr_running = True
        self.lyr_stop.clear()
        self.log_line(f"fetching lyrics for {len(todo)} songs…")
        self.notify(f"fetching lyrics for {len(todo)} songs — press the button again to stop")
        self.lyrics_missing_job(todo, list(self.cfg["lyrics_sources"]))

    @work(thread=True, exclusive=True, group="lyrics_all")
    def lyrics_missing_job(self, todo, sources):
        found, missed = 0, []
        for n, (artist, album, stem, path) in enumerate(todo, 1):
            if self.lyr_stop.is_set():
                break
            who = "" if artist == "Unknown" else artist
            text = None
            try:
                text, _ = fetch_lyrics(who, lyric_title(who, stem), None if album == "Singles" else album, None, sources)
                if text:
                    save_lyrics(path, text)
                    found += 1
                else:
                    missed.append(path.relative_to(self.root).as_posix())
            except Exception:
                missed.append(path.relative_to(self.root).as_posix())
            self.call_from_thread(self.lyrics_progress, n, len(todo), found, path, bool(text))
            time.sleep(0.4)                               # be polite to the lyrics sites
        self.call_from_thread(self.lyrics_finished, found, missed, len(todo))

    def lyrics_progress(self, n, total, found, path, ok):
        self.tool_say(f"lyrics {n}/{total} · {found} found")
        if ok and path == self.cur_path:
            self.load_lyrics(path)

    def lyrics_finished(self, found, missed, total):
        self.lyr_running = False
        self.col["no_lyrics"] = list(dict.fromkeys(self.col["no_lyrics"] + missed))
        self.save_col()
        stopped = self.lyr_stop.is_set()
        msg = f"lyrics {'stopped' if stopped else 'done'}: {found} found" + (f", {len(missed)} not found" if missed else "")
        self.tool_say(msg)
        self.log_line("— " + msg)
        self.notify(msg)

    def dl_tick(self):
        if self.query_one(TabbedContent).active != "download":
            return
        th, st = THEME, self.dl
        status, bar = self.query_one("#dl_status", Static), self.query_one("#dl_bar", Static)
        wait = f"  · {len(self.dl_queue)} waiting" if self.dl_queue else ""
        if self.updating:
            return status.update(Text("▶ updating yt-dlp…" + wait, style=th["accent"], no_wrap=True))
        if st is None:
            status.update(Text("idle — paste a link and press DOWNLOAD" + wait, style=th["dim"]))
            return bar.update("")
        if self.busy:
            status.update(Text(f"▶ {st.now or 'starting…'}  {st.pct:.0f}%{wait}", style=th["accent"],
                               no_wrap=True, overflow="ellipsis"))
        else:
            status.update(Text((f"✓ {st.note}" if st.note.startswith("finished") else f"— {st.note}") + wait,
                               style=th["text"], no_wrap=True, overflow="ellipsis"))
        total = max(1, st.total)
        label = f"{st.done + st.skipped}/{total} "
        w = max(5, bar.size.width - len(label))
        n = int(min(1.0, (st.done + st.skipped + st.pct / 100) / total) * w)
        out = Text(label, style=th["dim"], no_wrap=True)
        out.append("━" * n, style=th["accent"])
        out.append("─" * (w - n), style=th["line"])
        bar.update(out)

    # ── playback ──
    def play(self, i, queue=None):
        if queue is not None:
            self.queue = queue
        if not self.queue:
            return
        self.idx = i % len(self.queue)
        self.start(self.queue[self.idx])

    def play_upnext(self, track):
        """Play a song from the up-next list; the normal queue keeps its place for afterwards."""
        if self.idx is None:
            self.queue, self.idx = [track], 0
        self.start(track)

    def load_lyrics(self, path):
        self.lyrics, self.plain_lines = [], []
        for ext in (".lrc", ".txt"):
            f = path.with_suffix(ext)
            if f.exists():
                txt = f.read_text(errors="ignore")
                if ext == ".lrc":
                    self.lyrics = parse_lrc(txt)
                if not self.lyrics:
                    self.plain_lines = [l.strip() for l in txt.splitlines()
                                        if l.strip() and not re.fullmatch(r"\[.*\]", l.strip())]
                break

    def start(self, track, paused=False):
        artist, album, title, path = track
        self.cur_track, self.cur_path = track, path
        self.mpv.cmd("loadfile", str(path))
        self.mpv.cmd("set_property", "pause", paused)
        self.set_paused(paused)
        self.started, self._cur = time.time(), None
        self.wave, self.cover_img, self.cover_state, self._cover_key = None, None, "loading", None
        self.load_lyrics(path)
        self.query_one("#title", Static).update(
            Text((DECO if self.stars_on else "{}").format(f"{album} — {display_title(title)}"),
                 style="bold", no_wrap=True, overflow="ellipsis"))
        self.render_lyrics(0, 0)
        self.load_media(path)
        if self.mode == "queue":
            self.refresh_view()

    @work(thread=True, exclusive=True, group="media")
    def load_media(self, path):
        wave, img = compute_wave(path), load_cover(path)
        self.call_from_thread(self.set_media, path, wave, img)

    def set_media(self, path, wave, img):
        if path != self.cur_path:
            return
        self.wave = wave
        self.cover_state = img if isinstance(img, str) else ("ok" if img is not None else "none")
        self.cover_img = None if isinstance(img, str) else img
        self._cover_key = None

    def advance(self, step, auto=False):
        if auto and self.repeat and self.cur_track:
            return self.start(self.cur_track)
        if step > 0 and self.upnext:
            return self.play_upnext(self.upnext.pop(0))
        if not self.queue or self.idx is None:
            return
        self.play(random.randrange(len(self.queue)) if self.shuffle else self.idx + step)

    def seek_to(self, frac):
        dur = self.mpv.cmd("get_property", "duration") if self.idx is not None else None
        if dur:
            self.mpv.cmd("seek", max(0, min(1, frac)) * dur, "absolute")

    def set_paused(self, value):
        self.paused = value
        self.query_one("#pp", Button).label = "PLAY" if value else "PAUSE"

    # ── rendering ──
    def tick(self):
        if self.idx is None:
            return self.render_seek(0, 0)          # idle: keep the empty waveform sized to the window
        if not self.mpv:
            return
        if time.time() - self.started > 1.5 and self.mpv.cmd("get_property", "idle-active"):
            return self.advance(1, auto=True)
        pos = self.mpv.cmd("get_property", "time-pos") or 0
        dur = self.mpv.cmd("get_property", "duration") or 0
        self.render_seek(pos, dur)
        self.render_lyrics(pos, dur)
        self.render_cover()

    def render_seek(self, pos, dur):
        th = THEME
        self._seek_last = (pos, dur)
        sb = self.query_one("#seek", Seekbar)
        w = max(10, sb.size.width)
        has = bool(dur)
        frac = min(1, pos / dur) if has else 0
        head = int(frac * (w - 1)) if has else None
        hover = sb.hover if has else None
        top_cells, bot_cells = seek_cells(w, self.wave, frac, head, hover, self.paused, th)
        top, bot = Text(no_wrap=True), Text(no_wrap=True)
        for ch, st in top_cells:
            top.append(ch, st)
        for ch, st in bot_cells:
            bot.append(ch, st)
        top.append("\n")
        top.append_text(bot)
        sb.update(top)

        # times row:  elapsed ········ [badges / hover time] ········ total
        left = Text(fmt(pos), style=f"bold {th['accent2']}")
        right = Text(fmt(dur), style=th["dim"])
        mid = Text()
        if hover is not None:
            mid.append(f"▸ {fmt(hover / max(1, w - 1) * dur)}", style=th["text"])
        else:
            if self.paused and self.idx is not None:
                mid.append("❚❚ paused  ", style=th["dim"])
            if self.shuffle:
                mid.append("⇄ shuffle  ", style=th["accent"])
            if self.repeat:
                mid.append("↻ repeat", style=th["accent"])
        tw = max(10, self.query_one("#times").size.width)
        g1 = max(1, (tw - left.cell_len - mid.cell_len - right.cell_len) // 2)
        g2 = max(1, tw - left.cell_len - g1 - mid.cell_len - right.cell_len)
        times = Text(no_wrap=True)
        for part in (left, Text(" " * g1), mid, Text(" " * g2), right):
            times.append_text(part)
        self.query_one("#times", Static).update(times)

    def seek_redraw(self):
        if self._seek_last:
            self.render_seek(*self._seek_last)

    def render_lyrics(self, pos, dur):
        n = max(1, CFG["player"]["lyric_window"])
        if self.lyrics:
            lines = [t for _, t in self.lyrics]
            cur = max((k for k, (ts, _) in enumerate(self.lyrics) if ts <= pos), default=-1)
        elif self.plain_lines:                      # unsynced: scroll by track progress (approximate)
            lines = self.plain_lines
            cur = min(len(lines) - 1, int(pos / dur * len(lines))) if dur else 0
        else:
            lines, cur = None, None
        key = (cur, lines is not None)
        if key == self._cur:
            return
        self._cur = key
        out = Text(no_wrap=True, overflow="ellipsis", justify="center")
        if lines is None:
            out.append("no lyrics — press l to search", style=THEME["dim"])
        else:
            lo = cur - n // 2
            for k in range(lo, lo + n):
                txt = (lines[k] or "♪") if 0 <= k < len(lines) else ""
                style = f"bold {THEME['accent2']}" if k == cur else THEME["dim"]
                out.append(txt + ("\n" if k < lo + n - 1 else ""), style=style)
        self.query_one("#lyrics", Static).update(out)

    def star_active(self):
        return self.cover_mode == "star" or self.idx is None or self.cover_state == "none"

    def spin(self):
        if isinstance(self.screen, SplashScreen):
            return
        now = time.time()
        self.angle += (now - self._t) * (3.2 if self.busy else 0.9)   # spins faster while downloading
        self._t = now
        tab = self.query_one(TabbedContent).active
        if tab == "player" and self.star_active():
            self.draw_star("#cover", 72)
            self._cover_key = None
        elif tab == "download":
            self.draw_star("#dlstar", 40)

    def draw_star(self, sel, cap):
        box = self.query_one(sel).size
        if box.height < 4:
            return
        cols = max(8, min(box.width, box.height * 2, cap))
        self.query_one(sel, Static).update(star_text(cols, self.angle, THEME["accent"]))

    def twinkle(self):
        if not self.stars_on:
            return
        frame, th = int(time.time() / 0.6), THEME
        top = self.query_one("#stars_top", Static)
        row1 = star_row(top.size.width, frame, 1, th["accent"], th["dim"])
        row1.append("\n")
        row1.append_text(star_row(top.size.width, frame, 2, th["accent"], th["dim"]))
        top.update(row1)
        bot = self.query_one("#stars_bot", Static)
        bot.update(star_row(bot.size.width, frame, 3, th["accent"], th["dim"]))
        dl = self.query_one("#stars_dl", Static)
        dl.update(star_row(dl.size.width, frame, 4, th["accent"], th["dim"]))

    def do_cover_toggle(self):
        self.cover_mode = "star" if self.cover_mode == "album" else "album"
        self._cover_key = None

    def render_cover(self):
        if self.star_active():
            return
        box = self.query_one("#cover").size
        cols = max(8, min(box.width, box.height * 2, CFG["player"]["cover_cols"]))
        key = (cols, self.cover_state, id(self.cover_img))
        if key == self._cover_key:
            return
        self._cover_key = key
        if self.cover_state == "ok":
            out = cover_text(self.cover_img, cols, CFG["player"]["cover_style"])
        else:
            msg = {"pillow": "install Pillow to see covers:\n.venv/bin/pip install pillow"}.get(
                self.cover_state, "loading cover…")
            out = Text(msg, style=THEME["dim"], justify="center")
        self.query_one("#cover", Static).update(out)

    def volume_text(self, n):
        out = Text("VOL  ▕", style=THEME["dim"], no_wrap=True)
        for i in range(Volbar.WIDTH):
            if i < n:
                out.append("█", lerp_hex(THEME["accent"], THEME["accent2"], i / (Volbar.WIDTH - 1)))
            else:
                out.append("░", THEME["line"])
        out.append(f"▏ {self.volume}%", style=THEME["text"])
        return out

    def render_volume(self):
        n = round(self.volume / 100 * Volbar.WIDTH)
        self.query_one("#volume", Static).update(
            self.volume_text(n))

    # ── key actions (bound through [keys] in config.toml) ──
    def do_play_pause(self):
        if self.idx is None:
            return self.play(self.query_one("#lib", DataTable).cursor_row)
        self.set_paused(not self.paused)
        self.mpv.cmd("set_property", "pause", self.paused)

    def do_next(self): self.advance(1)
    def do_previous(self): self.advance(-1)
    def do_seek_back(self): self.mpv.cmd("seek", -5, "relative")
    def do_seek_forward(self): self.mpv.cmd("seek", 5, "relative")
    def do_shuffle(self): self.shuffle = not self.shuffle
    def do_repeat(self): self.repeat = not self.repeat
    def do_quit(self):
        if self.quitting or not CFG["player"].get("splash", True):
            return self.exit()
        self.quitting = True
        self.push_screen(SplashScreen("outro"), lambda _: self.exit())

    async def action_quit(self):
        self.do_quit()
    def do_volume_down(self): self.set_volume(-5)
    def do_volume_up(self): self.set_volume(5)

    def set_volume(self, delta=0, abs=None):
        self.volume = max(0, min(100, abs if abs is not None else self.volume + delta))
        self.mpv.cmd("set_property", "volume", self.volume)
        self.render_volume()

    def do_filter(self):
        f = self.query_one("#filter", Input)
        f.display = True
        f.focus()

    def do_lyrics(self):
        tracks = [] if self.is_list_view() else self.selected_tracks()
        if tracks:
            artist, _, title, path = tracks[0]
            self.notify("searching lyrics…")
            self.lyrics_job(path, artist, display_title(title), self.cfg["lyrics_sources"])

    @work(thread=True)
    def lyrics_job(self, path, artist, title, sources):
        text, src = fetch_lyrics(artist, clean_title(title), None, None, sources)
        if text:
            save_lyrics(path, text)
        self.call_from_thread(self.lyrics_done, path, src if text else None)

    def lyrics_done(self, path, src, removed=False):
        self.notify("lyrics removed" if removed else f"lyrics saved ({src})" if src else "no lyrics found")
        if (src or removed) and path == self.cur_path:
            self.load_lyrics(path)
            self.render_lyrics(0, 0)

    def do_tab_player(self):
        self.query_one(TabbedContent).active = "player"
        self.query_one("#lib").focus()

    def do_tab_download(self):
        self.query_one(TabbedContent).active = "download"
        self.query_one("#query").focus()

    def do_tab_settings(self):
        self.query_one(TabbedContent).active = "settings"
        self.query_one("#presets").focus()

    # ── settings tab: live colour editing ──
    def show_slot(self, name, force=False):
        if name == self._slot and not force:
            return
        self._slot = name
        self.query_one("#hex", Input).value = THEME[name]
        self.query_one("#hexlabel", Static).update(f"{name} — {SLOT_INFO[name]}   (Enter on a colour to edit)")

    def refresh_slots(self):
        ol = self.query_one("#slots", OptionList)
        hi = ol.highlighted or 0
        ol.clear_options()
        for k in COLOR_KEYS:
            t = Text()
            t.append("▕", style=THEME["dim"])
            t.append("   ", style=f"on {THEME[k]}")
            t.append("▏ ", style=THEME["dim"])
            t.append(f"{k:<11}{THEME[k]}   {SLOT_INFO[k]}")
            ol.add_option(Option(t, id=k))
        ol.highlighted = hi

    def refresh_presets(self):
        ol = self.query_one("#presets", OptionList)
        hi = ol.highlighted
        ol.clear_options()
        for name, p in PRESETS.items():
            t = Text(("✦ " if all(THEME[k] == p[k] for k in COLOR_KEYS) else "  ") + f"{name:<15}")
            t.append("  ", style=f"on {p['background']}")
            for k in ("accent", "accent2", "text"):
                t.append("██", style=p[k])
            ol.add_option(Option(t))
        if hi is not None:
            ol.highlighted = hi

    def apply_theme(self, changes):
        THEME.update(changes)
        self.refresh_css(animate=False)          # re-reads $sn-* variables -> restyles every widget
        self._cover_key = None                   # redraw cover / star in the new colours
        self.render_volume()
        self.seek_redraw()
        self.twinkle()
        self.refresh_slots()
        self.refresh_presets()
        self.query_one("#set_status", Static).update(Text("● unsaved — press SAVE to keep it", style=THEME["accent2"]))

    def do_set_save(self):
        save_theme(THEME)
        self.query_one("#set_status", Static).update(Text(f"✓ saved to {CONFIG_FILE}", style=THEME["accent"]))
        self.notify("colours saved")

    def do_set_reset(self):
        self.apply_theme(PRESETS["Sonora Purple"])
        self.show_slot(self._slot or COLOR_KEYS[0], force=True)


def main():
    missing = [b for b in ("mpv", "ffmpeg") if not shutil.which(b)]
    if missing:
        hint = "winget install mpv ffmpeg  (or scoop install mpv ffmpeg)" if IS_WIN else f"sudo apt install {' '.join(missing)}"
        sys.exit(f"missing programs: {', '.join(missing)}  ->  {hint}")
    Sonora().run()


if __name__ == "__main__":
    main()
