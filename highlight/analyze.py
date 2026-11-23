"""Analisis completo de un partido grabado."""
import json
import pickle
import time
from pathlib import Path

import numpy as np

from . import events as ev_mod
from .extract import RMS_RATE, extract, find_stingers, load_audio_rms, load_context
from .ffmpeg import hms, probe_duration
from .render import cut, poster, safe_name
from .scorebug import Profile, read_scorebug_video

HALF_NAME = {"T": "alta", "B": "baja"}
PREVIEW_HANDLE = 15.0  # segundos de mas en la vista previa, antes y despues
PRE_HANDLE = 45.0      # en la previa (himnos, saludo) mas margen para encontrar el momento


def half_label(inning, half):
    return f"{inning}ª {HALF_NAME.get(half, half)}" if inning else "Sin entrada"


def score_text(state):
    a, h = state.get("away"), state.get("home")
    if a is None or h is None:
        return ""
    return f"{a}-{h}"


def bases_text(b):
    if not b:
        return ""
    names = [n for flag, n in zip(b, ["1ª", "2ª", "3ª"]) if flag == "1"]
    return ", ".join(names) if names else "vacias"


PRE_TITLES = {"himno": "Himno", "saludo": "Saludo"}


def pregame(work, raw, fps):
    """Himnos y saludo antes del primer lanzamiento (ver events.detect_pregame)."""
    from .extract import load_audio_samples
    try:
        from .server import load_config
        if not load_config().get("previa", True):
            return []
    except Exception:
        pass

    t_first = ev_mod.first_scorebug_time(raw["present"], fps)
    # sin scorebug en todo el archivo (la señal arranco antes y se corto antes
    # del juego): todo el archivo es previa
    samples, sr = load_audio_samples(work, max_s=t_first)
    if samples is None:
        return []
    return ev_mod.detect_pregame(samples, sr, t_first)


def find_final_graphic(video, t0, t1, crop=None, W=320, H=180):
    """Busca el grafico del resultado final (la tabla con las entradas) despues
    del ultimo lanzamiento: un recuadro grande y quieto en la mitad de abajo de
    la pantalla mientras la imagen de fondo se mueve (festejos, saludos).
    Sirve para cualquier diseno de grafico. Devuelve (inicio, fin) o None."""
    import subprocess

    import cv2

    from .ffmpeg import ffmpeg_exe

    if t1 - t0 < 10:
        return None
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t0):.2f}", "-i", str(video),
           "-t", f"{t1 - t0:.2f}", "-vf", f"fps=1,scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    try:
        data = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=600).stdout
    except subprocess.TimeoutExpired:
        return None
    n = len(data) // (W * H)
    if n < 10:
        return None
    v = np.frombuffer(data[: n * W * H], np.uint8).reshape(n, H, W).astype(np.float32)
    keep = np.ones((H, W), bool)
    keep[: int(0.45 * H)] = False          # la tabla va en la mitad de abajo
    keep[int(0.97 * H):] = False
    if crop:                                # el scorebug no cuenta
        x0, y0 = int(crop["x"] * W) - 4, int(crop["y"] * H) - 4
        keep[max(0, y0): int((crop["y"] + crop["h"]) * H) + 4, max(0, x0): int((crop["x"] + crop["w"]) * W) + 4] = False
    hits = []
    for i in range(2, n - 2):
        win = v[i - 2:i + 3]
        sd = win.std(0)
        mean = win.mean(0)
        ed = np.abs(cv2.Sobel(mean, cv2.CV_32F, 1, 0)) + np.abs(cv2.Sobel(mean, cv2.CV_32F, 0, 1))
        stat = (sd < 5) & (ed > 40) & keep
        dyn = float(np.median(sd[~stat]))
        m = cv2.morphologyEx(stat.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 15), np.uint8))
        k, _, st, _ = cv2.connectedComponentsWithStats(m)
        ok = False
        if k > 1:
            j = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
            w, h, a = st[j, 2] / W, st[j, 3] / H, st[j, 4] / (W * H)
            # una tabla: ancha y baja. El fondo puede estar quieto (plano
            # general del estadio vacio) o moverse (festejos, saludos)
            ok = w >= 0.28 and h >= 0.04 and a >= 0.035 and w / max(h, 1e-3) >= 2.5 and (dyn > 4 or a < 0.2)
        hits.append(ok)
    # recuadros que se quedan en pantalla 8 s o mas; antes de la tabla del
    # resultado suele aparecer otro (jugadora del partido, resumen): se usa el
    # ultimo, que es la tabla que acompana el saludo
    runs, i = [], 0
    while i < len(hits):
        if hits[i]:
            j = i
            while j + 1 < len(hits) and hits[j + 1]:
                j += 1
            if j - i + 1 >= 8:
                runs.append((t0 + i + 2, t0 + j + 2))
            i = j + 1
        else:
            i += 1
    return runs[-1] if runs else None


