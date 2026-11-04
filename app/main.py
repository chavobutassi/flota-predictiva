"""Aplicación web de la flota: tablero, salidas y retornos, lecturas e incidencias.

Las reglas del negocio viven en la base de datos (triggers y restricciones):
la aplicación solo muestra los datos, valida el formato y traduce los rechazos
de la base a mensajes comprensibles.
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg import errors as pgerr

from . import config, db, seguridad

log = logging.getLogger("flota")
BASE = Path(__file__).parent
TZ = ZoneInfo(config.ZONA_HORARIA)
limite = seguridad.LimiteIntentos()

# Qué roles pueden hacer cada operación (ver docs/diseno.md).
ROLES_GESTION = {"ADMIN", "GESTOR"}
ROLES_OPERAN = {"ADMIN", "GESTOR", "CONDUCTOR"}
ROLES_LECTURA = {"ADMIN", "GESTOR", "CONDUCTOR", "TECNICO"}

MENSAJES_OK = {
    "salida": "Salida registrada.",
    "retorno": "Retorno registrado.",
    "lectura": "Lectura registrada.",
    "incidencia": "Incidencia registrada.",
    "clave": "Contraseña actualizada.",
}


@asynccontextmanager
async def ciclo_de_vida(_):
    db.abrir()
    yield
    db.cerrar()


app = FastAPI(lifespan=ciclo_de_vida, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
plantillas = Jinja2Templates(directory=BASE / "templates")


def fecha(valor, formato="%d/%m/%Y %H:%M"):
    if valor is None:
        return "—"
    if isinstance(valor, datetime):
        valor = valor.astimezone(TZ)
    return valor.strftime(formato)


def numero(valor, decimales=0):
    if valor is None:
        return "—"
    texto = f"{valor:,.{decimales}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


plantillas.env.filters["fecha"] = fecha
plantillas.env.filters["numero"] = numero


# ---------------------------------------------------------------- seguridad HTTP

@app.middleware("http")
async def cabeceras(request: Request, call_next):
    respuesta = await call_next(request)
    respuesta.headers["Content-Security-Policy"] = (
        "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["X-Frame-Options"] = "DENY"
    respuesta.headers["Referrer-Policy"] = "same-origin"
    respuesta.headers["Cache-Control"] = "no-store"
    if config.PRODUCCION:
        respuesta.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return respuesta


class Redirigir(Exception):
    def __init__(self, destino):
        self.destino = destino


@app.exception_handler(Redirigir)
async def _redirigir(_, exc: Redirigir):
    return RedirectResponse(exc.destino, status_code=303)


class Prohibido(Exception):
    pass


@app.exception_handler(Prohibido)
async def _prohibido(request: Request, _):
    return pantalla(request, "error.html", {"titulo": "Sin permiso",
                    "texto": "Tu usuario no tiene permiso para esta operación."}, 403)


# ---------------------------------------------------------------- sesión y permisos

def sesion_actual(request: Request, permitir_cambio_clave=False) -> dict:
    """Usuario autenticado, con sus roles leídos en cada pedido (una baja rige al instante)."""
    ses = seguridad.leer_cookie_sesion(request.cookies.get(seguridad.COOKIE))
    if not ses:
        raise Redirigir("/login")
    fila = db.consultar_uno(
        """SELECT u.id, u.nombre, u.apellido, u.email, c.debe_cambiar,
                  COALESCE(array_agg(r.codigo) FILTER (WHERE r.codigo IS NOT NULL), '{}') AS roles
             FROM usuario u
             JOIN credencial_acceso c ON c.usuario_id = u.id
             LEFT JOIN usuario_rol ur ON ur.usuario_id = u.id
             LEFT JOIN rol r ON r.id = ur.rol_id
            WHERE u.id = %s AND u.activo
            GROUP BY u.id, c.debe_cambiar""", (ses["u"],))
    if not fila:
        raise Redirigir("/login")
    if fila["debe_cambiar"] and not permitir_cambio_clave:
        raise Redirigir("/clave")
    fila["roles"] = set(fila["roles"])
    fila["csrf"] = ses["csrf"]
    return fila


def exigir(usuario: dict, permitidos: set):
    if not (usuario["roles"] & permitidos):
        raise Prohibido()


async def formulario(request: Request, usuario: dict) -> dict:
    datos = dict(await request.form())
    if not seguridad.csrf_valido(usuario, datos.get("csrf")):
        raise Prohibido()
    return datos


def pantalla(request: Request, nombre: str, contexto: dict, estado: int = 200, usuario=None):
    contexto = {"request": request, "usuario": usuario, **contexto}
    return plantillas.TemplateResponse(request, nombre, contexto, status_code=estado)


# ---------------------------------------------------------------- validación de datos

def a_decimal(texto, campo, obligatorio=True, maximo=Decimal("9999999.9")):
    texto = (texto or "").strip().replace(",", ".")
    if not texto:
        if obligatorio:
            raise ValueError(f"Falta {campo}.")
        return None
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        raise ValueError(f"{campo.capitalize()} no es un número válido.") from None
    if not valor.is_finite() or valor < 0 or valor > maximo:
        raise ValueError(f"{campo.capitalize()} fuera de rango.")
    return valor.quantize(Decimal("0.1"))


def a_entero(texto, campo):
    try:
        return int((texto or "").strip())
    except ValueError:
        raise ValueError(f"Falta {campo}.") from None


def a_texto(texto, campo, largo, obligatorio=False):
    texto = (texto or "").strip()
    if not texto and obligatorio:
        raise ValueError(f"Falta {campo}.")
    if len(texto) > largo:
        raise ValueError(f"{campo.capitalize()}: máximo {largo} caracteres.")
    return texto or None


def a_fecha_local(texto):
    texto = (texto or "").strip()
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto).replace(tzinfo=TZ)
    except ValueError:
        raise ValueError("La fecha de retorno previsto no es válida.") from None


def traducir(exc: Exception) -> str:
    """Convierte un rechazo de la base en un mensaje para el usuario."""
    if isinstance(exc, pgerr.UniqueViolation):
        return "Ese registro ya existe o el vehículo ya tiene una salida en curso."
    if isinstance(exc, pgerr.CheckViolation):
        if exc.diag.constraint_name:                  # restricción de tabla: mensaje genérico
            return "Algún valor no cumple las reglas de la base (revisá fechas y kilómetros)."
        return exc.diag.message_primary               # regla de un trigger: ya está en castellano
    if isinstance(exc, pgerr.ForeignKeyViolation):
        return "Una de las opciones elegidas no es válida."
    log.exception("Error inesperado de base de datos")
    return "No se pudo guardar. Intentá de nuevo."


# ---------------------------------------------------------------- acceso

@app.get("/salud")
async def salud():
    return JSONResponse({"estado": "ok"})


@app.get("/login", response_class=HTMLResponse)
async def login_pantalla(request: Request):
    return pantalla(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request):
    datos = dict(await request.form())
    email = (datos.get("email") or "").strip().lower()[:254]
    clave = (datos.get("clave") or "")[:200]
    ip = request.client.host if request.client else "?"
    llave = f"{ip}|{email}"
    if limite.bloqueado(llave):
        return pantalla(request, "login.html",
                        {"error": "Demasiados intentos. Esperá 15 minutos."}, 429)
    fila = db.consultar_uno(
        """SELECT u.id, c.password_hash FROM usuario u
             LEFT JOIN credencial_acceso c ON c.usuario_id = u.id
            WHERE lower(u.email) = %s AND u.activo""", (email,))
    if not seguridad.verificar(clave, fila and fila["password_hash"]) or not fila:
        limite.fallo(llave)
        return pantalla(request, "login.html", {"error": "Correo o contraseña incorrectos."}, 401)
    limite.exito(llave)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(seguridad.COOKIE, seguridad.crear_cookie_sesion(fila["id"]),
                    max_age=config.SESION_HORAS * 3600, httponly=True,
                    secure=config.PRODUCCION, samesite="lax")
    return resp


@app.post("/salir")
async def salir(request: Request):
    usuario = sesion_actual(request, permitir_cambio_clave=True)
    await formulario(request, usuario)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(seguridad.COOKIE)
    return resp


@app.get("/clave", response_class=HTMLResponse)
async def clave_pantalla(request: Request):
    usuario = sesion_actual(request, permitir_cambio_clave=True)
    return pantalla(request, "clave.html", {"error": None}, usuario=usuario)


@app.post("/clave")
async def clave_guardar(request: Request):
    usuario = sesion_actual(request, permitir_cambio_clave=True)
    datos = await formulario(request, usuario)
    actual, nueva, repetida = (datos.get(k, "") for k in ("actual", "nueva", "repetida"))
    fila = db.consultar_uno("SELECT password_hash FROM credencial_acceso WHERE usuario_id = %s",
                            (usuario["id"],))
    error = None
    if not seguridad.verificar(actual, fila["password_hash"]):
        error = "La contraseña actual no es correcta."
    elif nueva != repetida:
        error = "Las contraseñas nuevas no coinciden."
    elif nueva == actual:
        error = "La contraseña nueva debe ser distinta de la actual."
    else:
        error = seguridad.validar_clave_nueva(nueva)
    if error:
        return pantalla(request, "clave.html", {"error": error}, 400, usuario)
    with db.conexion() as con:
        con.execute("""UPDATE credencial_acceso SET password_hash = %s, debe_cambiar = FALSE,
                              actualizada_en = now() WHERE usuario_id = %s""",
                    (seguridad.hashear(nueva), usuario["id"]))
    return RedirectResponse("/?ok=clave", status_code=303)


# ---------------------------------------------------------------- tablero

CONSULTA_TABLERO = """
SELECT v.id, v.identificador, tv.nombre AS tipo, v.marca, v.modelo, v.anio,
       ev.codigo AS estado_codigo, ev.nombre AS estado, ub.nombre AS ubicacion,
       v.odometro_km, ul.dias_sin_lectura,
       a.id AS asignacion_id, r.nombre || ' ' || r.apellido AS responsable, a.retorno_previsto,
       (a.retorno_previsto < now()) AS retorno_vencido,
       COALESCE(s.vencidos, 0) AS vencidos, s.proxima_tarea, s.proxima_fecha
  FROM vehiculo v
  JOIN tipo_vehiculo tv ON tv.id = v.tipo_vehiculo_id
  JOIN estado_vehiculo ev ON ev.id = v.estado_id
  LEFT JOIN ubicacion ub ON ub.id = v.ubicacion_id
  LEFT JOIN v_ultima_lectura ul ON ul.vehiculo_id = v.id
  LEFT JOIN asignacion a ON a.vehiculo_id = v.id AND a.retorno_real IS NULL AND a.anulada_en IS NULL
  LEFT JOIN usuario r ON r.id = a.responsable_id
  LEFT JOIN (
        SELECT vehiculo_id,
               count(*) FILTER (WHERE km_restantes < 0 OR dias_restantes < 0 OR horas_restantes < 0) AS vencidos,
               (array_agg(tarea ORDER BY fecha_estimada NULLS LAST))[1] AS proxima_tarea,
               min(fecha_estimada) AS proxima_fecha
          FROM v_proximo_servicio GROUP BY vehiculo_id) s ON s.vehiculo_id = v.id
 WHERE (%(estado)s = '' AND ev.codigo <> 'BAJA' OR ev.codigo = %(estado)s)
   AND (%(q)s = '' OR v.identificador ILIKE '%%' || %(q)s || '%%'
        OR v.marca ILIKE '%%' || %(q)s || '%%' OR v.modelo ILIKE '%%' || %(q)s || '%%')
   AND (NOT %(solo_vencidos)s OR COALESCE(s.vencidos, 0) > 0)
 ORDER BY (a.retorno_previsto < now()) DESC NULLS LAST, COALESCE(s.vencidos, 0) DESC, v.identificador
