"""Servidor local del panel de highlights.

    python -m highlight.server            (puerto 8790 por defecto)

Todo corre en la PC: lee los videos del disco, analiza en una cola (uno
detras de otro) y exporta los clips aprobados a una carpeta.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .analyze import analyze
from .render import adjusted_window, audio_chain, audio_gain, cut, safe_name
from .scorebug import list_profiles
from . import fixture as fixture_mod

APP_DIR = Path(__file__).resolve().parent
REPO = APP_DIR.parent
STATIC = APP_DIR / "static"
CONFIG_PATH = REPO / "config.json"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".ts", ".flv"}

DEFAULT_CONFIG = {
    "video_folders": [str(Path.home() / "Videos" / "Highlight Videos")],
    "data_dir": str(REPO / "datos"),
    "export_dir": "",
    "default_profile": "auto",
    # modo automatico: cada partido nuevo de la carpeta se analiza solo
    "auto_analyze": False,
    "auto_approve": "sugeridos_k",   # "", "sugeridos" o "sugeridos_k" (+ ponches de cierre)
    "auto_xml": True,                # dejar lista la linea para Premiere
    "live_mode": False,              # analizar mientras se graba (MKV que crece)
    # senales SRT (Castr) que el panel graba solo: [{"name": "Campo 1", "url": "srt://..."}]
    "srt_sources": [],
    "auto_cut": True,                # cortar solo el archivo cuando termina cada partido
    "rec_auto_at": "",               # "HH:MM" (hora de la PC): empezar a grabar todos los campos solo
    "rec_stop_at": "03:00",          # "HH:MM": parar todas las grabaciones (por las dudas, de noche)
    "stop_after_last": True,         # termino el ultimo partido del dia en esa cancha: parar en vez de seguir
    "previa": True,                  # buscar himnos y saludo antes del primer lanzamiento
    "denoise": False,                # sacar el ruido de fondo del audio al exportar
    "fixture": "u18_panam_2026.json",  # nombres de los partidos (highlight/fixtures/)
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def runs_dir():
    p = Path(load_config()["data_dir"]) / "analisis"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- videos

def scan_videos():
    cfg = load_config()
    seen, out = set(), []
    data_dir = Path(cfg["data_dir"]).resolve()
    for folder in cfg["video_folders"]:
        base = Path(folder).expanduser()
        if not base.exists():
            continue
        for path in base.iterdir():  # solo esa carpeta, sin subcarpetas
            try:
                if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
                    continue
                rp = path.resolve()
                if rp in seen or data_dir in rp.parents:
                    continue
                st = path.stat()
                if st.st_size < cfg.get("min_size_mb", 50) * 1024 * 1024:  # clips chicos no son partidos
                    continue
                seen.add(rp)
                recording = (time.time() - st.st_mtime) < 20
                out.append({"name": path.name, "path": str(rp), "sizeMb": round(st.st_size / 1048576),
                            "modified": st.st_mtime, "recording": recording})
            except OSError:
                continue
    out.sort(key=lambda v: v["modified"], reverse=True)
    analyzed = {}
    for run in scan_runs():
        if not run.get("subset"):
            analyzed.setdefault(run["video"], run["runId"])
    for v in out:
        v["runId"] = analyzed.get(v["path"])
    return out


def scan_runs():
    out = []
    for mp in runs_dir().glob("*/manifest.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        dec = load_decisions(m["runId"])
        out.append({"runId": m["runId"], "videoName": m.get("videoName"), "video": m.get("video"),
                    "subset": bool(m.get("subsetOf")),
                    "clips": len(m.get("clips", [])), "created": m.get("created", mp.stat().st_mtime),
                    "finalScore": m.get("stats", {}).get("finalScore", ""),
                    "approved": sum(1 for d in dec.values() if d.get("decision") == "approved"),
                    "reviewed": sum(1 for d in dec.values() if d.get("decision"))})
    out.sort(key=lambda r: r["created"], reverse=True)
    return out


def run_path(run_id):
    # el id ya es seguro (lo genera el panel); solo se valida, sin recortarlo
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
        raise ValueError("runId invalido")
    p = (runs_dir() / run_id).resolve()
    if runs_dir().resolve() not in p.parents:
        raise ValueError("runId invalido")
    return p


def load_run(run_id):
    p = run_path(run_id) / "manifest.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m["decisions"] = load_decisions(run_id)
    return m


def load_decisions(run_id):
    p = run_path(run_id) / "decisiones.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_decisions(run_id, decisions):
    p = run_path(run_id) / "decisiones.json"
    p.write_text(json.dumps(decisions, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


def delete_run(run_id):
    p = run_path(run_id)
    if p.exists():
        shutil.rmtree(p)
    return {"ok": True}


# ---------------------------------------------------------------- cola

JOBS = []
JOB_Q = queue.Queue()
JOB_LOCK = threading.Lock()


def enqueue(path, profile, reuse_run=None, auto=False):
    path = str(Path(path).resolve())
    with JOB_LOCK:
        for j in JOBS:
            if j["path"] == path and j["status"] in ("en cola", "analizando"):
                return j
        job = {"id": f"j{int(time.time() * 1000)}", "path": path, "name": Path(path).name, "profile": profile,
               "status": "en cola", "progress": 0.0, "message": "", "runId": None, "error": None,
               "queued": time.time(), "reuse": reuse_run, "auto": auto}
        JOBS.append(job)
    JOB_Q.put(job)
    return job


def is_closing_k(c):
    if c.get("type") != "tercer_out":
        return False
    if "strikeout" in c:
        return bool(c["strikeout"])
    return bool(re.search(r"conteo \d-2", c.get("detail", "")))


def auto_finish(m):
    """Despues de un analisis automatico: pre-aprobar y dejar la linea lista."""
    cfg = load_config()
    extra = ""
    mode = cfg.get("auto_approve") or ""
    if mode:
        dec = load_decisions(m["runId"])
        n = 0
        for c in m["clips"]:
            if dec.get(c["id"], {}).get("decision"):
                continue
            if c.get("suggested") or (mode == "sugeridos_k" and is_closing_k(c)):
                dec[c["id"]] = {"decision": "approved"}
                n += 1
        save_decisions(m["runId"], dec)
        extra += f" · {n} pre-aprobadas"
    if cfg.get("auto_xml") and mode:
        try:
            r = export_sequence(m["runId"])
            extra += f" · línea Premiere lista"
        except Exception as exc:
            extra += f" · no se pudo armar la línea ({exc})"
    return extra


# ---------------------------------------------------------------- en vivo

LIVE = {}


def start_live(path, profile="auto", on_game_over=None, hint=None):
    from .live import LiveSession

    path = str(Path(path).resolve())
    s = LIVE.get(path)
    if s and not s.finished and s.status != "error":
        return s.info()

    def done(m):
        return auto_finish(m) if m else ""

    # un analisis en vivo de este mismo video que quedo a medias (el panel se
    # reinicio o se cayo mientras grababa): se sigue en ese mismo analisis,
    # conservando las jugadas ya armadas (con su video) y lo ya marcado
    resume = None
    for old in sorted(runs_dir().glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            m = json.loads(old.read_text(encoding="utf-8"))
        except Exception:
            continue
        ls = str(m.get("liveStatus") or "")
        if m.get("video") and str(Path(m["video"]).resolve()) == path and ls and "medias entradas" not in ls:
            resume = old.parent
            break
    if resume is not None:
        # el ffmpeg del analisis anterior puede seguir leyendo solo: se cierra
        try:
            from .recorder import kill_ffmpeg_matching
            kill_ffmpeg_matching(resume.name)
        except Exception:
            traceback.print_exc()
    s = LiveSession(path, runs_dir(), profile, on_done=done, on_game_over=on_game_over, profile_hint=hint,
                    resume_dir=resume)
    s.start()
    LIVE[path] = s
    return s.info()


def stop_live(path):
    s = LIVE.get(str(Path(path).resolve()))
    if s:
        s.stop()
        return s.info()
    return {"error": "no hay sesion en vivo"}


# ---------------------------------------------------------------- grabar SRT

RECORDERS = {}
_SRT_OK = None


def rec_folder():
    folders = load_config()["video_folders"]
    return Path(folders[0]).expanduser() if folders else REPO / "grabaciones"


def rec_infos():
    global _SRT_OK
    if not _SRT_OK:
        from .recorder import has_srt
        _SRT_OK = has_srt()
    out = []
    for src in load_config().get("srt_sources", []):
        r = RECORDERS.get(src["name"])
        info = r.info() if r else None
        if info is not None:
            ss = LIVE.get(str(Path(r.file).resolve())) if r.file else None
            info["live"] = ss.info() if ss else None
            info["hasPreview"] = bool(r.preview and r.frame is not None)
            info["hasAudio"] = bool(r.preview and not r.no_audio)
            info["sample"] = SAMPLES.get(src["name"])
        info = info if info is not None else {"name": src["name"], "url": src["url"], "status": "detenido", "message": "",
                                   "file": None, "sizeMb": None, "since": None, "files": []}
        out.append(info)
    cfg = load_config()
    return {"sources": out, "srtOk": _SRT_OK, "autoCut": cfg.get("auto_cut", True),
            "autoAt": cfg.get("rec_auto_at", ""), "stopAt": cfg.get("rec_stop_at", ""), "shutdown": bool(cfg.get("shutdown_after_stop")), "autoAtDone": _AUTO_REC_DONE.get("day") == f"{time.strftime('%Y-%m-%d')} {cfg.get('rec_auto_at', '')}"}


def rec_action(name, action, adopt_state=None):
    from .recorder import Recorder

    src = next((s for s in load_config().get("srt_sources", []) if s["name"] == name), None)
    if not src:
        return {"error": f"no hay una señal llamada {name}"}
    r = RECORDERS.get(name)
    if action in ("start", "monitor"):
        was_recording = False
        if r and r.url != src["url"]:
            # cambio la direccion de la senal: si estaba grabando, sigue
            # grabando con la nueva (archivo nuevo, mismo partido)
            was_recording = bool(r.record)
            r.stop()
            r = None
            if was_recording:
                action = "start"
        if not r or not (r.thread and r.thread.is_alive()):
            def game_over(session, over):
                # fin del partido detectado en el scorebug: cerrar el archivo y
                # seguir con el proximo partido en uno nuevo
                rec = RECORDERS.get(name)
                # sin ninguna jugada todavia (previa, o la senal arranco antes del
                # juego) no es un fin de partido: no se corta ni cambia el nombre
                started = any(not str(k).startswith("PRE|") for k in getattr(session, "ids", {}))
                if not started:
                    return
                if rec and load_config().get("auto_cut", True) and rec.file and \
                        Path(rec.file).resolve() == session.video:
                    why = {"fin": "fin del partido", "nuevo partido": "empezó otro partido",
                           "pausa larga": "scorebug ausente mucho tiempo"}.get(over["reason"], over["reason"])
                    _game_state(name, over=True)
                    cfg = load_config()
                    s2 = next((x for x in cfg.get("srt_sources", []) if x["name"] == name), {})
                    fx = fixture_mod.load(cfg.get("fixture"), fixture_edits_path()) if cfg.get("stop_after_last", True) else None
                    if fx and s2.get("field") and not fixture_mod.next_game(fx, s2["field"]):
                        # era el ultimo partido del dia en esa cancha: se deja de
                        # grabar (el monitor sigue) en vez de abrir otro archivo
                        rec.last_cut = f"{time.strftime('%H:%M')} {why}: último partido del día, grabación parada"
                        session.finish()
                        threading.Thread(target=rec.set_record, args=(False,), daemon=True).start()
                        _remember_recording(name, False)
                    else:
                        rec.cut(f"{why} ({over.get('score', '')}, {over.get('inning')}ª)")

            def new_file(path):
                # cada archivo nuevo se analiza en vivo mientras se graba
                rec = RECORDERS.get(name)
                # el archivo anterior de este campo ya no crece: su analisis se
                # cierra enseguida (sale el Final sin esperar)
                for prev in (rec.files[:-1] if rec else []):
                    ps = LIVE.get(str(Path(prev).resolve()))
                    if ps and not ps.finished:
                        ps.finish()
                title = re.sub(r" parte \d+$", "", Path(path).stem)
                st = _game_state(name)
                if not st or st.get("title") != title or st.get("over"):
                    _game_state(name, title=title, over=False)
                hint = None
                for prev in reversed(rec.files[:-1] if rec else []):
                    ss = LIVE.get(str(Path(prev).resolve()))
                    if ss and ss.profile is not None:
                        hint = (ss.profile_name, ss.crop, ss.loc_info)
                        break
                hint_file = Path(load_config()["data_dir"]) / "grabando" / f"{safe_name(name)}.scorebug.json"
                if hint is None and hint_file.exists():
                    # el panel se reinicio: el scorebug de este campo esta donde estaba
                    try:
                        hd = json.loads(hint_file.read_text(encoding="utf-8"))
                        if time.time() - hd.get("t", 0) < 12 * 3600:
                            hint = (hd["profile"], hd.get("crop"), hd.get("info") or {})
                    except Exception:
                        hint = None
                ss_new = start_live(path, load_config().get("default_profile") or "auto", on_game_over=game_over, hint=hint)

                def remember():
                    # guardar donde esta el scorebug de este campo apenas se encuentre
                    s3 = LIVE.get(str(Path(path).resolve()))
                    for _ in range(3600):
                        if not s3 or s3.finished or s3.status == "error":
                            return
                        if s3.profile is not None:
                            try:
                                hint_file.parent.mkdir(parents=True, exist_ok=True)
                                hint_file.write_text(json.dumps({"profile": s3.profile_name, "crop": s3.crop,
                                                                 "info": s3.loc_info, "t": time.time()}), encoding="utf-8")
                            except Exception:
                                pass
                            return
                        time.sleep(5)
                threading.Thread(target=remember, daemon=True).start()
            def game_title():
                # si este campo estaba grabando un partido que no termino y la
                # senal (o el panel) se corto hace poco, es el mismo partido
                st = _game_state(name)
                if st and not st.get("over") and time.time() - st.get("t", 0) < 20 * 60 and st.get("title"):
                    return st["title"]
                cfg = load_config()
                s2 = next((x for x in cfg.get("srt_sources", []) if x["name"] == name), {})
                fx = fixture_mod.load(cfg.get("fixture"), fixture_edits_path())
                g = None
                if st and st.get("over"):
                    # el partido anterior ya termino: el archivo nuevo es el que
                    # le sigue en el fixture (aunque ya sea la hora del otro,
                    # porque los partidos se atrasan)
                    g = fixture_mod.game_after(fx, s2.get("field"), st.get("title")) or \
                        fixture_mod.next_game(fx, s2.get("field"), within_h=3.0)
                g = g or fixture_mod.match(fx, s2.get("field"))
                return fixture_mod.title(g) if g else None

            r = Recorder(name, src["url"], rec_folder(), on_new_file=new_file, preview=True,
                         pid_dir=Path(load_config()["data_dir"]) / "grabando", title_fn=game_title)
            RECORDERS[name] = r
            if adopt_state:
                r.adopt(adopt_state)
            else:
                r.record = action == "start"
                r.start()
        elif action == "start":
            r.set_record(True)
        if action == "start":
            _remember_recording(name, True)
    elif r and action == "stop":
        # deja de grabar; el monitor sigue mostrando la senal
        ps = LIVE.get(str(Path(r.file).resolve())) if r.file else None
        if ps and not ps.finished:
            ps.finish()
        threading.Thread(target=r.set_record, args=(False,), daemon=True).start()
        _remember_recording(name, False)
    elif r and action == "off":
        threading.Thread(target=r.stop, daemon=True).start()
        _remember_recording(name, False)
    elif r and action == "cut":
        # Cortar juego a mano = termino el partido: el archivo que sigue toma el
        # nombre del proximo partido del fixture
        _game_state(name, over=True)
        threading.Thread(target=r.cut, daemon=True).start()
    return rec_infos()


def _game_state(name, **update):
    """Partido que esta grabando cada campo (para no cambiarle el nombre si la
    senal o el panel se cortan en medio del juego)."""
    p = Path(load_config()["data_dir"]) / "grabando" / f"{safe_name(name)}.partido.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        st = {}
    if update:
        st.update(update)
        st["t"] = time.time()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return st


def fixture_edits_path():
    return Path(load_config()["data_dir"]) / "fixture_cambios.json"


def fixture_info():
    cfg = load_config()
    fx = fixture_mod.load(cfg.get("fixture"), fixture_edits_path())
    return {"fixture": fx, "file": cfg.get("fixture"), "available": fixture_mod.list_fixtures()}


SAMPLES = {}


def rec_sample(name, minutes=10, every=5):
    """Fotos a resolucion completa de los ultimos minutos grabados de una
    senal, para calibrar un scorebug nuevo. Quedan en <carpeta>/muestras/."""
    r = RECORDERS.get(name)
    if not r or not r.file:
        return {"error": "Tocá Grabar primero: la muestra sale de lo que se está grabando."}
    if SAMPLES.get(name, {}).get("status") == "sacando":
        return SAMPLES[name]
    src = r.file
    dur = time.time() - (r.started_file_at or time.time())
    t0 = max(0.0, dur - minutes * 60)
    out = rec_folder() / "muestras" / f"{safe_name(name)} {time.strftime('%Y-%m-%d %H-%M')}"
    out.mkdir(parents=True, exist_ok=True)
    SAMPLES[name] = {"status": "sacando", "folder": str(out)}

    def work():
        from .ffmpeg import ffmpeg_exe
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t0:.1f}", "-i", src,
               "-t", f"{dur - t0:.1f}", "-vf", f"fps=1/{every}", "-q:v", "2", str(out / "f_%03d.jpg")]
        try:
            subprocess.run(cmd, timeout=1800)
            n = len(list(out.glob("*.jpg")))
            SAMPLES[name] = {"status": "listo", "folder": str(out), "fotos": n}
        except Exception as exc:
            SAMPLES[name] = {"status": "error", "folder": str(out), "error": str(exc)}

    threading.Thread(target=work, daemon=True).start()
    return SAMPLES[name]


def _rec_state_path():
    return Path(load_config()["data_dir"]) / "grabando.json"


def _remember_recording(name, on):
    """Que señales estan grabando: si el panel o la PC se reinician, siguen solas."""
    p = _rec_state_path()
    try:
        names = set(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else set()
    except Exception:
        names = set()
    (names.add if on else names.discard)(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(names), ensure_ascii=False), encoding="utf-8")


def ensure_monitors():
    """Cada senal cargada se conecta sola para el monitor (sin grabar).
    Las que ya no estan en la configuracion se apagan."""
    names = {s["name"] for s in load_config().get("srt_sources", [])}
    for name, r in list(RECORDERS.items()):
        if name not in names:
            r.stop()
            RECORDERS.pop(name, None)
    for name in names:
        try:
            rec_action(name, "monitor")
        except Exception:
            traceback.print_exc()


def resume_recordings():
    from .recorder import adoptable, kill_orphans, _safe as _rsafe
    pid_dir = Path(load_config()["data_dir"]) / "grabando"
    # las grabaciones que siguieron solas mientras el panel se reiniciaba se
    # retoman sin cortarlas; los ffmpeg sueltos que no son de nadie se cierran
    live = adoptable(pid_dir)
    kill_orphans(pid_dir, keep=[st["pid"] for st in live.values()])
    for src in load_config().get("srt_sources", []):
        st = live.get(_rsafe(src["name"]))
        if st and st.get("url") == src["url"]:
            try:
                print(f"Sigue grabando {src['name']} ({Path(st['file']).name}): me engancho sin cortar")
                rec_action(src["name"], "start", adopt_state=st)
            except Exception:
                traceback.print_exc()
    time.sleep(2)
    ensure_monitors()
    p = _rec_state_path()
    try:
        names = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except Exception:
        names = []
    for name in names:
        try:
            print(f"Retomando la grabación de {name}")
            rec_action(name, "start")
        except Exception:
            traceback.print_exc()


_AUTO_REC_DONE = {}
_AUTO_STOP_DONE = {}


SHUTDOWN = {"pending": False}


def _busy():
    """Que sigue trabajando: grabaciones, analisis, exportaciones."""
    if any(r.record for r in RECORDERS.values()):
        return "grabando"
    if any(j["status"] in ("en cola", "analizando") for j in JOBS):
        return "analizando"
    if any(not ps.finished for ps in LIVE.values()):
        return "cerrando análisis en vivo"
    if any(j["status"] == "exportando" for j in JOBS):
        return "exportando"
    if any(v.get("status") == "copiando" for v in REMUX.values()):
        return "exportando"
    return None


def shutdown_when_idle(max_wait_min=60, grace_s=300):
    """Despues de parar todo: espera que terminen los analisis y las
    exportaciones (hasta 1 hora) y apaga la PC con 5 minutos de aviso
    (se cancela destildando la opcion en el panel o con 'shutdown /a')."""
    t0 = time.time()
    while _busy() and time.time() - t0 < max_wait_min * 60:
        time.sleep(15)
    if not load_config().get("shutdown_after_stop") or _busy():
        print(f"Apagado automático cancelado ({_busy() or 'opción destildada'})")
        return
    print("Apagado automático: la PC se apaga en 5 minutos")
    if os.name == "nt":
        subprocess.run(["shutdown", "/s", "/t", str(grace_s), "/c",
                        "Highlights: terminó todo, la PC se apaga en 5 minutos. Para cancelar: destildá 'Apagar la PC' en el panel."],
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        SHUTDOWN["pending"] = True


def cancel_shutdown():
    if os.name == "nt" and SHUTDOWN.get("pending"):
        subprocess.run(["shutdown", "/a"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    SHUTDOWN["pending"] = False


def rec_scheduler(period=20, window_min=30):
    """Grabar solo a una hora fija (rec_auto_at, hora de la PC): a esa hora se
    ponen a grabar todos los campos que no esten grabando. Una vez por dia;
    si la PC estaba apagada a esa hora, arranca igual dentro de los 30 minutos."""
    while True:
        try:
            at = str(load_config().get("rec_auto_at") or "").strip()
            if at:
                hh, mm = [int(x) for x in at.split(":")[:2]]
                now = time.localtime()
                mins = now.tm_hour * 60 + now.tm_min - (hh * 60 + mm)
                today = f"{time.strftime('%Y-%m-%d')} {at}"   # cambiar la hora vale de nuevo el mismo dia
                if 0 <= mins < window_min and _AUTO_REC_DONE.get("day") != today:
                    _AUTO_REC_DONE["day"] = today
                    for src in load_config().get("srt_sources", []):
                        r = RECORDERS.get(src["name"])
                        if r and r.record:
                            continue
                        print(f"Grabación automática ({at}): {src['name']}")
                        try:
                            rec_action(src["name"], "start")
                        except Exception:
                            traceback.print_exc()
            stop_at = str(load_config().get("rec_stop_at") or "").strip()
            if stop_at:
                hh, mm = [int(x) for x in stop_at.split(":")[:2]]
                now = time.localtime()
                mins = now.tm_hour * 60 + now.tm_min - (hh * 60 + mm)
                key = f"{time.strftime('%Y-%m-%d')} {stop_at}"
                if 0 <= mins < window_min and _AUTO_STOP_DONE.get("day") != key:
                    _AUTO_STOP_DONE["day"] = key
                    for nm, r in list(RECORDERS.items()):
                        if r.record:
                            print(f"Parada automática ({stop_at}): {nm}")
                            try:
                                rec_action(nm, "stop")
                            except Exception:
                                traceback.print_exc()
                    if load_config().get("shutdown_after_stop"):
                        threading.Thread(target=shutdown_when_idle, daemon=True).start()
        except Exception:
            traceback.print_exc()
        time.sleep(period)


# ---------------------------------------------------------------- carpeta vigilada

_SEEN_SIZES = {}
_AUTO_FAILED = set()


def watcher(period=30, settle_s=120):
    """Modo automatico: un video que no cambia de tamano por ~2 minutos se
    considera terminado de grabar y se manda a analizar."""
    while True:
        try:
            for nm, rr in list(RECORDERS.items()):
                if rr.record and rr.file:
                    _game_state(nm, alive=True)   # sigue grabando: refresca la hora
        except Exception:
            pass
        try:
            cfg = load_config()
            if cfg.get("auto_analyze"):
                busy = {j["path"] for j in JOBS if j["status"] in ("en cola", "analizando")}
                busy |= {p for p, ss in LIVE.items() if not (ss.finished or ss.status == "error")}
                vids = scan_videos()
                # partidos ya juntados: sus partes sueltas no se analizan mas
                joined = {re.match(r"(Gm \d+) ", x["name"]).group(1) for x in vids
                          if "(completo)" in x["name"] and re.match(r"Gm \d+ ", x["name"])}
                for v in vids:
                    if v.get("runId") or v["path"] in busy or v["path"] in _AUTO_FAILED:
                        continue
                    g = re.match(r"(Gm \d+) ", v["name"])
                    if g and g.group(1) in joined and "(completo)" not in v["name"]:
                        continue
                    if "(completo)" in v["name"]:
                        # un archivo juntado ya esta entero: analisis normal, no en vivo
                        if v["path"] not in LIVE and time.time() - v["modified"] > 30:
                            enqueue(v["path"], cfg.get("default_profile") or "auto", auto=True)
                        continue
                    if cfg.get("live_mode") and time.time() - v["modified"] < 60 and v["path"] not in LIVE:
                        # se esta grabando: analizar en vivo
                        start_live(v["path"], cfg.get("default_profile") or "auto")
                        continue
                    if v["path"] in LIVE:
                        continue
                    if any(j["path"] == v["path"] and j["status"] == "error" for j in JOBS):
                        _AUTO_FAILED.add(v["path"])
                        continue
                    size = Path(v["path"]).stat().st_size
                    prev = _SEEN_SIZES.get(v["path"])
                    _SEEN_SIZES[v["path"]] = (size, prev[1] if prev and prev[0] == size else time.time())
                    stable_since = _SEEN_SIZES[v["path"]][1]
                    if time.time() - v["modified"] >= settle_s and time.time() - stable_since >= settle_s:
                        enqueue(v["path"], cfg.get("default_profile") or "auto", auto=True)
        except Exception:
            traceback.print_exc()
        time.sleep(period)


def worker():
    while True:
        job = JOB_Q.get()
        if job.get("status") == "cancelado":
            continue
        job["status"] = "analizando"
        job["started"] = time.time()

        def prog(stage, frac, msg):
            job["progress"] = frac
            if msg:
                job["message"] = msg

        try:
            reuse = None
            if job.get("reuse"):
                works = sorted((d for d in run_path(job["reuse"]).glob("trabajo*") if d.is_dir()),
                               key=lambda d: d.stat().st_mtime)
                reuse = works[-1] if works else run_path(job["reuse"]) / "trabajo"
            m = analyze(job["path"], runs_dir(), job["profile"], on_progress=prog, reuse_work=reuse)
            if job.get("reuse"):
                # conservar las decisiones del analisis anterior cuando coinciden los clips
                old = load_decisions(job["reuse"])
                if old:
                    old_clips = {c["id"]: c for c in load_run(job["reuse"])["clips"]}
                    moved = {}
                    for oid, d in old.items():
                        oc = old_clips.get(oid)
                        if not oc:
                            continue
                        for c in m["clips"]:
                            if abs(c["scorebugTime"] - oc["scorebugTime"]) < 3 and c["half"] == oc["half"]:
                                moved[c["id"]] = d
                                break
                    save_decisions(m["runId"], moved)
            job["runId"] = m["runId"]
            job["message"] = f"{m['stats']['clips']} jugadas en {m['stats']['halves']} medias entradas"
            if job.get("auto"):
                job["message"] += auto_finish(m)
            job["status"] = "listo"
            job["progress"] = 1.0
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            traceback.print_exc()
        job["finished"] = time.time()


# ---------------------------------------------------------------- exportar

def start_export(run_id, folder=""):
    """Exportar en segundo plano: el trabajo aparece en la lista de trabajos
    (con su avance) y sigue aunque se cambie de pantalla en el panel."""
    m = load_run(run_id)
    with JOB_LOCK:
        for j in JOBS:
            if j.get("kind") == "export" and j.get("runId") == run_id and j["status"] == "exportando":
                return j
        job = {"id": f"x{int(time.time() * 1000)}", "kind": "export", "path": "", "runId": run_id,
               "name": "Exportar clips · " + Path(m.get("videoName") or run_id).stem, "status": "exportando",
               "progress": 0.0, "message": "Preparando…", "error": None, "queued": time.time(), "auto": False}
        JOBS.append(job)

    def prog(k, n, title):
        job["progress"] = k / max(1, n)
        job["message"] = f"Clip {k + 1} de {n}: {title}" if k < n else "Terminando…"

    def work():
        try:
            r = export_approved(run_id, folder, progress=prog)
            job["result"] = r
            job["status"] = "listo"
            job["progress"] = 1.0
            job["message"] = f"{r['count']} clips en {r['folder']}" + (f" (fallaron: {', '.join(r['failed'])})" if r["failed"] else "")
        except Exception as exc:
            traceback.print_exc()
            job["status"], job["error"] = "error", str(exc)
        job["finished"] = time.time()
    threading.Thread(target=work, daemon=True).start()
    return job


def export_approved(run_id, folder="", progress=None):
    m = load_run(run_id)
    dec = m["decisions"]
    video = Path(m["video"])
    if not video.exists() and not any(c.get("video") for c in m["clips"]):
        raise FileNotFoundError(f"No encuentro el video original: {video}")
    cfg = load_config()
    if folder:
        out_dir = Path(folder).expanduser()
    elif cfg.get("export_dir"):
        out_dir = Path(cfg["export_dir"]).expanduser() / safe_name(Path(m["videoName"]).stem)
    else:
        out_dir = Path(cfg["data_dir"]) / "exportados" / safe_name(Path(m["videoName"]).stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    done, failed = [], []
    order = 0
    total = sum(1 for c in m["clips"] if (dec.get(c["id"]) or {}).get("decision") == "approved")
    for c in m["clips"]:
        d = dec.get(c["id"]) or {}
        if d.get("decision") != "approved":
            continue
        if progress:
            try:
                progress(order, total, c.get("title") or c["id"])
            except Exception:
                pass
        order += 1
        start, length = adjusted_window(c, d)
        name = f"{order:02d}_{c['half']}_{safe_name(c['title'])}_{int(start // 60):03d}m{int(start % 60):02d}s.mp4"
        dest = out_dir / name
        if cut(Path(c.get("video") or video), dest, start, length, mode="export"):
            done.append({"order": order, "id": c["id"], "half": c["halfLabel"], "title": c["title"],
                         "file": dest.name, "start": round(start, 2), "length": round(length, 2)})
        else:
            failed.append(c["id"])
    lines = [f"{x['order']:02d}\t{x['half']}\t{x['title']}\t{x['file']}" for x in done]
    (out_dir / "orden_para_premiere.txt").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "decisiones_panel.json").write_text(
        json.dumps({"runId": run_id, "video": str(video), "clips": done, "decisions": dec}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return {"folder": str(out_dir), "count": len(done), "failed": failed}


def subset_run(run_id, types, label=""):
    """Analisis aparte con solo algunos tipos de jugada (por ejemplo las
    carreras) de todas las partes de un mismo partido, mas la previa.
    Copia las vistas previas: el analisis original no se toca."""
    src = load_run(run_id)
    base = re.sub(r" parte \d+$", "", Path(src["videoName"]).stem)
    parts = []
    for mp in runs_dir().glob("*/manifest.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("subsetOf"):
            continue
        if re.sub(r" parte \d+$", "", Path(m.get("videoName", "")).stem) == base:
            m["decisions"] = load_decisions(m["runId"])
            parts.append(m)
    def part_no(m):
        g = re.search(r" parte (\d+)$", Path(m["videoName"]).stem)
        return int(g.group(1)) if g else 1
    parts.sort(key=part_no)
    types = set(types or ["carrera"])
    label = label or ("solo carreras" if types == {"carrera"} else "solo " + ", ".join(sorted(types)))
    new_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_name(base)}_{safe_name(label)}"
    out = runs_dir() / new_id
    (out / "clips").mkdir(parents=True, exist_ok=True)
    clips, halves, dec, n = [], [], {}, 0
    for m in parts:
        for c in m["clips"]:
            if c["type"] not in types and c.get("half") != "PRE":
                continue
            n += 1
            nc = dict(c)
            nc["id"] = f"P{n}" if c.get("half") == "PRE" else f"{n:02d}"
            nc["video"] = m["video"]
            for key in ("file", "poster"):
                if c.get(key):
                    f = run_path(m["runId"]) / c[key]
                    if f.exists():
                        dest = out / "clips" / f"{nc['id']}_{Path(c[key]).name.split('_', 1)[-1]}"
                        shutil.copy2(f, dest)
                        nc[key] = f"clips/{dest.name}"
            if c["id"] in m["decisions"]:
                dec[nc["id"]] = m["decisions"][c["id"]]
            clips.append(nc)
            h = next((x for x in halves if x["key"] == nc["half"]), None)
            if h is None:
                h = {"key": nc["half"], "label": nc.get("halfLabel", nc["half"]), "start": nc["start"], "end": nc["end"],
                     "clips": [], "status": "ok"}
                halves.append(h)
            h["clips"].append(nc["id"])
            h["end"] = max(h["end"], nc["end"])
    halves.sort(key=lambda h: h["key"] != "PRE")
    if not clips:
        shutil.rmtree(out, ignore_errors=True)
        raise ValueError("No hay jugadas de ese tipo en este partido")
    first = parts[0]
    manifest = {"runId": new_id, "video": first["video"], "videoName": f"{base} · {label}.mkv",
                "profile": first.get("profile"), "duration": max(p.get("duration") or 0 for p in parts),
                "created": time.time(), "subsetOf": [p["runId"] for p in parts],
                "liveStatus": f"{len(clips)} jugadas en {len(halves)} medias entradas · {label}",
                "stats": {"halves": len(halves), "clips": len(clips),
                          "carreras": sum(1 for c in clips if c["type"] == "carrera")},
                "halves": halves, "clips": clips, "logic": [f"Analisis aparte: {label} de {len(parts)} parte(s)."]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "decisiones.json").write_text(json.dumps(dec, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"runId": new_id, "clips": len(clips), "parts": len(parts)}


def export_dir_for(m, folder=""):
    cfg = load_config()
    if folder:
        out_dir = Path(folder).expanduser()
    elif cfg.get("export_dir"):
        out_dir = Path(cfg["export_dir"]).expanduser() / safe_name(Path(m["videoName"]).stem)
    else:
        out_dir = Path(cfg["data_dir"]) / "exportados" / safe_name(Path(m["videoName"]).stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


REMUX = {}
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


def merge_game(path, min_mb=5, mp4=False):
    """Junta todas las partes de un partido (los archivos 'Gm NN ...' de la
    misma carpeta, en el orden en que se grabaron) en un solo MKV sin
    recodificar: '<partido> (completo).mkv'. Las partes no se tocan."""
    from .ffmpeg import ffmpeg_exe
    src = Path(path)
    g = re.match(r"(Gm \d+)\b", src.stem)
    if not g:
        return {"error": "El nombre no empieza con 'Gm NN'"}
    prefix = g.group(1) + " "
    parts = sorted((f for f in src.parent.iterdir()
                    if f.is_file() and f.suffix.lower() == ".mkv" and f.name.startswith(prefix)
                    and "(completo)" not in f.name and f.stat().st_size > min_mb * 1048576),
                   key=lambda f: f.stat().st_mtime)
    if any(time.time() - f.stat().st_mtime < 20 for f in parts):
        return {"error": "Todavía se está grabando una parte de este partido"}
    if len(parts) < 2:
        return {"error": "Ese partido tiene una sola parte"}
    title = re.sub(r" parte \d+$", "", parts[-1].stem)
    if mp4:
        # directo a MP4 para Premiere, en exportados (no se vuelve a analizar)
        cfg = load_config()
        base = Path(cfg["export_dir"]).expanduser() if cfg.get("export_dir") else Path(cfg["data_dir"]) / "exportados"
        out_dir = base / safe_name(title + " completo")
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{title} COMPLETO desde partes.mp4"
    else:
        dest = src.parent / f"{title} (completo).mkv"
    key = "merge|" + str(dest)
    if REMUX.get(key, {}).get("status") == "copiando":
        return REMUX[key]
    if dest.exists():
        return {"status": "listo", "file": str(dest), "parts": len(parts)}
    REMUX[key] = {"status": "copiando", "file": str(dest), "parts": len(parts)}

    def work():
        lst = dest.with_suffix(".txt")
        # el temporal no termina en .mkv: si no, el modo en vivo lo toma por
        # una grabacion y lo empieza a analizar (y en Windows no se puede renombrar)
        tmp = dest.with_name(dest.name + ".tmp")
        lst.write_text("".join("file '" + str(f).replace("'", "'\\''") + "'\n" for f in parts), encoding="utf-8")
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               *(["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", "-f", "mp4"] if mp4
                 else ["-map", "0", "-c", "copy", "-f", "matroska"]), str(tmp)]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600, check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            tmp.replace(dest)
            REMUX[key] = {"status": "listo", "file": str(dest), "parts": len(parts)}
        except Exception as exc:
            REMUX[key] = {"status": "error", "file": str(dest), "error": str(exc)}
            tmp.unlink(missing_ok=True)
        finally:
            lst.unlink(missing_ok=True)
    threading.Thread(target=work, daemon=True).start()
    return REMUX[key]


def remux_full(path, fast=False):
    """Partido completo en MP4 para Premiere (Premiere no abre MKV). Copia sin
    recodificar: tarda lo que tarda copiar el archivo. Queda en la carpeta de
    exportacion (no en la de partidos, para que no se analice de nuevo)."""
    from .ffmpeg import ffmpeg_exe
    src = Path(path)
    if not src.exists():
        return {"error": f"No existe {src}"}
    cfg = load_config()
    base = Path(cfg["export_dir"]).expanduser() if cfg.get("export_dir") else Path(cfg["data_dir"]) / "exportados"
    out_dir = base / safe_name(src.stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / (f"{src.stem.replace(' (completo)', '')} COMPLETO.mp4" if fast else f"{src.stem} (completo).mp4")
    key = str(src) + ("|fast" if fast else "")
    if REMUX.get(key, {}).get("status") == "copiando":
        return REMUX[key]
    if dest.exists() and dest.stat().st_size > 0.9 * src.stat().st_size:
        return {"status": "listo", "file": str(dest)}
    REMUX[key] = {"status": "copiando", "file": str(dest)}

    def work():
        tmp = dest.with_suffix(".part.mp4")
        # fast: audio sin filtros (solo se copia): tarda lo que tarda copiar el archivo
        acodec = ["-c:a", "copy"] if fast else ["-af", audio_chain(), "-ar", "48000", "-c:a", "aac", "-b:a", "192k"]
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0",
               "-map", "0:a:0?", "-c:v", "copy", *acodec, "-movflags", "+faststart", str(tmp)]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600, check=True)
            tmp.replace(dest)
            REMUX[key] = {"status": "listo", "file": str(dest)}
        except Exception as exc:
            REMUX[key] = {"status": "error", "file": str(dest), "error": str(exc)}
            try:
                tmp.unlink()
            except OSError:
                pass
    threading.Thread(target=work, daemon=True).start()
    return REMUX[key]


def export_range(path, start, end, name="", folder=""):
    """Un tramo del partido recodificado a MP4 comun (lo abre cualquier editor).
    Con folder: todo directo en esa carpeta y el nombre adelante (asi quedan
    en orden aunque el partido este en varias partes)."""
    src = Path(path)
    cfg = load_config()
    base = Path(cfg["export_dir"]).expanduser() if cfg.get("export_dir") else Path(cfg["data_dir"]) / "exportados"
    if folder:
        out_dir = Path(folder).expanduser()
        if not out_dir.is_absolute():
            out_dir = base / safe_name(folder)
        stamp = f"{int(start // 60):03d}m{int(start % 60):02d}s"
        fname = f"{safe_name(name)}_{stamp}.mp4" if name else f"tramo_{safe_name(src.stem)}_{stamp}.mp4"
    else:
        out_dir = base / safe_name(src.stem)
        fname = f"tramo_{int(start // 60):03d}m{int(start % 60):02d}s_{int(end // 60):03d}m{int(end % 60):02d}s{('_' + safe_name(name)) if name else ''}.mp4"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / fname
    key = f"{src}|{start}|{end}"
    if REMUX.get(key, {}).get("status") == "copiando":
        return REMUX[key]
    if dest.exists() and dest.stat().st_size > 1000:
        return {"status": "listo", "file": str(dest)}
    REMUX[key] = {"status": "copiando", "file": str(dest)}

    def work():
        ok = cut(src, dest, float(start), float(end) - float(start), mode="export")
        REMUX[key] = {"status": "listo" if ok else "error", "file": str(dest)}
    threading.Thread(target=work, daemon=True).start()
    return REMUX[key]


SEG_HANDLE = 12.0   # segundos de mas a cada lado de cada jugada (para estirar en Premiere)


def copy_segment(video, out_dir, name, start, length, handle=SEG_HANDLE):
    """Copia (sin recodificar) un pedazo del original a MP4 para Premiere.
    Al copiar, el pedazo arranca en el cuadro clave anterior a start - handle:
    se mide cuanto antes arranco por la duracion que salio."""
    from .ffmpeg import ffmpeg_exe, probe_duration
    out_dir.mkdir(parents=True, exist_ok=True)
    s0 = max(0.0, start - handle)
    dur = length + (start - s0) + handle
    dest = out_dir / f"{safe_name(name)}_{int(s0)}.mp4"
    if not dest.exists() or dest.stat().st_size < 10000:
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{s0:.3f}", "-i", str(video),
               "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy",
               # el audio llega muy bajo de la senal: se nivela aca (el video no se toca)
               "-af", audio_chain(), "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
               "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(dest)]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)
        except Exception:
            return None
    if not dest.exists() or dest.stat().st_size < 10000:
        return None
    got = probe_duration(dest) or dur
    k = max(0.0, s0 - max(0.0, got - dur))   # donde arranca de verdad el pedazo en el original
    return {"file": str(dest), "file_start": round(k, 3), "file_duration": got}


def export_sequence(run_id, folder=""):
    """Secuencia XML para Premiere con los aprobados, apuntando al original."""
    from .premiere import build_sequence

    m = load_run(run_id)
    dec = m["decisions"]
    video = Path(m["video"])
    if not video.exists() and not any(c.get("video") for c in m["clips"]):
        raise FileNotFoundError(f"No encuentro el video original: {video}")
    items = []
    out_dir = export_dir_for(m, folder)
    media = out_dir / "medios"
    for c in m["clips"]:
        d = dec.get(c["id"]) or {}
        if d.get("decision") != "approved":
            continue
        start, length = adjusted_window(c, d)
        cv = Path(c.get("video") or video)
        it = {"start": start, "length": length, "gain": audio_gain(cv, start, length),
              "title": f"{c['id']} {c['halfLabel']} - {c['title']}",
              "marker": " · ".join(x for x in [c["halfLabel"], c["title"], c.get("detail", "")] if x)}
        if cv.suffix.lower() not in (".mp4", ".mov") or cv != video:
            seg = copy_segment(cv, media, f"{c['id']}_{c['half']}", start, length)
            if seg:
                it.update(seg)
                it["gain"] = 1.0   # el pedazo ya trae el audio nivelado
        items.append(it)
    if not items:
        raise ValueError("No hay jugadas aprobadas")
    name = f"Highlights {Path(m['videoName']).stem}"
    xml = build_sequence(name, video, items)
    dest = out_dir / f"{safe_name(Path(m['videoName']).stem)}_highlights.xml"
    dest.write_text(xml, encoding="utf-8")
    total = sum(i["length"] for i in items)
    return {"file": str(dest), "folder": str(out_dir), "count": len(items), "seconds": round(total, 1)}


def render_adjusted(run_id, clip_id, decision, rng=None):
    m = load_run(run_id)
    c = next(c for c in m["clips"] if c["id"] == clip_id)
    if rng:
        # vista previa de un tramo pedido (para alargar la jugada en el mismo cuadro)
        start = max(0.0, float(rng[0]))
        length = max(1.0, float(rng[1]) - start)
    else:
        start, length = adjusted_window(c, decision)
    out_dir = run_path(run_id) / "ajustados"
    out_dir.mkdir(exist_ok=True)
    dest = out_dir / f"{clip_id}_{int(time.time())}.mp4"
    if not cut(c.get("video") or m["video"], dest, start, length, mode="preview"):
        raise RuntimeError("No se pudo cortar el clip ajustado (¿esta el video original?)")
    return {"file": f"/media/{run_id}/ajustados/{dest.name}", "start": round(start, 2), "end": round(start + length, 2),
            "length": length}


def quick_window(video, out_dir, start, length):
    """Un tramo del video original para mirar al instante: el video se copia
    tal cual (sin recodificar, tarda un segundo) y solo se convierte el audio.
    Arranca en el cuadro clave anterior a start: se devuelve donde arranca de
    verdad, para pasar el tiempo del reproductor al del video original."""
    from .ffmpeg import ffmpeg_exe, probe_duration
    out_dir.mkdir(parents=True, exist_ok=True)
    s0 = max(0.0, float(start))
    length = max(2.0, float(length))
    dest = out_dir / f"ventana_{int(s0)}_{int(length)}_{int(time.time() * 1000) % 100000}.mp4"
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{s0:.3f}", "-i", str(video),
           "-t", f"{length:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
           "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(dest)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not dest.exists() or dest.stat().st_size < 10000:
        raise RuntimeError("No pude sacar ese tramo del video original")
    got = probe_duration(dest) or length
    real = max(0.0, s0 - max(0.0, got - length))
    return dest, real, real + got


def view_window(run_id, center, before=90.0, after=90.0, clip_id=None):
    """'Ver alrededor': el original alrededor de un momento, sin esperar."""
    m = load_run(run_id)
    video = m["video"]
    if clip_id:
        c = next((c for c in m["clips"] if c["id"] == clip_id), None)
        if c and c.get("video"):
            video = c["video"]
    start = max(0.0, float(center) - float(before))
    dest, real, end = quick_window(video, run_path(run_id) / "ajustados", start, float(before) + float(after))
    return {"file": f"/media/{run_id}/ajustados/{dest.name}", "start": round(real, 2), "end": round(end, 2)}


def add_clip(run_id, start, end, title="", video=None):
    """Jugada nueva marcada a mano (una carrera o un final que el analisis no
    encontro). Queda en el analisis como cualquier otra: se aprueba y exporta."""
    from .render import poster
    rp = run_path(run_id)
    m = json.loads((rp / "manifest.json").read_text(encoding="utf-8"))
    start, end = float(start), float(end)
    if end - start < 1:
        raise ValueError("Marcá un inicio y un fin")
    halves = [h for h in m.get("halves") or [] if h.get("key") not in ("PRE",)]
    mid = (start + end) / 2
    h = next((h for h in halves if h.get("start", 0) <= mid <= h.get("end", 0)), None)
    if h is None and halves:
        h = min(halves, key=lambda x: min(abs(x.get("start", 0) - mid), abs(x.get("end", 0) - mid)))
    n = 1 + sum(1 for c in m["clips"] if str(c["id"]).startswith("M"))
    cid = f"M{n:02d}"
    src = video or m["video"]
    dest, real, real_end = quick_window(src, rp / "clips", start - 5, (end - start) + 10)
    final_name = rp / "clips" / f"{cid}_manual.mp4"
    try:
        dest.replace(final_name)
    except OSError:
        final_name = dest
    c = {"id": cid, "half": h["key"] if h else "FIN", "halfLabel": h.get("label", "") if h else "Final",
         "type": "manual", "title": title or "Jugada agregada", "detail": "agregada a mano",
         "priority": 50, "suggested": False, "runs": 0, "start": round(start, 2), "end": round(end, 2),
         "duration": round(end - start, 2), "pitchTime": round(start, 2), "scorebugTime": round(start, 2),
         "timing": "manual", "file": f"clips/{final_name.name}",
         "previewStart": round(real, 2), "previewEnd": round(real_end, 2)}
    if video:
        c["video"] = video
    jpg = final_name.with_suffix(".jpg")
    try:
        poster(final_name, jpg, max(0.5, start - real + 1))
        if jpg.exists():
            c["poster"] = f"clips/{jpg.name}"
    except Exception:
        pass
    # si el analisis sigue en vivo, la jugada entra en lo que el en vivo publica
    for ss in list(LIVE.values()):
        if getattr(ss, "run_id", None) == run_id and not ss.finished:
            ss.ids[f"MAN|{cid}"] = cid
            ss.published[cid] = c
    m["clips"].append(c)
    m["clips"].sort(key=lambda x: (x.get("start", 0) if x.get("type") != "final" else 1e12))
    for hh in m.get("halves") or []:
        if h and hh.get("key") == h["key"]:
            hh.setdefault("clips", []).append(cid)
            hh["clips"].sort(key=lambda i: next((x.get("start", 0) for x in m["clips"] if x["id"] == i), 0))
    tmp = rp / "manifest.json.tmp"
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    for _ in range(20):
        try:
            tmp.replace(rp / "manifest.json")
            break
        except PermissionError:
            time.sleep(0.25)
    return c


def browse_folder(title="Elegir carpeta"):
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title=title)
        root.destroy()
        return {"folder": folder or ""}
    except Exception as exc:
        return {"folder": "", "error": str(exc)}


# ---------------------------------------------------------------- http

CTYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".json": "application/json", ".mp4": "video/mp4",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        try:
            if path == "/":
                return self.serve_file(STATIC / "index.html")
            if path == "/api/config":
                return self.send_json(load_config())
            if path == "/api/profiles":
                return self.send_json({"profiles": list_profiles()})
            if path == "/api/videos":
                return self.send_json({"videos": scan_videos()})
            if path == "/api/runs":
                return self.send_json({"runs": scan_runs()})
            if path == "/api/run":
                return self.send_json(load_run(q.get("id", [""])[0]))
            if path == "/api/jobs":
                return self.send_json({"jobs": JOBS[-30:] + [ss.info() for ss in LIVE.values()]})
            if path == "/api/rec":
                return self.send_json(rec_infos())
            if path == "/api/fixture":
                return self.send_json(fixture_info())
            if path == "/api/rec-audio":
                return self.stream_audio(RECORDERS.get(q.get("name", [""])[0]))
            if path == "/api/rec-live":
                return self.stream_monitor(RECORDERS.get(q.get("name", [""])[0]))
            if path == "/api/browse-folder":
                return self.send_json(browse_folder(q.get("title", ["Elegir carpeta"])[0]))
            if path.startswith("/media/"):
                rest = path[len("/media/"):]
                run_id, _, rel = rest.partition("/")
                base = run_path(run_id)
                f = (base / rel).resolve()
                if base not in f.parents:
                    return self.send_error(403)
                return self.serve_file(f)
            f = (STATIC / path.lstrip("/")).resolve()
            if STATIC.resolve() not in f.parents:
                return self.send_error(403)
            return self.serve_file(f)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, status=500)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", "0") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/config":
                cfg = load_config()
                for k in ("video_folders", "data_dir", "export_dir", "default_profile",
                          "auto_analyze", "auto_approve", "auto_xml", "live_mode", "min_size_mb",
                          "srt_sources", "auto_cut", "fixture", "previa", "denoise", "shutdown_after_stop", "rec_auto_at", "rec_stop_at", "stop_after_last"):
                    if k in body:
                        cfg[k] = body[k]
                save_config(cfg)
                if body.get("shutdown_after_stop") is False:
                    cancel_shutdown()
                if "srt_sources" in body:
                    threading.Thread(target=ensure_monitors, daemon=True).start()
                return self.send_json(cfg)
            if u.path == "/api/job-cancel":
                n = 0
                for j in JOBS:
                    if j["status"] == "en cola" and (j["id"] == body.get("id") or body.get("all")):
                        j["status"] = "cancelado"
                        n += 1
                return self.send_json({"cancelled": n})
            if u.path == "/api/queue":
                profile = body.get("profile") or load_config()["default_profile"]
                jobs = [enqueue(p, profile) for p in body.get("paths", [])]
                return self.send_json({"jobs": jobs})
            if u.path == "/api/live-start":
                return self.send_json(start_live(body["path"], body.get("profile") or load_config()["default_profile"]))
            if u.path == "/api/rec":
                return self.send_json(rec_action(body["name"], body["action"]))
            if u.path == "/api/rec-sample":
                return self.send_json(rec_sample(body["name"], int(body.get("minutes") or 10)))
            if u.path == "/api/fixture-edit":
                fixture_mod.save_edit(fixture_edits_path(), body["n"], body)
                return self.send_json(fixture_info())
            if u.path == "/api/live-stop":
                return self.send_json(stop_live(body["path"]))
            if u.path == "/api/reanalyze":
                m = load_run(body["runId"])
                job = enqueue(m["video"], body.get("profile") or m.get("profile"), reuse_run=body["runId"])
                return self.send_json({"jobs": [job]})
            if u.path == "/api/decisions":
                return self.send_json(save_decisions(body["runId"], body.get("decisions") or {}))
            if u.path == "/api/export":
                if body.get("wait"):
                    return self.send_json(export_approved(body["runId"], body.get("folder", "")))
                return self.send_json(start_export(body["runId"], body.get("folder", "")))
            if u.path == "/api/export-xml":
                return self.send_json(export_sequence(body["runId"], body.get("folder", "")))
            if u.path == "/api/window":
                return self.send_json(view_window(body["runId"], body["center"], body.get("before", 90),
                                                  body.get("after", 90), body.get("clipId")))
            if u.path == "/api/add-clip":
                return self.send_json(add_clip(body["runId"], body["start"], body["end"], body.get("title", ""),
                                               body.get("video")))
            if u.path == "/api/render-adjusted":
                return self.send_json(render_adjusted(body["runId"], body["clipId"], body.get("decision") or {},
                                                      body.get("range")))
            if u.path == "/api/subset":
                return self.send_json(subset_run(body["runId"], body.get("types") or ["carrera"], body.get("label", "")))
            if u.path == "/api/export-range":
                return self.send_json(export_range(body["path"], float(body["start"]), float(body["end"]), body.get("name", ""), body.get("folder", "")))
            if u.path == "/api/merge":
                return self.send_json(merge_game(body["path"], mp4=bool(body.get("mp4"))))
            if u.path == "/api/remux":
                return self.send_json(remux_full(body["path"], bool(body.get("fast"))))
            if u.path == "/api/update":
                return self.send_json(self_update())
            if u.path == "/api/delete-run":
                return self.send_json(delete_run(body["runId"]))
        except Exception as exc:
            traceback.print_exc()
            return self.send_json({"error": str(exc)}, status=500)
        return self.send_error(404)

    def stream_audio(self, rec):
        """Sonido del monitor: MP3 sin fin, el navegador lo reproduce con <audio>."""
        if not rec or rec.no_audio:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = rec.listen()
        try:
            while rec.thread is not None and rec.thread.is_alive():
                try:
                    data = q.get(timeout=5)
                except Exception:
                    continue
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            rec.unlisten(q)

    def stream_monitor(self, rec):
        """Video del monitor como MJPEG (multipart): el navegador lo muestra
        fluido en un <img>, sin reproductor ni internet."""
        if not rec:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=cuadro")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seq = -1
        try:
            while True:
                frame, new_seq = rec.wait_frame(seq)
                if frame is None or new_seq == seq:
                    if rec.thread is None or not rec.thread.is_alive():
                        break
                    continue
                seq = new_seq
                self.wfile.write(b"--cuadro\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def serve_file(self, path):
        path = Path(path)
        if not path.exists() or not path.is_file():
            return self.send_error(404)
        ctype = CTYPES.get(path.suffix.lower(), "application/octet-stream")
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                a, b = m.groups()
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def log_message(self, fmt, *args):
        if "/api/jobs" in (args[0] if args else ""):
            return
        print(fmt % args)


class _Tee:
    """Lo que el panel escribe en la ventana negra tambien va a datos/panel.log
    (para saber por que se cerro, si pasa)."""
    def __init__(self, *streams):
        self.streams = [x for x in streams if x]

    def write(self, data):
        for st in self.streams:
            try:
                st.write(data)
                st.flush()
            except Exception:
                pass

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


def _setup_log():
    import faulthandler
    import sys
    try:
        d = Path(load_config()["data_dir"])
        d.mkdir(parents=True, exist_ok=True)
        log = open(d / "panel.log", "a", encoding="utf-8", buffering=1)
        log.write(f"\n===== panel abierto {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        sys.stdout = _Tee(sys.__stdout__, log)
        sys.stderr = _Tee(sys.__stderr__, log)
        faulthandler.enable(log)
    except Exception:
        pass


def self_update():
    """Boton Actualizar: baja la version nueva de GitHub y reinicia el panel.
    Las grabaciones siguen (el panel nuevo se engancha a ellas)."""
    import sys
    repo = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(["git", "pull"], cwd=repo, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return {"error": f"No pude bajar la version nueva: {exc}"}
    if out.returncode != 0:
        return {"error": (out.stderr or out.stdout).strip()[-400:]}
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(repo / "requirements.txt")],
                       cwd=repo, capture_output=True, timeout=600)
    except Exception:
        pass

    def bye():
        time.sleep(1.5)
        os._exit(0)   # correr_servidor.bat lo vuelve a abrir en 3 segundos
    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True, "git": out.stdout.strip()[-400:]}


class ExclusiveServer(ThreadingHTTPServer):
    """En Windows, con SO_REUSEADDR dos paneles pueden abrir el mismo puerto a
    la vez y grabar dos veces. Se pide el puerto en exclusiva."""
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    import sys
    _setup_log()
    port = int(os.environ.get("HIGHLIGHT_PORT", "8790"))
    host = os.environ.get("HIGHLIGHT_HOST", "127.0.0.1")
    try:
        srv = ExclusiveServer((host, port), Handler)
    except OSError:
        print(f"El panel ya esta abierto (puerto {port} ocupado).")
        sys.exit(3)
    srv.daemon_threads = True
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=resume_recordings, daemon=True).start()
    threading.Thread(target=rec_scheduler, daemon=True).start()
    # el video y el audio del monitor van por otro puerto: el navegador abre
    # pocas conexiones por puerto y si las ocupan las transmisiones, los botones
    # del panel quedan esperando
    try:
        srv2 = ExclusiveServer((host, port + 1), Handler)
        srv2.daemon_threads = True
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
    except OSError:
        print(f"No pude abrir el puerto {port + 1} para el monitor")
    print(f"Panel de highlights: http://127.0.0.1:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
