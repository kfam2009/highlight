// Respaldo: copia las grabaciones de Cloudflare Stream a R2, cuando vos digas.
//
// Todo pasa dentro de Cloudflare: el video va de Stream a R2 sin pasar por tu
// PC ni por tu internet. La pagina (abrila en el navegador) solo da la orden y
// muestra el avance.
//
// Configuracion en el Worker (ver LEEME.md):
//   R2 bucket binding:  BUCKET
//   Variable:           ACCOUNT_ID   (ID de la cuenta de Cloudflare)
//   Secretos:           CF_API_TOKEN (token con permiso Stream: Editar)
//                       CLAVE        (contraseña para usar la pagina)

const PART = 64 * 1024 * 1024; // pedazos de 64 MB (R2 pide partes iguales, salvo la ultima)

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "/index.html") {
      return new Response(PAGE, { headers: { "content-type": "text/html; charset=utf-8" } });
    }
    if (!url.pathname.startsWith("/api/")) return new Response("No existe", { status: 404 });
    if (!env.CLAVE || request.headers.get("x-clave") !== env.CLAVE) {
      return json({ error: "Clave incorrecta" }, 401);
    }
    try {
      const body = request.method === "POST" ? await request.json() : {};
      switch (url.pathname) {
        case "/api/videos": return json(await listVideos(env));
        case "/api/preparar": return json(await prepare(env, body.uid));
        case "/api/empezar": return json(await start(env, body));
        case "/api/parte": return json(await uploadPart(env, body));
        case "/api/terminar": return json(await complete(env, body));
        case "/api/cancelar": return json(await abort(env, body));
        case "/api/entero": return json(await copyWhole(env, body));
      }
      return json({ error: "No existe" }, 404);
    } catch (err) {
      return json({ error: String(err && err.message || err) }, 500);
    }
  },
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

async function cf(env, path, init = {}) {
  const r = await fetch(`https://api.cloudflare.com/client/v4/accounts/${env.ACCOUNT_ID}/stream${path}`, {
    ...init,
    headers: { authorization: `Bearer ${env.CF_API_TOKEN}`, "content-type": "application/json" },
  });
  const data = await r.json();
  if (!data.success) throw new Error("Cloudflare Stream: " + JSON.stringify(data.errors || data));
  return data.result;
}

