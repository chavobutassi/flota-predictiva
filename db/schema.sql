-- ============================================================
-- Flota de vehículos con mantenimiento predictivo
-- PostgreSQL / Supabase
--
-- Adaptado del diseño de un sistema académico de alquiler de bicicletas:
-- se conserva la estructura de activos con estado, salida/devolución,
-- historiales, órdenes de mantenimiento, incidencias y auditoría.
-- Se descarta todo lo financiero de cara al cliente (anticipos, pagos,
-- reembolsos, cotización) porque aquí el "alquiler" es una asignación
-- interna de un vehículo a un responsable.
-- ============================================================

-- ---------- Identidad y acceso ----------

CREATE TABLE rol (
    id      SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    codigo  VARCHAR(20) NOT NULL UNIQUE,
    nombre  VARCHAR(50) NOT NULL
);

CREATE TABLE usuario (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auth_user_id  UUID UNIQUE,                       -- vínculo opcional con Supabase Auth
    nombre        VARCHAR(50)  NOT NULL,
    apellido      VARCHAR(50)  NOT NULL,
    email         VARCHAR(254) NOT NULL,
    activo        BOOLEAN      NOT NULL DEFAULT TRUE,
    creado_en     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    version       INTEGER      NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX uq_usuario_email ON usuario (lower(email));

CREATE TABLE usuario_rol (
    usuario_id INTEGER  NOT NULL REFERENCES usuario(id),
    rol_id     SMALLINT NOT NULL REFERENCES rol(id),
    PRIMARY KEY (usuario_id, rol_id)
);

-- ---------- Catálogos ----------

CREATE TABLE ubicacion (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nombre      VARCHAR(100) NOT NULL UNIQUE,
    tipo        VARCHAR(20)  NOT NULL CHECK (tipo IN ('BASE','TALLER','DESTACAMENTO','OTRA')),
    activa      BOOLEAN      NOT NULL DEFAULT TRUE   -- baja lógica, nunca física
);

CREATE TABLE tipo_vehiculo (
    id                  SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    codigo              VARCHAR(30) NOT NULL UNIQUE,
    nombre              VARCHAR(80) NOT NULL,
    capacidad_personas  SMALLINT CHECK (capacidad_personas > 0)
);

CREATE TABLE estado_vehiculo (
    id      SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    codigo  VARCHAR(30) NOT NULL UNIQUE,
    nombre  VARCHAR(80) NOT NULL
);

CREATE TABLE estado_orden (
    id      SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    codigo  VARCHAR(30) NOT NULL UNIQUE,
    nombre  VARCHAR(80) NOT NULL
);

CREATE TABLE tipo_mantenimiento (
    id      SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    codigo  VARCHAR(20) NOT NULL UNIQUE,             -- PREVENTIVO / CORRECTIVO
    nombre  VARCHAR(50) NOT NULL
);

-- ---------- Vehículos ----------

CREATE TABLE vehiculo (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identificador       VARCHAR(30) NOT NULL UNIQUE,          -- patente o código interno
    tipo_vehiculo_id    SMALLINT NOT NULL REFERENCES tipo_vehiculo(id),
    estado_id           SMALLINT NOT NULL REFERENCES estado_vehiculo(id),
    ubicacion_id        INTEGER  REFERENCES ubicacion(id),    -- NULL mientras está asignado
    marca               VARCHAR(50),
    modelo              VARCHAR(50),
    anio                SMALLINT CHECK (anio BETWEEN 1950 AND 2100),
    fecha_alta          DATE NOT NULL DEFAULT current_date,
    fecha_baja          DATE,
    odometro_km         NUMERIC(10,1) NOT NULL DEFAULT 0 CHECK (odometro_km >= 0),
    horas_motor         NUMERIC(10,1) NOT NULL DEFAULT 0 CHECK (horas_motor >= 0),
    version             INTEGER NOT NULL DEFAULT 0,
    CHECK (fecha_baja IS NULL OR fecha_baja >= fecha_alta)
);
CREATE INDEX ix_vehiculo_estado ON vehiculo (estado_id);
CREATE INDEX ix_vehiculo_ubicacion ON vehiculo (ubicacion_id);

-- ---------- Asignaciones (equivalente al alquiler) ----------
-- Decisiones:
--  * No hay reservas: la asignación se registra en el momento de la salida.
--  * Un vehículo por asignación. Si hiciera falta una salida con varios
--    vehículos, se agrega una cabecera, como ALQUILER/ALQUILER_DETALLE.
--  * El estado se deriva de las fechas (columna generada): no puede quedar
--    inconsistente con ellas. EN_CURSO / FINALIZADA / ANULADA.
--  * Anular sirve para corregir una salida cargada por error; no se borra.

CREATE TABLE asignacion (
    id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehiculo_id          INTEGER NOT NULL REFERENCES vehiculo(id),
    responsable_id       INTEGER NOT NULL REFERENCES usuario(id),
    ubicacion_salida_id  INTEGER NOT NULL REFERENCES ubicacion(id),
    ubicacion_retorno_id INTEGER REFERENCES ubicacion(id),
    motivo               VARCHAR(200),
    salida_real          TIMESTAMPTZ NOT NULL DEFAULT now(),
    retorno_previsto     TIMESTAMPTZ,                       -- opcional; permite detectar demoras
    retorno_real         TIMESTAMPTZ,
    odometro_salida_km   NUMERIC(10,1) NOT NULL CHECK (odometro_salida_km >= 0),
    odometro_retorno_km  NUMERIC(10,1) CHECK (odometro_retorno_km >= 0),
    observaciones        VARCHAR(500),
    anulada_en           TIMESTAMPTZ,
    motivo_anulacion     VARCHAR(200),
    creado_en            TIMESTAMPTZ NOT NULL DEFAULT now(),
    estado               VARCHAR(10) GENERATED ALWAYS AS (
                             CASE WHEN anulada_en IS NOT NULL THEN 'ANULADA'
                                  WHEN retorno_real IS NOT NULL THEN 'FINALIZADA'
                                  ELSE 'EN_CURSO' END) STORED,
    CHECK (retorno_previsto IS NULL OR retorno_previsto > salida_real),
    CHECK (retorno_real IS NULL OR retorno_real >= salida_real),
    CHECK (retorno_real IS NULL OR (odometro_retorno_km IS NOT NULL AND ubicacion_retorno_id IS NOT NULL)),
    CHECK (odometro_retorno_km IS NULL OR odometro_retorno_km >= odometro_salida_km),
    CHECK (anulada_en IS NULL OR (retorno_real IS NULL AND motivo_anulacion IS NOT NULL))
);
CREATE INDEX ix_asignacion_vehiculo ON asignacion (vehiculo_id, salida_real DESC);
CREATE INDEX ix_asignacion_responsable ON asignacion (responsable_id);

-- Un vehículo no puede tener dos asignaciones en curso a la vez.
CREATE UNIQUE INDEX uq_asignacion_en_curso
    ON asignacion (vehiculo_id)
    WHERE retorno_real IS NULL AND anulada_en IS NULL;

-- ---------- Lecturas de uso (insumo del modelo predictivo) ----------
-- Hoy las lecturas son manuales: salen de cada salida y retorno (fuente
-- ASIGNACION, se generan solas) y de lecturas sueltas entre viajes (MANUAL).
-- Cuando exista telemetría, se insertan filas con fuente TELEMETRIA: nada
-- más del modelo cambia. Ver README.

CREATE TABLE lectura_uso (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehiculo_id  INTEGER NOT NULL REFERENCES vehiculo(id),
    leida_en     TIMESTAMPTZ NOT NULL,
    odometro_km  NUMERIC(10,1) NOT NULL CHECK (odometro_km >= 0),
    horas_motor  NUMERIC(10,1) CHECK (horas_motor >= 0),
    fuente       VARCHAR(20) NOT NULL DEFAULT 'MANUAL'
                 CHECK (fuente IN ('MANUAL','ASIGNACION','TELEMETRIA','IMPORTACION')),
    UNIQUE (vehiculo_id, leida_en)
);
CREATE INDEX ix_lectura_vehiculo_fecha ON lectura_uso (vehiculo_id, leida_en DESC);

-- ---------- Mantenimiento ----------

-- Plan: qué tarea se hace y cada cuánto, por tipo de vehículo.
-- Se puede definir por km, por días, por horas de motor, o combinaciones:
-- vence lo que ocurra primero.
CREATE TABLE plan_mantenimiento (
    id                SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tipo_vehiculo_id  SMALLINT NOT NULL REFERENCES tipo_vehiculo(id),
    tarea             VARCHAR(100) NOT NULL,
    cada_km           INTEGER CHECK (cada_km > 0),
    cada_dias         INTEGER CHECK (cada_dias > 0),
    cada_horas        INTEGER CHECK (cada_horas > 0),
    activo            BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (tipo_vehiculo_id, tarea),
    CHECK (cada_km IS NOT NULL OR cada_dias IS NOT NULL OR cada_horas IS NOT NULL)
);

CREATE TABLE orden_mantenimiento (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehiculo_id         INTEGER NOT NULL REFERENCES vehiculo(id),
    tipo_id             SMALLINT NOT NULL REFERENCES tipo_mantenimiento(id),
    estado_id           SMALLINT NOT NULL REFERENCES estado_orden(id),
    plan_id             SMALLINT REFERENCES plan_mantenimiento(id),   -- solo preventivas
    incidencia_id       INTEGER,                                      -- FK abajo (referencia circular)
    asignado_a_id       INTEGER REFERENCES usuario(id),
    taller_id           INTEGER REFERENCES ubicacion(id),
    descripcion         VARCHAR(500),
    abierta_en          TIMESTAMPTZ NOT NULL DEFAULT now(),
    iniciada_en         TIMESTAMPTZ,
    cerrada_en          TIMESTAMPTZ,
    odometro_km         NUMERIC(10,1) CHECK (odometro_km >= 0),       -- km al momento del servicio
    horas_motor         NUMERIC(10,1) CHECK (horas_motor >= 0),
    costo_total         NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (costo_total >= 0),
    version             INTEGER NOT NULL DEFAULT 0,
    CHECK (iniciada_en IS NULL OR iniciada_en >= abierta_en),
    CHECK (cerrada_en  IS NULL OR cerrada_en  >= abierta_en)
);
CREATE INDEX ix_orden_vehiculo ON orden_mantenimiento (vehiculo_id, abierta_en DESC);
CREATE INDEX ix_orden_estado ON orden_mantenimiento (estado_id);

CREATE TABLE orden_item (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    orden_id        INTEGER NOT NULL REFERENCES orden_mantenimiento(id),
    descripcion     VARCHAR(150) NOT NULL,
    cantidad        NUMERIC(8,2) NOT NULL DEFAULT 1 CHECK (cantidad > 0),
    costo_unitario  NUMERIC(12,2) NOT NULL CHECK (costo_unitario >= 0)
);
CREATE INDEX ix_orden_item_orden ON orden_item (orden_id);

-- ---------- Incidencias ----------

CREATE TABLE incidencia (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehiculo_id     INTEGER NOT NULL REFERENCES vehiculo(id),
    asignacion_id   INTEGER REFERENCES asignacion(id),
    reportada_por   INTEGER NOT NULL REFERENCES usuario(id),
    tipo            VARCHAR(20) NOT NULL
                    CHECK (tipo IN ('FALLA','ACCIDENTE','DANIO','ROBO_PERDIDA','OTRO')),
    severidad       VARCHAR(10) NOT NULL CHECK (severidad IN ('BAJA','MEDIA','ALTA','CRITICA')),
    sistema         VARCHAR(30),                       -- motor, frenos, neumáticos, eléctrico...
    descripcion     VARCHAR(500) NOT NULL,
    ocurrida_en     TIMESTAMPTZ NOT NULL,
    odometro_km     NUMERIC(10,1) CHECK (odometro_km >= 0),
    resuelta_en     TIMESTAMPTZ,
    CHECK (resuelta_en IS NULL OR resuelta_en >= ocurrida_en)
);
CREATE INDEX ix_incidencia_vehiculo ON incidencia (vehiculo_id, ocurrida_en DESC);

ALTER TABLE orden_mantenimiento
    ADD CONSTRAINT fk_orden_incidencia
    FOREIGN KEY (incidencia_id) REFERENCES incidencia(id);

-- ---------- Historiales y auditoría ----------

CREATE TABLE historial_estado_vehiculo (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehiculo_id    INTEGER NOT NULL REFERENCES vehiculo(id),
    estado_ant_id  SMALLINT REFERENCES estado_vehiculo(id),
    estado_nuevo_id SMALLINT NOT NULL REFERENCES estado_vehiculo(id),
    cambiado_en    TIMESTAMPTZ NOT NULL DEFAULT now(),
    usuario_id     INTEGER REFERENCES usuario(id),     -- NULL si lo hizo el sistema
    origen         VARCHAR(20) NOT NULL DEFAULT 'USUARIO'
                   CHECK (origen IN ('USUARIO','SISTEMA')),
    motivo         VARCHAR(200)
);
CREATE INDEX ix_hist_vehiculo ON historial_estado_vehiculo (vehiculo_id, cambiado_en DESC);

CREATE TABLE historial_orden (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    orden_id       INTEGER NOT NULL REFERENCES orden_mantenimiento(id),
    estado_ant_id  SMALLINT REFERENCES estado_orden(id),
    estado_nuevo_id SMALLINT NOT NULL REFERENCES estado_orden(id),
    cambiado_en    TIMESTAMPTZ NOT NULL DEFAULT now(),
    usuario_id     INTEGER REFERENCES usuario(id),
    motivo         VARCHAR(200)
);

CREATE TABLE auditoria_sistema (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    administrador_id INTEGER NOT NULL REFERENCES usuario(id),
    accion         VARCHAR(50)  NOT NULL,
    entidad        VARCHAR(50)  NOT NULL,
    entidad_id     VARCHAR(50),
    motivo         VARCHAR(300) NOT NULL,
    detalle        JSONB,
    ocurrida_en    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Capa predictiva ----------
-- Fase 1 (reglas): se calcula con la vista v_proximo_servicio.
-- Fase 2 (modelo): el job de Python escribe aquí el riesgo estimado.

CREATE TABLE prediccion_mantenimiento (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehiculo_id      INTEGER NOT NULL REFERENCES vehiculo(id),
    plan_id          SMALLINT REFERENCES plan_mantenimiento(id),
    calculada_en     TIMESTAMPTZ NOT NULL DEFAULT now(),
    modelo           VARCHAR(50) NOT NULL,              -- 'reglas-v1', 'riesgo-falla-v1', ...
    fecha_estimada   DATE,
    riesgo_falla_30d NUMERIC(5,4) CHECK (riesgo_falla_30d BETWEEN 0 AND 1),
    detalle          JSONB
);
CREATE INDEX ix_pred_vehiculo ON prediccion_mantenimiento (vehiculo_id, calculada_en DESC);

-- ---------- Reglas automáticas (triggers) ----------
-- Objetivo: que el odómetro se cargue UNA sola vez (en la salida y el retorno)
-- y que un error de tipeo no contamine el modelo predictivo.

-- 1) Una lectura no puede contradecir el orden temporal de las anteriores.
CREATE FUNCTION fn_lectura_validar() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM lectura_uso
               WHERE vehiculo_id = NEW.vehiculo_id
                 AND leida_en <= NEW.leida_en AND odometro_km > NEW.odometro_km) THEN
        RAISE EXCEPTION 'Odómetro % menor que una lectura anterior del vehículo %: revisar posible error de tipeo',
            NEW.odometro_km, NEW.vehiculo_id USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM lectura_uso
               WHERE vehiculo_id = NEW.vehiculo_id
                 AND leida_en >= NEW.leida_en AND odometro_km < NEW.odometro_km) THEN
        RAISE EXCEPTION 'Odómetro % mayor que una lectura posterior del vehículo %: revisar posible error de tipeo',
            NEW.odometro_km, NEW.vehiculo_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER tg_lectura_validar BEFORE INSERT ON lectura_uso
    FOR EACH ROW EXECUTE FUNCTION fn_lectura_validar();

