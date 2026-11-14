"use strict";
/* Interfaz del detector de Sigatoka: acceso, análisis (foto / cámara), revisión manual e historial. */

const REQ = { tipos: ["image/jpeg", "image/png", "image/webp"], maxMB: 25, ladoMin: 224, ladoCamara: 1024 };
const TITULOS = { sana: "Hoja sana", sigatoka: "Sigatoka negra", otro: "No es una hoja de banano" };
const ETIQUETAS = { sana: "Sana", sigatoka: "Sigatoka", otro: "No es hoja" };

const $ = (id) => document.getElementById(id);
const pct = (x) => `${(x * 100).toFixed(1)} %`;

/* ============================================================ API */
class ErrorSesion extends Error {}

async function api(ruta, { metodo = "GET", json, form } = {}) {
  const opciones = { method: metodo, headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" };
  if (json !== undefined) { opciones.headers["Content-Type"] = "application/json"; opciones.body = JSON.stringify(json); }
  if (form) opciones.body = form;
  let resp;
  try { resp = await fetch(ruta, opciones); }
  catch { throw new Error("Sin conexión con el servidor."); }
  const datos = await resp.json().catch(() => ({}));
  if (resp.status === 401 && !ruta.startsWith("/api/login")) { mostrarAcceso(); throw new ErrorSesion(datos.detail); }
  if (!resp.ok) {
    const d = datos.detail;
    throw new Error(Array.isArray(d) ? "Revise los datos ingresados." : (d || `Error ${resp.status}`));
  }
  return datos;
}

/* ============================================================ ACCESO */
const acceso = { vista: $("vista-acceso"), login: $("form-login"), registro: $("form-registro") };

function mostrarAcceso(formulario = "login") {
  detenerCamara();
  $("vista-app").hidden = true;
  acceso.vista.hidden = false;
  acceso.login.hidden = formulario !== "login";
  acceso.registro.hidden = formulario !== "registro";
  (formulario === "login" ? $("login-usuario") : $("reg-nombre")).focus();
}

function mostrarApp(usuario) {
  acceso.vista.hidden = true;
  $("vista-app").hidden = false;
  $("nombre-usuario").textContent = usuario.nombre;
  $("nombre-usuario").title = `@${usuario.usuario}`;
  activarPestana(ui.tabs[0]);
}

document.querySelectorAll("[data-ir]").forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  [acceso.login, acceso.registro].forEach((f) => { f.querySelector(".form-error").textContent = ""; });
  mostrarAcceso(a.dataset.ir);
}));

document.querySelectorAll(".ver-clave").forEach((b) => b.addEventListener("click", () => {
  const input = $(b.dataset.objetivo);
  const ver = input.type === "password";
  input.type = ver ? "text" : "password";
  b.textContent = ver ? "Ocultar" : "Ver";
  b.setAttribute("aria-label", ver ? "Ocultar contraseña" : "Mostrar contraseña");
}));

function fallo(mensaje, campo) { const e = new Error(mensaje); e.campo = campo; return e; }

async function enviarFormulario(form, accion) {
  const error = form.querySelector(".form-error");
  const boton = form.querySelector("button[type=submit]");
  form.querySelectorAll("input").forEach((i) => i.removeAttribute("aria-invalid"));
  error.textContent = "";
  try {
    accion.validar();
  } catch (e) {
    error.textContent = e.message;
    e.campo?.setAttribute("aria-invalid", "true"); e.campo?.focus();
    return;
  }
  const textoBoton = boton.textContent;
  boton.disabled = true; boton.textContent = "Un momento…";
  try {
    const usuario = await api(accion.ruta, { metodo: "POST", json: accion.datos() });
    form.reset();
    mostrarApp(usuario);
  } catch (e) {
    error.textContent = e.message;
  } finally {
    boton.disabled = false; boton.textContent = textoBoton;
  }
}

acceso.login.addEventListener("submit", (e) => {
  e.preventDefault();
  const u = $("login-usuario"), c = $("login-contrasena");
  enviarFormulario(acceso.login, {
    ruta: "/api/login",
    validar() {
      if (!u.value.trim()) throw fallo("Escriba su usuario.", u);
      if (!c.value) throw fallo("Escriba su contraseña.", c);
    },
    datos: () => ({ usuario: u.value.trim(), contrasena: c.value }),
  });
});