def refine_final(video, fin, crop=None, total_s=120.0):
    """El Final tiene que traer el grafico del resultado (resumen de las
    entradas) y los jugadores saludandose: ~2 minutos que terminan despues del
    grafico."""
    if not fin:
        return fin
    g = find_final_graphic(video, fin["pitchTime"] + 10, min(fin["pitchTime"] + 330, fin.get("videoEnd") or 1e9), crop)
    if not g:
        # sin el grafico: 4 minutos desde el ultimo lanzamiento (festejos y
        # saludo casi siempre entran); sobra, pero se recorta con I / O
        fin = dict(fin)
        fin["end"] = round(min(fin["start"] + 240.0, fin.get("videoEnd") or fin["start"] + 240.0), 2)
        fin["duration"] = round(fin["end"] - fin["start"], 2)
        return fin
    fin = dict(fin)
    end = max(g[1] + 8.0, fin["start"] + total_s)
    end = min(end, fin["start"] + 240.0, fin.get("videoEnd") or end)
    fin["end"] = round(end, 2)
    fin["graphic"] = [round(g[0], 1), round(g[1], 1)]
    return fin


def build_clips(result):
    clips = []
    halves = []
    n = 0
    pre = result.get("pregame") or []
    if pre:
        ids = []
        for k, p in enumerate(pre, 1):
            cid = f"P{k}"
            detail = "10 s del himno (marcá inicio con I y el fin se pone solo)" if p["type"] == "himno" \
                else "los equipos se saludan antes del juego"
            clips.append({
                "id": cid, "half": "PRE", "halfLabel": "Previa", "type": p["type"], "title": p["label"],
                "detail": detail, "priority": 90, "suggested": True, "runs": 0, "strikeout": False,
                "start": p["start"], "end": p["end"], "duration": round(p["end"] - p["start"], 2),
                "pitchTime": p["start"] + 2.0, "scorebugTime": p["start"], "replay": None, "timing": "previa",
                "sourceTime": hms(p["start"]), "before": {}, "after": {},
            })
            ids.append(cid)
        halves.append({"key": "PRE", "label": "Previa: himnos y saludo", "inning": None, "half": None,
                       "start": pre[0]["start"], "end": pre[-1]["end"], "clips": ids})
    for h in result["story"]:
        key = f"{h['inning']}{h['half']}"
        ids = []
        for e in h["events"]:
            n += 1
            cid = f"{n:02d}"
            b, a = e.get("before") or {}, e.get("after") or {}
            title = e["label"]
            if e["type"] == "tercer_out" and e.get("strikeout"):
                title = "3er out (posible ponche)"
            if e["type"] == "carrera" and e.get("runs") not in (0, None):
                r = e["runs"]
                title = "Carrera" if r == 1 else (f"{r} carreras" if r != "?" else "Carrera(s)")
                if e.get("timing") == "tramo":
                    title += " · varias jugadas, revisá el tramo"
            detail = []
            if score_text(b) and score_text(a) and score_text(b) != score_text(a):
                detail.append(f"{score_text(b)} → {score_text(a)}")
            elif score_text(b):
                detail.append(score_text(b))
            if e.get("count_before"):
                detail.append(f"conteo {e['count_before']}")
            if b.get("outs") is not None:
                detail.append(f"{b['outs']} out" + ("s" if str(b["outs"]) != "1" else ""))
            if b.get("bases") is not None and a.get("bases") is not None and b.get("bases") != a.get("bases") and not e.get("half_changed"):
                detail.append(f"bases {bases_text(b['bases'])} → {bases_text(a['bases'])}")
            elif b.get("bases") not in (None, "000"):
                detail.append(f"en base: {bases_text(b['bases'])}")
            clips.append({
                "id": cid,
                "half": key,
                "halfLabel": half_label(h["inning"], h["half"]),
                "type": e["type"],
                "title": title,
                "detail": " · ".join(detail),
                "priority": e["priority"],
                "suggested": e["type"] in ("carrera", "hit", "doble_play") or (e["type"] == "tercer_out" and b.get("bases") not in (None, "000")),
                "runs": e.get("runs"),
                "strikeout": bool(e.get("strikeout")),
                "start": e["start"],
                "end": e["end"],
                "duration": round(e["end"] - e["start"], 2),
                "pitchTime": e["pitchTime"],
                "scorebugTime": e["scorebugTime"],
                "replay": e.get("replay"),
                "timing": e["timing"],
                "sourceTime": hms(e["start"]),
                "before": b,
                "after": a,
            })
            ids.append(cid)
        types = [e["type"] for e in h["events"]]
        if h.get("missing"):
            status = "sin_scorebug"
        elif types == ["revisar"]:
            status = "sin_jugadas"
        elif any(e.get("half_changed") for e in h["events"]):
            status = "ok"
        else:
            status = "sin_cierre"  # no se vio el 3er out (pasa en la ultima entrada: lo cubre el Final)
        same = next((x for x in halves if x["key"] == key), None)
        if same is not None:
            # la misma media entrada aparece dos veces (el grafico volvio un
            # rato a una entrada anterior): una sola en la lista
            same["clips"] += ids
            same["end"] = max(same["end"], h["end"])
            if status == "ok":
                same["status"] = "ok"
            continue
        halves.append({"key": key, "label": half_label(h["inning"], h["half"]), "inning": h["inning"], "half": h["half"],
                       "start": h["start"], "end": h["end"], "clips": ids, "status": status})
    fin = result.get("final")
    if fin:
        n += 1
        cid = f"{n:02d}"
        sc = score_text(fin["after"])
        clips.append({
            "id": cid, "half": "FIN", "halfLabel": "Final", "type": "final",
            "title": f"Final {sc}" if sc else "Final",
            "detail": "última jugada, festejos, saludos y score final"
                      + (" · con el gráfico del resultado" if fin.get("graphic") else " · no encontré el gráfico del resultado, revisá el fin")
                      + (" · ojo: la grabación parece cortarse antes del final" if fin.get("recordingCut") else ""),
            "priority": fin["priority"], "suggested": True, "runs": 0,
            "start": fin["start"], "end": fin["end"], "duration": round(fin["end"] - fin["start"], 2),
            "pitchTime": fin["pitchTime"], "scorebugTime": fin["scorebugTime"], "replay": None,
            "timing": "final", "sourceTime": hms(fin["start"]), "before": fin["before"], "after": fin["after"],
        })
        halves.append({"key": "FIN", "label": "Final", "inning": None, "half": None,
                       "start": fin["start"], "end": fin["end"], "clips": [cid]})
    return clips, halves


