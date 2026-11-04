"""Prueba de punta a punta de la aplicación contra una base con los datos de demostración.

ATENCIÓN: escribe en la base (salidas, lecturas, incidencias, contraseñas de prueba).
Usar solo sobre una base de desarrollo:

    DATABASE_URL=postgresql://... SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))") \
        python3 tests/prueba_app.py
"""
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, seguridad  # noqa: E402
from app.main import app  # noqa: E402

# Contraseñas aleatorias en cada corrida: no hay claves conocidas en el repositorio.
CLAVE = "Aa1-" + secrets.token_urlsafe(14)
CLAVE_NUEVA = "Bb2-" + secrets.token_urlsafe(14)
USUARIOS = {
    "admin": "marcelo.ibanez@flota-demo.test",
    "gestor": "gabriela.sosa@flota-demo.test",
    "tecnico": "diego.pereyra@flota-demo.test",
    "conductor": "andrea.molina@flota-demo.test",
    "otro_conductor": "carlos.medina@flota-demo.test",
}
fallos = []


def verificar(condicion, texto):
    print(("  ok   " if condicion else "  FALLA ") + texto)
    if not condicion:
        fallos.append(texto)


def csrf_de(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def sql(consulta, params=None):
    with psycopg.connect(config.DATABASE_URL, row_factory=psycopg.rows.dict_row, autocommit=True) as con:
        cur = con.execute(consulta, params)
    return cur.fetchall() if cur.description else []


def cliente(rol, debe_cambiar=False):
    c = TestClient(app, follow_redirects=False)
    sql("""INSERT INTO credencial_acceso (usuario_id, password_hash, debe_cambiar)
           SELECT id, %s, %s FROM usuario WHERE email = %s
           ON CONFLICT (usuario_id) DO UPDATE SET password_hash = EXCLUDED.password_hash,
                                                  debe_cambiar = EXCLUDED.debe_cambiar""",
        (seguridad.hashear(CLAVE), debe_cambiar, USUARIOS[rol]))
    r = c.post("/login", data={"email": USUARIOS[rol], "clave": CLAVE})
    assert r.status_code == 303, (rol, r.status_code)
    return c


def token(c, ruta):
    return csrf_de(c.get(ruta).text)


with TestClient(app, follow_redirects=False) as anonimo:
    print("Acceso")
    verificar(anonimo.get("/").status_code == 303 and anonimo.get("/").headers["location"] == "/login",
              "sin sesión, el tablero redirige al login")
    verificar(anonimo.get("/vehiculos/1").status_code == 303, "sin sesión, la ficha redirige al login")
    verificar(anonimo.post("/vehiculos/1/salida", data={}).status_code == 303, "sin sesión, no se puede registrar una salida")
    anonimo.cookies.set(seguridad.COOKIE, "cookie-falsificada")
    verificar(anonimo.get("/").status_code == 303, "una cookie falsificada no da acceso")
    anonimo.cookies.clear()
    verificar(anonimo.get("/salud").json() == {"estado": "ok"}, "/salud responde sin datos")
    verificar(anonimo.get("/docs").status_code == 404, "no se expone la documentación de la API")
    r = anonimo.post("/login", data={"email": USUARIOS["admin"], "clave": "mala"})
    verificar(r.status_code == 401 and "incorrectos" in r.text, "contraseña incorrecta: 401 con mensaje genérico")
    r2 = anonimo.post("/login", data={"email": "no.existe@x.test", "clave": "mala"})
    verificar(r2.status_code == 401 and "incorrectos" in r2.text, "usuario inexistente: mismo mensaje que contraseña mala")
    for _ in range(5):
        anonimo.post("/login", data={"email": "bloqueo@x.test", "clave": "x"})
    verificar(anonimo.post("/login", data={"email": "bloqueo@x.test", "clave": "x"}).status_code == 429,
              "después de 5 intentos fallidos se bloquea")

    print("Cambio obligatorio de contraseña")
    c = cliente("admin", debe_cambiar=True)
    verificar(c.get("/").headers.get("location") == "/clave", "con contraseña temporal, todo lleva a /clave")
    t = token(c, "/clave")
    r = c.post("/clave", data={"csrf": t, "actual": CLAVE, "nueva": "corta1A", "repetida": "corta1A"})
    verificar(r.status_code == 400 and "12 caracteres" in r.text, "rechaza una contraseña corta")
    r = c.post("/clave", data={"csrf": t, "actual": CLAVE, "nueva": CLAVE, "repetida": CLAVE})
    verificar(r.status_code == 400, "rechaza reutilizar la contraseña temporal")
    r = c.post("/clave", data={"csrf": "falso", "actual": CLAVE, "nueva": CLAVE_NUEVA, "repetida": CLAVE_NUEVA})
    verificar(r.status_code == 403, "sin token CSRF válido no cambia la contraseña")
    r = c.post("/clave", data={"csrf": t, "actual": CLAVE, "nueva": CLAVE_NUEVA, "repetida": CLAVE_NUEVA})
    verificar(r.status_code == 303, "cambia la contraseña con datos válidos")
    verificar(c.get("/").status_code == 200, "luego del cambio accede al tablero")
    h = sql("SELECT password_hash FROM credencial_acceso c JOIN usuario u ON u.id=c.usuario_id WHERE email=%s",
            (USUARIOS["admin"],))[0]["password_hash"]
    verificar(h.startswith("$argon2") and CLAVE_NUEVA not in h, "la contraseña se guarda con Argon2, no en claro")

    print("Tablero y ficha")
    c = cliente("gestor")
    r = c.get("/")
    verificar(r.status_code == 200 and "Tablero de flota" in r.text, "el tablero carga")
    verificar("Content-Security-Policy" in r.headers and r.headers["X-Frame-Options"] == "DENY", "cabeceras de seguridad presentes")
    verificar(c.get("/?q=hilux").status_code == 200, "la búsqueda funciona")
    verificar(c.get("/?q=%27%3B%20DROP%20TABLE%20vehiculo%3B--").status_code == 200, "una búsqueda con SQL no rompe nada")
    verificar(sql("SELECT count(*) n FROM vehiculo")[0]["n"] == 40, "la tabla vehiculo sigue intacta")
    verificar(c.get("/?estado=DISPONIBLE&vencidos=1").status_code == 200, "los filtros combinados funcionan")
    verificar(c.get("/vehiculos/999999").status_code == 404, "vehículo inexistente: 404")

    print("Salida, lectura, incidencia y retorno")
    v = sql("""SELECT v.id, v.odometro_km, v.ubicacion_id FROM vehiculo v JOIN estado_vehiculo e ON e.id=v.estado_id
                WHERE e.codigo='DISPONIBLE' ORDER BY v.id LIMIT 1""")[0]
    vid, km = v["id"], float(v["odometro_km"])
    ficha = c.get(f"/vehiculos/{vid}")
    verificar(ficha.status_code == 200 and "Registrar salida" in ficha.text, "la ficha ofrece registrar la salida")
    t = token(c, f"/vehiculos/{vid}/salida")
    base = {"csrf": t, "ubicacion": v["ubicacion_id"], "motivo": "Traslado de prueba", "observaciones": ""}
    ge = sql("SELECT id FROM usuario WHERE email=%s", (USUARIOS["conductor"],))[0]["id"]
    r = c.post(f"/vehiculos/{vid}/salida", data={**base, "responsable": ge, "odometro": str(km - 50)})
    verificar(r.status_code == 400 and "menor" in r.text, "salida con odómetro menor: la base la rechaza y se muestra el motivo")
    r = c.post(f"/vehiculos/{vid}/salida", data={**base, "responsable": ge, "odometro": "abc"})
    verificar(r.status_code == 400 and "número" in r.text, "odómetro no numérico: mensaje claro")
    r = c.post(f"/vehiculos/{vid}/salida", data={**base, "responsable": ge, "odometro": str(km), "csrf": "x"})
    verificar(r.status_code == 403, "salida sin CSRF válido: 403")
    r = c.post(f"/vehiculos/{vid}/salida", data={**base, "responsable": ge,
               "odometro": str(km + 1).replace(".", ","), "retorno_previsto": "2030-01-01T10:00"})
    verificar(r.status_code == 303, "salida válida (con coma decimal) registrada")
    est = sql("SELECT e.codigo FROM vehiculo v JOIN estado_vehiculo e ON e.id=v.estado_id WHERE v.id=%s", (vid,))[0]["codigo"]
    verificar(est == "ASIGNADO", "el trigger dejó el vehículo ASIGNADO")
    r = c.post(f"/vehiculos/{vid}/salida", data={**base, "responsable": ge, "odometro": str(km + 2)})
    verificar(r.status_code == 400 and "no está disponible" in r.text, "segunda salida del mismo vehículo: rechazada")

    asig = sql("SELECT id FROM asignacion WHERE vehiculo_id=%s AND estado='EN_CURSO'", (vid,))[0]["id"]
    t = token(c, f"/vehiculos/{vid}/lectura")
    r = c.post(f"/vehiculos/{vid}/lectura", data={"csrf": t, "odometro": str(km - 10)})
    verificar(r.status_code == 400 and "menor" in r.text, "lectura menor a una anterior: rechazada")
    r = c.post(f"/vehiculos/{vid}/lectura", data={"csrf": t, "odometro": str(km + 20)})
    verificar(r.status_code == 303, "lectura suelta válida registrada")

    t = token(c, f"/vehiculos/{vid}/incidencia")
    r = c.post(f"/vehiculos/{vid}/incidencia", data={"csrf": t, "tipo": "FALLA", "severidad": "MEDIA",
               "sistema": "frenos", "descripcion": "<script>alert(1)</script> ruido al frenar", "odometro": ""})
    verificar(r.status_code == 303, "incidencia registrada")
    r = c.post(f"/vehiculos/{vid}/incidencia", data={"csrf": t, "tipo": "INVENTADO", "severidad": "MEDIA", "descripcion": "x"})
    verificar(r.status_code == 400, "incidencia con un tipo inválido: rechazada")
    pagina = c.get(f"/vehiculos/{vid}").text
    verificar("<script>alert(1)</script>" not in pagina and "&lt;script&gt;" in pagina, "el texto de la incidencia se escapa (sin XSS)")
    verificar(sql("SELECT asignacion_id FROM incidencia WHERE vehiculo_id=%s ORDER BY id DESC LIMIT 1", (vid,))[0]["asignacion_id"] == asig,
              "la incidencia queda ligada a la salida en curso")

    otro = cliente("otro_conductor")
    verificar(otro.get(f"/asignaciones/{asig}/retorno").status_code == 403, "un conductor no puede cerrar la salida de otro")
    tec = cliente("tecnico")
    verificar(tec.get(f"/vehiculos/{vid}/salida").status_code == 403, "el técnico no puede registrar salidas")
    verificar(tec.get(f"/vehiculos/{vid}").status_code == 200, "el técnico sí puede ver la ficha")

    dueño = cliente("conductor")
    t = token(dueño, f"/asignaciones/{asig}/retorno")
    r = dueño.post(f"/asignaciones/{asig}/retorno", data={"csrf": t, "odometro": str(km), "ubicacion": v["ubicacion_id"]})
    verificar(r.status_code == 400 and "menor" in r.text, "retorno con odómetro menor al de salida: rechazado")
    r = dueño.post(f"/asignaciones/{asig}/retorno", data={"csrf": t, "odometro": str(km + 85.5), "ubicacion": v["ubicacion_id"]})
    verificar(r.status_code == 303, "el responsable cierra su propia salida")
    r = dueño.post(f"/asignaciones/{asig}/retorno", data={"csrf": t, "odometro": str(km + 90), "ubicacion": v["ubicacion_id"]})
    verificar(r.status_code == 303 and dueño.get(f"/asignaciones/{asig}/retorno").status_code == 303,
              "una salida ya cerrada no se puede cerrar otra vez")

    print("Coherencia de la base después de usar la app")
    d = sql("""SELECT e.codigo, v.odometro_km, (SELECT max(odometro_km) FROM lectura_uso WHERE vehiculo_id=v.id) AS maxl
                 FROM vehiculo v JOIN estado_vehiculo e ON e.id=v.estado_id WHERE v.id=%s""", (vid,))[0]
    verificar(d["codigo"] == "DISPONIBLE", "tras el retorno el vehículo vuelve a DISPONIBLE")
    verificar(d["odometro_km"] == d["maxl"] == type(d["maxl"])(str(km + 85.5)), "odómetro del vehículo = lectura más alta")
    verificar(sql("""SELECT count(*) n FROM vehiculo v JOIN estado_vehiculo e ON e.id=v.estado_id
                      WHERE (e.codigo='ASIGNADO') <> EXISTS (SELECT 1 FROM asignacion a WHERE a.vehiculo_id=v.id
                            AND a.retorno_real IS NULL AND a.anulada_en IS NULL)""")[0]["n"] == 0,
              "ASIGNADO coincide con tener una salida en curso (toda la flota)")

    print("Baja de usuario")
    sql("UPDATE usuario SET activo = FALSE WHERE email = %s", (USUARIOS["otro_conductor"],))
    verificar(otro.get("/").status_code == 303, "al dar de baja a un usuario, su sesión deja de valer enseguida")
    sql("UPDATE usuario SET activo = TRUE WHERE email = %s", (USUARIOS["otro_conductor"],))

    print("Salir")
    t = token(c, "/")
    verificar(c.post("/salir", data={"csrf": t}).status_code == 303, "cierre de sesión")

print()
print("TODO OK" if not fallos else f"{len(fallos)} FALLAS: {fallos}")
sys.exit(1 if fallos else 0)
