"""Ubicar ffmpeg y helpers chicos para llamarlo."""
import os
import re
import shutil
import subprocess
from pathlib import Path

_FFMPEG = None


def ffmpeg_exe():
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    env = os.environ.get("HIGHLIGHT_FFMPEG")
    if env and Path(env).exists():
        _FFMPEG = env
        return _FFMPEG
    try:
        import imageio_ffmpeg  # type: ignore

        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        return _FFMPEG
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        _FFMPEG = found
        return _FFMPEG
    raise RuntimeError(
        "No encuentro ffmpeg. Instalalo con 'pip install imageio-ffmpeg' "
        "o defini la variable HIGHLIGHT_FFMPEG con la ruta a ffmpeg.exe"
    )


def probe_duration(video):
    """Duracion en segundos leyendo la salida de ffmpeg -i."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(video)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def hms(seconds, ms=True):
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    seconds -= h * 3600
    m = int(seconds // 60)
    seconds -= m * 60
    return f"{h:02d}:{m:02d}:{seconds:06.3f}" if ms else f"{h:d}:{m:02d}:{int(seconds):02d}"


def run_with_progress(cmd, duration, on_progress=None):
    """Corre ffmpeg con -progress y reporta 0..1."""
    full = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore")
    err_tail = []

    import threading

    def drain():
        for line in proc.stderr:
            err_tail.append(line)
            if len(err_tail) > 40:
                err_tail.pop(0)

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    for line in proc.stdout:
        if on_progress and duration and line.startswith("out_time_ms="):
            try:
                v = int(line.split("=", 1)[1]) / 1_000_000
                on_progress(min(1.0, v / duration))
            except ValueError:
                pass
    proc.wait()
    t.join(timeout=2)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg fallo:\n" + "".join(err_tail[-15:]))
