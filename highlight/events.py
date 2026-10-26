"""De la lectura cruda del scorebug a la historia del partido.

1. stable():     cada campo se acepta recien cuando se mantiene ~1 s (filtra
                 ruido de compresion y cuadros de transicion).
2. raw changes:  momentos en que cambian outs, bases, carreras o entrada.
3. events:       se agrupan cambios cercanos en una sola jugada, se descartan
                 correcciones del operador (out que aparece y se va) y se
                 clasifica la jugada (carrera, hit/avance, out, 3er out,
                 doble play, posible ponche, base por bolas).
4. timing:       el operador del grafico actualiza tarde (a veces despues del
                 replay). Para ubicar el lanzamiento real se usa, en orden:
                 el ultimo MPH antes del replay/cambio, un pico de audio, y
                 si no hay nada, un margen fijo.
5. story:        las jugadas se agrupan por media entrada. Ninguna media
                 entrada queda afuera: si no se detecto nada, igual aparece
                 con su tramo completo para revisarla a mano.
"""
import numpy as np

RESULT_FIELDS = ["outs", "bases", "away", "home"]
ALL_FIELDS = ["inning", "half", "outs", "count", "bases", "away", "home"]

PRIORITY = {
    "carrera": 100,
    "hit": 70,
    "doble_play": 65,
    "tercer_out": 50,
    "out": 40,
    "ponche": 30,
    "base_por_bolas": 25,
    "otro": 20,
    "revisar": 10,
}

LABELS = {
    "carrera": "Carrera",
    "hit": "Hit / avance",
    "doble_play": "Doble play",
    "tercer_out": "3er out",
    "out": "Out",
    "ponche": "Out (posible ponche)",
    "base_por_bolas": "Base por bolas",
    "otro": "Cambio en el juego",
    "revisar": "Revisar a mano",
}


def stable(seq, minrun=5):
    out = [None] * len(seq)
    cur = None
    run_v, run_n, run_start = None, 0, 0
    for i, v in enumerate(seq):
        if v is None:
            out[i] = cur
            continue
        if v == run_v:
            run_n += 1
        else:
            run_v, run_n, run_start = v, 1, i
        if run_n >= minrun and v != cur:
            cur = v
            for j in range(run_start, i + 1):
                out[j] = cur
        out[i] = cur
    return out


def smooth_state(raw):
    st = {k: stable(raw[k]) for k in ALL_FIELDS}
    cut = game_end_index(st)
    if cut is not None:
        for k in ALL_FIELDS:
            st[k] = st[k][:cut] + [None] * (len(st[k]) - cut)
    return st


def game_end_index(st):
    """Despues del partido muchas transmisiones pasan un resumen con el
    scorebug de jugadas viejas. Se corta en el primer momento en que la
    entrada o las carreras vuelven para atras."""
    best = {"inning": 0, "away": 0, "home": 0}
    half_rank = {"T": 0, "B": 1}
    last_pos = (0, 0)
    for i in range(len(st["inning"])):
        inn = _num(st["inning"][i])
        h = st["half"][i]
        if inn is not None and h in half_rank:
            pos = (inn, half_rank[h])
            if pos < last_pos and last_pos[0] - inn >= 1:
                return i
            last_pos = max(last_pos, pos)
        for k in ("away", "home"):
            v = _num(st[k][i])
            if v is not None:
                if v < best[k]:
                    return i
                best[k] = max(best[k], v)
    return None


def onsets(flags, fps, min_gap_s=3.0):
    f = np.asarray(flags, dtype=bool)
    idx = np.where(f[1:] & ~f[:-1])[0] + 1
    ev = []
    for i in idx:
        if not ev or i - ev[-1] > min_gap_s * fps:
            ev.append(int(i))
    return [i / fps for i in ev]