-- 2) El vehículo siempre refleja la lectura más alta registrada.
CREATE FUNCTION fn_lectura_aplicar() RETURNS trigger AS $$
BEGIN
    UPDATE vehiculo
       SET odometro_km = GREATEST(odometro_km, NEW.odometro_km),
           horas_motor = GREATEST(horas_motor, COALESCE(NEW.horas_motor, horas_motor))
     WHERE id = NEW.vehiculo_id;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER tg_lectura_aplicar AFTER INSERT ON lectura_uso
    FOR EACH ROW EXECUTE FUNCTION fn_lectura_aplicar();

-- Utilidad de corrección: si una lectura errónea se eliminó o se corrigió,
-- recalcula el odómetro del vehículo desde sus lecturas. Debe usarla un ADMIN
-- dejando constancia en auditoria_sistema.
CREATE FUNCTION fn_recalcular_odometro(p_vehiculo_id INTEGER) RETURNS void AS $$
    UPDATE vehiculo v
       SET odometro_km = COALESCE((SELECT max(odometro_km) FROM lectura_uso WHERE vehiculo_id = v.id), 0)
     WHERE v.id = p_vehiculo_id;
$$ LANGUAGE sql;

-- 3a) Antes de registrar una salida: el vehículo debe estar disponible.
--     Se bloquea la fila del vehículo para evitar dos salidas simultáneas.
CREATE FUNCTION fn_asignacion_validar() RETURNS trigger AS $$
DECLARE
    v_codigo VARCHAR;
