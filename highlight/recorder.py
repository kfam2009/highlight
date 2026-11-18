"""Grabar las senales SRT (Castr) directo desde el panel, sin OBS.

ffmpeg copia el video tal como llega (sin recodificar: casi no usa la PC) a
un MKV en la carpeta de grabaciones. Si la senal se corta, sigue intentando y
cuando vuelve abre un archivo nuevo ("parte 2"). "Cortar juego" cierra el
archivo actual y empieza otro al instante, para separar un partido del
siguiente cuando la senal es continua.
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from .ffmpeg import ffmpeg_exe

RETRY_S = 5
MONITOR_FPS = 25
MONITOR_W = 640
SRT_TIMEOUT_US = 15_000_000   # sin datos 15 s = senal caida


_SRT_EXE = None


def _exe_has_srt(exe):
    try:
        out = subprocess.run([exe, "-hide_banner", "-protocols"], capture_output=True, text=True, timeout=20).stdout
        return bool(re.search(r"^\s*srt\s*$", out, re.M))
    except Exception:
        return False


def srt_ffmpeg():
    """Un ffmpeg que sepa leer SRT: el del panel, o uno instalado aparte
    (por ejemplo con: winget install Gyan.FFmpeg)."""
    global _SRT_EXE
    if _SRT_EXE:
        return _SRT_EXE
    cands = []
    try:
        cands.append(ffmpeg_exe())
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        cands.append(found)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(str(Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"))
        cands += [str(p) for p in (Path(local) / "Microsoft" / "WinGet" / "Packages").glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe")]
    for exe in cands:
        if exe and Path(exe).exists() and _exe_has_srt(exe):
            _SRT_EXE = exe
            return exe
    return None


def has_srt():
    return srt_ffmpeg() is not None


def _safe(s):
    return re.sub(r"[^\w\- ]+", "", s).strip() or "Campo"


def _with_timeout(url):
    if not url.startswith("srt://") or "timeout=" in url:
        return url
    return url + ("&" if "?" in url else "?") + f"timeout={SRT_TIMEOUT_US}"


class Recorder:
    def __init__(self, name, url, folder, on_new_file=None, preview=None):
        self.name, self.url, self.folder = name, url, Path(folder)
        self.preview = bool(preview)   # video chico para el monitor del panel
        self.frame, self.frame_seq = None, 0
        self.frame_cv = threading.Condition()
        self.on_new_file = on_new_file
        self.proc = None
        self.file = None
        self.files = []
        self.status = "detenido"
        self.message = ""
        self.started_file_at = None
        self.last_cut = ""
        self._stop = threading.Event()
        self._cut = threading.Event()
        self.thread = None

    # ------------------------------------------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return self
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._kill()

    def cut(self, reason=""):
        """Cerrar el archivo actual y seguir grabando en uno nuevo."""
        self.last_cut = f"{datetime.now():%H:%M} {reason or 'corte a mano'}"
        self._cut.set()
        self._kill()

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.write(b"q")   # ffmpeg cierra bien el MKV
                p.stdin.flush()
            except Exception:
                pass
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    # ------------------------------------------------------------------ monitor
    def _read_frames(self, pipe):
        """Leer siempre la salida (si no, ffmpeg se frena y la grabacion
        tambien) y guardar el ultimo cuadro."""
        buf = b""
        try:
            while True:
                chunk = pipe.read1(65536) if hasattr(pipe, "read1") else pipe.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    a = buf.find(b"\xff\xd8")
                    if a < 0:
                        buf = b""
                        break
                    b = buf.find(b"\xff\xd9", a + 2)
                    if b < 0:
                        buf = buf[a:]
                        if len(buf) > 4_000_000:
                            buf = b""
                        break
                    with self.frame_cv:
                        self.frame = buf[a:b + 2]
                        self.frame_seq += 1
                        self.frame_cv.notify_all()
                    buf = buf[b + 2:]
        except Exception:
            pass

    def wait_frame(self, last_seq, timeout=5.0):
        with self.frame_cv:
            if self.frame_seq == last_seq or self.frame is None:
                self.frame_cv.wait(timeout)
            return self.frame, self.frame_seq

    # ------------------------------------------------------------------
    def _new_path(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        base = f"{_safe(self.name)} {datetime.now():%Y-%m-%d %H-%M}"
        p = self.folder / f"{base}.mkv"
        k = 2
        while p.exists():
            p = self.folder / f"{base} ({k}).mkv"
            k += 1
        return p

    def _run(self):
        while not self._stop.is_set():
            self._cut.clear()
            path = self._new_path()
            cmd = [srt_ffmpeg() or ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                   "-i", _with_timeout(self.url),
                   "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", "matroska", str(path)]
            if self.preview:
                # monitor: video chico en JPEGs seguidos por la salida estandar
                cmd += ["-map", "0:v:0", "-vf", f"fps={MONITOR_FPS},scale={MONITOR_W}:-2", "-q:v", "7",
                        "-c:v", "mjpeg", "-f", "mjpeg", "pipe:1"]
            if self.status != "sin señal":
                self.status, self.message = "conectando", "Esperando la señal"
            t0 = time.time()
            try:
                errf = tempfile.TemporaryFile()
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE if self.preview else subprocess.DEVNULL, stderr=errf)
                if self.preview:
                    threading.Thread(target=self._read_frames, args=(self.proc.stdout,), daemon=True).start()
            except Exception as exc:
                self.status, self.message = "error", str(exc)
                return
            announced = False
            while self.proc.poll() is None:
                if not announced and path.exists() and path.stat().st_size > 1_000_000:
                    announced = True
                    self.file, self.started_file_at = str(path), time.time()
                    self.files.append(str(path))
                    self.status, self.message = "grabando", path.name
                    if self.on_new_file:
                        try:
                            self.on_new_file(str(path))
                        except Exception:
                            pass
                time.sleep(1)
            errf.seek(0)
            err = errf.read().decode("utf-8", "replace").strip().splitlines()
            errf.close()
            if not announced and path.exists() and path.stat().st_size < 1_000_000:
                try:
                    path.unlink()   # intento fallido, sin video
                except OSError:
                    pass
            self.file = None
            with self.frame_cv:
                self.frame = None
                self.frame_cv.notify_all()
            if self._stop.is_set():
                break
            if self._cut.is_set():
                continue   # nuevo archivo enseguida
            self.status = "sin señal"
            self.message = (err[-1] if err else "Se cortó la señal") + " · reintentando"
            if time.time() - t0 < RETRY_S:
                self._stop.wait(RETRY_S)
        self.status, self.message = "detenido", ""

    def info(self):
        size = None
        if self.file:
            try:
                size = round(Path(self.file).stat().st_size / 1048576)
            except OSError:
                pass
        return {"name": self.name, "url": self.url, "status": self.status, "message": self.message,
                "file": Path(self.file).name if self.file else None, "sizeMb": size,
                "since": self.started_file_at if self.file else None,
                "files": [Path(f).name for f in self.files], "lastCut": self.last_cut}
