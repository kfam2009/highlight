"""Modo en vivo: analizar el partido mientras se graba.

Un solo ffmpeg sigue el archivo a medida que crece (-follow) y va dejando:
  scorebug_NNN.mp4   tramos de 60 s del recorte del scorebug
  contexto.raw       imagen chica a 2 fps (se va agregando)
  audio.raw          audio mono 8 kHz (se va agregando)
Cada ~30 s se leen los tramos nuevos, se rearma la historia del partido y se
publican las jugadas que ya tienen margen suficiente (el scorebug se
actualiza tarde y el replay viene despues). Cuando el archivo deja de crecer
unos minutos, ffmpeg termina y se cierra el analisis con el clip Final.
Los ids de las jugadas son estables: lo que el operador ya aprobo no se mueve.
"""
import csv
import json
import subprocess
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np

from . import events as ev_mod
from .analyze import PRE_HANDLE, PREVIEW_HANDLE, build_clips, half_label, pregame, refine_final, score_text, _np
from .extract import CTX_FPS, CTX_H, CTX_W, AUDIO_RATE, RMS_RATE, find_stingers
from .ffmpeg import ffmpeg_exe, hms
from .render import cut, poster, safe_name
from .scorebug import Profile, read_scorebug_video, list_profiles

SEGMENT_S = 60          # tramos de scorebug
PUBLISH_MARGIN = 50.0   # segundos que se espera despues del cambio del scorebug
IDLE_END_S = 180        # sin crecer este tiempo = termino la grabacion
IDLE_DONE_S = 90        # en vivo: archivo quieto este tiempo = se cierra el analisis