BEGIN
    SELECT e.codigo INTO v_codigo
      FROM vehiculo v JOIN estado_vehiculo e ON e.id = v.estado_id
     WHERE v.id = NEW.vehiculo_id FOR UPDATE OF v;

    IF v_codigo IS DISTINCT FROM 'DISPONIBLE' THEN
        RAISE EXCEPTION 'El vehículo % no está disponible (estado: %)',
            NEW.vehiculo_id, COALESCE(v_codigo, 'inexistente')
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER tg_asignacion_validar BEFORE INSERT ON asignacion
    FOR EACH ROW EXECUTE FUNCTION fn_asignacion_validar();

-- 3b) Después de registrar la salida: genera la lectura y lo marca asignado.
CREATE FUNCTION fn_asignacion_salida() RETURNS trigger AS $$
DECLARE
    v_ant      SMALLINT := (SELECT estado_id FROM vehiculo WHERE id = NEW.vehiculo_id);
    v_asignado SMALLINT := (SELECT id FROM estado_vehiculo WHERE codigo = 'ASIGNADO');
BEGIN
    INSERT INTO lectura_uso (vehiculo_id, leida_en, odometro_km, fuente)
    VALUES (NEW.vehiculo_id, NEW.salida_real, NEW.odometro_salida_km, 'ASIGNACION');

    UPDATE vehiculo SET estado_id = v_asignado, ubicacion_id = NULL WHERE id = NEW.vehiculo_id;

    INSERT INTO historial_estado_vehiculo (vehiculo_id, estado_ant_id, estado_nuevo_id, cambiado_en, origen, motivo)
    VALUES (NEW.vehiculo_id, v_ant, v_asignado, NEW.salida_real, 'SISTEMA', 'Salida, asignación ' || NEW.id);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER tg_asignacion_salida AFTER INSERT ON asignacion
    FOR EACH ROW EXECUTE FUNCTION fn_asignacion_salida();