acceso.registro.addEventListener("submit", (e) => {
  e.preventDefault();
  const n = $("reg-nombre"), u = $("reg-usuario"), c = $("reg-contrasena"), c2 = $("reg-confirmar");
  enviarFormulario(acceso.registro, {
    ruta: "/api/registro",
    validar() {
      if (!n.value.trim()) throw fallo("Escriba su nombre.", n);
      if (!/^[A-Za-z0-9._-]{3,32}$/.test(u.value.trim()))
        throw fallo("El usuario debe tener de 3 a 32 caracteres: letras, números, punto, guion o guion bajo.", u);
      if (c.value.length < 8) throw fallo("La contraseña debe tener al menos 8 caracteres.", c);
      if (c.value !== c2.value) throw fallo("Las contraseñas no coinciden.", c2);
    },
    datos: () => ({ nombre: n.value.trim(), usuario: u.value.trim(), contrasena: c.value }),
  });
});

$("btn-salir").addEventListener("click", async () => {
  try { await api("/api/logout", { metodo: "POST" }); } catch { /* sin importancia */ }
  mostrarAcceso();
});

/* La eliminación de la cuenta es una Saga entre usuarios, historial y métricas: se sigue su estado. */
$("btn-eliminar-cuenta").addEventListener("click", async () => {
  if (!confirm("¿Eliminar su cuenta y todo su historial? Esta acción no se puede deshacer.")) return;
  try {
    const { saga_id } = await api("/api/cuenta", { metodo: "DELETE" });
    let s = { estado: "en_curso" };
    for (let i = 0; i < 20 && s.estado === "en_curso"; i++) {
      await new Promise((r) => setTimeout(r, 500));
      s = await api(`/api/cuenta/eliminacion/${saga_id}`);
    }
    alert(s.estado === "completada" ? "Su cuenta fue eliminada."
      : s.estado === "compensada" ? "No se pudo borrar el historial. Su cuenta sigue activa, inicie sesión de nuevo."
      : "La eliminación sigue en proceso.");
  } catch (e) { if (!(e instanceof ErrorSesion)) alert(e.message); }
  mostrarAcceso();
});

/* ============================================================ UI común */
const ui = {
  tabs: [$("tab-foto"), $("tab-camara"), $("tab-historial")],
  paneles: { "tab-foto": $("panel-foto"), "tab-camara": $("panel-camara"), "tab-historial": $("panel-historial") },
  res: $("resultado"), titulo: $("res-titulo"), barra: $("res-barra"), detalle: $("res-detalle"),
  advertencia: $("res-aviso"), tabla: $("res-tabla"), revision: $("revision"), revEstado: $("revision-estado"),
};
let prediccionActual = null;

function ocultarResultado() { ui.res.hidden = true; prediccionActual = null; }