def _frames_from_start(video, seconds=240, every=12, width=960):
    """Fotos de los primeros minutos (sin buscar dentro del archivo, que en una
    grabacion en curso puede no tener indice)."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(video), "-t", str(seconds),
           "-vf", f"fps=1/{every},scale={width}:-2,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600)
    data = proc.stdout
    # alto real segun la relacion de aspecto del video
    probe = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(video)], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, errors="ignore").stderr
    import re
    m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", probe)
    w0, h0 = (int(m.group(1)), int(m.group(2))) if m else (1920, 1080)
    h = int(round(width * h0 / w0 / 2)) * 2
    n = len(data) // (width * h)
    return [np.frombuffer(data[i * width * h:(i + 1) * width * h], np.uint8).reshape(h, width) for i in range(n)]


def _frames_at(video, times, width=960):
    """Fotos sueltas en esos segundos (el MKV que se esta grabando se puede
    saltar a un punto sin leer todo lo anterior)."""
    out = []
    for t in times:
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.1f}", "-i", str(video),
               "-frames:v", "1", "-vf", f"scale={width}:-2,format=gray", "-f", "image2pipe", "-vcodec", "png", "-"]
        try:
            data = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30).stdout
        except subprocess.TimeoutExpired:
            continue
        if data:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                out.append(img)
    return out


def _locate_in_frames(frames, profile_names):
    from .locate import locate_frames

    best = None
    for name in profile_names:
        prof = Profile(name)
        crop, info = locate_frames(frames, prof)
        if info.get("status") in ("en su lugar", "corrido"):
            key = (info["found"], info.get("score", 0))
            if best is None or key > best[0]:
                best = (key, name, crop, info)
    return best


def load_audio_rms_raw(path):
    p = Path(path)
    if not p.exists() or p.stat().st_size < 1000:
        return None
    a = np.fromfile(p, dtype=np.int16).astype(np.float32)
    hop = AUDIO_RATE // RMS_RATE
    n = len(a) // hop
    if n == 0:
        return None
    return np.sqrt((a[: n * hop].reshape(n, hop) ** 2).mean(1))


def load_ctx_raw(path):
    p = Path(path)
    if not p.exists():
        return None
    data = np.fromfile(p, dtype=np.uint8)
    n = data.size // (CTX_W * CTX_H * 3)
    return data[: n * CTX_W * CTX_H * 3].reshape(n, CTX_H, CTX_W, 3) if n else None


class LiveSession:
    def __init__(self, video, runs_dir, profile_name="auto", on_done=None, on_game_over=None, profile_hint=None):
        self.video = Path(video).resolve()
        self._pc_tpl, self._pc_n = None, 0
        self.runs_dir = Path(runs_dir)
        self.profile_name = profile_name
        self.on_done = on_done
        self.on_game_over = on_game_over   # grabacion de todo el dia: avisar fin del partido
        self.profile_hint = profile_hint   # (perfil, recorte, info) del partido anterior del mismo campo
        self.game_over = None
        self.profile = None
        self.crop = None
        self.status = "esperando"
        self.message = "Esperando los primeros minutos de grabación"
        self.error = None
        self.run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_name(self.video.stem)}"
        self.run_dir = self.runs_dir / self.run_id
        self.work = self.run_dir / "trabajo"
        self.clip_dir = self.run_dir / "clips"
        self.processed_s = 0.0
        self.pregame = None
        self.raw = None
        self.cache = {}
        self.segments_done = set()
        self.ids = {}          # clave estable -> id
        self.published = {}    # id -> clip
        self.proc = None
        self.thread = None
        self.stop_flag = False
        self.finish_flag = False
        self.finished = False

    # ------------------------------------------------------------------ util
    def info(self):
        return {"id": "live_" + self.run_id, "path": str(self.video), "name": self.video.name, "status": self.status,
                "message": self.message, "error": self.error, "runId": self.run_id if self.run_dir.exists() else None,
                "progress": None, "live": True, "processed": round(self.processed_s, 1), "auto": True,
                "finished": self.finished, "scoreNow": self.score_now()}

    def score_now(self):
        """Lo ultimo que dice el scorebug (para el monitor)."""
        r = self.raw
        if not r or not r.get("present"):
            return None
        out = {}
        for k in ("inning", "half", "outs", "away", "home", "count"):
            v = next((x for x in reversed(r[k][-300:]) if x is not None), None)
            out[k] = v
        out["onAir"] = bool(any(r["present"][-10:]))
        out["at"] = round(self.processed_s, 1)
        if self.game_over:
            out["over"] = self.game_over.get("reason")
        return out

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def finish(self):
        """La grabacion de este archivo termino: cerrar el analisis apenas
        el archivo deje de crecer."""
        self.finish_flag = True

    def stop(self):
        self.stop_flag = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    # ------------------------------------------------------------------ main
    def _run(self):
        try:
            if not self._wait_initial(2 * 1024 * 1024 if (self.profile_hint or self.profile_name not in (None, "", "auto"))
                                      else 150 * 1024 * 1024):
                self.status, self.finished = "corto", True
                self.message = "Grabación muy corta, no se analiza (se puede analizar a mano)"
                return
            self._pick_profile()
            self._start_ffmpeg()
            last_size, grew_at = -1, time.time()
            while True:
                done = self.proc.poll() is not None
                # la grabacion termino (Cortar juego, Detener, se corto la senal):
                # si el archivo no crece mas, se cierra el analisis enseguida y
                # sale el Final (ffmpeg con -follow no siempre se entera solo)
                try:
                    size = self.video.stat().st_size
                except OSError:
                    size = last_size
                if size != last_size:
                    last_size, grew_at = size, time.time()
                elif not done and (time.time() - grew_at > IDLE_DONE_S or self.finish_flag) \
                        and time.time() - grew_at > 20:
                    time.sleep(5)   # que termine de escribir lo ultimo que leyo
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=20)
                    except Exception:
                        pass
                    done = True
                try:
                    self._cycle(final=False)
                except Exception:
                    # un error de un ciclo (archivo tomado, disco ocupado...) no
                    # corta el analisis: se reintenta en el proximo
                    traceback.print_exc()
                    self.errors = getattr(self, "errors", 0) + 1
                    if self.errors > 20:
                        raise
                if done or self.stop_flag:
                    break
                time.sleep(30)
            self._cycle(final=True)
            self.status = "listo"
            self.finished = True
            if self.on_done:
                try:
                    extra = self.on_done(self._manifest())
                    if extra:
                        self.message += extra
                except Exception:
                    traceback.print_exc()
        except Exception as exc:
            traceback.print_exc()
            self.status = "error"
            self.error = str(exc)

    def _wait_initial(self, need_bytes=150 * 1024 * 1024):
        # unos minutos de grabacion antes de buscar el scorebug
        last, since = -1, time.time()
        while not self.stop_flag:
            size = self.video.stat().st_size
            if size >= need_bytes:
                return True
            if size != last:
                last, since = size, time.time()
            elif time.time() - since > IDLE_END_S:
                return False   # dejo de crecer antes de tener video suficiente
            time.sleep(15)
        return False

    def _pick_profile(self):
        self.status = "en vivo"
        self.message = "Buscando el scorebug"
        if self.profile_hint:
            # mismo campo, misma transmision: el scorebug esta donde estaba
            self.profile_name, self.crop, self.loc_info = self.profile_hint
            self.profile = Profile(self.profile_name)
            return
        if self.profile_name not in (None, "", "auto"):
            # perfil elegido en la configuracion: el scorebug esta donde dice el
            # perfil; se arranca enseguida sin buscarlo (buscar en un archivo que
            # se esta grabando es lento)
            self.profile = Profile(self.profile_name)
            self.crop, self.loc_info = None, {"status": "posicion del perfil"}
            return
        names = [p["name"] for p in list_profiles()]
        frames = _frames_from_start(self.video)
        best = _locate_in_frames(frames, names) if len(frames) >= 6 else None
        # la grabacion puede arrancar mucho antes del juego (previa, himnos):
        # se mira cada minuto el ultimo tramo grabado hasta que aparece el scorebug
        last_size, last_t, rate = self.video.stat().st_size, time.time(), None
        idle_since = time.time()
        while not best and not self.stop_flag:
            time.sleep(60)
            size, now = self.video.stat().st_size, time.time()
            if size > last_size:
                rate = (size - last_size) / (now - last_t)
                idle_since = now
            elif now - idle_since > IDLE_END_S:
                break   # dejo de grabar sin que aparezca el scorebug
            last_size, last_t = size, now
            if not rate:
                continue
            dur = size / rate
            self.message = f"Buscando el scorebug (llevo {int(dur // 60)} min de grabación)"
            times = [max(0.0, dur - 600 + k * 50) for k in range(12)]
            best = _locate_in_frames(_frames_at(self.video, [t for t in times if t < dur - 3]), names)
        if not best:
            raise RuntimeError("No encuentro el scorebug en los primeros minutos del video")
        _, self.profile_name, self.crop, self.loc_info = best
        self.profile = Profile(self.profile_name)

    def _start_ffmpeg(self):
        self.work.mkdir(parents=True, exist_ok=True)
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        p = self.profile
        c = self.crop or p.crop
        W, H = p.crop_size
        crop = (f"crop=trunc(iw*{c['w']}/2)*2:trunc(ih*{c['h']}/2)*2:trunc(iw*{c['x']}):trunc(ih*{c['y']}),"
                f"scale={W}:{H}")
        fc = (f"[0:v]fps={p.fps},{crop}[sb];"
              f"[0:v]fps={CTX_FPS},scale={CTX_W}:{CTX_H}:flags=area[cx]")
        cmd = [
            ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-follow", "1", "-rw_timeout", str(IDLE_END_S * 1_000_000), "-i", f"file:{self.video}",
            "-filter_complex", fc,
            "-map", "[sb]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-g", str(p.fps * 2),
            "-f", "segment", "-segment_time", str(SEGMENT_S), "-reset_timestamps", "1",
            "-segment_list", str(self.work / "scorebug_lista.csv"), "-segment_list_type", "csv",
            str(self.work / "scorebug_%04d.mp4"),
            "-map", "[cx]", "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-flush_packets", "1",
            str(self.work / "contexto.raw"),
            "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-f", "s16le", "-flush_packets", "1",
            str(self.work / "audio.raw"),
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        (self.work / "ubicacion_scorebug.json").write_text(json.dumps(self.loc_info), encoding="utf-8")
        self.message = "Leyendo el partido en vivo"

    # ------------------------------------------------------------------ ciclo
    def _new_segments(self):
        lst = self.work / "scorebug_lista.csv"
        if not lst.exists():
            return []
        out = []
        with open(lst, newline="") as fh:
            for row in csv.reader(fh):
                if row and row[0] not in self.segments_done:
                    out.append(row)
        return out

    def _cycle(self, final):
        for row in self._new_segments():
            name = row[0]
            seg = read_scorebug_video(self.work / name, self.profile, cache=self.cache)
            if self.raw is None:
                self.raw = {k: [] for k in seg if isinstance(seg[k], list)}
                self.raw["fps"] = seg["fps"]
            # cada tramo dura lo que dice la lista; se ajusta a cuadros exactos
            n_exp = int(round((float(row[2]) - float(row[1])) * self.profile.fps)) if len(row) >= 3 else seg["frames"]
            for k, v in seg.items():
                if isinstance(v, list):
                    vv = v[:n_exp] + [v[-1] if v else None] * max(0, n_exp - len(v)) if n_exp else v
                    self.raw[k].extend(vv)
            self.segments_done.add(name)
        if not self.raw or not self.raw["present"]:
            return
        self.processed_s = len(self.raw["present"]) / self.profile.fps
        if self.on_game_over and not self.game_over and not final:
            sport = str(self.profile.cfg.get("sport", "")).lower()
            reg = int(self.profile.cfg.get("innings") or (9 if ("beis" in sport or "baseball" in sport) else 7))
            over = ev_mod.detect_game_over(self.raw, self.profile.fps, regulation=reg)
            if over:
                self.game_over = over
                try:
                    self.on_game_over(self, over)
                except Exception:
                    traceback.print_exc()
        ctx = load_ctx_raw(self.work / "contexto.raw")
        stingers = find_stingers(ctx, self.profile.stinger)
        rms = load_audio_rms_raw(self.work / "audio.raw")
        result = ev_mod.build_story(self.raw, stingers, rms, RMS_RATE,
                                    replay_from_absence=bool(self.profile.cfg.get("replay_from_absence")),
                                    out_from_count=bool(self.profile.cfg.get("out_from_count")))
        if not final:
            result["final"] = None
        else:
            try:
                result["final"] = refine_final(self.video, result.get("final"), self.crop or self.profile.crop)
            except Exception:
                traceback.print_exc()
        # previa (himnos y saludo): se busca una sola vez, cuando ya arranco el juego
        if self.pregame is None:
            t_first = ev_mod.first_scorebug_time(self.raw["present"], self.profile.fps)
            if final or (t_first is not None and self.processed_s >= t_first + 30):
                try:
                    self.pregame = pregame(self.work, self.raw, self.profile.fps)
                except Exception:
                    traceback.print_exc()
                    self.pregame = []
        result["pregame"] = self.pregame or []
        clips, halves = build_clips(result)
        try:
            from . import trim
            if ctx is not None and (self._pc_tpl is None or len(ctx) - self._pc_n > 1200):
                self._pc_tpl, self._pc_n = trim.pitch_template(ctx), len(ctx)
            trim.apply(clips, trim.pitch_shots(ctx, self._pc_tpl))
        except Exception:
            traceback.print_exc()
        limit = float("inf") if final else self.processed_s - PUBLISH_MARGIN
        for c in clips:
            if c["type"] == "revisar" and not final:
                continue
            if not final and (c["scorebugTime"] > limit or c["end"] + (PRE_HANDLE if c["half"] == "PRE" else PREVIEW_HANDLE) > self.processed_s):
                continue
            key = f"{c['half']}|{c['type'] if c['type'] in ('final', 'revisar') else round(c['scorebugTime'])}"
            if c["half"] == "PRE":
                key = f"PRE|{c['id']}"
            if key in self.ids:
                continue
            cid = c["id"] if c["half"] == "PRE" else f"{sum(1 for k in self.ids if not k.startswith('PRE|')) + 1:02d}"
            self.ids[key] = cid
            c["id"] = cid
            self._render(c)
            self.published[cid] = c
        self._write_manifest(result, halves, final)
        pend = sum(1 for c in clips if c["type"] != "revisar") - sum(1 for c in self.published.values() if c["type"] != "revisar")
        self.message = (f"En vivo: {hms(self.processed_s, ms=False)} leídos · {len(self.published)} jugadas"
                        + (f" · {pend} por confirmar" if pend > 0 else "")) if not final else \
            f"{len(self.published)} jugadas en {sum(1 for h in halves if h['key'] not in ('FIN', 'PRE'))} medias entradas"

    def _render(self, c):
        hd = PRE_HANDLE if c["half"] == "PRE" else PREVIEW_HANDLE
        p0 = max(0.0, min(c["start"], c.get("origStart", c["start"])) - hd)
        p1 = max(c["end"], c.get("origEnd", c["end"])) + hd
        out = self.clip_dir / f"{c['id']}_{c['half']}_{c['type']}.mp4"
        if cut(self.video, out, p0, p1 - p0, mode="preview"):
            c["file"] = f"clips/{out.name}"
            c["previewStart"], c["previewEnd"] = round(p0, 2), round(p1, 2)
            jpg = self.clip_dir / f"{out.stem}.jpg"
            poster(out, jpg, min(p1 - p0 - 0.5, max(0.5, c["pitchTime"] - p0 + 1.5)))
            if jpg.exists():
                c["poster"] = f"clips/{jpg.name}"

    def _manifest(self):
        p = self.run_dir / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def _write_manifest(self, result, halves, final):
        clips = sorted(self.published.values(), key=lambda c: (c["start"] if c["type"] != "final" else 1e12))
        by_half = {}
        for c in clips:
            by_half.setdefault(c["half"], []).append(c["id"])
        hs = []
        for h in halves:
            h = dict(h)
            h["clips"] = by_half.get(h["key"], [])
            if h["clips"] or final:
                hs.append(h)
        last = result["events"][-1]["after"] if result["events"] else {}
        manifest = {
            "runId": self.run_id, "video": str(self.video), "videoName": self.video.name, "profile": self.profile_name,
            "duration": self.processed_s, "created": time.time(), "live": not final,
            "liveStatus": self.message, "scorebugLocation": getattr(self, "loc_info", None),
            "stats": {"halves": sum(1 for h in hs if h["key"] not in ("FIN", "PRE")), "clips": len(clips),
                      "carreras": sum(1 for c in clips if c["type"] == "carrera"),
                      "pitchesMph": len(result["mph"]), "replays": len(result["replays"]),
                      "finalScore": score_text(last)},
            "halves": hs, "clips": clips,
            "logic": ["Analisis en vivo: las jugadas aparecen ~1 minuto despues de que cambia el scorebug."],
        }
        tmp = self.run_dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=_np), encoding="utf-8")
        # en Windows el reemplazo falla si en ese instante el panel (o el
        # antivirus) esta leyendo el archivo: se reintenta; si igual no se
        # puede, se escribe en el proximo ciclo (nunca se corta el analisis)
        for k in range(20):
            try:
                tmp.replace(self.run_dir / "manifest.json")
                break
            except PermissionError:
                time.sleep(0.25)
        else:
            print(f"No pude escribir {self.run_dir / 'manifest.json'} (lo reintento en el próximo ciclo)")