-- 4) Retorno: genera la lectura y devuelve el vehículo a disponible, salvo
--    que durante el viaje se lo haya puesto fuera de servicio o en mantenimiento.
CREATE FUNCTION fn_asignacion_retorno() RETURNS trigger AS $$
DECLARE
    v_asignado   SMALLINT := (SELECT id FROM estado_vehiculo WHERE codigo = 'ASIGNADO');
    v_disponible SMALLINT := (SELECT id FROM estado_vehiculo WHERE codigo = 'DISPONIBLE');
BEGIN
    INSERT INTO lectura_uso (vehiculo_id, leida_en, odometro_km, fuente)
    VALUES (NEW.vehiculo_id, NEW.retorno_real, NEW.odometro_retorno_km, 'ASIGNACION');

    UPDATE vehiculo SET ubicacion_id = NEW.ubicacion_retorno_id WHERE id = NEW.vehiculo_id;

    UPDATE vehiculo SET estado_id = v_disponible
     WHERE id = NEW.vehiculo_id AND estado_id = v_asignado;
    IF FOUND THEN
        INSERT INTO historial_estado_vehiculo (vehiculo_id, estado_ant_id, estado_nuevo_id, cambiado_en, origen, motivo)
        VALUES (NEW.vehiculo_id, v_asignado, v_disponible, NEW.retorno_real, 'SISTEMA', 'Retorno, asignación ' || NEW.id);
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER tg_asignacion_retorno AFTER UPDATE ON asignacion
    FOR EACH ROW WHEN (OLD.retorno_real IS NULL AND NEW.retorno_real IS NOT NULL)
    EXECUTE FUNCTION fn_asignacion_retorno();

