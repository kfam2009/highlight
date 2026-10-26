"""Una sola pasada de ffmpeg sobre el video original.

Genera en la carpeta del analisis:
  scorebug.mp4   recorte del scorebug a 5 fps (para leer el marcador)
  contexto.raw   cuadros de 64x36 a 2 fps (para detectar replays/stingers)
  audio.wav      audio mono 8 kHz (relato / publico)

El video original no se toca y no se copia. Esto es lo unico que lee el
partido completo; los clips se cortan despues solo en los tramos elegidos.
"""
import wave
from pathlib import Path

import numpy as np

from .ffmpeg import ffmpeg_exe, run_with_progress

CTX_W, CTX_H, CTX_FPS = 64, 36, 2
AUDIO_RATE = 8000
RMS_RATE = 4  # valores de volumen por segundo


def extract(video, out_dir, profile, duration=None, on_progress=None, crop=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    c = crop or profile.crop
    W, H = profile.crop_size
    crop = (f"crop=trunc(iw*{c['w']}/2)*2:trunc(ih*{c['h']}/2)*2:trunc(iw*{c['x']}):trunc(ih*{c['y']}),"
            f"scale={W}:{H}")
    fc = (f"[0:v]fps={profile.fps},{crop}[sb];"
          f"[0:v]fps={CTX_FPS},scale={CTX_W}:{CTX_H}:flags=area[cx]")
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-y", "-i", str(video),
        "-filter_complex", fc,
        "-map", "[sb]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(out_dir / "scorebug.mp4"),
        "-map", "[cx]", "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", str(out_dir / "contexto.raw"),
        "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-c:a", "pcm_s16le", str(out_dir / "audio.wav"),
    ]
    run_with_progress(cmd, duration, on_progress)
    return out_dir


def load_context(out_dir):
    p = Path(out_dir) / "contexto.raw"
    if not p.exists():
        return None
    data = np.fromfile(p, dtype=np.uint8)
    n = data.size // (CTX_W * CTX_H * 3)
    return data[: n * CTX_W * CTX_H * 3].reshape(n, CTX_H, CTX_W, 3)


def find_stingers(ctx, template, thr=25.0):
    """Tiempos (s) donde aparece la placa de transicion de los replays."""
    if ctx is None or template is None:
        return []
    t = template.astype(np.float32)
    out = []
    step = 2000
    for i in range(0, len(ctx), step):
        d = np.abs(ctx[i:i + step].astype(np.float32) - t).mean((1, 2, 3))
        out.extend((np.where(d < thr)[0] + i).tolist())
    return [i / CTX_FPS for i in out]


def load_audio_rms(out_dir):
    p = Path(out_dir) / "audio.wav"
    if not p.exists() or p.stat().st_size < 1000:
        return None
    with wave.open(str(p)) as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    hop = sr // RMS_RATE
    n = len(a) // hop
    if n == 0:
        return None
    a = a[: n * hop].reshape(n, hop)
    return np.sqrt((a ** 2).mean(1))
