"""Grabar las senales SRT (Castr) directo desde el panel.

ffmpeg copia el video tal como llega (sin recodificar: casi no usa la PC) a
un MKV en la carpeta de grabaciones. Si la senal se corta, sigue intentando y
cuando vuelve abre un archivo nuevo ("parte 2"). "Cortar juego" cierra el
archivo actual y empieza otro al instante, para separar un partido del
siguiente cuando la senal es continua.
"""
import json
import os
import queue
import socket
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
MONITOR_FPS = 30   # la senal viene a 60: 30 es justo la mitad y se ve parejo
MONITOR_W = 640
SRT_TIMEOUT_US = 15_000_000   # sin datos 15 s = senal caida
SRT_LATENCY_US = 2_000_000    # 2 s de margen para recuperar paquetes (senal internacional)


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
    """Opciones SRT para una senal que viene de lejos por internet: margen
    de 2 s para recuperar paquetes perdidos (el valor por defecto, 120 ms, es
    para redes locales y con una senal de otro pais se entrecorta y se cae)."""
    if not url.startswith("srt://"):
        return url
    extra = []
    if "timeout=" not in url:
        extra.append(f"timeout={SRT_TIMEOUT_US}")
    if "latency=" not in url:
        extra.append(f"latency={SRT_LATENCY_US}")
    if not extra:
        return url
    return url + ("&" if "?" in url else "?") + "&".join(extra)