def read_scorebug_segments(work, profile, on_progress=None):
    """Lee los tramos scorebug_NNNN.mp4 de un analisis en vivo y los une."""
    import csv
    rows = [r for r in csv.reader(open(Path(work) / "scorebug_lista.csv", newline="")) if r]
    raw = None
    for k, row in enumerate(rows):
        seg = read_scorebug_video(Path(work) / row[0], profile)
        if raw is None:
            raw = {key: [] for key in seg if isinstance(seg[key], list)}
            raw["fps"] = seg["fps"]
        n = int(round((float(row[2]) - float(row[1])) * profile.fps)) if len(row) >= 3 else seg["frames"]
        for key, v in seg.items():
            if isinstance(v, list) and key in raw:
                raw[key].extend(v[:n] + [v[-1] if v else None] * max(0, n - len(v)) if n else v)
        if on_progress:
            on_progress((k + 1) / len(rows))
    if raw is None:
        raw = read_scorebug_video(Path(work) / "scorebug.mp4", profile)
    raw["frames"] = len(raw.get("present", []))
    return raw


def analyze(video, runs_dir, profile_name="on_softball", on_progress=None, render_previews=True, reuse_work=None):
    video = Path(video).resolve()
    if not video.exists():
        raise FileNotFoundError(f"No existe {video}")

    def prog(stage, frac, msg=""):
        if on_progress:
            on_progress(stage, float(frac), msg)

    duration = probe_duration(video) or 0
    picked = None
    if profile_name in (None, "", "auto"):
        from .locate import pick_profile
        from .scorebug import list_profiles

        prog("extraer", 0, "Buscando qué scorebug tiene el video")
        names = [p["name"] for p in list_profiles()]
        name, crop0, info0 = pick_profile(video, duration, names)
        if not name:
            raise RuntimeError("No reconozco el scorebug de este video con ningun perfil. "
                               "Hace falta armar un perfil nuevo para este grafico.")
        profile_name, picked = name, (crop0, info0)
    profile = Profile(profile_name)
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_name(video.stem)}"
    run_dir = Path(runs_dir) / run_id
    work = run_dir / "trabajo"
    clip_dir = run_dir / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    crop = None
    if reuse_work:
        # re-analizar sin volver a leer el video (por ejemplo con una version
        # nueva de la logica): se reutiliza lo extraido antes
        import shutil
        shutil.copytree(reuse_work, work, dirs_exist_ok=True)
    else:
        from .locate import locate

        if picked:
            crop, loc_info = picked
        else:
            prog("extraer", 0, "Buscando el scorebug en la imagen")
            crop, loc_info = locate(video, profile, duration)
        if loc_info.get("status") == "no encontrado":
            prog("extraer", 0, "No encontre el scorebug en las fotos de prueba; uso la posicion del perfil")
        prog("extraer", 0.01, "Leyendo el partido (scorebug, audio, imagen)")
        extract(video, work, profile, duration, lambda f: prog("extraer", f * 0.6), crop=crop)
    t_extract = time.time() - t0
    if not reuse_work:
        (work / "ubicacion_scorebug.json").write_text(json.dumps(loc_info), encoding="utf-8")

    prog("scorebug", 0.6, "Leyendo el scorebug")
    if not (work / "scorebug.mp4").exists() and (work / "scorebug_lista.csv").exists():
        # trabajo de un analisis en vivo: el scorebug esta en tramos de 1 minuto
        raw = read_scorebug_segments(work, profile, lambda f: prog("scorebug", 0.6 + f * 0.15))
    else:
        raw = read_scorebug_video(work / "scorebug.mp4", profile, lambda f: prog("scorebug", 0.6 + f * 0.15))
    with open(work / "scorebug_raw.pkl", "wb") as fh:
        pickle.dump(raw, fh)

    prog("historia", 0.76, "Armando la historia del partido")
    ctx = load_context(work)
    stingers = find_stingers(ctx, profile.stinger)
    rms = load_audio_rms(work)
    if rms is None and (work / "audio.raw").exists():
        from .live import load_audio_rms_raw
        rms = load_audio_rms_raw(work / "audio.raw")
    result = ev_mod.build_story(raw, stingers, rms, RMS_RATE,
                                replay_from_absence=bool(profile.cfg.get("replay_from_absence")),
                                out_from_count=bool(profile.cfg.get("out_from_count")))
    try:
        result["final"] = refine_final(video, result.get("final"), crop or profile.crop)
    except Exception:
        pass
    try:
        result["pregame"] = pregame(work, raw, profile.fps)
    except Exception:
        result["pregame"] = []
    clips, halves = build_clips(result)

    if render_previews:
        for k, c in enumerate(clips):
            prog("clips", 0.78 + 0.22 * k / max(1, len(clips)), f"Cortando vista previa {k + 1}/{len(clips)}")
            out = clip_dir / f"{c['id']}_{c['half']}_{c['type']}.mp4"
            # la vista previa trae margen de mas a cada lado para poder marcar
            # inicio y fin por fuera del corte sugerido
            hd = PRE_HANDLE if c["half"] == "PRE" else PREVIEW_HANDLE
            p0 = max(0.0, c["start"] - hd)
            p1 = min(duration or c["end"] + hd, c["end"] + hd)
            if cut(video, out, p0, p1 - p0, mode="preview"):
                c["file"] = f"clips/{out.name}"
                c["previewStart"], c["previewEnd"] = round(p0, 2), round(p1, 2)
                jpg = clip_dir / f"{out.stem}.jpg"
                poster(out, jpg, min(p1 - p0 - 0.5, max(0.5, c["pitchTime"] - p0 + 1.5)))
                if jpg.exists():
                    c["poster"] = f"clips/{jpg.name}"

    manifest = {
        "runId": run_id,
        "video": str(video),
        "videoName": video.name,
        "profile": profile_name,
        "duration": duration,
        "created": time.time(),
        "timings": {"extraer_s": round(t_extract, 1), "total_s": round(time.time() - t0, 1)},
        "scorebugLocation": _read_json(work / "ubicacion_scorebug.json"),
        "stats": {
            "halves": sum(1 for h in halves if h["key"] not in ("FIN", "PRE")),
            "clips": len(clips),
            "carreras": sum(1 for c in clips if c["type"] == "carrera"),
            "pitchesMph": len(result["mph"]),
            "replays": len(result["replays"]),
            "finalScore": score_text(result["events"][-1]["after"]) if result["events"] else "",
        },
        "halves": halves,
        "clips": clips,
        "logic": [
            "La historia sale del scorebug: cada cambio de outs, bases, carreras o entrada es una jugada.",
            "Ninguna media entrada queda afuera; si no se detecto nada aparece como 'revisar a mano'.",
            "El scorebug se actualiza tarde: el lanzamiento se ubica con el MPH previo, el replay y el audio.",
            "Correcciones del operador (un out que aparece y se va) se descartan.",
            "Los clips de jugadas con replay incluyen el replay; se puede recortar o extender antes de exportar.",
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=_np), encoding="utf-8")
    prog("listo", 1.0, "Listo")
    return manifest


def _read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def _np(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))
