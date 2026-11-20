# Respaldo a R2

Una página dentro de Cloudflare que copia las grabaciones de **Cloudflare
Stream** a tu bucket de **R2** cuando vos lo pedís. El video va de Stream a R2
dentro de Cloudflare: **no pasa por tu PC ni usa tu internet**. La página solo
da la orden y muestra el avance.

## Instalarlo (una sola vez, ~10 minutos)

Todo desde el panel de Cloudflare (dash.cloudflare.com):

1. **Bucket de R2**: *R2 → Create bucket*, por ejemplo `respaldo-partidos`
   (si ya tenés uno, usá ese).
2. **Token para Stream**: *Mi perfil → API Tokens → Create Token → Custom
   token*. Permiso: **Account → Stream → Edit**. Copiá el token (se ve una sola
   vez).
3. **ID de la cuenta**: en *Workers & Pages* (columna derecha, "Account ID").
   Copialo.
4. **Crear el Worker**: *Workers & Pages → Create → Worker*, nombre
   `respaldo-r2` → *Deploy*. Después *Edit code*, borrá lo que trae, pegá todo
   el contenido de `worker.js` y *Deploy*.
5. **Configurarlo**: en el Worker, *Settings*:
   - *Bindings → Add → R2 bucket*: nombre de la variable **`BUCKET`**, el
     bucket del paso 1.
   - *Variables and Secrets → Add*:
     - `ACCOUNT_ID` (texto): el ID del paso 3.
     - `CF_API_TOKEN` (secreto): el token del paso 2.
     - `CLAVE` (secreto): una contraseña que inventes para usar la página.
   - *Deploy* para que tome los cambios.
6. Abrí la dirección del Worker (`https://respaldo-r2.<tu-subdominio>.workers.dev`),
   poné la clave y **Ver grabaciones**.

## Usarlo

- Aparecen las grabaciones de Stream, las más nuevas arriba, con una columna
  **En R2** que dice si ya están copiadas.
- **Copiar a R2** en una, o **Copiar todas las que faltan**.
- Primero Stream arma el MP4 de la grabación (puede tardar unos minutos en un
  partido largo) y después se copia en pedazos de 64 MB. Hay que dejar la
  página abierta hasta que diga **sí**.
- En R2 quedan en la carpeta `stream/` con la fecha, la hora y el nombre de la
  grabación.

## Costos (Cloudflare)

- Stream cobra el MP4 como si alguien mirara el video una vez: un partido de
  2 horas son 120 minutos entregados (≈ US$ 0,12).
- R2: los primeros 10 GB por mes son gratis; después ≈ US$ 0,015 por GB al mes.
  Copiar de Stream a R2 no tiene costo de transferencia.
- Stream no arma MP4 de grabaciones en vivo de más de 4 horas: por eso conviene
  que la productora corte la transmisión entre partidos.

## Si algo falla

- *Clave incorrecta*: revisá el secreto `CLAVE`.
- *Cloudflare Stream: ...*: el token no tiene permiso Stream Edit o el
  `ACCOUNT_ID` está mal.
- Si aparece un error de límite de CPU, el Worker necesita el plan Workers
  Paid (US$ 5 por mes).