function mostrarResultado(r) {
  prediccionActual = r;
  ui.res.hidden = false;
  ui.res.dataset.clase = r.clase;
  ui.titulo.textContent = TITULOS[r.clase] ?? r.diagnostico;
  if (r.es_hoja_banano) {
    ui.barra.style.width = `${(r.confianza * 100).toFixed(1)}%`;
    ui.detalle.textContent = `Confianza del modelo: ${pct(r.confianza)}`;
  } else {
    ui.barra.style.width = "0";
    ui.detalle.textContent = "Pruebe con otra foto, centrando la hoja y con buena luz.";
  }
  ui.advertencia.hidden = !r.advertencia;
  ui.advertencia.textContent = r.advertencia ? `⚠ ${r.advertencia}` : "";
  ui.revision.hidden = false;
  $("res-mas").hidden = false;
  marcarOpcion(null);
  ui.revEstado.textContent = "Indique la clase real para saber si la IA acertó.";
  const filas = [
    ["P(sigatoka | hoja)", pct(r.prob_sigatoka)], ["P(no es hoja)", pct(r.prob_no_hoja)],
    ["Resolución recibida", `${r.ancho} × ${r.alto} px`],
    ["Distancia OOD", `${r.distancia_ood} (límite ${r.umbral_ood_alto})`], ["Tiempo", `${r.ms} ms`],
  ];
  ui.tabla.replaceChildren(...filas.flatMap(([k, v]) => {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    return [dt, dd];
  }));
  ui.res.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function mostrarError(mensaje) {
  prediccionActual = null;
  ui.res.hidden = false;
  ui.res.dataset.clase = "error";
  ui.titulo.textContent = mensaje;
  ui.detalle.textContent = "";
  ui.advertencia.hidden = true;
  ui.revision.hidden = true;
  ui.tabla.replaceChildren();
  $("res-mas").hidden = true;
}

function marcarOpcion(clase) {
  document.querySelectorAll(".opcion").forEach((b) => b.setAttribute("aria-pressed", b.dataset.real === clase));
}

document.querySelectorAll(".opcion").forEach((b) => b.addEventListener("click", async () => {
  if (!prediccionActual) return;
  const real = b.dataset.real;
  marcarOpcion(real);
  ui.revEstado.textContent = "Guardando…";
  try {
    await api(`/api/predicciones/${prediccionActual.id}`, { metodo: "PATCH", json: { clase_real: real } });
    ui.revEstado.textContent = real === prediccionActual.clase
      ? "✓ Guardado: la IA acertó."
      : `✗ Guardado: la IA se equivocó (era ${ETIQUETAS[real]}).`;
  } catch (e) {
    if (!(e instanceof ErrorSesion)) ui.revEstado.textContent = e.message;
  }
}));

/* Guía de ejemplo: visible antes de analizar; se oculta con un buen resultado y reaparece
   resaltada si la IA no reconoció la hoja o hubo un error con la imagen. */
const MOTIVOS = {
  otro: "No se reconoció una hoja de banano. Revise la foto:",
  advertencia: "Imagen poco habitual. Para un mejor resultado:",
  error: "Revise la imagen:",
};
function guia(el, estado, motivo = "error") {   // estado: "normal" | "oculta" | "resaltada"
  const titulo = el.querySelector(".guia-titulo");
  titulo.dataset.original ??= titulo.textContent;
  el.hidden = estado === "oculta";
  el.classList.toggle("resaltada", estado === "resaltada");
  titulo.textContent = estado === "resaltada" ? MOTIVOS[motivo] : titulo.dataset.original;
}
function guiaSegunResultado(el, r) {
  if (r.clase === "otro") guia(el, "resaltada", "otro");
  else if (r.advertencia) guia(el, "resaltada", "advertencia");
  else guia(el, "oculta");
}

function analizar(blob, nombre, origen) {
  const form = new FormData();
  form.append("imagen", blob, nombre);
  form.append("origen", origen);
  return api("/api/predicciones", { metodo: "POST", form });
}

/* ============================================================ FOTO */
const guiaFoto = $("guia-foto"), guiaCamara = $("guia-camara");
const foto = { zona: $("zona"), archivo: $("archivo"), previa: $("vista-previa"), texto: $("zona-texto"),
               cargando: $("zona").querySelector(".cargando"), url: null, ocupado: false };

async function validarArchivo(archivo) {
  if (!REQ.tipos.includes(archivo.type)) throw new Error("Formato no admitido. Use JPG, PNG o WEBP.");
  if (archivo.size > REQ.maxMB * 1024 * 1024) throw new Error(`La imagen pesa más de ${REQ.maxMB} MB.`);
  let w = 0, h = 0;
  try {
    const bmp = await createImageBitmap(archivo);
    ({ width: w, height: h } = bmp); bmp.close?.();
  } catch { return; }                       // si el navegador no puede leerla, decide el servidor
  if (Math.min(w, h) < REQ.ladoMin)
    throw new Error(`La imagen es muy pequeña (${w}×${h} px). El mínimo es ${REQ.ladoMin}×${REQ.ladoMin} px.`);
}

async function procesarArchivo(archivo) {
  if (!archivo || foto.ocupado) return;
  try { await validarArchivo(archivo); } catch (e) { mostrarError(e.message); guia(guiaFoto, "resaltada"); return; }
  if (foto.url) URL.revokeObjectURL(foto.url);
  foto.url = URL.createObjectURL(archivo);
  foto.previa.src = foto.url; foto.previa.hidden = false;
  foto.zona.classList.add("con-imagen");
  foto.texto.querySelector("strong").textContent = "Cambiar foto";
  foto.ocupado = true; foto.cargando.hidden = false; ocultarResultado();
  try {
    const r = await analizar(archivo, archivo.name || "foto.jpg", "foto");
    mostrarResultado(r); guiaSegunResultado(guiaFoto, r);
  } catch (e) {
    if (!(e instanceof ErrorSesion)) { mostrarError(e.message); guia(guiaFoto, "resaltada"); }
  } finally {
    foto.ocupado = false; foto.cargando.hidden = true;
  }
}

foto.archivo.addEventListener("change", () => { procesarArchivo(foto.archivo.files[0]); foto.archivo.value = ""; });
["dragenter", "dragover"].forEach((ev) => foto.zona.addEventListener(ev, (e) => { e.preventDefault(); foto.zona.classList.add("arrastrando"); }));
["dragleave", "drop"].forEach((ev) => foto.zona.addEventListener(ev, (e) => { e.preventDefault(); foto.zona.classList.remove("arrastrando"); }));
foto.zona.addEventListener("drop", (e) => procesarArchivo(e.dataTransfer.files[0]));
document.addEventListener("paste", (e) => {
  if ($("vista-app").hidden) return;
  const item = [...(e.clipboardData?.items ?? [])].find((i) => i.type.startsWith("image/"));
  if (item) { activarPestana(ui.tabs[0]); procesarArchivo(item.getAsFile()); }
});

/* ============================================================ CÁMARA (tomar foto y analizar) */
const cam = { visor: $("visor"), video: $("video"), captura: $("captura"), marco: $("marco"), aviso: $("camara-aviso"),
              disparo: $("btn-disparo"), otra: $("btn-otra"), girar: $("btn-girar"),
              cargando: $("visor").querySelector(".cargando"), flujo: null, frontal: false };

/* El modelo analiza el CUADRADO CENTRAL (lado = mín(ancho, alto)). El marco dibuja exactamente esa zona
   y la foto que se envía es ese mismo recorte reescalado a 1024 px: así todas las cámaras envían
   una imagen del mismo tamaño y formato (JPEG 1024×1024), sin importar su resolución. */
function ubicarMarco() {
  const { videoWidth: w, videoHeight: h } = cam.video;
  if (!w || !h) return;
  const forma = `${w} / ${h}`;
  if (cam.visor.style.aspectRatio !== forma) cam.visor.style.aspectRatio = forma;
  const W = cam.visor.clientWidth, H = cam.visor.clientHeight;
  const escala = Math.min(W / w, H / h), dw = w * escala, dh = h * escala, lado = Math.min(dw, dh);
  Object.assign(cam.marco.style, { width: `${lado}px`, height: `${lado}px`,
    left: `${(W - dw) / 2 + (dw - lado) / 2}px`, top: `${(H - dh) / 2 + (dh - lado) / 2}px` });
}
new ResizeObserver(ubicarMarco).observe(cam.visor);
cam.video.addEventListener("loadedmetadata", ubicarMarco);
cam.video.addEventListener("resize", ubicarMarco);

function avisoCamara(texto) { cam.aviso.textContent = texto; cam.aviso.hidden = !texto; }

async function iniciarCamara() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    avisoCamara("El navegador solo permite la cámara en conexiones seguras (https). "
      + "Use la pestaña Foto, que también puede abrir la cámara del celular.");
    cam.disparo.disabled = true;
    return;
  }
  detenerCamara();
  try {
    cam.flujo = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: cam.frontal ? "user" : "environment", width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false });
  } catch (e) {
    avisoCamara(e.name === "NotAllowedError" ? "Permiso de cámara denegado. Actívelo en la configuración del navegador."
                                             : "No se encontró una cámara disponible.");
    cam.disparo.disabled = true;
    return;
  }
  avisoCamara("");
  cam.video.srcObject = cam.flujo;
  cam.visor.classList.add("activo");
  modoVista();
  const camaras = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
  cam.girar.hidden = camaras.length < 2;
}