def pnum_changes(raw, fps, thr=9.0, need=4):
    """Momentos en que cambia el contador de lanzamientos P:xx."""
    sig = raw.get("pnum_sig")
    if not sig:
        return []
    cur, cand, n, ci = None, None, 0, 0
    out = []
    for i, a in enumerate(sig):
        if a is None or raw["mph"][i]:
            continue
        a = a.astype(np.float32)
        if cur is None:
            cur = a
            continue
        if np.abs(a - cur).mean() > thr:
            if cand is not None and np.abs(a - cand).mean() <= thr:
                n += 1
            else:
                cand, n, ci = a, 1, i
            if n >= need:
                out.append(ci / fps)
                cur, cand, n = cand, None, 0
        else:
            cand, n = None, 0
            cur = 0.9 * cur + 0.1 * a
    return out


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _count_parts(c):
    try:
        b, s = c.split("-")
        return int(b), int(s)
    except Exception:
        return None, None


def raw_changes(st, fps):
    n = len(st["outs"])
    prev = {k: None for k in ALL_FIELDS}
    changes = []
    for i in range(n):
        cur = {k: st[k][i] for k in ALL_FIELDS}
        if prev["outs"] is not None:
            diff = [k for k in ["inning", "half"] + RESULT_FIELDS if cur[k] != prev[k] and cur[k] is not None]
            if diff:
                changes.append({"t": i / fps, "i": i, "fields": diff, "before": dict(prev), "after": dict(cur)})
        prev = cur
    return changes


def group_changes(changes, merge_s=10.0, clear_merge_s=150.0):
    groups = []
    for ch in changes:
        if groups and ch["t"] - groups[-1]["t_last"] <= merge_s:
            g = groups[-1]
            g["t_last"] = ch["t"]
            g["after"] = ch["after"]
            g["fields"] = sorted(set(g["fields"]) | set(ch["fields"]))
        else:
            groups.append({"t": ch["t"], "t_last": ch["t"], "fields": list(ch["fields"]),
                           "before": ch["before"], "after": ch["after"]})
    # correcciones del operador: el estado vuelve a ser el mismo
    groups = [g for g in groups if any(g["before"][k] != g["after"][k] for k in ["inning", "half"] + RESULT_FIELDS)]
    # bases que se limpian sin out ni carrera antes de un cambio de entrada:
    # es el operador limpiando el grafico despues del 3er out -> se une al cambio
    merged = []
    k = 0
    while k < len(groups):
        g = groups[k]
        only_bases_cleared = (
            g["fields"] == ["bases"]
            and g["after"]["bases"] == "000"
            and k + 1 < len(groups)
            and (groups[k + 1]["after"]["half"] != g["after"]["half"] or groups[k + 1]["after"]["inning"] != g["after"]["inning"])
            and groups[k + 1]["t"] - g["t"] <= clear_merge_s
        )
        if only_bases_cleared:
            nxt = groups[k + 1]
            merged.append({"t": g["t"], "t_last": nxt["t_last"], "fields": sorted(set(g["fields"]) | set(nxt["fields"])),
                           "before": g["before"], "after": nxt["after"]})
            k += 2
            continue
        merged.append(g)
        k += 1
    # avance de bases que se deshace enseguida (error del operador)
    clean = []
    k = 0
    while k < len(merged):
        g = merged[k]
        if (k + 1 < len(merged) and g["fields"] == ["bases"] and merged[k + 1]["fields"] == ["bases"]
                and merged[k + 1]["after"]["bases"] == g["before"]["bases"]
                and merged[k + 1]["t"] - g["t_last"] <= 60):
            k += 2
            continue
        clean.append(g)
        k += 1
    return clean


def last_count_before(st, fps, t_end, t_start):
    i1 = int(t_end * fps)
    i0 = max(0, int(t_start * fps))
    last = None
    for i in range(i1, i0, -1):
        c = st["count"][i]
        if c and c != "0-0" and not c.startswith("?"):
            return c
        if last is None and c:
            last = c
    return last or "0-0"


