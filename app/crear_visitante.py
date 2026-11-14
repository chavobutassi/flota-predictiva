"""Prepara el usuario de la entrada de visitante (demo pública, solo lectura).

Uso:   DEMO_EMAIL=correo python -m app.crear_visitante

Toma un usuario de los datos de demostración cuyo único rol sea TECNICO y le crea
una credencial con una contraseña aleatoria que nadie conoce (solo se puede entrar
por el botón "Entrar como visitante", nunca con contraseña). Después hay que definir
DEMO_EMAIL en el servicio para activar el botón.
"""
import secrets
import sys

from . import config, db, seguridad


def main():
    email = config.DEMO_EMAIL
    if not email:
        sys.exit("Definí DEMO_EMAIL con el correo de un usuario de demostración.")
    db.abrir()
    try:
        with db.conexion() as con:
            u = con.execute(
                """SELECT u.id, COALESCE(array_agg(r.codigo) FILTER (WHERE r.codigo IS NOT NULL), '{}') AS roles
                     FROM usuario u
                     LEFT JOIN usuario_rol ur ON ur.usuario_id = u.id
                     LEFT JOIN rol r ON r.id = ur.rol_id
                    WHERE lower(u.email) = %s AND u.activo GROUP BY u.id""", (email,)).fetchone()
            if not u:
                sys.exit("No existe un usuario activo con ese correo.")
            if set(u["roles"]) != {"TECNICO"}:
                sys.exit("El usuario visitante debe tener únicamente el rol TECNICO (solo lectura).")
            con.execute(
                """INSERT INTO credencial_acceso (usuario_id, password_hash, debe_cambiar)
                   VALUES (%s, %s, FALSE)
                   ON CONFLICT (usuario_id) DO UPDATE
                      SET password_hash = EXCLUDED.password_hash, debe_cambiar = FALSE, actualizada_en = now()""",
                (u["id"], seguridad.hashear(secrets.token_urlsafe(48))))
        print(f"Listo: {email} puede entrar como visitante.")
    finally:
        db.cerrar()


if __name__ == "__main__":
    main()