function detenerCamara() {
  cam.flujo?.getTracks().forEach((t) => t.stop());
  cam.flujo = null;
  cam.video.srcObject = null;
  cam.visor.classList.remove("activo");
  cam.visor.style.aspectRatio = "";
}

function modoVista() {                        // vista en vivo, lista para tomar la foto
  cam.captura.hidden = true; cam.video.hidden = false;
  delete cam.visor.dataset.resultado;
  cam.disparo.hidden = false; cam.disparo.disabled = false; cam.otra.hidden = true;
  guia(guiaCamara, "normal");
}

async function tomarFoto() {
  const { videoWidth: w, videoHeight: h } = cam.video;
  if (!w || !h) return;
  const lado = Math.min(w, h);
  if (lado < REQ.ladoMin) { mostrarError(`La cámara tiene muy poca resolución (${w}×${h} px).`); return; }

  // 1) congelar lo que se ve (foto completa, con el marco encima)
  const fotograma = document.createElement("canvas");
  fotograma.width = w; fotograma.height = h;
  fotograma.getContext("2d").drawImage(cam.video, 0, 0);
  cam.captura.src = fotograma.toDataURL("image/jpeg", 0.85);
  cam.captura.hidden = false; cam.video.hidden = true;
  cam.visor.classList.remove("flash"); void cam.visor.offsetWidth; cam.visor.classList.add("flash");
  navigator.vibrate?.(30);

  // 2) recortar el cuadrado del marco y estandarizar a 1024×1024 JPEG
  const salida = Math.min(lado, REQ.ladoCamara);
  const recorte = document.createElement("canvas");
  recorte.width = recorte.height = salida;
  recorte.getContext("2d").drawImage(fotograma, (w - lado) / 2, (h - lado) / 2, lado, lado, 0, 0, salida, salida);
  const blob = await new Promise((r) => recorte.toBlob(r, "image/jpeg", 0.92));

  // 3) enviar con animación de carga y mostrar la predicción
  cam.disparo.disabled = true; cam.cargando.hidden = false; ocultarResultado();
  try {
    const r = await analizar(blob, "camara.jpg", "camara");
    cam.visor.dataset.resultado = r.clase;
    mostrarResultado(r); guiaSegunResultado(guiaCamara, r);
  } catch (e) {
    if (!(e instanceof ErrorSesion)) { mostrarError(e.message); guia(guiaCamara, "resaltada"); }
  } finally {
    cam.cargando.hidden = true;
    cam.disparo.hidden = true; cam.otra.hidden = false;
  }
}

