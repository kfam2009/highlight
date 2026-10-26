# Highlights · softball / béisbol

Panel local para armar los highlights de un partido grabado (OBS / vMix).
La historia del juego sale del **scorebug**: cada cambio de outs, bases,
carreras o entrada es una jugada, así **ninguna media entrada se pierde**,
haya relato o no. El operador revisa las jugadas ordenadas por entrada,
aprueba, ajusta el corte si hace falta y exporta los aprobados a una carpeta
para Premiere.

## Uso diario

1. Doble clic en **`iniciar_panel.bat`**. La primera vez prepara Python y las
   dependencias (tarda un par de minutos); después abre el panel en
   `http://127.0.0.1:8790`.
2. En **Configuración** poné la carpeta donde OBS / vMix guardan las
   grabaciones (una vez por PC).
3. Marcá uno o varios juegos y **Analizar seleccionados**. Se analizan en
   cola, uno detrás de otro; mientras tanto podés revisar el anterior. Un
   archivo que se está grabando aparece como "grabando" y no se puede elegir.
4. Abrí el análisis: las jugadas están agrupadas por media entrada.
   - **Aprobar / Descartar** (o teclas **A** / **D** con el mouse sobre el clip,
     **↑ ↓** para moverse, **espacio** para reproducir).
   - **Aprobar sugeridos** marca de una vez carreras, hits, doble plays y
     terceros outs con corredores en base.
   - **Ajustar corte**: recortar ±0.5 s o extender 5/10/20 s antes o
     después. "Ver ajustado" corta una vista previa nueva desde el original.
5. **Exportar aprobados**: corta cada clip desde el video original (buena
   calidad) y deja en la carpeta `orden_para_premiere.txt` con el orden.

Las decisiones se guardan solas; podés cerrar el panel y seguir después.

Para actualizar el programa: **`actualizar.bat`** (hace `git pull`).

## Cómo funciona

```
video original ──(1 pasada de ffmpeg)──► scorebug 5 fps · imagen 64x36 2 fps · audio 8 kHz
                                              │
                    lectura del scorebug ◄────┘   carreras, entrada, alta/baja, outs,
                                                   conteo, bases, MPH de cada pitcheo
                              │
              historia: cambios → jugadas → medias entradas
                              │
      ubicar el lanzamiento: el scorebug se actualiza tarde (a veces después
      del replay o de una tanda de publicidad), así que se busca hacia atrás:
      replay (placa de transición) → último MPH → pico de audio
                              │
           vistas previas 720p ──► panel ──► exportación desde el original
```

- **Tipos de jugada**: carrera, hit / avance, doble play, 3er out, out,
  posible ponche (out con 2 strikes y sin movimiento de bases), base por
  bolas. Si una media entrada no tiene nada detectado, aparece igual como
  "revisar a mano" con su tramo.
- **Correcciones del operador** del gráfico (un out que aparece y se va,
  bases que se borran y vuelven) se descartan.
- Si aparece un número que el perfil no conoce (por ejemplo una carrera 7),
  igual se detecta el **cambio**; solo se muestra el valor como `?`.

## Línea para Premiere

**Línea para Premiere** genera `<partido>_highlights.xml` en la carpeta de
exportación. En Premiere: *Archivo → Importar* ese archivo y aparece la
secuencia armada en orden, cada jugada como un clip del video original (sin
renderizar) con un marcador que dice entrada, jugada y marcador. Como apunta
al original, cualquier corte se puede estirar en la línea de tiempo.
**Sugeridos + ponches de cierre** aprueba de una vez lo sugerido más los
ponches que terminan una entrada.

## Perfiles de scorebug

Cada diseño de gráfico es un perfil en `highlight/profiles/<nombre>/`:
posición de cada dato, plantillas de los números, la imagen de referencia
para saber si el scorebug está en pantalla y la placa de los replays.
Perfiles incluidos:

- `on_softball`: el gráfico del partido del 15/08/2026 (dos campos, mismo gráfico).
- `wbsc_softball`: el gráfico de la WBSC (Mundial), calibrado con la final NZL-VEN.

Con **Automático** el panel saca unas fotos del video, busca cuál de los
scorebugs aparece y dónde está (aunque esté corrido o en otro tamaño) y ajusta
el recorte solo. Cambiar de campo no requiere nada; un diseño de gráfico
nuevo sí necesita un perfil nuevo (se arma con un partido de muestra).

## Consola

```
.venv\Scripts\python -m highlight.cli "D:\Grabaciones\partido.mkv"
```

## Estructura

```
iniciar_panel.bat / actualizar.bat
highlight/
  server.py     servidor local, cola de análisis, exportación
  analyze.py    orquesta un análisis completo
  extract.py    pasada única de ffmpeg sobre el original
  scorebug.py   lectura del scorebug cuadro por cuadro
  events.py     historia del partido y ubicación de cada lanzamiento
  render.py     cortes (vista previa y exportación)
  static/       la página del panel
  profiles/     perfiles de scorebug
datos/          análisis y exportaciones (no va a GitHub)
```