class Recorder:
    def __init__(self, name, url, folder, on_new_file=None, preview=None, pid_dir=None, title_fn=None):
        self.name, self.url, self.folder = name, url, Path(folder)
        # pid del ffmpeg: si el panel se cierra de golpe, al volver se cierra el que quedo suelto
        self.pid_file = Path(pid_dir) / f"{_safe(name)}.pid" if pid_dir else None
        self.preview = bool(preview)   # video chico para el monitor del panel
        self.frame, self.frame_seq = None, 0
        self.frame_cv = threading.Condition()
        # sonido del monitor: ffmpeg manda MP3 por UDP local (nunca frena la grabacion)
        self.no_audio = False
        self.listeners = set()
        self.listeners_lock = threading.Lock()
        self.audio_sock = None
        self.audio_level = -90.0
        self.level_t = 0.0
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
        self.record = False   # False = solo monitor (se ve la senal, no se graba)
        self.title_fn = title_fn   # nombre del partido segun el fixture (o None)
        # estado para retomar la grabacion si el panel se reinicia: el ffmpeg
        # sigue grabando solo y el panel nuevo se vuelve a enganchar
        self.state_file = Path(pid_dir) / f"{_safe(name)}.rec.json" if pid_dir else None
        self.ports = {}        # puertos UDP fijos: video, audio y nivel del monitor
        self.video_sock = None
        self._adopt = None     # estado de un ffmpeg que ya estaba grabando

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

    def set_record(self, on):
        """Pasar de solo monitor a grabar (o al reves): se reconecta en el acto."""
        if bool(on) == self.record:
            return
        self.record = bool(on)
        self._cut.set()
        self._kill()

    def cut(self, reason=""):
        """Cerrar el archivo actual y seguir grabando en uno nuevo."""
        self.last_cut = f"{datetime.now():%H:%M} {reason or 'corte a mano'}"
        self._cut.set()
        self._kill()

    def _kill(self):
        pid = getattr(self, "adopted_pid", None)
        if pid:
            # ffmpeg de antes del reinicio del panel: no tenemos su entrada
            # para pedirle que cierre, se lo cierra de una
            _kill_pid(pid)
            return
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

    def _udp(self, key, rcvbuf=1 << 20):
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sk.bind(("127.0.0.1", int(self.ports.get(key) or 0)))
        except OSError:
            sk.bind(("127.0.0.1", 0))
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.ports[key] = sk.getsockname()[1]
        return sk

    def _video_port(self):
        if self.video_sock is None:
            try:
                self.video_sock = self._udp("video", 8 << 20)
                threading.Thread(target=self._read_video_udp, daemon=True).start()
            except OSError:
                return None
        return self.video_sock.getsockname()[1]

    def _read_video_udp(self):
        class _Src:
            def __init__(self, sk):
                self.sk = sk

            def read1(self, n):
                while True:
                    try:
                        return self.sk.recv(65536)
                    except OSError:
                        time.sleep(0.5)
        self._read_frames(_Src(self.video_sock))

    def _audio_port(self):
        if self.audio_sock is None:
            try:
                sk = self._udp("audio")
                self.audio_sock = sk
                threading.Thread(target=self._read_audio, daemon=True).start()
            except OSError:
                return None
        return self.audio_sock.getsockname()[1]

    def _level_port(self):
        if getattr(self, "level_sock", None) is None:
            try:
                self.level_sock = self._udp("level")
                threading.Thread(target=self._read_level, daemon=True).start()
            except OSError:
                self.level_sock = None
                return None
        return self.level_sock.getsockname()[1]

    def _read_level(self):
        import array
        import math
        while True:
            try:
                data = self.level_sock.recv(65536)
            except OSError:
                time.sleep(1)
                continue
            a = array.array("h")
            a.frombytes(data[: len(data) // 2 * 2])
            if not len(a):
                continue
            rms = math.sqrt(sum(x * x for x in a) / len(a)) / 32768.0
            db = 20 * math.log10(rms + 1e-6)
            # sube rapido, baja despacio
            self.audio_level = db if db > self.audio_level else 0.85 * self.audio_level + 0.15 * db
            self.level_t = time.time()

    def _read_audio(self):
        while True:
            try:
                data = self.audio_sock.recv(65536)
            except OSError:
                time.sleep(1)
                continue
            with self.listeners_lock:
                for q in list(self.listeners):
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        pass   # el que escucha esta lento: se saltea un pedazo

    def listen(self):
        q = queue.Queue(maxsize=400)
        with self.listeners_lock:
            self.listeners.add(q)
        return q

    def unlisten(self, q):
        with self.listeners_lock:
            self.listeners.discard(q)

    def wait_frame(self, last_seq, timeout=5.0):
        with self.frame_cv:
            if self.frame_seq == last_seq or self.frame is None:
                self.frame_cv.wait(timeout)
            return self.frame, self.frame_seq

    def adopt(self, state):
        """Engancharse a un ffmpeg que siguio grabando mientras el panel se
        reiniciaba (la grabacion no se corta)."""
        self.ports = dict(state.get("ports") or {})
        self.record = True
        self._adopt = state
        return self.start()

    def _save_state(self, pid, path, recording):
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"pid": pid, "file": str(path) if path else None,
                                                   "record": recording, "ports": self.ports, "url": self.url,
                                                   "started": self.started_file_at, "t": time.time()}),
                                       encoding="utf-8")
        except OSError:
            pass

    def _clear_state(self):
        if self.state_file:
            try:
                self.state_file.unlink()
            except OSError:
                pass

    def _run_adopted(self, st):
        """El ffmpeg de antes del reinicio sigue grabando: se espera a que
        termine (corte de senal, Cortar juego, Detener) y despues sigue normal."""
        self.adopted_pid = int(st["pid"])
        path = Path(st["file"])
        if self.preview:
            self._video_port()
            self._audio_port()
            self._level_port()
        self.file, self.started_file_at = str(path), st.get("started") or time.time()
        self.files.append(str(path))
        self.status, self.message = "grabando", path.name
        if self.on_new_file:
            try:
                self.on_new_file(str(path))
            except Exception:
                pass
        n = 0
        while not self._stop.is_set() and not self._cut.is_set():
            if self.record is not True:
                self._cut.set()
                break
            n += 1
            if n % 3 == 0 and not _is_ffmpeg(self.adopted_pid):
                break
            time.sleep(1)
        if self._cut.is_set() or self._stop.is_set():
            _kill_pid(self.adopted_pid)
        self.adopted_pid = None
        self.file = None
        with self.frame_cv:
            self.frame = None
            self.frame_cv.notify_all()

    # ------------------------------------------------------------------
    def _new_path(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        game = None
        if self.title_fn:
            try:
                game = self.title_fn()
            except Exception:
                game = None
        if game:
            # "Gm 03 Honduras vs Puerto Rico"; si se corto y volvio: "... parte 2"
            base = game
            p = self.folder / f"{base}.mkv"
            k = 2
            while p.exists():
                p = self.folder / f"{base} parte {k}.mkv"
                k += 1
            return p
        base = f"{_safe(self.name)} {datetime.now():%Y-%m-%d %H-%M}"
        p = self.folder / f"{base}.mkv"
        k = 2
        while p.exists():
            p = self.folder / f"{base} ({k}).mkv"
            k += 1
        return p

    def _run(self):
        if self._adopt:
            st, self._adopt = self._adopt, None
            try:
                self._run_adopted(st)
            except Exception:
                self.adopted_pid = None
        while not self._stop.is_set():
            self._cut.clear()
            recording = self.record
            path = self._new_path() if recording else None
            cmd = [srt_ffmpeg() or ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                   "-i", _with_timeout(self.url)]
            if recording:
                cmd += ["-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", "matroska", str(path)]
            if self.preview:
                # monitor: video chico en JPEGs seguidos por la salida estandar
                # (por UDP local y no por la salida estandar: si el panel se
                # reinicia, ffmpeg sigue grabando y el panel nuevo se engancha)
                vport = self._video_port()
                cmd += ["-map", "0:v:0", "-vf", f"fps={MONITOR_FPS},scale={MONITOR_W}:-2", "-q:v", "7",
                        "-c:v", "mjpeg", "-f", "mjpeg", f"udp://127.0.0.1:{vport}?pkt_size=32000"]
                if not self.no_audio:
                    port = self._audio_port()
                    if port:
                        # para escuchar: volumen nivelado solo (la senal suele venir baja);
                        # la grabacion guarda el audio original sin tocar
                        cmd += ["-map", "0:a:0?", "-af", "dynaudnorm=f=150:g=5:m=30", "-ac", "2", "-ar", "44100",
                                "-c:a", "libmp3lame", "-b:a", "128k",
                                "-f", "mp3", f"udp://127.0.0.1:{port}?pkt_size=1316"]
                    lport = self._level_port()
                    if lport:
                        # audio crudo chiquito solo para el medidor de nivel
                        cmd += ["-map", "0:a:0?", "-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le",
                                "-f", "s16le", f"udp://127.0.0.1:{lport}?pkt_size=800"]
            if self.status != "sin señal":
                self.status, self.message = "conectando", "Esperando la señal"
            t0 = time.time()
            try:
                errf = tempfile.TemporaryFile()
                # en Windows, sin consola propia: si se cierra la ventana del
                # panel, ffmpeg no se entera y la grabacion sigue
                flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errf,
                                             creationflags=flags)
                self._save_state(self.proc.pid, path, recording)
                if self.pid_file:
                    try:
                        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
                        self.pid_file.write_text(str(self.proc.pid))
                    except OSError:
                        pass
            except Exception as exc:
                self.status, self.message = "error", str(exc)
                return
            announced = False
            while self.proc.poll() is None:
                if self.record != recording:
                    # pidieron grabar (o dejar de grabar) justo mientras arrancaba
                    # la conexion: se reconecta en el modo correcto
                    self._cut.set()
                    self._kill()
                    break
                if not recording:
                    if self.frame is not None and self.status != "monitor":
                        self.status, self.message = "monitor", "Viendo la señal (sin grabar)"
                    time.sleep(1)
                    continue
                if not announced and path.exists() and path.stat().st_size > 1_000_000:
                    announced = True
                    self.file, self.started_file_at = str(path), time.time()
                    self.files.append(str(path))
                    self.status, self.message = "grabando", path.name
                    self._save_state(self.proc.pid, path, recording)
                    if self.on_new_file:
                        try:
                            self.on_new_file(str(path))
                        except Exception:
                            pass
                time.sleep(1)
            errf.seek(0)
            err = errf.read().decode("utf-8", "replace").strip().splitlines()
            errf.close()
            if recording and not announced and path.exists() and path.stat().st_size < 1_000_000:
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
            if not announced and self.status != "monitor" and not self.no_audio and any("does not contain any stream" in e or "matches no streams" in e
                                                           for e in err):
                self.no_audio = True   # la senal viene sin audio: grabar sin el sonido del monitor
                continue
            self.status = "sin señal"
            self.message = (err[-1] if err else "Se cortó la señal") + " · reintentando"
            if time.time() - t0 < RETRY_S:
                self._stop.wait(RETRY_S)
        if self.pid_file:
            try:
                self.pid_file.unlink()
            except OSError:
                pass
        self._clear_state()
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
                "files": [Path(f).name for f in self.files], "lastCut": self.last_cut, "record": self.record,
                "level": round(self.audio_level, 1) if time.time() - self.level_t < 3 else None}