-- 5) Anulación de una salida cargada por error: el vehículo vuelve a su lugar.
--    La lectura de odómetro de la salida NO se borra sola: si era errónea, un
--    ADMIN la corrige y ejecuta fn_recalcular_odometro().
CREATE FUNCTION fn_asignacion_anulacion() RETURNS trigger AS $$
DECLARE
    v_asignado   SMALLINT := (SELECT id FROM estado_vehiculo WHERE codigo = 'ASIGNADO');
    v_disponible SMALLINT := (SELECT id FROM estado_vehiculo WHERE codigo = 'DISPONIBLE');
BEGIN
    UPDATE vehiculo SET estado_id = v_disponible, ubicacion_id = NEW.ubicacion_salida_id
     WHERE id = NEW.vehiculo_id AND estado_id = v_asignado;
    IF FOUND THEN
        INSERT INTO historial_estado_vehiculo (vehiculo_id, estado_ant_id, estado_nuevo_id, cambiado_en, origen, motivo)
        VALUES (NEW.vehiculo_id, v_asignado, v_disponible, NEW.anulada_en, 'SISTEMA', 'Anulación, asignación ' || NEW.id);
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER tg_asignacion_anulacion AFTER UPDATE ON asignacion
    FOR EACH ROW WHEN (OLD.anulada_en IS NULL AND NEW.anulada_en IS NOT NULL)
    EXECUTE FUNCTION fn_asignacion_anulacion();

