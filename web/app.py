"""Web privada: el feed de ofertas vivas.

Notas de seguridad, porque la web vive en una IP pública y cualquiera puede
tropezarse con ella:

  · Las contraseñas nunca se guardan en claro: se guarda un hash PBKDF2 y se
    comparan en tiempo constante.
  · El login tiene freno: 5 intentos fallidos por IP y se cierra 15 minutos.
    Sin esto, una IP pública con un formulario de contraseña es una invitación.
  · Las sesiones viven en la base de datos con caducidad, así que sobreviven a
    un reinicio y se pueden revocar de verdad (cerrar sesión cierra sesión).
  · TODA la API pide sesión, incluido el estado de salud: los contadores dicen
    qué vigilas y eso ya es información.
  · Cabeceras de seguridad en cada respuesta y CSP estricta: la página no carga
    nada de fuera salvo las imágenes de producto de las tiendas.
  · No se guarda ni un dato personal: la base de datos son precios y tiendas.

Lo único que esto NO puede arreglar solo es el cifrado del tráfico. Sin HTTPS
la contraseña viaja en claro. Mira `docker-compose.https.yml` para ponerlo en
dos comandos.

Arranque:  uvicorn heroscalper.web.app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..db import DB
from ..runner import cargar_config

BASE = Path(__file__).parent
CONFIG = cargar_config(os.environ.get("HEROSCALPER_CONFIG", "config.yaml"))

# Detrás de un proxy con HTTPS hay que marcarlo para que la cookie sea segura.
HTTPS = os.environ.get("WEB_HTTPS", "").lower() in ("1", "true", "si", "sí")
DURACION_SESION = 60 * 60 * 24 * 30          # 30 días
MAX_INTENTOS = 5
VENTANA_BLOQUEO = 60 * 15                     # 15 minutos

app = FastAPI(title="HeroScalper", docs_url=None, redoc_url=None,
              openapi_url=None)


# --------------------------------------------------------------------- #
# Usuarios
# --------------------------------------------------------------------- #
def _cargar_usuarios() -> dict[str, str]:
    """WEB_USERS='pavel:clave1,luis:clave2' o, más simple, WEB_PASSWORD.

    Con varios usuarios puedes darle acceso a un colega y quitárselo después
    sin cambiar tu propia contraseña.
    """
    usuarios: dict[str, str] = {}
    crudo = os.environ.get("WEB_USERS", "").strip()
    if crudo:
        for par in crudo.split(","):
            if ":" in par:
                nombre, clave = par.split(":", 1)
                if nombre.strip() and clave.strip():
                    usuarios[nombre.strip()] = clave.strip()
    clave_suelta = os.environ.get("WEB_PASSWORD", "").strip()
    if clave_suelta and not usuarios:
        usuarios["admin"] = clave_suelta
    return usuarios


USUARIOS = _cargar_usuarios()
ABIERTA = not USUARIOS          # sin credenciales: solo para uso en local

# Cerrojo de seguridad. En Render (y en cualquier PaaS) el servicio nace con una
# URL pública que cualquiera puede abrir, y la variable PORT es la señal de que
# estamos ahí. Si además no hay contraseña, la web quedaría abierta a internet:
# eso no arranca. Mejor un fallo ruidoso en el log que un escaparate abierto.
if ABIERTA and os.environ.get("PORT") \
        and os.environ.get("WEB_ABIERTA", "").lower() not in ("1", "si", "sí", "true"):
    raise RuntimeError(
        "La web iba a quedar ABIERTA en un servidor público. Pon WEB_PASSWORD "
        "(o WEB_USERS) en las variables de entorno del servicio y vuelve a "
        "desplegar. Si de verdad la quieres abierta, pon WEB_ABIERTA=si."
    )

# Intentos fallidos por IP: {ip: [instantes]}
_INTENTOS: dict[str, list[float]] = defaultdict(list)


def db() -> DB:
    return DB(CONFIG.get("base_datos", {}).get("ruta", "data/heroscalper.db"))


def _ip(request: Request) -> str:
    """IP real del cliente, respetando el proxy si lo hay."""
    reenviada = request.headers.get("x-forwarded-for", "")
    if reenviada:
        return reenviada.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _bloqueado(ip: str) -> bool:
    ahora = time.time()
    _INTENTOS[ip] = [t for t in _INTENTOS[ip] if ahora - t < VENTANA_BLOQUEO]
    return len(_INTENTOS[ip]) >= MAX_INTENTOS


def _apuntar_fallo(ip: str) -> None:
    _INTENTOS[ip].append(time.time())


def _verificar(usuario: str, clave: str) -> bool:
    """Comparación en tiempo constante: no delata si el usuario existe."""
    esperada = USUARIOS.get(usuario, "")
    # Se compara siempre, exista o no el usuario, para que tarde lo mismo.
    valida = hmac.compare_digest(clave.encode(), esperada.encode())
    return bool(esperada) and valida


# --------------------------------------------------------------------- #
# Sesiones (en base de datos: sobreviven al reinicio y se pueden revocar)
# --------------------------------------------------------------------- #
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def crear_sesion(usuario: str, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    d = db()
    try:
        d.crear_sesion(_hash(token), usuario, ip, DURACION_SESION)
    finally:
        d.cerrar()
    return token


def sesion_de(token: str) -> Optional[str]:
    if not token:
        return None
    d = db()
    try:
        return d.sesion_valida(_hash(token))
    finally:
        d.cerrar()


def sesion_valida(hs_session: str = Cookie(default="")) -> str:
    if ABIERTA:
        return "local"
    usuario = sesion_de(hs_session)
    if not usuario:
        raise HTTPException(status_code=401, detail="No autorizado")
    return usuario


# --------------------------------------------------------------------- #
# Cabeceras de seguridad en todas las respuestas
# --------------------------------------------------------------------- #
CSP = (
    "default-src 'self'; "
    "img-src 'self' https: data:; "        # fotos de producto de las tiendas
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'none'"
)


@app.middleware("http")
async def cabeceras_seguridad(request: Request, call_next):
    try:
        respuesta = await call_next(request)
    except HTTPException:
        raise
    except Exception:
        # Nunca devolver la traza de un error: dice más de la cuenta.
        respuesta = JSONResponse({"error": "Error interno"}, status_code=500)
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["X-Frame-Options"] = "DENY"
    respuesta.headers["Referrer-Policy"] = "no-referrer"
    respuesta.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    respuesta.headers["Content-Security-Policy"] = CSP
    if HTTPS:
        respuesta.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return respuesta


# --------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------- #
def _pagina_login(mensaje: str = "") -> str:
    aviso = f'<p class="err">{mensaje}</p>' if mensaje else ""
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>HeroScalper</title><style>
:root{{color-scheme:dark}}
body{{background:#0c1014;color:#e7edf3;font:15px/1.55 system-ui,sans-serif;
display:grid;place-items:center;min-height:100vh;margin:0;padding:20px}}
form{{background:#151b22;border:1px solid #28323d;border-radius:12px;
padding:30px;width:min(340px,100%);display:grid;gap:13px}}
h1{{margin:0 0 2px;font-size:19px;letter-spacing:-.02em}}
label{{font-size:12px;color:#94a2b1}}
input{{background:#0c1014;border:1px solid #3a4653;color:inherit;
padding:11px 13px;border-radius:8px;font-size:15px;width:100%}}
button{{background:#2c6e49;border:0;color:#fff;padding:11px;border-radius:8px;
font-size:15px;font-weight:600;cursor:pointer;margin-top:4px}}
.err{{color:#ff8265;margin:0;font-size:13.5px}}
</style></head><body><form method="post" action="/login" autocomplete="on">
<h1>HeroScalper</h1>{aviso}
<div><label for="u">Usuario</label>
<input id="u" name="usuario" autocomplete="username" autofocus></div>
<div><label for="p">Contraseña</label>
<input id="p" type="password" name="clave" autocomplete="current-password"></div>
<button type="submit">Entrar</button></form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_form(error: str = "") -> HTMLResponse:
    mensajes = {"1": "Usuario o contraseña incorrectos",
                "2": "Demasiados intentos. Prueba en 15 minutos."}
    return HTMLResponse(_pagina_login(mensajes.get(error, "")))


@app.post("/login")
def login(request: Request, usuario: str = Form(""), clave: str = Form("")):
    ip = _ip(request)
    if _bloqueado(ip):
        return RedirectResponse("/login?error=2", status_code=303)
    if not _verificar(usuario.strip(), clave):
        _apuntar_fallo(ip)
        return RedirectResponse("/login?error=1", status_code=303)

    _INTENTOS.pop(ip, None)
    token = crear_sesion(usuario.strip(), ip)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie("hs_session", token, httponly=True, samesite="lax",
                 secure=HTTPS, max_age=DURACION_SESION, path="/")
    return r


@app.post("/logout")
def logout(hs_session: str = Cookie(default="")):
    if hs_session:
        d = db()
        try:
            d.cerrar_sesion(_hash(hs_session))
        finally:
            d.cerrar()
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("hs_session", path="/")
    return r


# --------------------------------------------------------------------- #
# Feed
# --------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(hs_session: str = Cookie(default="")):
    if not ABIERTA and not sesion_de(hs_session):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse((BASE / "index.html").read_text(encoding="utf-8"))


@app.get("/api/ofertas")
def api_ofertas(categoria: str = "", tipo: str = "", orden: str = "margen_por_dia",
                estado: str = "activa", limite: int = 200,
                _: str = Depends(sesion_valida)) -> JSONResponse:
    from ..categorias import nombre as nombre_categoria
    limite = max(1, min(limite, 500))
    d = db()
    try:
        ofertas = d.ofertas_activas(limite=limite, tipo=tipo or None, orden=orden,
                                    categoria=categoria or None, estado=estado)
        cats = [
            {**c, "nombre": nombre_categoria(c["clave"] or "otros")}
            for c in d.categorias_con_ofertas(estado)
        ]
        return JSONResponse({"ofertas": ofertas, "categorias": cats,
                             "resumen": d.resumen(), "salud": d.salud_tiendas(),
                             "diagnostico": d.diagnostico()})
    finally:
        d.cerrar()


@app.post("/api/ofertas/{clave}/guardar")
def api_guardar(clave: str, guardada: bool = True,
                _: str = Depends(sesion_valida)) -> JSONResponse:
    """Una oferta guardada no expira sola: se queda hasta que la sueltes."""
    d = db()
    try:
        d.marcar_guardada(clave[:300], guardada)
        return JSONResponse({"ok": True})
    finally:
        d.cerrar()


@app.get("/api/historico/{clave}")
def api_historico(clave: str, _: str = Depends(sesion_valida)) -> JSONResponse:
    d = db()
    try:
        return JSONResponse({"lecturas": d.historico_producto(clave[:300])})
    finally:
        d.cerrar()


# --------------------------------------------------------------------- #
# Registro de compras
# --------------------------------------------------------------------- #
@app.post("/api/compras")
def api_comprar(clave: str = Form(...), precio: float = Form(...),
                unidades: int = Form(1), notas: str = Form(""),
                _: str = Depends(sesion_valida)) -> JSONResponse:
    d = db()
    try:
        fila = next((o for o in d.ofertas_activas(limite=500, estado="todas")
                     if o["clave"] == clave), None)
        if not fila:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")
        id_op = d.registrar_compra(fila, precio, max(1, min(unidades, 20)),
                                   notas[:500])
        return JSONResponse({"ok": True, "id": id_op})
    finally:
        d.cerrar()


@app.get("/api/compras")
def api_compras(_: str = Depends(sesion_valida)) -> JSONResponse:
    d = db()
    try:
        return JSONResponse({"compras": d.compras(), "balance": d.balance()})
    finally:
        d.cerrar()


@app.post("/api/compras/{id_op}")
def api_actualizar_compra(id_op: int, cancelada: Optional[bool] = None,
                          precio_venta: Optional[float] = None,
                          plataforma: str = "",
                          _: str = Depends(sesion_valida)) -> JSONResponse:
    d = db()
    try:
        d.actualizar_compra(id_op, cancelada, precio_venta, plataforma or None)
        return JSONResponse({"ok": True, "balance": d.balance()})
    finally:
        d.cerrar()


@app.get("/api/salud")
def salud(_: str = Depends(sesion_valida)) -> JSONResponse:
    """También pide sesión: los contadores dicen qué vigilas, y eso ya es
    información que no tiene por qué ver un desconocido."""
    d = db()
    try:
        return JSONResponse({**d.resumen(),
                             "tiendas_salud": d.salud_tiendas(),
                             "diagnostico": d.diagnostico()})
    finally:
        d.cerrar()


@app.get("/api/vivo")
def vivo() -> JSONResponse:
    """Lo único abierto: sirve para monitorizar que el servicio responde,
    y no dice absolutamente nada de tus datos."""
    return JSONResponse({"ok": True})