"""


@app.get("/", response_class=HTMLResponse)
async def tablero(request: Request, estado: str = "", q: str = "", vencidos: str = "", ok: str = ""):
    usuario = sesion_actual(request)
    exigir(usuario, ROLES_LECTURA)
    q = q.strip()[:50]
    estado = estado.strip().upper()[:30]
    vehiculos = db.consultar(CONSULTA_TABLERO, {"estado": estado, "q": q,
                                                "solo_vencidos": vencidos == "1"})
    resumen = db.consultar_uno(
        """SELECT count(*) FILTER (WHERE e.codigo = 'DISPONIBLE')         AS disponibles,
                  count(*) FILTER (WHERE e.codigo = 'ASIGNADO')           AS asignados,
                  count(*) FILTER (WHERE e.codigo IN ('EN_MANTENIMIENTO','FUERA_DE_SERVICIO')) AS en_taller,
                  (SELECT count(DISTINCT vehiculo_id) FROM v_proximo_servicio
                    WHERE km_restantes < 0 OR dias_restantes < 0 OR horas_restantes < 0) AS con_vencidos,
                  (SELECT count(*) FROM asignacion WHERE retorno_real IS NULL AND anulada_en IS NULL
                      AND retorno_previsto < now()) AS retornos_vencidos
             FROM vehiculo v JOIN estado_vehiculo e ON e.id = v.estado_id WHERE e.codigo <> 'BAJA'""")
    estados = db.consultar("SELECT codigo, nombre FROM estado_vehiculo ORDER BY id")
    return pantalla(request, "tablero.html", {
        "vehiculos": vehiculos, "resumen": resumen, "estados": estados,
        "f_estado": estado, "f_q": q, "f_vencidos": vencidos == "1",
        "ok": MENSAJES_OK.get(ok), "puede_operar": bool(usuario["roles"] & ROLES_OPERAN),
    }, usuario=usuario)


# ---------------------------------------------------------------- ficha de vehículo

def cargar_vehiculo(vehiculo_id: int):
    return db.consultar_uno(
        """SELECT v.*, tv.nombre AS tipo, ev.codigo AS estado_codigo, ev.nombre AS estado,
                  ub.nombre AS ubicacion
             FROM vehiculo v JOIN tipo_vehiculo tv ON tv.id = v.tipo_vehiculo_id
             JOIN estado_vehiculo ev ON ev.id = v.estado_id
             LEFT JOIN ubicacion ub ON ub.id = v.ubicacion_id
            WHERE v.id = %s""", (vehiculo_id,))


def asignacion_en_curso(vehiculo_id: int):
    return db.consultar_uno(
        """SELECT a.*, r.nombre || ' ' || r.apellido AS responsable
             FROM asignacion a JOIN usuario r ON r.id = a.responsable_id
            WHERE a.vehiculo_id = %s AND a.retorno_real IS NULL AND a.anulada_en IS NULL""",
        (vehiculo_id,))


@app.get("/vehiculos/{vehiculo_id}", response_class=HTMLResponse)
async def ficha(request: Request, vehiculo_id: int, ok: str = ""):
    usuario = sesion_actual(request)
    exigir(usuario, ROLES_LECTURA)
    v = cargar_vehiculo(vehiculo_id)
    if not v:
        return pantalla(request, "error.html", {"titulo": "No encontrado",
                        "texto": "Ese vehículo no existe."}, 404, usuario)
    return pantalla(request, "vehiculo.html", {
        "v": v,
        "en_curso": asignacion_en_curso(vehiculo_id),
        "servicios": db.consultar(
            """SELECT tarea, km_restantes, dias_restantes, horas_restantes, fecha_estimada
                 FROM v_proximo_servicio WHERE vehiculo_id = %s
                ORDER BY fecha_estimada NULLS LAST, tarea""", (vehiculo_id,)),
        "asignaciones": db.consultar(
            """SELECT a.salida_real, a.retorno_real, a.estado, a.odometro_salida_km,
                      a.odometro_retorno_km, a.motivo, r.nombre || ' ' || r.apellido AS responsable
                 FROM asignacion a JOIN usuario r ON r.id = a.responsable_id
                WHERE a.vehiculo_id = %s ORDER BY a.salida_real DESC LIMIT 15""", (vehiculo_id,)),
        "lecturas": db.consultar(
            """SELECT leida_en, odometro_km, horas_motor, fuente FROM lectura_uso
                WHERE vehiculo_id = %s ORDER BY leida_en DESC LIMIT 15""", (vehiculo_id,)),
        "incidencias": db.consultar(
            """SELECT ocurrida_en, tipo, severidad, sistema, descripcion, resuelta_en
                 FROM incidencia WHERE vehiculo_id = %s ORDER BY ocurrida_en DESC LIMIT 10""",
            (vehiculo_id,)),
        "ordenes": db.consultar(
            """SELECT o.abierta_en, o.cerrada_en, t.nombre AS tipo, e.nombre AS estado,
                      o.descripcion, o.costo_total
                 FROM orden_mantenimiento o JOIN tipo_mantenimiento t ON t.id = o.tipo_id
                 JOIN estado_orden e ON e.id = o.estado_id
                WHERE o.vehiculo_id = %s ORDER BY o.abierta_en DESC LIMIT 10""", (vehiculo_id,)),
        "ok": MENSAJES_OK.get(ok), "puede_operar": bool(usuario["roles"] & ROLES_OPERAN),
    }, usuario=usuario)


# ---------------------------------------------------------------- salida

def contexto_salida(vehiculo_id, usuario):
    return {
        "v": cargar_vehiculo(vehiculo_id),
        "responsables": db.consultar(
            "SELECT id, nombre || ' ' || apellido AS nombre FROM usuario WHERE activo ORDER BY apellido, nombre"),
        "ubicaciones": db.consultar("SELECT id, nombre FROM ubicacion WHERE activa ORDER BY nombre"),
        "gestiona": bool(usuario["roles"] & ROLES_GESTION),
    }


@app.get("/vehiculos/{vehiculo_id}/salida", response_class=HTMLResponse)
async def salida_pantalla(request: Request, vehiculo_id: int):
    usuario = sesion_actual(request)
    exigir(usuario, ROLES_OPERAN)
    ctx = contexto_salida(vehiculo_id, usuario)
    if not ctx["v"]:
        raise Redirigir("/")
    return pantalla(request, "salida.html", {**ctx, "error": None, "d": {}}, usuario=usuario)


@app.post("/vehiculos/{vehiculo_id}/salida")
async def salida_guardar(request: Request, vehiculo_id: int):
    usuario = sesion_actual(request)
    exigir(usuario, ROLES_OPERAN)
    datos = await formulario(request, usuario)
    ctx = contexto_salida(vehiculo_id, usuario)
    if not ctx["v"]:
        raise Redirigir("/")
    try:
        odometro = a_decimal(datos.get("odometro"), "el odómetro")
        ubicacion = a_entero(datos.get("ubicacion"), "la ubicación de salida")
        responsable = (a_entero(datos.get("responsable"), "el responsable")
                       if usuario["roles"] & ROLES_GESTION else usuario["id"])
        motivo = a_texto(datos.get("motivo"), "el motivo", 200, obligatorio=True)
        previsto = a_fecha_local(datos.get("retorno_previsto"))
        obs = a_texto(datos.get("observaciones"), "las observaciones", 500)
        with db.conexion() as con:
            con.execute(
                """INSERT INTO asignacion (vehiculo_id, responsable_id, ubicacion_salida_id, motivo,
                                           retorno_previsto, odometro_salida_km, observaciones)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (vehiculo_id, responsable, ubicacion, motivo, previsto, odometro, obs))
    except ValueError as exc:
        return pantalla(request, "salida.html", {**ctx, "error": str(exc), "d": datos}, 400, usuario)
    except pgerr.Error as exc:
        return pantalla(request, "salida.html", {**ctx, "error": traducir(exc), "d": datos}, 400, usuario)
    return RedirectResponse(f"/vehiculos/{vehiculo_id}?ok=salida", status_code=303)