-- ---------- Vistas ----------

-- Última lectura de cada vehículo y antigüedad. Con carga manual, una lectura
-- vieja hace poco confiable cualquier predicción: conviene mirarla siempre.
CREATE VIEW v_ultima_lectura AS
SELECT DISTINCT ON (l.vehiculo_id)
    l.vehiculo_id,
    l.leida_en,
    l.odometro_km,
    l.fuente,
    (current_date - l.leida_en::date) AS dias_sin_lectura
FROM lectura_uso l
ORDER BY l.vehiculo_id, l.leida_en DESC;

-- Lecturas con un salto implausible respecto de la anterior (posible tipeo).
-- Umbral: más de 1000 km/día, con un piso de medio día para no marcar
-- viajes largos legítimos. Ajustable según la flota.
CREATE VIEW v_lecturas_sospechosas AS
WITH l AS (
    SELECT id, vehiculo_id, leida_en, odometro_km, fuente,
           lag(leida_en)    OVER w AS leida_ant,
           lag(odometro_km) OVER w AS km_ant
    FROM lectura_uso
    WINDOW w AS (PARTITION BY vehiculo_id ORDER BY leida_en)
)
SELECT id AS lectura_id, vehiculo_id, leida_en, odometro_km, fuente,
       odometro_km - km_ant AS salto_km,
       round((odometro_km - km_ant)
             / GREATEST(EXTRACT(EPOCH FROM (leida_en - leida_ant)) / 86400.0, 0.5), 0) AS km_por_dia