cam.disparo.addEventListener("click", tomarFoto);
cam.otra.addEventListener("click", () => { modoVista(); ocultarResultado(); });
cam.girar.addEventListener("click", () => { cam.frontal = !cam.frontal; iniciarCamara(); });
window.addEventListener("pagehide", detenerCamara);
document.addEventListener("visibilitychange", () => {
  if (ui.paneles["tab-camara"].hidden || $("vista-app").hidden) return;
  if (document.hidden) detenerCamara();
  else if (cam.disparo.hidden === false) iniciarCamara();
});

/* ============================================================ HISTORIAL */
const hist = { lista: $("lista"), vacia: $("lista-vacia"), pendientes: $("solo-pendientes") };
const fechaLocal = new Intl.DateTimeFormat("es", { dateStyle: "short", timeStyle: "short" });

function pintarResumen(r) {
  if (!r) { $("r-pendientes").textContent = "Las métricas no están disponibles en este momento."; return; }
  $("r-total").textContent = r.total; $("r-revisadas").textContent = r.revisadas;
  $("r-aciertos").textContent = r.aciertos; $("r-errores").textContent = r.errores;
  $("r-pendientes").textContent = r.pendientes ? `Faltan ${r.pendientes} por revisar.` : "";
  $("m-vp").textContent = r.VP; $("m-fn").textContent = r.FN; $("m-fp").textContent = r.FP; $("m-vn").textContent = r.VN;
  const orden = ["sana", "sigatoka", "otro"];
  $("m3").replaceChildren(...orden.map((real) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th"); th.textContent = ETIQUETAS[real]; tr.append(th);
    orden.forEach((pred) => {
      const td = document.createElement("td"); td.textContent = r.matriz[real][pred];
      if (real === pred) td.className = "diag";
      tr.append(td);
    });
    return tr;
  }));
}

function etiqueta(clase) {
  const s = document.createElement("span"); s.className = `etq ${clase}`; s.textContent = ETIQUETAS[clase]; return s;
}