# ---------------------------------------------------------------- retorno

def contexto_retorno(asignacion_id):
    a = db.consultar_uno(
        """SELECT a.*, v.identificador, v.odometro_km AS odometro_actual,
                  r.nombre || ' ' || r.apellido AS responsable
             FROM asignacion a JOIN vehiculo v ON v.id = a.vehiculo_id
             JOIN usuario r ON r.id = a.responsable_id WHERE a.id = %s""", (asignacion_id,))
    return {"a": a, "ubicaciones": db.consultar(
        "SELECT id, nombre FROM ubicacion WHERE activa ORDER BY nombre")}


def puede_cerrar(usuario, a):
    return bool(usuario["roles"] & ROLES_GESTION) or (
        bool(usuario["roles"] & ROLES_OPERAN) and a["responsable_id"] == usuario["id"])


@app.get("/asignaciones/{asignacion_id}/retorno", response_class=HTMLResponse)
async def retorno_pantalla(request: Request, asignacion_id: int):
    usuario = sesion_actual(request)
    ctx = contexto_retorno(asignacion_id)
    if not ctx["a"] or ctx["a"]["estado"] != "EN_CURSO":
        raise Redirigir("/")
    if not puede_cerrar(usuario, ctx["a"]):
        raise Prohibido()
    return pantalla(request, "retorno.html", {**ctx, "error": None, "d": {}}, usuario=usuario)