def classify(g, st, fps, t_prev):
    b, a = g["before"], g["after"]
    half = b["half"] or a["half"]
    half_changed = (a["half"] != b["half"]) or (a["inning"] != b["inning"])
    side = "away" if half == "T" else "home"
    bv, av = b.get(side), a.get(side)
    runs = 0
    if bv != av and av is not None:
        nb, na = _num(bv), _num(av)
        runs = (na - nb) if (nb is not None and na is not None and na > nb) else ("?" if bv != av else 0)
    if half_changed:
        outs_gained = 3 - (_num(b["outs"]) or 0)
    else:
        ob, oa = _num(b["outs"]), _num(a["outs"])
        outs_gained = (oa - ob) if (ob is not None and oa is not None) else 0
    count = last_count_before(st, fps, g["t"], max(t_prev, g["t"] - 120))
    balls, strikes = _count_parts(count)
    bases_changed = b["bases"] != a["bases"] and not half_changed

    if runs:
        typ = "carrera"
    elif outs_gained >= 2:
        typ = "doble_play"
    elif outs_gained == 1 and half_changed:
        typ = "tercer_out"
    elif outs_gained == 1:
        typ = "ponche" if (strikes == 2 and not bases_changed) else "out"
    elif bases_changed:
        typ = "base_por_bolas" if balls == 3 else "hit"
    elif outs_gained < 0:
        typ = "revisar"
    else:
        typ = "otro"
    return {
        "type": typ,
        "label": LABELS[typ],
        "priority": PRIORITY[typ],
        "runs": runs,
        "outs_gained": outs_gained,
        "count_before": count,
        "half_changed": half_changed,
        "batting": side,
        "inning": b["inning"],
        "half": half,
        "strikeout": strikes == 2 and not runs and outs_gained == 1 and (half_changed or not bases_changed),
    }


def _first_in(times, t0, t1):
    for t in times:
        if t0 <= t <= t1:
            return t
    return None


def _last_in(times, t0, t1):
    best = None
    for t in times:
        if t0 <= t <= t1:
            best = t
    return best


def loud_onset(audio_rms, audio_rate, t0, t1, thr, min_len_s=0.75, present=None, fps=5):
    if audio_rms is None or thr is None:
        return None
    i0, i1 = max(0, int(t0 * audio_rate)), min(len(audio_rms), int(t1 * audio_rate))
    need = max(1, int(min_len_s * audio_rate))
    run = 0
    for i in range(i0, i1):
        ok = True
        if present is not None:
            j = int(i / audio_rate * fps)
            ok = 0 <= j < len(present) and any(present[max(0, j - 2 * fps):j + 1])
        if ok and audio_rms[i] > thr:
            run += 1
            if run >= need:
                return (i - run + 1) / audio_rate
        else:
            run = 0
    return None


def replay_blocks(stingers, gap_s=25.0):
    blocks = []
    for t in stingers:
        if blocks and t - blocks[-1][1] <= gap_s:
            blocks[-1][1] = t
        else:
            blocks.append([t, t])
    return [(a, b) for a, b in blocks]


