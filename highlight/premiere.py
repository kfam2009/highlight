"""Secuencia para Premiere (XML de Final Cut 7, que Premiere importa).

Premiere no abre MKV (el formato de las grabaciones): por eso cada jugada
aprobada se copia sin recodificar a un MP4 chico con unos segundos de mas a
cada lado (para poder estirar el corte), y la secuencia apunta a esos MP4.
En Premiere: Archivo > Importar > el .xml  -> aparece la secuencia armada.
"""
import re
import subprocess
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

from .ffmpeg import ffmpeg_exe, probe_duration


def probe_info(video):
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(video)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore")
    err = proc.stderr
    info = {"width": 1920, "height": 1080, "fps": 30.0, "sample_rate": 48000, "channels": 2}
    m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", err)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Video:.*?([\d.]+) fps", err) or re.search(r"Video:.*?([\d.]+) tbr", err)
    if m:
        info["fps"] = float(m.group(1))
    m = re.search(r"Audio:.*?(\d+) Hz,\s*([^,]+)", err)
    if m:
        info["sample_rate"] = int(m.group(1))
        info["channels"] = 1 if "mono" in m.group(2) else 2
    info["duration"] = probe_duration(video) or 0
    return info


def _rate(fps):
    """timebase entero + bandera NTSC como espera el XML."""
    for nominal, real in ((24, 23.976), (30, 29.97), (60, 59.94)):
        if abs(fps - real) < 0.02:
            return nominal, True, real
    tb = int(round(fps)) or 30
    return tb, False, float(tb)


def path_url(p):
    p = str(Path(p).resolve()).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        return "file://localhost/" + quote(p, safe="/:")
    return "file://localhost" + quote(p, safe="/")


def build_sequence(name, video, items, info=None):
    """items: lista de dicts con start, length (segundos), title, marker.
    Si un item trae "file" (un pedazo del original) y "file_start" (en que
    segundo del original empieza ese pedazo), el clip apunta a ese archivo."""
    info = info or probe_info(video)
    tb, ntsc, real = _rate(info["fps"])
    ntsc_s = "TRUE" if ntsc else "FALSE"
    fr = lambda s: int(round(float(s) * real))
    rate = f"<rate><timebase>{tb}</timebase><ntsc>{ntsc_s}</ntsc></rate>"

    files = {}   # ruta -> (id, frames)

    def file_ref(path, dur_s):
        key = str(path)
        if key in files:
            return f'<file id="{files[key][0]}"/>', files[key][1]
        fid = f"file-{len(files) + 1}"
        frames = fr(dur_s) if dur_s else 10 ** 7
        files[key] = (fid, frames)
        return (f"""<file id="{fid}"><name>{escape(Path(path).name)}</name><pathurl>{escape(path_url(path))}</pathurl>{rate}<duration>{frames}</duration>
<media><video><samplecharacteristics>{rate}<width>{info['width']}</width><height>{info['height']}</height></samplecharacteristics></video>
<audio><samplecharacteristics><depth>16</depth><samplerate>{info['sample_rate']}</samplerate></samplecharacteristics><channelcount>{info['channels']}</channelcount></audio></media></file>""",
                frames)

    vclips, a1clips, a2clips = [], [], []
    pos = 0
    for k, it in enumerate(items, start=1):
        src = it.get("file") or video
        off = float(it.get("file_start") or 0.0) if it.get("file") else 0.0
        fdef, file_frames = file_ref(src, it.get("file_duration") if it.get("file") else info["duration"])
        fid = files[str(src)][0]
        cin = max(0, fr(it["start"] - off))
        cout = cin + max(1, fr(it["length"]))
        end = pos + (cout - cin)
        title = escape(it["title"])
        marker = f"<marker><name>{escape(it.get('marker') or it['title'])}</name><comment></comment><in>{cin}</in><out>-1</out></marker>"
        links = (f"<link><linkclipref>v{k}</linkclipref><mediatype>video</mediatype><trackindex>1</trackindex><clipindex>{k}</clipindex></link>"
                 f"<link><linkclipref>a{k}</linkclipref><mediatype>audio</mediatype><trackindex>1</trackindex><clipindex>{k}</clipindex><groupindex>1</groupindex></link>")
        if info["channels"] == 2:
            links += f"<link><linkclipref>b{k}</linkclipref><mediatype>audio</mediatype><trackindex>2</trackindex><clipindex>{k}</clipindex><groupindex>1</groupindex></link>"
        common = f"<name>{title}</name><enabled>TRUE</enabled><duration>{file_frames}</duration>{rate}<start>{pos}</start><end>{end}</end><in>{cin}</in><out>{cout}</out>"
        g = float(it.get("gain") or 1.0)
        level = ("" if abs(g - 1.0) < 0.01 else
                 "<filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid>"
                 "<effectcategory>audiolevels</effectcategory><effecttype>audiolevels</effecttype><mediatype>audio</mediatype>"
                 f"<parameter><parameterid>level</parameterid><name>Level</name><valuemin>0</valuemin>"
                 f"<valuemax>3.98109</valuemax><value>{g:.4f}</value></parameter></effect></filter>")
        vclips.append(f'<clipitem id="v{k}">{common}{fdef}{marker}{links}</clipitem>')
        a1clips.append(f'<clipitem id="a{k}">{common}<file id="{fid}"/><sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>{level}{links}</clipitem>')
        if info["channels"] == 2:
            a2clips.append(f'<clipitem id="b{k}">{common}<file id="{fid}"/><sourcetrack><mediatype>audio</mediatype><trackindex>2</trackindex></sourcetrack>{level}{links}</clipitem>')
        pos = end

    atracks = f"<track>{''.join(a1clips)}</track>" + (f"<track>{''.join(a2clips)}</track>" if a2clips else "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
<sequence id="seq-1"><name>{escape(name)}</name><duration>{pos}</duration>{rate}
<media>
<video><format><samplecharacteristics>{rate}<width>{info['width']}</width><height>{info['height']}</height><pixelaspectratio>square</pixelaspectratio></samplecharacteristics></format>
<track>{''.join(vclips)}</track></video>
<audio><numOutputChannels>2</numOutputChannels><format><samplecharacteristics><depth>16</depth><samplerate>{info['sample_rate']}</samplerate></samplecharacteristics></format>
{atracks}</audio>
</media>
</sequence>
</xmeml>
"""