@app.post("/asignaciones/{asignacion_id}/retorno")
async def retorno_guardar(request: Request, asignacion_id: int):
    usuario = sesion_actual(request)
    datos = await formulario(request, usuario)
    ctx = contexto_retorno(asignacion_id)
    if not ctx["a"] or ctx["a"]["estado"] != "EN_CURSO":
        raise Redirigir("/")
    if not puede_cerrar(usuario, ctx["a"]):
        raise Prohibido()
    try:
        odometro = a_decimal(datos.get("odometro"), "el odómetro de retorno")
        if odometro < ctx["a"]["odometro_salida_km"]:
            raise ValueError(
                f"El odómetro de retorno ({numero(odometro, 1)}) no puede ser menor al de salida "
                f"({numero(ctx['a']['odometro_salida_km'], 1)}).")
        ubicacion = a_entero(datos.get("ubicacion"), "la ubicación de retorno")
        obs = a_texto(datos.get("observaciones"), "las observaciones", 500)
        with db.conexion() as con:
            cur = con.execute(
                """UPDATE asignacion
                      SET retorno_real = now(), odometro_retorno_km = %s, ubicacion_retorno_id = %s,
                          observaciones = COALESCE(%s, observaciones)
                    WHERE id = %s AND retorno_real IS NULL AND anulada_en IS NULL""",
                (odometro, ubicacion, obs, asignacion_id))
            if cur.rowcount != 1:
                raise ValueError("Esta salida ya fue cerrada.")
    except ValueError as exc:
        return pantalla(request, "retorno.html", {**ctx, "error": str(exc), "d": datos}, 400, usuario)
    except pgerr.Error as exc:
        return pantalla(request, "retorno.html", {**ctx, "error": traducir(exc), "d": datos}, 400, usuario)
    return RedirectResponse(f"/vehiculos/{ctx['a']['vehiculo_id']}?ok=retorno", status_code=303)