// Nombre del archivo en R2: fecha y nombre de la grabacion
function keyFor(v) {
  const day = (v.created || "").slice(0, 16).replace("T", " ").replace(":", "-");
  const name = ((v.meta && v.meta.name) || v.uid).replace(/[\\/:*?"<>|]+/g, "").trim();
  return `stream/${day} ${name} [${v.uid.slice(0, 8)}].mp4`;
}

async function listVideos(env) {
  const vids = await cf(env, "?asc=false&limit=200");
  // lo que ya esta en R2 (una sola consulta)
  const have = {};
  let cursor;
  do {
    const l = await env.BUCKET.list({ prefix: "stream/", cursor });
    for (const o of l.objects) have[o.key] = o;
    cursor = l.truncated ? l.cursor : undefined;
  } while (cursor);
  const out = [];
  for (const v of vids) {
    const key = keyFor(v);
    const head = have[key];
    out.push({
      uid: v.uid, name: (v.meta && v.meta.name) || "", created: v.created, duration: v.duration,
      state: v.status && v.status.state, live: !!v.liveInput, key,
      enR2: head ? { size: head.size, uploaded: head.uploaded } : null,
    });
  }
  return { videos: out };
}

// Pide a Stream que arme el MP4 descargable y devuelve el estado
async function prepare(env, uid) {
  let r = await cf(env, `/${uid}/downloads`);
  if (!r || !r.default) r = await cf(env, `/${uid}/downloads`, { method: "POST" });
  const d = r.default || {};
  return { status: d.status, percent: d.percentComplete || 0, url: d.url };
}

async function downloadUrl(env, uid) {
  const d = await prepare(env, uid);
  if (d.status !== "ready") throw new Error("El MP4 todavía no está listo en Stream");
  return d.url;
}

async function start(env, { uid, key }) {
  const src = await downloadUrl(env, uid);
  const h = await fetch(src, { method: "HEAD" });
  const size = Number(h.headers.get("content-length") || 0);
  if (!size) throw new Error("No pude saber el tamaño del video");
  const up = await env.BUCKET.createMultipartUpload(key, { httpMetadata: { contentType: "video/mp4" } });
  return { key, uploadId: up.uploadId, size, partSize: PART, parts: Math.ceil(size / PART) };
}

async function uploadPart(env, { uid, key, uploadId, n, size }) {
  const src = await downloadUrl(env, uid);
  const a = (n - 1) * PART;
  const b = Math.min(size, n * PART) - 1;
  const r = await fetch(src, { headers: { range: `bytes=${a}-${b}` } });
  if (r.status !== 206) {
    r.body && r.body.cancel();
    return { sinRango: true };
  }
  const len = b - a + 1;
  const { readable, writable } = new FixedLengthStream(len);
  r.body.pipeTo(writable);
  const up = env.BUCKET.resumeMultipartUpload(key, uploadId);
  const part = await up.uploadPart(n, readable);
  return { partNumber: part.partNumber, etag: part.etag };
}

async function complete(env, { key, uploadId, parts }) {
  const up = env.BUCKET.resumeMultipartUpload(key, uploadId);
  const obj = await up.complete(parts);
  return { ok: true, key, size: obj.size };
}

async function abort(env, { key, uploadId }) {
  await env.BUCKET.resumeMultipartUpload(key, uploadId).abort();
  return { ok: true };
}

// Si Stream no deja pedir pedazos: se copia de una vez (hasta 5 GB)
async function copyWhole(env, { uid, key }) {
  const src = await downloadUrl(env, uid);
  const r = await fetch(src);
  const size = Number(r.headers.get("content-length") || 0);
  if (!size) throw new Error("No pude saber el tamaño del video");
  const { readable, writable } = new FixedLengthStream(size);
  r.body.pipeTo(writable);
  const obj = await env.BUCKET.put(key, readable, { httpMetadata: { contentType: "video/mp4" } });
  return { ok: true, key, size: obj.size };
}

const PAGE = `<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Respaldo a R2</title>
<style>
:root{--bg:#0f141c;--panel:#161d28;--line:#283244;--ink:#e6ebf2;--muted:#8b97a8;--ok:#22c55e;--warn:#f59e0b;--bad:#f87171;--acc:#3b82f6}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0 4px}.muted{color:var(--muted)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input{background:#0b1018;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:7px 9px}
button{background:#1f2937;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:6px 12px;cursor:pointer}
button.p{background:var(--acc);border-color:var(--acc)}button:disabled{opacity:.5;cursor:default}
table{width:100%;border-collapse:collapse;margin-top:12px}td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
.bar{height:6px;background:#0b1018;border-radius:3px;overflow:hidden;width:140px}.bar div{height:100%;background:var(--acc);width:0}
.ok{color:var(--ok)}.bad{color:var(--bad)}
</style></head><body><main>
<h1>Respaldo a R2</h1>
<div class="muted">Copia las grabaciones de Cloudflare Stream a R2 dentro de Cloudflare: no pasa por tu PC ni usa tu internet. Podés cerrar la página solo cuando terminen las copias.</div>
<div class="row" style="margin-top:12px"><input id="clave" type="password" placeholder="Clave"><button class="p" id="entrar">Ver grabaciones</button>
<button id="todos" disabled>Copiar todas las que faltan</button><span id="msg" class="muted"></span></div>
<table><thead><tr><th>Grabación</th><th>Duración</th><th>En R2</th><th></th></tr></thead><tbody id="lista"></tbody></table>
</main><script>
const $=s=>document.querySelector(s);let videos=[];
try{$('#clave').value=localStorage.getItem('clave')||''}catch(e){}
async function api(p,b){const r=await fetch('/api/'+p,{method:b?'POST':'GET',headers:{'x-clave':$('#clave').value,'content-type':'application/json'},body:b?JSON.stringify(b):undefined});
const d=await r.json();if(!r.ok||d.error)throw new Error(d.error||r.status);return d}
const fmtD=s=>s>0?Math.floor(s/3600)+':'+String(Math.floor(s%3600/60)).padStart(2,'0')+' h':'—';
const fmtB=b=>(b/1073741824).toFixed(2)+' GB';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function cargar(){$('#msg').textContent='Cargando…';
try{localStorage.setItem('clave',$('#clave').value)}catch(e){}
try{const d=await api('videos');videos=d.videos;pintar();$('#msg').textContent=videos.length+' grabaciones';$('#todos').disabled=false}
catch(e){$('#msg').textContent='Error: '+e.message}}
function pintar(){$('#lista').innerHTML=videos.map((v,i)=>'<tr><td><div>'+esc(v.name||v.uid)+'</div><div class="muted">'+esc((v.created||'').slice(0,16).replace('T',' '))+(v.live?' · grabación en vivo':'')+(v.state&&v.state!=='ready'?' · '+esc(v.state):'')+'</div></td><td>'+fmtD(v.duration)+'</td><td id="st'+i+'">'+(v.enR2?'<span class="ok">sí · '+fmtB(v.enR2.size)+'</span>':'<span class="muted">no</span>')+'</td><td><button data-i="'+i+'"'+(v.state!=='ready'?' disabled':'')+'>'+(v.enR2?'Copiar de nuevo':'Copiar a R2')+'</button></td></tr>').join('');
document.querySelectorAll('[data-i]').forEach(b=>b.onclick=()=>copiar(+b.dataset.i,b))}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function copiar(i,btn){const v=videos[i],st=$('#st'+i);if(btn)btn.disabled=true;
const show=(t,p)=>st.innerHTML=esc(t)+(p!=null?'<div class="bar"><div style="width:'+p+'%"></div></div>':'');
try{for(;;){const d=await api('preparar',{uid:v.uid});if(d.status==='ready')break;show('Stream arma el MP4… '+Math.round(d.percent||0)+'%',d.percent||0);await sleep(5000)}
const s=await api('empezar',{uid:v.uid,key:v.key});const parts=[];
for(let n=1;n<=s.parts;n++){show('Copiando '+n+'/'+s.parts+' ('+fmtB(s.size)+')',Math.round(100*(n-1)/s.parts));
let r;for(let t=0;t<4;t++){try{r=await api('parte',{uid:v.uid,key:s.key,uploadId:s.uploadId,n,size:s.size});break}catch(e){if(t===3)throw e;await sleep(3000)}}
if(r.sinRango){await api('cancelar',{key:s.key,uploadId:s.uploadId});show('Copiando de una vez…',null);const w=await api('entero',{uid:v.uid,key:v.key});v.enR2={size:w.size};st.innerHTML='<span class="ok">sí · '+fmtB(w.size)+'</span>';return}
parts.push({partNumber:r.partNumber,etag:r.etag})}
const c=await api('terminar',{key:s.key,uploadId:s.uploadId,parts});v.enR2={size:c.size};st.innerHTML='<span class="ok">sí · '+fmtB(c.size)+'</span>'}
catch(e){st.innerHTML='<span class="bad">error: '+esc(e.message)+'</span>';if(btn)btn.disabled=false}}
$('#entrar').onclick=cargar;
$('#todos').onclick=async()=>{$('#todos').disabled=true;for(let i=0;i<videos.length;i++){if(!videos[i].enR2&&videos[i].state==='ready')await copiar(i)}$('#todos').disabled=false};
if($('#clave').value)cargar();
</script></body></html>`;
