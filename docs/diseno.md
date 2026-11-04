# Decisiones de diseño

## Cómo se registra el uso

- **Solo se registra la salida.** No hay reservas: la asignación se crea cuando el vehículo sale.
- **El odómetro se lee a mano.** Se carga en la salida y en el retorno; el sistema genera la lectura por sí solo (fuente `ASIGNACION`), así se escribe una sola vez. También se admiten lecturas sueltas entre viajes (fuente `MANUAL`), por ejemplo al cargar combustible o en un inventario mensual.

**Ciclo de una asignación:**

1. *Salida*: se registra responsable, ubicación de salida y odómetro. El sistema verifica que el vehículo esté `DISPONIBLE`, genera la lectura, lo pasa a `ASIGNADO` y deja constancia en el historial.
2. *Retorno*: se registra odómetro y ubicación de retorno (obligatorios). El vehículo vuelve a `DISPONIBLE`, **salvo** que durante el viaje se lo haya pasado a fuera de servicio o a mantenimiento: ese estado se respeta.
3. *Anulación*: una salida cargada por error se anula con motivo obligatorio. No se borra. El vehículo vuelve a su ubicación.

## Calidad de datos con carga manual

El punto débil de una flota con odómetro manual es el error de tipeo, y un solo valor absurdo arruina el modelo predictivo. El esquema lo cubre en tres capas:

- **Se rechaza** una lectura menor que otra anterior (o mayor que una posterior) del mismo vehículo, con un mensaje que sugiere revisar el tipeo.
- **Se marca** una lectura que salta de forma implausible hacia arriba (por ejemplo 154000 en lugar de 15400) en la vista `v_lecturas_sospechosas`. El umbral (más de 1000 km/día, con un piso de medio día) es ajustable.
- **Se mide la antigüedad** de la última lectura de cada vehículo (`dias_sin_lectura`). Con carga manual, un vehículo sin lecturas recientes tiene predicciones poco confiables.

**Límite conocido:** una lectura errónea *hacia arriba* se acepta (no hay forma de saberlo en el momento) y luego bloquea las lecturas correctas siguientes. La corrección la hace un administrador: corrige la lectura y ejecuta `fn_recalcular_odometro(vehiculo_id)`, dejando constancia en `auditoria_sistema`. Cuando exista la aplicación, esto debería ser una pantalla de administración.

## Telemetría: cómo se adaptaría más adelante

Telemetría significa que el vehículo informa su odómetro (y a veces horas de motor, ubicación o fallas) sin intervención humana, mediante un rastreador GPS, un dispositivo conectado al puerto de diagnóstico (OBD) o el sistema del fabricante.

No hace falta decidir nada hoy:

- Cada lectura tiene un campo `fuente` que admite `TELEMETRIA` e `IMPORTACION`.
- Un proceso externo (por ejemplo un script de Python) insertaría filas en `lectura_uso` con esa fuente. Las validaciones, las vistas predictivas y el tablero funcionan igual, sin cambios.
- Si un proveedor de rastreo entrega un archivo (CSV o Excel), la fuente `IMPORTACION` permite cargarlo en bloque.

La telemetría mejora la calidad (lecturas diarias, sin tipeo) pero no es requisito: el modelo funciona con lecturas manuales, con menor precisión.

## Qué se adaptó de un sistema de alquiler de bicicletas

El modelo parte del diseño de estados, historiales y auditoría de un sistema académico de alquiler de bicicletas.

**Se conserva:**

- Activos con estado y su historial de cambios.
- Salida y retorno con ubicación de origen y de retorno.
- Órdenes de mantenimiento preventivo y correctivo con estados.
- Incidencias que originan órdenes correctivas.
- Auditoría exclusiva de administradores, con motivo obligatorio.
- Bajas lógicas, nunca físicas; bloqueo optimista (`version`) en las tablas de uso concurrente.

**Se descarta** (no aporta a una flota interna): pagos, reembolsos, anticipos, cotización, reservas con vencimiento, alquiler de varias unidades a la vez (aquí una asignación es un vehículo) y datos personales detallados.

**Se agrega** (lo que hace al proyecto predictivo): lecturas de uso, planes de mantenimiento por km/días/horas, ítems de costo por orden, tabla de predicciones y las vistas de próximo servicio, ritmo de uso, lecturas sospechosas y costo por km.

## Cómo funciona la parte predictiva

**Fase 1: reglas (incluida en el esquema).** `v_proximo_servicio` calcula, para cada vehículo y tarea, los km, días y horas que faltan, y estima la fecha de vencimiento usando el **ritmo de uso real** (km por día en los últimos 90 días) en lugar de un promedio fijo. Un vehículo que se usa el doble vence antes.

**Fase 2: riesgo de falla.** Un proceso en Python lee las órdenes correctivas, las incidencias y las lecturas, y estima la probabilidad de falla en los próximos 30 días por vehículo y por sistema (motor, frenos, etc.). Se guarda en `prediccion_mantenimiento` con `riesgo_falla_30d`. Se empieza con un modelo simple y explicable (regresión logística o análisis de supervivencia) y se compara contra la línea base de reglas. Un modelo complejo solo se justifica si mejora esa línea base. La validación debe ser temporal (entrenar con el pasado, evaluar con el futuro), no aleatoria.

**Fase 3: decisiones.** Alertas de vehículos por vencer o con riesgo alto, costo por km por tipo, y comparación entre reemplazar y seguir reparando.

## Datos reales

Los datos de este repositorio son sintéticos. Si se usa el sistema con datos reales de una organización (ubicaciones, movimientos, estado de las unidades), conviene mantenerlos en una instancia privada, con acceso restringido, y separada de cualquier repositorio o demostración pública. Un solo código y dos configuraciones: una pública con datos ficticios y otra privada con datos reales.

## Decisiones abiertas

- ¿Se registran las horas de motor además del odómetro? El esquema las admite; son opcionales.
- ¿Se quiere registrar quién autoriza una salida, además de quién la realiza?
- ¿Cómo se organiza el acceso por rol (ADMIN, GESTOR, TECNICO, CONDUCTOR) cuando exista la aplicación? Falta definir las políticas de acceso (RLS en Supabase).