# ---------------------------------------------------------------- lectura suelta

@app.get("/vehiculos/{vehiculo_id}/lectura", response_class=HTMLResponse)
async def lectura_pantalla(request: Request, vehiculo_id: int):
    usuario = sesion_actual(request)
    exigir(usuario, ROLES_LECTURA)
    v = cargar_vehiculo(vehiculo_id)
    if not v:
        raise Redirigir("/")
    return pantalla(request, "lectura.html", {"v": v, "error": None, "d": {}}, usuario=usuario)


@app.post("/vehiculos/{vehiculo_id}/lectura")
async def lectura_guardar(request: Request, vehiculo_id: int):
    usuario = sesion_actual(request)
    exigir(usuario, ROLES_LECTURA)
    datos = await formulario(request, usuario)
    v = cargar_vehiculo(vehiculo_id)
    if not v:
        raise Redirigir("/")
    try:
        odometro = a_decimal(datos.get("odometro"), "el odómetro")
        horas = a_decimal(datos.get("horas"), "las horas de motor", obligatorio=False)
        with db.conexion() as con:
            con.execute(
                """INSERT INTO lectura_uso (vehiculo_id, leida_en, odometro_km, horas_motor, fuente)
                   VALUES (%s, now(), %s, %s, 'MANUAL')""", (vehiculo_id, odometro, horas))
    except ValueError as exc:
        return pantalla(request, "lectura.html", {"v": v, "error": str(exc), "d": datos}, 400, usuario)
    except pgerr.Error as exc:
        return pantalla(request, "lectura.html", {"v": v, "error": traducir(exc), "d": datos}, 400, usuario)
    return RedirectResponse(f"/vehiculos/{vehiculo_id}?ok=lectura", status_code=303)