function filaPrediccion(p) {
  const tr = document.createElement("tr");
  const celda = (...hijos) => { const td = document.createElement("td"); td.append(...hijos); tr.append(td); return td; };

  const img = document.createElement("img");
  img.loading = "lazy"; img.alt = ""; img.src = `/api/predicciones/${p.id}/miniatura`;
  celda(img);

  const origen = document.createElement("span"); origen.className = "sub"; origen.textContent = p.origen === "camara" ? "Cámara" : "Foto";
  celda(fechaLocal.format(new Date(p.fecha)), origen);

  const conf = document.createElement("span"); conf.className = "sub"; conf.textContent = pct(p.confianza);
  celda(etiqueta(p.clase_predicha), conf);

  const sel = document.createElement("select");
  sel.setAttribute("aria-label", "Clase real");
  [["", "Sin revisar"], ["sana", "Sana"], ["sigatoka", "Sigatoka"], ["otro", "No es hoja"]].forEach(([v, t]) => {
    const o = document.createElement("option"); o.value = v; o.textContent = t; sel.append(o);
  });
  sel.value = p.clase_real ?? "";
  celda(sel);

  const marca = document.createElement("span");
  const pintarMarca = (real) => {
    const ok = real === p.clase_predicha;
    marca.className = "marca " + (!real ? "" : ok ? "ok" : "mal");
    marca.textContent = !real ? "" : ok ? "✓" : "✗";
    marca.title = !real ? "" : ok ? "Acierto" : "Error";
  };
  pintarMarca(p.clase_real);
  celda(marca);

  sel.addEventListener("change", async () => {
    try {
      const r = await api(`/api/predicciones/${p.id}`, { metodo: "PATCH", json: { clase_real: sel.value || null } });
      p.clase_real = sel.value || null; pintarMarca(p.clase_real); pintarResumen(r.resumen);
    } catch (e) {
      if (!(e instanceof ErrorSesion)) { alert(e.message); sel.value = p.clase_real ?? ""; }
    }
  });

  const borrar = document.createElement("button");
  borrar.className = "borrar"; borrar.type = "button"; borrar.textContent = "🗑"; borrar.setAttribute("aria-label", "Eliminar");
  borrar.addEventListener("click", async () => {
    if (!confirm("¿Eliminar esta predicción del historial?")) return;
    try {
      const r = await api(`/api/predicciones/${p.id}`, { metodo: "DELETE" });
      tr.remove(); pintarResumen(r.resumen);
      hist.vacia.hidden = hist.lista.children.length > 0;
    } catch (e) { if (!(e instanceof ErrorSesion)) alert(e.message); }
  });
  celda(borrar);
  return tr;
}

async function cargarHistorial() {
  try {
    const d = await api(`/api/predicciones?pendientes=${hist.pendientes.checked}`);
    pintarResumen(d.resumen);
    hist.lista.replaceChildren(...d.predicciones.map(filaPrediccion));
    hist.vacia.hidden = d.predicciones.length > 0;
  } catch (e) {
    if (!(e instanceof ErrorSesion)) { hist.vacia.hidden = false; hist.vacia.textContent = e.message; }
  }
}
hist.pendientes.addEventListener("change", cargarHistorial);

/* ============================================================ PESTAÑAS */
function activarPestana(tab) {
  ui.tabs.forEach((t) => {
    const activa = t === tab;
    t.setAttribute("aria-selected", activa); t.tabIndex = activa ? 0 : -1;
    ui.paneles[t.id].hidden = !activa;
  });
  ocultarResultado();
  guia(guiaFoto, "normal"); guia(guiaCamara, "normal");
  if (tab.id === "tab-camara") iniciarCamara(); else detenerCamara();
  if (tab.id === "tab-historial") cargarHistorial();
}

ui.tabs.forEach((t, i) => {
  t.addEventListener("click", () => activarPestana(t));
  t.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const sig = ui.tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + ui.tabs.length) % ui.tabs.length];
    sig.focus(); activarPestana(sig);
  });
});

/* ============================================================ INICIO */
(async () => {
  try { mostrarApp(await api("/api/yo")); }
  catch { if ($("vista-app").hidden) mostrarAcceso(); }
})();
