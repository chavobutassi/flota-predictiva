# Flota predictiva

Base de datos y generador de datos para **gestionar una flota de vehículos y anticipar su mantenimiento**: quién usa cada vehículo, cuántos km recorre, qué servicio le toca y cuándo, qué falló y cuánto costó repararlo.

El proyecto nace con dos objetivos: servir como pieza de portfolio de analítica de datos aplicada y, a la vez, poder operar una flota real. Por eso la base de datos hace cumplir sus propias reglas (no confía en la aplicación) y todos los datos incluidos son **sintéticos**.

> **Estado:** la base de datos, el generador de datos y una primera versión de la aplicación web (login con roles, tablero, salidas y retornos, lecturas e incidencias) están terminados y probados. Faltan las órdenes de mantenimiento en la app, la pantalla de corrección de lecturas y el modelo de riesgo de falla (ver [Hoja de ruta](#hoja-de-ruta)).

## Qué tiene de interesante

- **Modelo de datos con integridad real.** 20 tablas, 6 vistas y 6 triggers. Un vehículo no puede salir dos veces a la vez, el odómetro no puede retroceder, y cada salida y retorno genera solo la lectura, el cambio de estado y el historial.
- **Pensado para carga manual del odómetro.** Es el caso real de muchas flotas. Los errores de tipeo se rechazan, se marcan o se corrigen con auditoría, para que no contaminen las predicciones.
- **Mantenimiento predictivo en dos fases.** Primero reglas basadas en el ritmo de uso real de cada vehículo (ya incluidas como vistas SQL); después un modelo de riesgo de falla que debe superar esa línea base.
- **Datos sintéticos con verdad conocida.** El generador simula 2 años de una flota de 40 vehículos con un proceso de fallas documentado, de modo que un modelo se pueda evaluar contra algo que se conoce.

## Inicio rápido

Requisitos: PostgreSQL 14 o superior y el cliente `psql`.

```bash
createdb flota
./scripts/cargar_demo.sh "postgresql://usuario@localhost/flota"
```

La carga tarda alrededor de 90 segundos, porque cada una de las ~13.000 salidas y retornos dispara los triggers de validación.

Sin `psql` (por ejemplo en Windows): `pip install "psycopg[binary]"` y `python scripts/cargar_demo.py` con `DATABASE_URL` en el entorno. Conserva los usuarios que ya existan y se niega a correr sobre una base con datos. Con eso queda cargado:

| Qué | Cantidad |
| --- | --- |
| Vehículos (5 tipos) | 40 |
| Salidas y retornos | ~13.400 |
| Lecturas de odómetro | ~27.500 |
| Incidencias | 412 |
| Órdenes de mantenimiento en el período | ~810 |
| Cambios de estado registrados | ~28.200 |

### Consultas para probar

```sql
-- Próximos servicios, con la antigüedad de la última lectura del vehículo
SELECT identificador, tarea, km_restantes, dias_restantes, fecha_estimada, dias_sin_lectura
FROM v_proximo_servicio
WHERE fecha_estimada IS NOT NULL
ORDER BY fecha_estimada
LIMIT 10;

-- Lecturas de odómetro con un salto implausible (posible error de tipeo)
SELECT v.identificador, s.leida_en::date AS fecha, s.odometro_km, s.salto_km
FROM v_lecturas_sospechosas s
JOIN vehiculo v ON v.id = s.vehiculo_id
ORDER BY s.leida_en;

-- Costo de mantenimiento por km recorrido
SELECT identificador, ordenes_correctivas, ordenes_preventivas, km_registrados, costo_por_km
FROM v_costo_por_vehiculo
ORDER BY costo_por_km DESC NULLS LAST
LIMIT 10;
```

## Aplicación web

FastAPI + PostgreSQL, sin JavaScript en el navegador. Las reglas viven en la base; la aplicación valida el formato y muestra los rechazos de la base en castellano (por ejemplo, «Odómetro 500 menor que una lectura anterior»).

| Pantalla | Qué hace | Quién |
| --- | --- | --- |
| Tablero | Estado de la flota, retornos vencidos, servicios por vencer, antigüedad de la última lectura | Todos los roles |
| Ficha del vehículo | Servicios con km y días restantes, últimas salidas, lecturas, incidencias y órdenes | Todos los roles |
| Salida y retorno | Odómetro manual; el retorno solo lo cierra el responsable o un gestor | Admin, gestor, conductor |
| Lectura suelta | Odómetro entre viajes (carga de combustible, por ejemplo) | Todos los roles |
| Incidencia | Tipo, severidad, sistema; queda ligada a la salida en curso | Todos los roles |

**Seguridad:** contraseñas con Argon2 en una tabla aparte, contraseña temporal obligatoriamente cambiada en el primer ingreso, sesión en cookie firmada (`HttpOnly`, `SameSite`, `Secure` en producción), token CSRF en cada formulario, límite de 5 intentos de acceso fallidos por 15 minutos, cabeceras de seguridad (CSP, HSTS), consultas siempre parametrizadas y permisos leídos de la base en cada pedido (una baja rige al instante).

### Probarla en tu máquina

```bash
pip install -r app/requirements.txt
export DATABASE_URL="postgresql://usuario@localhost/flota"      # base ya cargada con cargar_demo.sh
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
python3 -m app.crear_usuario marcelo.ibanez@flota-demo.test     # pide una contraseña temporal
uvicorn app.main:app --reload
```

Abrir <http://localhost:8000> e ingresar con ese correo (usuario `ADMIN` de los datos de demostración). Para dar de alta usuarios reales: `python -m app.crear_usuario correo --nombre N --apellido A --rol GESTOR`.

### Publicarla en internet

1. **Base de datos** (por ejemplo Supabase, plan gratuito): crear el proyecto, copiar la cadena de conexión y cargar `db/schema.sql` con `psql`. Datos de demostración opcionales: `scripts/cargar_demo.sh`.
2. **Cerrar la API de datos**: ejecutar `db/supabase_seguridad.sql`. Es imprescindible en Supabase: sin él, quien tenga la clave pública puede leer y escribir las tablas, incluidos los hashes de contraseñas. La aplicación no usa esa API.
3. **Servidor** (por ejemplo Render): *New › Blueprint* sobre este repositorio (`render.yaml`, usa el `Dockerfile`). Cargar `DATABASE_URL` en el panel; `SECRET_KEY` se genera sola.
4. **Primer usuario**: desde tu máquina, con la misma `DATABASE_URL`, `python -m app.crear_usuario ... --rol ADMIN`.

No se probó el despliegue en Render ni en Supabase. Con datos reales de una organización conviene además un dominio propio con HTTPS, copias de seguridad de la base y revisar con quien corresponda dónde pueden alojarse esos datos.

### Probar la aplicación

```bash
python3 tests/prueba_app.py     # 47 comprobaciones; escribe en la base: usar solo una de desarrollo
```

## Estructura del repositorio

```
app/                       Aplicación web (FastAPI): rutas, base de datos, seguridad, plantillas
tests/prueba_app.py        Pruebas de punta a punta de la aplicación
Dockerfile, render.yaml    Publicación
db/schema.sql              Esquema: tablas, restricciones, triggers, vistas y catálogos
db/supabase_seguridad.sql  Cierra la API de datos de Supabase (RLS y permisos)
data/seed.sql.gz           Datos sintéticos listos para cargar (2 años de historia)
generador/generar_datos.py Generador reproducible de los datos (solo biblioteca estándar)
scripts/cargar_demo.sh     Carga esquema y datos en una base vacía (con psql)
scripts/cargar_demo.py     Carga los datos de demostración sin psql, conservando los usuarios existentes
docs/diseno.md             Decisiones de diseño y calidad de datos
docs/der.md                Diagrama de relaciones, tablas, vistas y reglas
```

## Los datos sintéticos

El generador (`generador/generar_datos.py`) reproduce, mediante las mismas operaciones que haría una aplicación, dos años de vida de una flota ficticia: salidas y retornos con odómetro manual, servicios preventivos con demoras, incidencias, órdenes correctivas, altas y bajas. Es determinista: la misma semilla y fecha producen el mismo resultado.

```bash
python3 generador/generar_datos.py --hasta 2026-11-04T12:00 --salida data/seed.sql.gz
```

**Flota:** 13 pick-ups 4x4, 8 utilitarios, 6 furgones, 7 camiones livianos y 6 camiones pesados, de entre 2016 y 2026, con sus planes de mantenimiento por km y por días. Hay 3 unidades incorporadas durante el período y 2 bajas.

**Casos incluidos a propósito** para que los tableros y el modelo tengan qué detectar: vehículos con el mantenimiento muy atrasado, lecturas de odómetro erróneas pendientes de corrección, correcciones ya auditadas, comisiones largas con el retorno vencido y vehículos en taller o fuera de servicio.

**El proceso que genera las fallas** es conocido, lo que permite evaluar un modelo contra una verdad:

- La tasa de incidencias por km aumenta con la edad del vehículo.
- Aumenta con el **atraso de los servicios preventivos** (la señal principal).
- El sistema que falla depende de qué servicio está atrasado (aceite y motor, frenos y frenos).
- Cada vehículo tiene una fragilidad propia, y en invierno la tasa es algo mayor.

Resultado de la simulación publicado por el generador, con las incidencias por 10.000 km según el atraso del mantenimiento al salir:

| Atraso del mantenimiento | Km recorridos | Incidencias | Tasa por 10.000 km |
| --- | ---: | ---: | ---: |
| Al día | 1.119.490 | 210 | 1,88 |
| Hasta 25 % del intervalo | 541.343 | 131 | 2,42 |
| 25 % a 60 % | 165.565 | 57 | 3,44 |
| Más de 60 % | 25.137 | 14 | 5,57 |

Esta tabla describe cómo se generaron los datos, no el resultado de un modelo ajustado.

## Verificación

La carga completa se probó en PostgreSQL 16. Después de cargar, se comprobaron estas condiciones, que deben dar cero filas de diferencia:

- El odómetro de cada vehículo coincide con su lectura más alta.
- Un vehículo está `ASIGNADO` si y solo si tiene una asignación en curso.
- Un vehículo `EN_MANTENIMIENTO` tiene una orden en curso.
- El último registro del historial de estados coincide con el estado actual.
- El costo de cada orden cerrada es igual a la suma de sus ítems.

También se probaron los rechazos del esquema: segunda salida de un vehículo no disponible, retorno sin odómetro, odómetro de retorno menor que el de salida, lectura menor que una anterior, anulación sin motivo, plan sin intervalos y email duplicado. Detalles en [docs/diseno.md](docs/diseno.md) y [docs/der.md](docs/der.md).

**Lo que no se probó:** el esquema y la aplicación no se ejecutaron en Supabase ni en Render. No hay políticas de acceso por fila para usar la API de Supabase desde un navegador: `db/supabase_seguridad.sql` la cierra por completo y la aplicación se conecta directo a Postgres.

## Hoja de ruta

1. ~~**Base de datos**: esquema, reglas de integridad y vistas.~~ Hecho.
2. ~~**Datos sintéticos**: flota, uso, mantenimiento e incidencias.~~ Hecho.
3. **Aplicación**: ~~login con roles, tablero, ficha, salida y retorno, lectura suelta e incidencias~~ hecho. Faltan: alta de vehículos, anulación de salidas, órdenes de mantenimiento y pantalla de corrección de lecturas para administradores.
4. **Tablero**: próximos servicios, vehículos por vencer, lecturas vencidas o sospechosas, costo por km, disponibilidad y tiempos de parada.
5. **Modelo de riesgo de falla**: probabilidad de falla a 30 días por vehículo y sistema, con validación temporal y comparación contra las reglas de la fase 1.
6. **Seguridad**: políticas de acceso por rol y pruebas sobre Supabase, auditoría de operaciones de la aplicación y segundo factor de acceso.

## Aviso sobre los datos

Todos los datos son ficticios: nombres, vehículos, costos (en pesos argentinos, actualizados por una inflación supuesta), ubicaciones y correos del dominio reservado `.test`. Cualquier parecido con personas, organizaciones o unidades reales es casual.

## Licencia

[MIT](LICENSE)