# ---------------------------------------------------------------- incidencia

TIPOS_INCIDENCIA = [("FALLA", "Falla"), ("ACCIDENTE", "Accidente"), ("DANIO", "Daño"),
                    ("ROBO_PERDIDA", "Robo o pérdida"), ("OTRO", "Otro")]
SEVERIDADES = [("BAJA", "Baja"), ("MEDIA", "Media"), ("ALTA", "Alta"), ("CRITICA", "Crítica")]
SISTEMAS = ["motor", "frenos", "transmisión", "suspensión", "neumáticos", "eléctrico",
            "refrigeración", "carrocería", "otro"]


def contexto_incidencia(v):
    return {"v": v, "tipos": TIPOS_INCIDENCIA, "severidades": SEVERIDADES, "sistemas": SISTEMAS}


@app.get("/vehiculos/{vehiculo_id}/incidencia", response_class=HTMLResponse)
async def incidencia_pantalla(request: Request, vehiculo_id: int):
    usuario = sesion_actual(request)
    exigir(usuario, ROLES_LECTURA)
    v = cargar_vehiculo(vehiculo_id)
    if not v:
        raise Redirigir("/")
    return pantalla(request, "incidencia.html", {**contexto_incidencia(v), "error": None, "d": {}},
                    usuario=usuario)