FROM l
WHERE km_ant IS NOT NULL
  AND (odometro_km - km_ant)
      / GREATEST(EXTRACT(EPOCH FROM (leida_en - leida_ant)) / 86400.0, 0.5) > 1000;

-- Ritmo de uso: km por día de cada vehículo en los últimos 90 días,
-- a partir de las lecturas de odómetro.
CREATE VIEW v_ritmo_uso AS
SELECT
    v.id AS vehiculo_id,
    CASE
        WHEN max(l.leida_en) = min(l.leida_en) THEN NULL
        ELSE (max(l.odometro_km) - min(l.odometro_km))
             / GREATEST(EXTRACT(EPOCH FROM (max(l.leida_en) - min(l.leida_en))) / 86400.0, 1)
    END AS km_por_dia
FROM vehiculo v
JOIN lectura_uso l ON l.vehiculo_id = v.id
WHERE l.leida_en >= now() - interval '90 days'
GROUP BY v.id;

-- Último servicio de cada plan por vehículo (órdenes preventivas cerradas).
CREATE VIEW v_ultimo_servicio AS
SELECT DISTINCT ON (o.vehiculo_id, o.plan_id)
    o.vehiculo_id,
    o.plan_id,
    o.cerrada_en,
    o.odometro_km,
    o.horas_motor
FROM orden_mantenimiento o
JOIN estado_orden e ON e.id = o.estado_id
WHERE o.plan_id IS NOT NULL
  AND e.codigo = 'CERRADA'
  AND o.cerrada_en IS NOT NULL
ORDER BY o.vehiculo_id, o.plan_id, o.cerrada_en DESC;

-- Próximo servicio por vehículo y plan (fase 1, basado en reglas).
-- Vence lo que ocurra primero: km, días u horas. La fecha por km se estima
-- con el ritmo de uso real del vehículo.
CREATE VIEW v_proximo_servicio AS
SELECT
    v.id                         AS vehiculo_id,
    v.identificador,
    p.id                         AS plan_id,
    p.tarea,
    COALESCE(u.odometro_km, 0)   AS km_ultimo_servicio,
    v.odometro_km                AS km_actual,
    CASE WHEN p.cada_km IS NOT NULL
         THEN COALESCE(u.odometro_km, 0) + p.cada_km - v.odometro_km END AS km_restantes,
    CASE WHEN p.cada_dias IS NOT NULL
         THEN (COALESCE(u.cerrada_en::date, v.fecha_alta) + p.cada_dias) - current_date END AS dias_restantes,
    CASE WHEN p.cada_horas IS NOT NULL
         THEN COALESCE(u.horas_motor, 0) + p.cada_horas - v.horas_motor END AS horas_restantes,
    r.km_por_dia,
    ul.dias_sin_lectura,
    LEAST(
        CASE WHEN p.cada_dias IS NOT NULL
             THEN COALESCE(u.cerrada_en::date, v.fecha_alta) + p.cada_dias END,
        CASE WHEN p.cada_km IS NOT NULL AND COALESCE(r.km_por_dia, 0) > 0
             THEN current_date + CEIL(
                    GREATEST(COALESCE(u.odometro_km, 0) + p.cada_km - v.odometro_km, 0)
                    / r.km_por_dia)::int END
    ) AS fecha_estimada
