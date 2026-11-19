# Highlights · softball / béisbol

Panel local que graba las señales SRT de cada campo y arma los highlights de
cada partido mientras se juega.
La historia del juego sale del **scorebug**: cada cambio de outs, bases,
carreras o entrada es una jugada, así **ninguna media entrada se pierde**,
haya relato o no. El operador revisa las jugadas ordenadas por entrada,
aprueba, ajusta el corte si hace falta y exporta los aprobados a una carpeta
para Premiere.

## Uso diario

1. Doble clic en **`iniciar_panel.bat`**. La primera vez prepara Python y las
   dependencias (tarda un par de minutos); después abre el panel en
   `http://127.0.0.1:8790`.
2. En **Configuración** cargá las señales SRT de cada campo (ver "Grabar las
   señales SRT") y tocá **Grabar**. Cada partido se graba, se separa y se
   analiza solo.
3. Para un video que ya tengas (descargado, de Cloudflare, etc.): ponelo en la
   carpeta de partidos, marcalo en **Juegos grabados** y **Analizar
   seleccionados**. Se analizan en cola, uno detrás de otro.
4. Abrí el análisis: las jugadas están agrupadas por media entrada.
   - **Aprobar / Descartar** (o teclas **A** / **D** con el mouse sobre el clip,
     **↑ ↓** para moverse, **espacio** para reproducir).
   - **Aprobar sugeridos** marca de una vez carreras, hits, doble plays y
     terceros outs con corredores en base.
   - **Marcar inicio / fin** (teclas **I** / **O**) en el punto exacto del
     video. La vista previa trae 15 s de más a cada lado para poder marcar por
     fuera del corte sugerido; **Alargar 10 s** estira el video en el mismo
     cuadro si hace falta más. **Ver jugada** (tecla **P**) reproduce de
     inicio a fin. Las vistas previas vienen muteadas.
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

## Modo automático

Arriba a la izquierda, **Modo automático → Analizar solo cada partido nuevo de
la carpeta**. Con eso activo, cada video que aparece en la carpeta de
grabaciones se analiza solo cuando deja de crecer unos 2 minutos (terminó la
grabación). Al terminar puede pre-aprobar los sugeridos (y los ponches que
cierran entradas) y dejar armada la línea para Premiere. Vos solo abrís el
análisis, revisás y ajustás.

### En vivo (mientras se graba)

Tildá también **En vivo**. Un archivo que se está grabando en la carpeta
empieza a leerse solo, sin esperar el final del partido: las jugadas aparecen
en el panel más o menos un minuto después de que cambia el scorebug, agrupadas
por media entrada, y se pueden ir aprobando durante el juego. Cuando la
grabación termina (3 minutos sin crecer) se cierra el análisis: se agrega el
final (festejos y saludos), se pre-aprueban los sugeridos y se arma la línea
para Premiere, igual que en el modo automático. También se puede arrancar a
mano con el botón **En vivo** de un video que está grabando.

Las grabaciones del panel ya son MKV. Un MP4 que todavía se está grabando no
se puede leer hasta que termina. Para los highlights usá la señal **sin
relato**.

### Fixture (nombre de cada partido)

El panel trae el fixture del torneo (`highlight/fixtures/`). En
**Configuración**, debajo del link de cada señal, elegí a qué cancha
corresponde. Cada grabación toma el nombre del partido que se está jugando o
está por empezar en esa cancha (por ejemplo `Gm 03 Honduras vs Puerto Rico`).
**Fixture → Ver / editar** muestra todos los partidos con el horario del lugar
y el de la PC; los equipos que se definen sobre la marcha (super ronda,
finales) se escriben ahí y se guardan solos. Sin fixture o fuera de horario, el
archivo se llama con el nombre del campo y la hora.

### Grabar las señales SRT

En **Configuración → Señales SRT** pegá el link SRT de Castr de cada campo
(una línea para Campo 1 y otra para Campo 2; el nombre se puede cambiar) y
**Guardar**. Apenas se guarda, cada campo se conecta y se ve en el **Monitor**
("en vivo · sin grabar"), así se puede chequear la señal antes del partido sin
llenar el disco.

Aparece la caja **Grabar señales** con un botón **Grabar** por campo. El panel
copia la señal tal como llega (sin recodificar, casi no usa la PC) a un MKV en
la carpeta de partidos y lo analiza en vivo. Si la señal se corta, reintenta
solo y sigue en un archivo nuevo. **Cortar juego** cierra el partido actual y
empieza el próximo en otro archivo. Con **Separar los partidos solo** (viene
activado) no hace falta: cuando el scorebug muestra un partido terminado
(7ª entrada o nocaut con ganador) y se va de pantalla unos minutos, o vuelve
en 1ª entrada 0-0, el panel corta el archivo y arranca el próximo partido.
**Monitor** muestra el video de cada señal en vivo (640 px, 25 cuadros, sacado
de lo que ya se está grabando: no gasta internet ni Cloudflare), un medidor
de audio por campo, el botón **Escuchar** para oír ese campo, y lo que dice el
scorebug. Necesita un ffmpeg con SRT: si el panel
avisa que falta, en PowerShell `winget install Gyan.FFmpeg` y reiniciar el
panel.

Arriba de cada análisis hay un control de entradas: dice si están todas las
medias entradas o cuáles conviene revisar (scorebug que no se vio, entrada sin
jugadas detectadas).

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
  live.py       análisis en vivo de una grabación que sigue creciendo
  extract.py    pasada única de ffmpeg sobre el original
  scorebug.py   lectura del scorebug cuadro por cuadro
  events.py     historia del partido y ubicación de cada lanzamiento
  render.py     cortes (vista previa y exportación)
  static/       la página del panel
  profiles/     perfiles de scorebug
datos/          análisis y exportaciones (no va a GitHub)
```