def time_play(ev, t_prev, mph, stingers_blocks, audio_rms, audio_rate, audio_thr, present=None, fps=5):
    """Ubica el lanzamiento real de la jugada.

    El operador del scorebug suele actualizar tarde (despues del replay o
    incluso despues de una tanda de publicidad), por eso se busca hacia atras
    desde el cambio del scorebug:
      - si hay un replay (stinger) en el medio, la jugada esta antes del replay;
      - el ultimo MPH antes de eso marca el lanzamiento (~2 s despues del pitch);
      - si despues de ese MPH hay mucho tiempo sin explicar y un pico fuerte de
        audio con el scorebug en pantalla, el pitch de la jugada no tuvo MPH
        (pasa en jonrones) y se usa el audio.
    """
    E = ev["t"]
    w0 = max(t_prev + 2.0, E - 300.0)
    # un replay que arranca enseguida del cambio anterior es de la jugada anterior
    rw0 = max(w0, t_prev + 20.0) if t_prev > 0 else w0
    rb = None
    for a, b in stingers_blocks:
        if rw0 <= a <= E:
            rb = (a, b)
            break
    search_end = rb[0] if rb else E
    how = "margen"
    m = _last_in(mph, w0, search_end - 1.0)
    loud = None
    allow_audio = not ev.get("half_changed")
    if allow_audio and (m is None or search_end - m > 35.0):
        a0 = (m + 8.0) if m is not None else w0
        loud = loud_onset(audio_rms, audio_rate, a0, search_end, audio_thr, present=present, fps=fps)
    if loud is not None:
        pitch = loud - 1.5
        how = "audio"
    elif m is not None:
        pitch = m - 2.0
        how = "mph"
    else:
        pitch = search_end - (14.0 if rb else 15.0)
    pitch = max(w0 - 2.0, pitch)
    if not rb:
        # replay que llega despues del cambio del scorebug (graficos que actualizan rapido)
        for a, b in stingers_blocks:
            if E < a <= E + 20.0 and a - pitch < 45.0:
                rb = (a, b)
                break
    big = ev["type"] in ("carrera", "hit", "doble_play")
    start = max(0.0, pitch - 5.0)
    live_end = pitch + (20.0 if big else 12.0)
    if rb and rb[0] - pitch < 45.0:
        end = max(live_end, (rb[1] + 1.0) if rb[1] > rb[0] else rb[0] + 12.0)
    else:
        end = live_end
    end = min(end, start + 80.0)
    return {
        "pitchTime": round(float(pitch), 2),
        "start": round(float(start), 2),
        "end": round(float(end), 2),
        "replay": [round(float(rb[0]), 2), round(float(rb[1]), 2)] if rb else None,
        "timing": how,
        "scorebugTime": round(float(E), 2),
    }


def final_segment(events, present, fps, mph=None, game_over_s=60.0):
    """Clip de cierre: ultimo lanzamiento + festejos + saludos + score final.

    Cuando termina el partido la transmision saca el scorebug por un buen rato
    (festejos, saludos, tabla de resultado). Ese hueco largo marca el final:
    el ultimo lanzamiento esta justo antes, y el clip sigue ~2 minutos para
    cubrir festejos, saludos y la placa con el score final.
    """
    if not events:
        return None
    last = events[-1]
    vid_end = len(present) / fps
    i0 = int(last["pitchTime"] * fps)
    need = int(game_over_s * fps)
    run, t_gone = 0, None
    for i in range(max(0, i0), len(present)):
        if not present[i]:
            run += 1
            if run >= need:
                t_gone = (i - run + 1) / fps
                break
        else:
            run = 0
    if t_gone is not None:
        m = _last_in(mph or [], t_gone - 20.0, t_gone)
        pitch = (m - 2.0) if m is not None else t_gone - 8.0
        pitch = max(pitch, last["pitchTime"])
        start = max(0.0, pitch - 5.0)
        end = min(t_gone + 120.0, vid_end)
        cut = False
    else:
        # el video termina antes de que la transmision salga del juego
        pitch = last["pitchTime"]
        start = max(0.0, pitch - 5.0)
        end = min(pitch + 150.0, vid_end)
        cut = vid_end - pitch < 150.0
    return {
        "type": "final", "label": "Final: festejos, saludos y score", "priority": 95,
        "runs": 0, "outs_gained": 0, "count_before": None, "half_changed": False, "batting": None,
        "inning": last["inning"], "half": last["half"], "t": last["t"],
        "before": last.get("after") or {}, "after": last.get("after") or {},
        "pitchTime": round(pitch, 2), "start": round(start, 2), "end": round(end, 2),
        "replay": None, "timing": "final", "scorebugTime": last.get("scorebugTime", last["t"]),
        "videoEnd": round(vid_end, 2), "recordingCut": cut,
    }