FROM vehiculo v
JOIN estado_vehiculo ev ON ev.id = v.estado_id AND ev.codigo <> 'BAJA'
JOIN plan_mantenimiento p ON p.tipo_vehiculo_id = v.tipo_vehiculo_id AND p.activo
LEFT JOIN v_ultimo_servicio u ON u.vehiculo_id = v.id AND u.plan_id = p.id
LEFT JOIN v_ritmo_uso r ON r.vehiculo_id = v.id
LEFT JOIN v_ultima_lectura ul ON ul.vehiculo_id = v.id;

-- Costo de mantenimiento por vehículo y por km recorrido.
-- El costo por km se calcula solo sobre el período que el sistema tiene
-- registrado: costo de las órdenes cerradas desde la primera lectura, dividido
-- por los km entre la primera y la última lectura. No se usa el odómetro total
-- del vehículo, que incluye años anteriores al sistema.
CREATE VIEW v_costo_por_vehiculo AS
WITH rango AS (
    SELECT vehiculo_id,
           min(leida_en)                       AS desde,
           max(odometro_km) - min(odometro_km) AS km_registrados
    FROM lectura_uso
    GROUP BY vehiculo_id
)
SELECT
    v.id AS vehiculo_id,
    v.identificador,
    count(o.id) FILTER (WHERE t.codigo = 'CORRECTIVO')  AS ordenes_correctivas,
    count(o.id) FILTER (WHERE t.codigo = 'PREVENTIVO')  AS ordenes_preventivas,
    COALESCE(sum(o.costo_total), 0)                     AS costo_total,
    r.km_registrados,
    CASE WHEN r.km_registrados > 0
         THEN round(COALESCE(sum(o.costo_total) FILTER (WHERE o.cerrada_en >= r.desde), 0)
                    / r.km_registrados, 2) END          AS costo_por_km
FROM vehiculo v
LEFT JOIN rango r ON r.vehiculo_id = v.id
LEFT JOIN orden_mantenimiento o ON o.vehiculo_id = v.id
LEFT JOIN tipo_mantenimiento t ON t.id = o.tipo_id
GROUP BY v.id, v.identificador, r.km_registrados, r.desde;

-- ---------- Datos iniciales de catálogos ----------

INSERT INTO rol (codigo, nombre) VALUES
    ('ADMIN',    'Administrador'),
    ('GESTOR',   'Gestor de flota'),
    ('TECNICO',  'Técnico de mantenimiento'),
    ('CONDUCTOR','Conductor / responsable');

INSERT INTO estado_vehiculo (codigo, nombre) VALUES
    ('DISPONIBLE',        'Disponible'),
    ('ASIGNADO',          'Asignado'),
    ('EN_MANTENIMIENTO',  'En mantenimiento'),
    ('FUERA_DE_SERVICIO', 'Fuera de servicio'),
    ('BAJA',              'Baja definitiva');

INSERT INTO estado_orden (codigo, nombre) VALUES
    ('ABIERTA',    'Abierta'),
    ('ASIGNADA',   'Asignada'),
    ('EN_CURSO',   'En curso'),
    ('CERRADA',    'Cerrada'),
    ('CANCELADA',  'Cancelada');

INSERT INTO tipo_mantenimiento (codigo, nombre) VALUES
    ('PREVENTIVO', 'Preventivo'),
    ('CORRECTIVO', 'Correctivo');