@app.post("/vehiculos/{vehiculo_id}/incidencia")
async def incidencia_guardar(request: Request, vehiculo_id: int):
    usuario = sesion_actual(request)
    exigir(usuario, ROLES_LECTURA)
    datos = await formulario(request, usuario)
    v = cargar_vehiculo(vehiculo_id)
    if not v:
        raise Redirigir("/")
    ctx = contexto_incidencia(v)
    try:
        tipo = datos.get("tipo", "")
        severidad = datos.get("severidad", "")
        if tipo not in {t for t, _ in TIPOS_INCIDENCIA} or severidad not in {s for s, _ in SEVERIDADES}:
            raise ValueError("Elegí el tipo y la severidad.")
        sistema = datos.get("sistema") or None
        if sistema is not None and sistema not in SISTEMAS:
            raise ValueError("Sistema no válido.")
        descripcion = a_texto(datos.get("descripcion"), "la descripción", 500, obligatorio=True)
        odometro = a_decimal(datos.get("odometro"), "el odómetro", obligatorio=False)
        en_curso = asignacion_en_curso(vehiculo_id)
        with db.conexion() as con:
            con.execute(
                """INSERT INTO incidencia (vehiculo_id, asignacion_id, reportada_por, tipo, severidad,
                                           sistema, descripcion, ocurrida_en, odometro_km)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s)""",
                (vehiculo_id, en_curso["id"] if en_curso else None, usuario["id"], tipo, severidad,
                 sistema, descripcion, odometro))
    except ValueError as exc:
        return pantalla(request, "incidencia.html", {**ctx, "error": str(exc), "d": datos}, 400, usuario)
    except pgerr.Error as exc:
        return pantalla(request, "incidencia.html", {**ctx, "error": traducir(exc), "d": datos}, 400, usuario)
    return RedirectResponse(f"/vehiculos/{vehiculo_id}?ok=incidencia", status_code=303)
