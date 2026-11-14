"""Configuración de la aplicación, tomada de variables de entorno."""
import os
import sys

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
# En producción (HTTPS) las cookies se marcan Secure.
PRODUCCION = os.environ.get("APP_ENV", "desarrollo").lower() == "produccion"
ZONA_HORARIA = os.environ.get("ZONA_HORARIA", "America/Argentina/Buenos_Aires")
SESION_HORAS = int(os.environ.get("SESION_HORAS", "12"))
# Entrada de visitante (demo pública): correo de un usuario ficticio de solo lectura.
# Si no se define, el botón no aparece y la ruta /demo no existe.
DEMO_EMAIL = os.environ.get("DEMO_EMAIL", "").strip().lower()

if not DATABASE_URL:
    sys.exit("Falta la variable de entorno DATABASE_URL.")
if len(SECRET_KEY) < 32:
    sys.exit("SECRET_KEY debe tener al menos 32 caracteres "
             "(generá una con: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\").")