def half_ranges(st, fps, min_len_s=20.0):
    """Tramos de tiempo de cada media entrada segun el scorebug.
    Tramos muy cortos (el operador cambia la entrada y la mitad en dos pasos)
    se absorben en el siguiente."""
    out = []
    cur, t0 = None, None
    n = len(st["inning"])
    for i in range(n):
        key = (st["inning"][i], st["half"][i])
        if key[0] is None or key[1] is None:
            continue
        if key != cur:
            if cur is not None:
                out.append({"inning": cur[0], "half": cur[1], "start": t0, "end": i / fps})
            cur, t0 = key, i / fps
    if cur is not None:
        out.append({"inning": cur[0], "half": cur[1], "start": t0, "end": n / fps})
    clean = []
    for h in out:
        if h["end"] - h["start"] < min_len_s:
            if clean:
                clean[-1]["end"] = h["end"]
            continue
        if clean and clean[-1]["inning"] == h["inning"] and clean[-1]["half"] == h["half"]:
            clean[-1]["end"] = h["end"]
            continue
        clean.append(h)
    return clean


def absence_replays(raw, st, fps, min_s=3.0, max_s=45.0):
    """Graficos que sacan el scorebug durante los replays: un hueco corto en
    medio de una misma media entrada se toma como replay (inicio, fin)."""
    pres = raw["present"]
    out, i, n = [], 0, len(pres)
    while i < n:
        if pres[i]:
            i += 1
            continue
        j = i
        while j < n and not pres[j]:
            j += 1
        dur = (j - i) / fps
        if min_s <= dur <= max_s and i > 0 and j < n:
            before = (st["inning"][i - 1], st["half"][i - 1])
            after = (st["inning"][j], st["half"][j])
            if before == after and None not in before:
                out.append((i / fps, j / fps))
        i = j
    return out


def build_story(raw, ctx_stingers=None, audio_rms=None, audio_rate=4, replay_from_absence=False):
    fps = raw["fps"]
    st = smooth_state(raw)
    mph = onsets(raw["mph"], fps)
    stingers = ctx_stingers or []
    blocks = replay_blocks(stingers)
    if replay_from_absence:
        blocks = sorted(blocks + absence_replays(raw, st, fps))
    audio_thr = float(np.percentile(audio_rms, 97)) if audio_rms is not None and len(audio_rms) else None

    groups = group_changes(raw_changes(st, fps))
    events = []
    first = next((i for i, v in enumerate(st["outs"]) if v is not None), 0)
    t_prev = first / fps
    for g in groups:
        info = classify(g, st, fps, t_prev)
        ev = {**info, "t": g["t"],
              "before": {k: g["before"][k] for k in ALL_FIELDS},
              "after": {k: g["after"][k] for k in ALL_FIELDS}}
        ev.update(time_play(ev, t_prev, mph, blocks, audio_rms, audio_rate, audio_thr, present=raw["present"], fps=fps))
        events.append(ev)
        t_prev = g["t_last"]

    halves = half_ranges(st, fps)
    story = [{**h, "events": []} for h in halves]
    for e in events:
        t = e["t"] - (1.0 if e["half_changed"] else 0.0)
        target = None
        for h in story:
            if h["start"] <= t < h["end"]:
                target = h
                break
        if target is None and story:
            target = min(story, key=lambda h: min(abs(t - h["start"]), abs(t - h["end"])))
        if target is not None:
            target["events"].append(e)
    # media entrada sin nada detectado: queda igual para revisar a mano
    for h in story:
        if not h["events"]:
            h["events"].append({
                "type": "revisar", "label": "Media entrada sin jugadas detectadas", "priority": PRIORITY["revisar"],
                "runs": 0, "outs_gained": 0, "count_before": None, "half_changed": False, "batting": None,
                "inning": h["inning"], "half": h["half"], "t": h["start"], "before": {}, "after": {},
                "pitchTime": h["start"], "start": h["start"], "end": min(h["end"], h["start"] + 80.0),
                "replay": None, "timing": "tramo", "scorebugTime": h["start"],
            })
    final = final_segment(events, raw["present"], fps, mph)
    return {"story": story, "events": events, "final": final, "mph": mph, "replays": blocks,
            "pitchMarks": pnum_changes(raw, fps)}