def _kill_pid(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except Exception:
        pass


def adoptable(pid_dir):
    """Grabaciones que siguieron solas mientras el panel se reiniciaba:
    {nombre del archivo de estado: estado}."""
    out = {}
    for f in Path(pid_dir).glob("*.rec.json"):
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if st.get("record") and st.get("pid") and st.get("file") and Path(st["file"]).exists() \
                and _is_ffmpeg(int(st["pid"])):
            out[f.name[:-len(".rec.json")]] = st
    return out


def kill_orphans(pid_dir, keep=()):
    """Cerrar ffmpeg de grabaciones que quedaron sueltos (el panel se cerro de
    golpe), menos los que se retoman (keep: pids)."""
    keep = {int(k) for k in keep}
    for f in Path(pid_dir).glob("*.pid"):
        try:
            pid = int(f.read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid in keep:
            continue
        if pid and _is_ffmpeg(pid):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
                else:
                    os.kill(pid, 15)
            except Exception:
                pass
        try:
            f.unlink()
        except OSError:
            pass
    # ffmpeg de grabacion SRT que quedaron sin archivo .pid (por ejemplo si
    # hubo dos paneles abiertos a la vez): se cierran todos
    if os.name == "nt":
        try:
            cmd = ("Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
                   "Where-Object { ($_.CommandLine -like '*srt://*' -or $_.CommandLine -like '*-follow 1*') -and @(%s) -notcontains $_.ProcessId } | "
                   "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }") % (",".join(str(k) for k in keep) or "0")
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                           capture_output=True, timeout=30)
        except Exception:
            pass


def _is_ffmpeg(pid):
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout.lower()
            return "ffmpeg" in out
        return b"ffmpeg" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return False
