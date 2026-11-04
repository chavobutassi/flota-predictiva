#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generador de datos sintéticos para la flota con mantenimiento predictivo.

Produce un script SQL (solo INSERT/UPDATE) que se ejecuta DESPUÉS de db/schema.sql
y reproduce, a través de las mismas operaciones que haría la aplicación, dos años
de vida de una flota ficticia: salidas y retornos con odómetro manual, servicios
preventivos con demoras, incidencias, órdenes correctivas, altas y bajas.

Los triggers del esquema generan las lecturas de odómetro y los cambios de estado
de las salidas y retornos; este script solo agrega lo que haría un usuario.

El proceso generador de fallas es deliberadamente conocido (y documentado en el
README) para poder evaluar luego un modelo predictivo contra una verdad conocida:

  * La tasa de incidencias por km aumenta con la edad del vehículo.
  * Aumenta con el atraso de los servicios preventivos (la señal principal).
  * El sistema que falla depende de qué servicio está atrasado.
  * Hay una fragilidad propia de cada vehículo (unos fallan más que otros).
  * En invierno la tasa es algo mayor.

Uso:
    python3 generar_datos.py --hasta 2026-11-04T12:00 --salida seed.sql
    python3 generar_datos.py --salida seed.sql.gz        # comprime si termina en .gz

Solo usa la biblioteca estándar de Python 3.8+.
Todos los datos son ficticios: nombres, vehículos, costos y ubicaciones.
"""
import argparse
import gzip
import json
import math
import random
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, date, time, timedelta

# --------------------------------------------------------------------------------------
# Identificadores de catálogos: coinciden con los que crea db/schema.sql (orden de INSERT)
# --------------------------------------------------------------------------------------
EST_DISPONIBLE, EST_ASIGNADO, EST_MANT, EST_FUERA, EST_BAJA = 1, 2, 3, 4, 5
ORD_ABIERTA, ORD_ASIGNADA, ORD_CURSO, ORD_CERRADA, ORD_CANCELADA = 1, 2, 3, 4, 5
TM_PREV, TM_CORR = 1, 2
ROL_ADMIN, ROL_GESTOR, ROL_TECNICO, ROL_CONDUCTOR = 1, 2, 3, 4
LOC_BASE, LOC_NORTE, LOC_SUR, LOC_TALLER, LOC_EXTERNO = 1, 2, 3, 4, 5

ADMIN_ID = 1
GESTORES = [2, 3]
TECNICOS = [4, 5, 6]
CONDUCTORES = list(range(7, 17))

# --------------------------------------------------------------------------------------
# Datos de referencia (todo ficticio)
# --------------------------------------------------------------------------------------
USUARIOS = [
    (1, 'Marcelo', 'Ibáñez', [ROL_ADMIN]),
    (2, 'Gabriela', 'Sosa', [ROL_GESTOR]),
    (3, 'Rubén', 'Aguirre', [ROL_GESTOR]),
    (4, 'Diego', 'Pereyra', [ROL_TECNICO]),
    (5, 'Nicolás', 'Ferreyra', [ROL_TECNICO]),
    (6, 'Sergio', 'Villalba', [ROL_TECNICO]),
    (7, 'Carlos', 'Medina', [ROL_CONDUCTOR]),
    (8, 'Lucía', 'Romero', [ROL_CONDUCTOR]),
    (9, 'Javier', 'Acosta', [ROL_CONDUCTOR]),
    (10, 'Mariano', 'Luna', [ROL_CONDUCTOR]),
    (11, 'Paula', 'Giménez', [ROL_CONDUCTOR]),
    (12, 'Hernán', 'Cabrera', [ROL_CONDUCTOR]),
    (13, 'Silvia', 'Ortiz', [ROL_CONDUCTOR]),
    (14, 'Federico', 'Ríos', [ROL_CONDUCTOR]),
    (15, 'Andrea', 'Molina', [ROL_CONDUCTOR]),
    (16, 'Tomás', 'Vera', [ROL_CONDUCTOR]),
]

UBICACIONES = [
    (LOC_BASE, 'Base Central', 'BASE'),
    (LOC_NORTE, 'Destacamento Norte', 'DESTACAMENTO'),
    (LOC_SUR, 'Destacamento Sur', 'DESTACAMENTO'),
    (LOC_TALLER, 'Taller Central', 'TALLER'),
    (LOC_EXTERNO, 'Taller Externo', 'TALLER'),
]

# Parámetros por tipo de vehículo.
#   anual: km anuales típicos; viaje_med/viaje_sig: mediana y sigma (lognormal) del km por salida;
#   vel: velocidad media (km/h); tasa: incidencias por 10.000 km a edad 0 y sin atrasos;
#   costo: multiplicador de costos correctivos; pm / pm_tire: multiplicador de repuestos.
TIPOS = {
    1: dict(codigo='PICKUP_4X4', nombre='Pick-up 4x4', cap=5, n=13, anual=28000, viaje_med=95,
            viaje_sig=0.8, vel=48, tasa=0.9, costo=1.0, pm=1.0, pm_tire=1.0, litros=8, mo=1.5,
            modelos=[('Toyota', 'Hilux 4x4'), ('Ford', 'Ranger 4x4'), ('Volkswagen', 'Amarok V6'),
                     ('Nissan', 'Frontier 4x4')]),
    2: dict(codigo='UTILITARIO', nombre='Utilitario liviano', cap=3, n=8, anual=22000, viaje_med=60,
            viaje_sig=0.75, vel=42, tasa=0.8, costo=0.6, pm=0.7, pm_tire=0.55, litros=5, mo=1.2,
            modelos=[('Renault', 'Kangoo Express'), ('Fiat', 'Fiorino Fire'),
                     ('Peugeot', 'Partner Furgón'), ('Citroën', 'Berlingo')]),
    3: dict(codigo='FURGON', nombre='Furgón / minibús', cap=12, n=6, anual=30000, viaje_med=110,
            viaje_sig=0.8, vel=52, tasa=1.0, costo=1.3, pm=1.2, pm_tire=1.2, litros=10, mo=1.8,
            modelos=[('Mercedes-Benz', 'Sprinter 515'), ('Renault', 'Master Minibús'),
                     ('Iveco', 'Daily Minibús')]),
    4: dict(codigo='CAMION_LIVIANO', nombre='Camión liviano', cap=3, n=7, anual=25000, viaje_med=120,
            viaje_sig=0.8, vel=50, tasa=1.1, costo=1.6, pm=1.5, pm_tire=1.8, litros=12, mo=2.0,
            modelos=[('Iveco', 'Daily 70C16'), ('Mercedes-Benz', 'Accelo 1016'),
                     ('Volkswagen', 'Delivery 9.170')]),
    5: dict(codigo='CAMION_PESADO', nombre='Camión pesado', cap=3, n=6, anual=35000, viaje_med=190,
            viaje_sig=0.75, vel=58, tasa=1.5, costo=3.0, pm=2.2, pm_tire=2.6, litros=24, mo=3.0,
            modelos=[('Mercedes-Benz', 'Atego 1726'), ('Iveco', 'Tector 170E22'),
                     ('Volkswagen', 'Constellation 24.280')]),
}
PREFIJO = {1: 'PU', 2: 'UT', 3: 'FU', 4: 'CL', 5: 'CP'}

# Planes de mantenimiento: (tarea, cada_km, cada_dias, clave interna)
PLANES = {
    1: [('Cambio de aceite y filtro', 10000, 180, 'aceite'),
        ('Rotación y revisión de neumáticos', 15000, None, 'neumaticos'),
        ('Revisión de frenos', 20000, 365, 'frenos'),
        ('Filtros de aire y combustible', 30000, None, 'filtros'),
        ('Servicio mayor', 60000, None, 'mayor'),
        ('Revisión de batería y sistema eléctrico', None, 365, 'bateria')],
    2: [('Cambio de aceite y filtro', 10000, 180, 'aceite'),
        ('Rotación y revisión de neumáticos', 15000, None, 'neumaticos'),
        ('Revisión de frenos', 20000, 365, 'frenos'),
        ('Filtros de aire y combustible', 30000, None, 'filtros'),
        ('Servicio mayor', 60000, None, 'mayor'),
        ('Revisión de batería y sistema eléctrico', None, 365, 'bateria')],
    3: [('Cambio de aceite y filtro', 15000, 180, 'aceite'),
        ('Rotación y revisión de neumáticos', 20000, None, 'neumaticos'),
        ('Revisión de frenos', 25000, 365, 'frenos'),
        ('Filtros de aire y combustible', 30000, None, 'filtros'),
        ('Servicio mayor', 90000, None, 'mayor'),
        ('Revisión de batería y sistema eléctrico', None, 365, 'bateria')],
    4: [('Cambio de aceite y filtro', 15000, 180, 'aceite'),
        ('Rotación y revisión de neumáticos', 30000, None, 'neumaticos'),
        ('Revisión de frenos', 30000, 365, 'frenos'),
        ('Filtros de aire y combustible', 30000, None, 'filtros'),
        ('Engrase y tren delantero', 20000, None, 'engrase'),
        ('Servicio mayor', 120000, None, 'mayor'),
        ('Revisión de batería y sistema eléctrico', None, 365, 'bateria')],
    5: [('Cambio de aceite y filtro', 20000, 180, 'aceite'),
        ('Rotación y revisión de neumáticos', 40000, None, 'neumaticos'),
        ('Revisión de frenos', 30000, 365, 'frenos'),
        ('Filtros de aire y combustible', 30000, None, 'filtros'),
        ('Engrase y tren delantero', 20000, None, 'engrase'),
        ('Servicio mayor', 150000, None, 'mayor'),
        ('Revisión de batería y sistema eléctrico', None, 365, 'bateria')],
}
DUR_PREV = {'aceite': (2, 3), 'neumaticos': (3, 5), 'frenos': (5, 9), 'filtros': (1.5, 2.5),
            'engrase': (2.5, 4), 'mayor': (24, 60), 'bateria': (1, 2)}

SIST_PESO = {'motor': .25, 'frenos': .12, 'neumaticos': .20, 'electrico': .12,
             'transmision': .06, 'suspension': .10, 'refrigeracion': .05, 'carroceria': .10}
SEVERIDAD = {  # BAJA, MEDIA, ALTA, CRITICA
    'motor': (.20, .35, .30, .15), 'transmision': (.20, .35, .30, .15),
    'frenos': (.25, .35, .30, .10), 'neumaticos': (.60, .30, .09, .01),
    'electrico': (.45, .35, .17, .03), 'carroceria': (.50, .35, .13, .02),
    'suspension': (.30, .40, .25, .05), 'refrigeracion': (.30, .35, .27, .08),
    'accidente': (.15, .40, .35, .10),
}
DESCRIPCIONES = {
    'motor': ['Recalentamiento del motor en ruta', 'Pérdida de potencia y humo oscuro',
              'Ruido anormal en el motor', 'Pérdida de aceite', 'Falla de arranque en frío'],
    'frenos': ['Frenos con vibración', 'Pastillas desgastadas, frenado deficiente',
               'Pérdida de líquido de frenos'],
    'neumaticos': ['Pinchadura de neumático', 'Neumático con corte lateral',
                   'Desgaste irregular de neumáticos'],
    'electrico': ['Batería descargada', 'Falla de alternador', 'Luces traseras sin funcionar',
                  'Falla del sistema de arranque'],
    'transmision': ['Embrague patina', 'Dificultad al cambiar de marcha', 'Ruido en la caja de cambios'],
    'suspension': ['Amortiguador roto', 'Ruido en el tren delantero', 'Rotura de elástico'],
    'refrigeracion': ['Pérdida de refrigerante', 'Falla del ventilador del radiador',
                      'Manguera del radiador rota'],
    'carroceria': ['Parabrisas astillado por piedra', 'Puerta con falla en cerradura',
                   'Rotura de espejo retrovisor'],
    'accidente': ['Colisión leve en maniobra', 'Vuelco parcial en camino de ripio',
                  'Choque con animal en ruta'],
}
REPUESTOS = {
    'motor': ['Juego de juntas y sellos', 'Bomba de agua', 'Inyectores', 'Turbocompresor',
              'Sensores y cableado del motor'],
    'frenos': ['Pastillas de freno', 'Discos de freno', 'Cilindros y cañerías de freno', 'Líquido y purga'],
    'neumaticos': ['Neumático (unidad)', 'Válvulas y reparación'],
    'electrico': ['Batería', 'Alternador', 'Motor de arranque', 'Cableado e iluminación'],
    'transmision': ['Kit de embrague', 'Rodamientos de caja', 'Aceite de caja y retenes'],
    'suspension': ['Amortiguadores (juego)', 'Elásticos y resortes', 'Rótulas y bujes'],
    'refrigeracion': ['Radiador', 'Mangueras y abrazaderas', 'Termostato y ventilador'],
    'carroceria': ['Parabrisas y vidrios', 'Cerradura y mecanismos', 'Espejos y plásticos',
                   'Chapa y pintura'],
}
COSTO_BASE = {  # mediana y sigma (lognormal), pesos de nov-2024
    'motor': (1200000, .7), 'transmision': (1000000, .6), 'frenos': (350000, .4),
    'neumaticos': (220000, .5), 'electrico': (180000, .6), 'suspension': (400000, .6),
    'refrigeracion': (260000, .5), 'carroceria': (300000, .7),
}
SEV_MULT = {'BAJA': .25, 'MEDIA': .6, 'ALTA': 1.4, 'CRITICA': 3.0}
MOTIVOS = {
    'liviano': ['Traslado de personal', 'Recorrida de caminos', 'Trámites y gestiones',
                'Inspección de obra', 'Apoyo a destacamento'],
    'carga': ['Traslado de carga', 'Reparto de insumos', 'Apoyo a destacamento',
              'Traslado de materiales'],
    'personal': ['Traslado de personal', 'Comisión de servicio', 'Capacitación en otra localidad'],
}
INFL_MENSUAL = 1.022   # inflación mensual supuesta para actualizar costos en pesos


# --------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------
def sq(s):
    return "'" + str(s).replace("'", "''") + "'"


def tsq(dt):
    return "'" + dt.strftime('%Y-%m-%d %H:%M:%S') + "-03'"


def tsn(dt, END):
    return tsq(dt) if (dt is not None and dt <= END) else 'NULL'


def slug(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    return s.lower().replace(' ', '')


def r100(x):
    return int(round(x / 100.0)) * 100


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def inflacion(dt, base):
    meses = (dt - base).days / 30.44
    return INFL_MENSUAL ** meses


def agregar_horas_laborales(inicio, horas):
    """Suma horas de trabajo de taller (lunes a viernes, de 8 a 18 h)."""
    cur, rest = inicio, horas
    while True:
        while cur.weekday() >= 5:
            cur = datetime.combine(cur.date() + timedelta(days=1), time(8, 0))
        if cur.hour < 8:
            cur = cur.replace(hour=8, minute=0, second=0)
        fin = datetime.combine(cur.date(), time(18, 0))
        disp = (fin - cur).total_seconds() / 3600
        if disp <= 0:
            cur = datetime.combine(cur.date() + timedelta(days=1), time(8, 0))
            continue
        if rest <= disp:
            return cur + timedelta(hours=rest)
        rest -= disp
        cur = datetime.combine(cur.date() + timedelta(days=1), time(8, 0))


def proximo_dia_habil(dt, rng):
    """Primer momento hábil de ingreso a taller a partir de dt (8:00 a 10:30)."""
    if dt.hour >= 15:
        dt = datetime.combine(dt.date() + timedelta(days=1), time(8, 0))
    elif dt.hour < 8:
        dt = datetime.combine(dt.date(), time(8, 0))
    while dt.weekday() >= 5:
        dt = datetime.combine(dt.date() + timedelta(days=1), time(8, 0))
    base = datetime.combine(dt.date(), time(8, 0))
    return max(dt, base + timedelta(minutes=rng.randint(0, 150)))


# --------------------------------------------------------------------------------------
# Modelo en memoria
# --------------------------------------------------------------------------------------
class Plan:
    def __init__(self, pid, tid, tarea, km, dias, clave):
        self.id, self.tid, self.tarea, self.km, self.dias, self.clave = pid, tid, tarea, km, dias, clave


class EstadoPlan:
    def __init__(self, km, fecha, delay):
        self.km, self.fecha, self.delay = km, fecha, delay


class Vehiculo:
    pass


class Contexto:
    def __init__(self, END, START, seed):
        self.END, self.START, self.seed = END, START, seed
        self.seq = 0
        self.eventos = []
        self.sig_asig = 1
        self.sig_orden = 1
        self.sig_inc = 1
        self.stats = defaultdict(float)
        self.expo = defaultdict(lambda: [0.0, 0])   # bucket de atraso -> [km expuestos, incidencias]

    def emit(self, t, sql):
        if t > self.END:
            return
        self.seq += 1
        self.eventos.append((t, self.seq, sql))


def draw_delay(rng):
    """Atraso tolerado antes de que el taller haga el servicio (fracción del intervalo)."""
    u = rng.random()
    if u < .55:
        return rng.uniform(0, .08)      # al día
    if u < .80:
        return rng.uniform(.08, .30)    # demora moderada
    if u < .95:
        return rng.uniform(.30, .80)    # servicio descuidado
    return rng.uniform(.80, 1.50)       # abandono: casi el doble del intervalo


# --------------------------------------------------------------------------------------
# Costos e ítems
# --------------------------------------------------------------------------------------
def items_preventivo(rng, tid, clave, infl):
    T = TIPOS[tid]
    pm = T['pm']
    ruido = lambda: rng.uniform(.93, 1.08)
    mo = lambda h: ('Mano de obra (horas)', h, r100(26000 * infl * ruido()))
    it = []
    if clave == 'aceite':
        it = [('Aceite de motor (litros)', T['litros'], r100(15000 * infl * pm ** .3 * ruido())),
              ('Filtro de aceite', 1, r100(28000 * infl * pm * ruido())), mo(T['mo'])]
    elif clave == 'neumaticos':
        it = [('Balanceo y alineación', 1, r100(55000 * infl * pm * ruido())), mo(2)]
        if rng.random() < .22:
            qty = rng.choice([2, 4]) if tid < 4 else rng.choice([2, 6])
            it.append(('Neumático (reposición)', qty, r100(380000 * infl * T['pm_tire'] * ruido())))
    elif clave == 'frenos':
        it = [('Pastillas de freno (juego)', 1, r100(95000 * infl * pm * ruido())), mo(3)]
        if rng.random() < .30:
            it.append(('Discos de freno (par)', 1, r100(260000 * infl * pm * ruido())))
    elif clave == 'filtros':
        it = [('Filtro de aire', 1, r100(38000 * infl * pm * ruido())),
              ('Filtro de combustible', 1, r100(52000 * infl * pm * ruido())), mo(1.5)]
    elif clave == 'engrase':
        it = [('Grasa y kit de engrase', 1, r100(30000 * infl * pm * ruido())), mo(2.5)]
        if rng.random() < .25:
            it.append(('Rótulas y bujes del tren delantero', 1, r100(150000 * infl * pm * ruido())))
    elif clave == 'mayor':
        if tid <= 2:
            it = [('Kit de distribución', 1, r100(360000 * infl * pm * ruido())),
                  ('Bomba de agua', 1, r100(110000 * infl * pm * ruido())),
                  ('Correa de accesorios', 1, r100(70000 * infl * pm * ruido())),
                  ('Líquido refrigerante', 1, r100(25000 * infl * ruido())), mo(8)]
        else:
            it = [('Kit de embrague', 1, r100(520000 * infl * pm * ruido())),
                  ('Líquido de transmisión', 1, r100(60000 * infl * pm * ruido())),
                  ('Revisión de inyectores', 1, r100(180000 * infl * pm * ruido())),
                  mo({3: 10, 4: 12, 5: 14}[tid])]
    elif clave == 'bateria':
        if rng.random() < .6:
            it = [('Batería', 1, r100(250000 * infl * pm * ruido())), mo(1)]
        else:
            it = [('Revisión de batería y alternador', 1, r100(40000 * infl * ruido())), mo(1)]
    lo, hi = DUR_PREV[clave]
    return it, rng.uniform(lo, hi)


def items_correctivo(rng, tid, sistema, sev, infl, dur_horas):
    T = TIPOS[tid]
    med, sig = COSTO_BASE[sistema]
    costo = rng.lognormvariate(math.log(med), sig) * SEV_MULT[sev] * T['costo'] * infl
    horas = max(1, int(round(dur_horas * 0.5)))
    unit_mo = r100(30000 * infl * (1 + .15 * (T['costo'] - 1)) * rng.uniform(.95, 1.05))
    partes = rng.sample(REPUESTOS[sistema], min(rng.randint(1, 3), len(REPUESTOS[sistema])))
    pesos = [rng.uniform(.5, 1.5) for _ in partes]
    tot_p = costo * .65
    items = []
    for nombre, w in zip(partes, pesos):
        qty = rng.choice([1, 1, 2]) if sistema in ('neumaticos', 'frenos') else 1
        unit = max(1000, r100(tot_p * w / sum(pesos) / qty))
        items.append((nombre, qty, unit))
    items.append(('Mano de obra (horas)', horas, unit_mo))
    return items


# --------------------------------------------------------------------------------------
# Emisión de entidades
# --------------------------------------------------------------------------------------
def emitir_orden(ctx, v, oid, tipo, plan_id, inc_id, tecnico, taller, desc, abierta, asignada,
                 iniciada, cerrada, odo_srv, items, gestor):
    END = ctx.END
    total = sum(q * u for _, q, u in items)
    if cerrada <= END:
        est = ORD_CERRADA
    elif iniciada <= END:
        est = ORD_CURSO
    elif asignada <= END:
        est = ORD_ASIGNADA
    else:
        est = ORD_ABIERTA
    ctx.emit(abierta,
             "INSERT INTO orden_mantenimiento (id, vehiculo_id, tipo_id, estado_id, plan_id, incidencia_id, "
             "asignado_a_id, taller_id, descripcion, abierta_en, iniciada_en, cerrada_en, odometro_km, costo_total) "
             f"OVERRIDING SYSTEM VALUE VALUES ({oid}, {v.id}, {tipo}, {est}, "
             f"{plan_id if plan_id else 'NULL'}, {inc_id if inc_id else 'NULL'}, "
             f"{tecnico if asignada <= END else 'NULL'}, {taller}, {sq(desc)}, {tsq(abierta)}, "
             f"{tsn(iniciada, END)}, {tsn(cerrada, END)}, "
             f"{odo_srv if iniciada <= END else 'NULL'}, {total if cerrada <= END else 0});")
    hist = [(None, ORD_ABIERTA, abierta, None, 'Orden generada'),
            (ORD_ABIERTA, ORD_ASIGNADA, asignada, gestor, 'Asignada a técnico'),
            (ORD_ASIGNADA, ORD_CURSO, iniciada, tecnico, 'Inicio del trabajo'),
            (ORD_CURSO, ORD_CERRADA, cerrada, tecnico, 'Trabajo terminado')]
    for ant, nue, t, usu, mot in hist:
        ctx.emit(t, "INSERT INTO historial_orden (orden_id, estado_ant_id, estado_nuevo_id, cambiado_en, "
                    f"usuario_id, motivo) VALUES ({oid}, {'NULL' if ant is None else ant}, {nue}, {tsq(t)}, "
                    f"{usu if usu else 'NULL'}, {sq(mot)});")
    for desc_i, qty, unit in items:
        ctx.emit(cerrada, "INSERT INTO orden_item (orden_id, descripcion, cantidad, costo_unitario) "
                          f"VALUES ({oid}, {sq(desc_i)}, {qty}, {unit});")


def cambiar_estado(ctx, v, t, nuevo, usuario, motivo, ubic=None, origen='USUARIO', extra=''):
    ant = v.estado
    sets = f"estado_id = {nuevo}"
    if ubic is not None:
        sets += f", ubicacion_id = {ubic}"
        v.loc = ubic
    ctx.emit(t, f"UPDATE vehiculo SET {sets}{extra} WHERE id = {v.id};")
    ctx.emit(t, "INSERT INTO historial_estado_vehiculo (vehiculo_id, estado_ant_id, estado_nuevo_id, "
                f"cambiado_en, usuario_id, origen, motivo) VALUES ({v.id}, {ant}, {nuevo}, {tsq(t)}, "
                f"{usuario if usuario else 'NULL'}, {sq(origen)}, {sq(motivo)});")
    v.estado = nuevo


def auditar(ctx, t, accion, entidad, entidad_id, motivo, detalle):
    ctx.emit(t, "INSERT INTO auditoria_sistema (administrador_id, accion, entidad, entidad_id, motivo, "
                f"detalle, ocurrida_en) VALUES ({ADMIN_ID}, {sq(accion)}, {sq(entidad)}, {sq(entidad_id)}, "
                f"{sq(motivo)}, {sq(json.dumps(detalle, ensure_ascii=False))}::jsonb, {tsq(t)});")


# --------------------------------------------------------------------------------------
# Atraso de mantenimiento (la señal que usará el modelo)
# --------------------------------------------------------------------------------------
def excesos(v, d, odo):
    """Atraso relativo (0 = al día) por clave de plan, acotado a [0, 1.5]."""
    out = {}
    for p in v.planes:
        st = v.plan_state[p.id]
        r = 0.0
        if p.km:
            r = max(r, (odo - st.km) / p.km - 1)
        if p.dias:
            r = max(r, (d - st.fecha).days / p.dias - 1)
        out[p.clave] = clip(r, 0, 1.5)
    return out


def elegir_sistema(rng, ex, invierno):
    w = dict(SIST_PESO)
    w['motor'] *= math.exp(1.5 * ex.get('aceite', 0) + .6 * ex.get('filtros', 0) + .5 * ex.get('mayor', 0))
    w['frenos'] *= math.exp(1.6 * ex.get('frenos', 0))
    w['neumaticos'] *= math.exp(1.0 * ex.get('neumaticos', 0))
    w['electrico'] *= math.exp(1.2 * ex.get('bateria', 0)) * (1.5 if invierno else 1.0)
    w['suspension'] *= math.exp(1.0 * ex.get('engrase', 0))
    w['transmision'] *= math.exp(.5 * ex.get('mayor', 0))
    return rng.choices(list(w), weights=list(w.values()))[0]


# --------------------------------------------------------------------------------------
# Simulación de un vehículo
# --------------------------------------------------------------------------------------
def servicios_vencidos(v, d, odo):
    vencidos, cercanos = [], []
    for p in v.planes:
        st = v.plan_state[p.id]
        r = 0.0
        if p.km:
            r = max(r, (odo - st.km) / p.km)
        if p.dias:
            r = max(r, (d - st.fecha).days / p.dias)
        if r >= 1 + st.delay:
            vencidos.append(p)
        elif r >= .9:
            cercanos.append(p)
    return vencidos, cercanos


def hacer_visita(ctx, v, d, vs, tareas, odo):
    rng, END = v.rng, ctx.END
    claves = [p.clave for p in tareas]
    taller = LOC_EXTERNO if (('mayor' in claves and rng.random() < .4) or rng.random() < .10) else LOC_TALLER
    tecnico, gestor = rng.choice(TECNICOS), rng.choice(GESTORES)
    abierta = datetime.combine(d, time(7, 0)) + timedelta(minutes=rng.randint(0, 40))
    asignada = abierta + timedelta(minutes=rng.randint(10, 50))
    infl = inflacion(vs, ctx.base_infl)
    cambiar_estado(ctx, v, vs, EST_MANT, tecnico, 'Ingreso a taller por mantenimiento preventivo', ubic=taller)
    fin = vs
    for p in tareas:
        items, dur = items_preventivo(rng, v.tid, p.clave, infl)
        iniciada = max(vs + timedelta(minutes=rng.randint(10, 40)), asignada + timedelta(minutes=5))
        cerrada = agregar_horas_laborales(iniciada, dur)
        fin = max(fin, cerrada)
        oid = ctx.sig_orden
        ctx.sig_orden += 1
        emitir_orden(ctx, v, oid, TM_PREV, p.id, None, tecnico, taller, f"Servicio preventivo: {p.tarea}",
                     abierta, asignada, iniciada, cerrada, odo, items, gestor)
        v.plan_state[p.id] = EstadoPlan(odo, cerrada.date(), draw_delay(rng))
        ctx.stats['ordenes_prev'] += 1
    cambiar_estado(ctx, v, fin + timedelta(minutes=5), EST_DISPONIBLE, tecnico, 'Alta tras mantenimiento')
    return fin + timedelta(minutes=30)


def correctiva(ctx, v, inc, ret_dt, t_inc, odo):
    """Genera la orden correctiva de una incidencia. Devuelve (fin_de_ocupación, hora_resolución)."""
    rng, END = v.rng, ctx.END
    sev = inc['sev']
    grave = sev in ('ALTA', 'CRITICA')
    opened = (t_inc + timedelta(minutes=30)) if grave else (ret_dt + timedelta(minutes=20))
    espera = {'BAJA': rng.randint(0, 1), 'MEDIA': rng.randint(0, 2), 'ALTA': rng.randint(1, 4),
              'CRITICA': rng.randint(3, 14)}[sev]
    cand = max(opened + timedelta(hours=4), ret_dt + timedelta(hours=1)) + timedelta(days=espera)
    inicio = proximo_dia_habil(cand, rng)
    asignada = min(opened + timedelta(hours=rng.uniform(1, 12)), inicio - timedelta(minutes=15))
    asignada = max(asignada, opened + timedelta(minutes=5))
    dur = {'BAJA': rng.uniform(2, 8), 'MEDIA': rng.uniform(6, 28), 'ALTA': rng.uniform(16, 64),
           'CRITICA': rng.uniform(48, 170)}[sev]
    cierre = agregar_horas_laborales(inicio, dur)
    p_ext = {'BAJA': .10, 'MEDIA': .10, 'ALTA': .25, 'CRITICA': .45}[sev]
    taller = LOC_EXTERNO if rng.random() < p_ext else LOC_TALLER
    tecnico, gestor = rng.choice(TECNICOS), rng.choice(GESTORES)
    items = items_correctivo(rng, v.tid, inc['sistema'], sev, inflacion(cierre, ctx.base_infl), dur)
    oid = ctx.sig_orden
    ctx.sig_orden += 1
    emitir_orden(ctx, v, oid, TM_CORR, None, inc['id'], tecnico, taller,
                 f"Reparación por incidencia: {inc['sistema']} - {inc['desc']}",
                 opened, asignada, inicio, cierre, odo, items, gestor)
    ctx.stats['ordenes_corr'] += 1
    if not grave:   # los graves ya quedaron fuera de servicio en el momento del incidente
        cambiar_estado(ctx, v, ret_dt + timedelta(minutes=1), EST_FUERA, gestor,
                       f"Fuera de servicio por incidencia ({inc['sistema']})")
    cambiar_estado(ctx, v, inicio, EST_MANT, tecnico, 'Ingreso a taller por incidencia', ubic=taller)
    cambiar_estado(ctx, v, cierre + timedelta(minutes=5), EST_DISPONIBLE, tecnico, 'Alta tras reparación')
    return cierre + timedelta(minutes=30), cierre


def simular(ctx, v):
    rng, START, END = v.rng, ctx.START, ctx.END
    T = TIPOS[v.tid]
    t_free = v.alta_dt
    odo = v.odo0
    d = max(START.date(), v.alta_dt.date())
    trips_anio = T['anual'] * v.uso / (T['viaje_med'] * math.exp(T['viaje_sig'] ** 2 / 2))
    motivos = MOTIVOS['personal'] if v.tid == 3 else MOTIVOS['carga'] if v.tid >= 4 else MOTIVOS['liviano']

    while d <= END.date():
        day0 = datetime.combine(d, time(0, 0))
        wd = d.weekday()
        invierno = d.month in (6, 7, 8)
        if t_free > day0 + timedelta(hours=16):
            d += timedelta(days=1)
            continue

        # Inventario mensual: lectura manual del odómetro (primer lunes, 06:30)
        t_inv = day0 + timedelta(hours=6, minutes=30)
        if wd == 0 and d.day <= 7 and t_free <= t_inv and v.alta_dt < t_inv and v.estado != EST_ASIGNADO:
            ctx.emit(t_inv, "INSERT INTO lectura_uso (vehiculo_id, leida_en, odometro_km, fuente) "
                            f"VALUES ({v.id}, {tsq(t_inv)}, {odo}, 'MANUAL');")

        # Baja programada
        if (v.baja_fecha and d >= v.baja_fecha and v.estado == EST_DISPONIBLE and wd < 5
                and t_free <= day0 + timedelta(hours=14)):
            t_b = day0 + timedelta(hours=15)
            cambiar_estado(ctx, v, t_b, EST_BAJA, GESTORES[0], 'Baja por antigüedad y costo de mantenimiento',
                           extra=f", fecha_baja = '{d.isoformat()}'")
            auditar(ctx, t_b, 'BAJA_VEHICULO', 'vehiculo', str(v.id),
                    'Baja definitiva por antigüedad y costo de mantenimiento',
                    {'identificador': v.ident, 'odometro_km': odo})
            ctx.stats['bajas'] += 1
            break

        # Servicios preventivos (solo días hábiles)
        if wd < 5:
            vencidos, cercanos = servicios_vencidos(v, d, odo)
            if vencidos and rng.random() > .12:      # a veces el taller posterga unos días
                vs = max(day0 + timedelta(hours=8, minutes=rng.randint(0, 90)), t_free + timedelta(minutes=10))
                if vs.date() == d and vs.hour < 15:
                    tareas = vencidos + [p for p in cercanos if rng.random() < .6]
                    t_free = hacer_visita(ctx, v, d, vs, tareas, odo)
                    d += timedelta(days=1)
                    continue

        # Viaje
        p_trip = trips_anio / 365 * 1.15 * (1.25 if wd < 5 else .4) * (.88 if invierno else 1.0)
        stale = v.stale_dia is not None and d >= v.stale_dia and not v.stale_hecho
        if stale or rng.random() < min(.9, p_trip):
            base_h = rng.uniform(6.75, 10.5) if wd < 5 else rng.uniform(8, 12)
            start = max(day0 + timedelta(hours=base_h), t_free + timedelta(minutes=5))
            if start.date() != d or start.hour >= 15:
                d += timedelta(days=1)
                continue
            if start > END:
                break
            aid = ctx.sig_asig
            ctx.sig_asig += 1
            odo_s = odo + rng.randint(0, 3)
            resp = rng.choice(v.conductores) if rng.random() < .8 else rng.choice(CONDUCTORES)
            motivo = rng.choice(motivos)

            if stale:   # comisión larga: salió hace semanas y no volvió; retorno previsto ya vencido
                v.stale_hecho = True
                rp = start + timedelta(days=21)
                ctx.emit(start, "INSERT INTO asignacion (id, vehiculo_id, responsable_id, ubicacion_salida_id, "
                                "motivo, salida_real, retorno_previsto, odometro_salida_km, creado_en) "
                                f"OVERRIDING SYSTEM VALUE VALUES ({aid}, {v.id}, {resp}, {v.loc}, "
                                f"{sq('Comisión prolongada en destacamento')}, {tsq(start)}, {tsq(rp)}, {odo_s}, {tsq(start)});")
                ctx.stats['viajes'] += 1
                v.estado = EST_ASIGNADO
                break

            km = clip(rng.lognormvariate(math.log(T['viaje_med']), T['viaje_sig']), 10, 700)
            noches = 0
            if km > 250 and rng.random() < .7:
                noches = rng.randint(1, 3)
            elif rng.random() < .04:
                noches = 1
            if km > 450 and noches == 0:     # un recorrido tan largo no se hace en el día
                noches = 1
            horas = km / T['vel'] + rng.uniform(1, 4)
            ret = start + timedelta(hours=horas) + timedelta(days=noches, hours=rng.uniform(0, 5) if noches else 0)
            km = int(round(km))
            odo_r = odo_s + km
            rp = ret + timedelta(hours=rng.uniform(0, 2)) if rng.random() < .85 else None

            # ¿Incidencia durante el viaje?
            ex = excesos(v, d, odo_s)
            ex_max = max(ex.values()) if ex else 0.0
            age = (d - date(v.anio, 7, 1)).days / 365.25
            tasa = (T['tasa'] * math.exp(.12 * age) * v.fragil * math.exp(1.5 * min(ex_max, 1.2))
                    * (1.2 if invierno else 1.0))
            p_inc = 1 - math.exp(-tasa * km / 10000)
            inc = None
            tipico_typo = (v.typo and not v.typo_hecho and start >= END - timedelta(days=8)
                           and ret <= END - timedelta(hours=1))
            if rng.random() < p_inc and not tipico_typo:
                accidente = rng.random() < .04
                sistema = 'accidente' if accidente else elegir_sistema(rng, ex, invierno)
                sev = rng.choices(['BAJA', 'MEDIA', 'ALTA', 'CRITICA'], weights=SEVERIDAD[sistema])[0]
                if accidente:
                    sistema_real = rng.choice(['carroceria', 'suspension'])
                    tipo = 'ACCIDENTE'
                else:
                    sistema_real = sistema
                    tipo = 'DANIO' if sistema == 'carroceria' and rng.random() < .8 else 'FALLA'
                frac = rng.uniform(.15, .95)
                t_inc = start + (ret - start) * frac
                odo_inc = odo_s + int(round(km * frac))
                inc = dict(id=ctx.sig_inc, sev=sev, sistema=sistema_real, tipo=tipo,
                           desc=rng.choice(DESCRIPCIONES[sistema]), t=t_inc, odo=odo_inc)
                ctx.sig_inc += 1
                if t_inc <= END:
                    ctx.stats['incidencias'] += 1

            # Exposición para verificar la señal (km y fallas por nivel de atraso)
            bucket = '0 al día' if ex_max <= 0 else '1 (0, 0.25]' if ex_max <= .25 else \
                '2 (0.25, 0.6]' if ex_max <= .6 else '3 > 0.6'
            ctx.expo[bucket][0] += km
            ctx.expo[bucket][1] += 1 if (inc and inc['t'] <= END) else 0

            if inc and inc['sev'] in ('ALTA', 'CRITICA'):    # se corta el viaje: grúa al taller
                ret = inc['t'] + timedelta(hours=rng.uniform(2, 9))
                odo_r = inc['odo']
                rp = None if rp is None else min(rp, ret)
                if rp is not None and rp <= start:
                    rp = None

            ctx.emit(start, "INSERT INTO asignacion (id, vehiculo_id, responsable_id, ubicacion_salida_id, "
                            "motivo, salida_real, retorno_previsto, odometro_salida_km, creado_en) "
                            f"OVERRIDING SYSTEM VALUE VALUES ({aid}, {v.id}, {resp}, {v.loc}, {sq(motivo)}, "
                            f"{tsq(start)}, {tsq(rp) if rp else 'NULL'}, {odo_s}, {tsq(start)});")
            ctx.stats['viajes'] += 1
            v.estado = EST_ASIGNADO

            gestor = rng.choice(GESTORES)
            loc_ret = v.home if rng.random() < .85 else rng.choice([LOC_BASE, LOC_NORTE, LOC_SUR])
            if inc and inc['sev'] in ('ALTA', 'CRITICA'):
                loc_ret = LOC_EXTERNO if rng.random() < .2 else LOC_TALLER

            if inc:
                resuelta = 'NULL'
                if inc['sev'] == 'BAJA' and rng.random() < .5:       # lo resuelve el conductor, sin orden
                    t_res = inc['t'] + timedelta(hours=rng.uniform(.1, 4))
                    resuelta = tsn(t_res, END)
                    orden = False
                else:
                    orden = True
                ctx.emit(inc['t'], "INSERT INTO incidencia (id, vehiculo_id, asignacion_id, reportada_por, tipo, "
                                   "severidad, sistema, descripcion, ocurrida_en, odometro_km, resuelta_en) "
                                   f"OVERRIDING SYSTEM VALUE VALUES ({inc['id']}, {v.id}, {aid}, {resp}, "
                                   f"{sq(inc['tipo'])}, {sq(inc['sev'])}, {sq(inc['sistema'])}, {sq(inc['desc'])}, "
                                   f"{tsq(inc['t'])}, {inc['odo']}, @RES{inc['id']}@);")
                inc['resuelta_placeholder'] = f"@RES{inc['id']}@"
                if inc['sev'] in ('ALTA', 'CRITICA'):
                    cambiar_estado(ctx, v, inc['t'], EST_FUERA, gestor,
                                   f"Fuera de servicio por incidencia grave ({inc['sistema']})")

            # Retorno
            odo_r_reg = odo_r
            if tipico_typo:
                ctx.stats['typos_pendientes'] += 1
                typo = int(str(odo_r) + str(rng.randint(0, 9))) if rng.random() < .5 else odo_r + rng.choice(
                    [2000, 3000, 4000, 5000, 7000])
                odo_r_reg = typo
                v.typo_hecho = True
            ctx.emit(ret, f"UPDATE asignacion SET retorno_real = {tsq(ret)}, odometro_retorno_km = {odo_r_reg}, "
                          f"ubicacion_retorno_id = {loc_ret} WHERE id = {aid};")
            if v.estado == EST_ASIGNADO:
                v.estado = EST_DISPONIBLE
            v.loc = loc_ret
            if tipico_typo:
                odo = odo_r_reg
                break      # queda como última lectura del vehículo, pendiente de corrección
            # Corrección de tipeos históricos (se corrigieron antes de la lectura siguiente)
            if rng.random() < .0012:
                tipo_err = int(str(odo_r) + str(rng.randint(0, 9)))
                auditar(ctx, ret + timedelta(hours=rng.uniform(1, 20)), 'CORRECCION_LECTURA', 'asignacion',
                        str(aid), 'Error de tipeo en el odómetro de retorno',
                        {'campo': 'odometro_retorno_km', 'valor_original': tipo_err, 'valor_corregido': odo_r})
                ctx.stats['correcciones'] += 1

            odo = odo_r
            t_free = ret + timedelta(minutes=rng.randint(20, 90))

            if inc:
                if orden:
                    t_free, cierre = correctiva(ctx, v, inc, ret, inc['t'], odo)
                    ctx.reemplazos[inc['resuelta_placeholder']] = tsn(cierre, END)
                else:
                    ctx.reemplazos[inc['resuelta_placeholder']] = resuelta
        d += timedelta(days=1)


# --------------------------------------------------------------------------------------
# Construcción de la flota
# --------------------------------------------------------------------------------------
def construir_flota(ctx, seed):
    master = random.Random(f"{seed}:flota")
    START, END = ctx.START, ctx.END
    vehiculos = []
    vid = 0
    nuevos = {1: 1, 2: 1, 4: 1}                 # unidades incorporadas durante el período
    pid = 0
    planes = {}
    for tid in TIPOS:
        planes[tid] = []
        for tarea, km, dias, clave in PLANES[tid]:
            pid += 1
            planes[tid].append(Plan(pid, tid, tarea, km, dias, clave))
    ctx.planes_por_tipo = planes

    for tid, T in TIPOS.items():
        for k in range(1, T['n'] + 1):
            vid += 1
            v = Vehiculo()
            v.id, v.tid = vid, tid
            v.ident = f"{PREFIJO[tid]}-{k:02d}"
            v.rng = random.Random(f"{seed}:veh:{vid}")
            marca, modelo = master.choice(T['modelos'])
            v.marca, v.modelo = marca, modelo
            v.planes = planes[tid]
            v.home = master.choices([LOC_BASE, LOC_NORTE, LOC_SUR], weights=[.6, .2, .2])[0]
            v.loc = v.home
            v.estado = EST_DISPONIBLE
            v.uso = clip(master.lognormvariate(0, .25), .6, 1.6)
            v.fragil = clip(master.lognormvariate(0, .35), .5, 2.2)
            v.conductores = master.sample(CONDUCTORES, 3)
            v.typo = v.typo_hecho = False
            v.stale_dia = None
            v.stale_hecho = False
            v.baja_fecha = None
            nuevo = k > T['n'] - nuevos.get(tid, 0)
            v.nuevo = nuevo
            if nuevo:
                v.anio = master.choice([2025, 2026]) if tid == 1 else 2025
                dia = START.date() + timedelta(days=master.randint(90, 600))
                while dia.weekday() >= 5:
                    dia += timedelta(days=1)
                v.alta_dt = datetime.combine(dia, time(9, 0))
                v.alta_fecha = dia
                v.odo0 = master.randint(5, 400)
            else:
                v.anio = master.choices(range(2016, 2025), weights=[1, 1, 2, 2, 3, 3, 4, 4, 3])[0]
                mes = master.randint(1, 6 if v.anio == 2024 else 12)
                v.alta_fecha = date(v.anio, mes, master.randint(1, 28))
                v.alta_dt = START - timedelta(days=1)
                edad = max(.4, (START.date() - v.alta_fecha).days / 365.25)
                v.odo0 = int(edad * T['anual'] * v.uso * master.uniform(.85, 1.15))
            if v.alta_dt > END - timedelta(days=30):
                v.alta_dt = END - timedelta(days=30)
            vehiculos.append(v)

    existentes = [v for v in vehiculos if not v.nuevo]
    # Dos bajas: las unidades más antiguas
    for v, dias in zip(sorted(existentes, key=lambda x: (x.anio, x.id))[:2], (300, 110)):
        v.baja_fecha = (END - timedelta(days=dias)).date()
    libres = [v for v in existentes if not v.baja_fecha]
    # Cuatro con lectura errónea pendiente de corrección
    for v in master.sample(libres, 4):
        v.typo = True
    # Dos pick-ups con comisión larga abierta (lectura vencida)
    pick = [v for v in libres if v.tid == 1 and not v.typo]
    for v, dias in zip(master.sample(pick, 2), (41, 36)):
        v.stale_dia = (END - timedelta(days=dias)).date()
    return vehiculos


def estado_inicial_planes(ctx, v):
    """Último servicio de cada plan antes de empezar a registrar en el sistema."""
    rng, T = v.rng, TIPOS[v.tid]
    daily = T['anual'] * v.uso / 365.0
    v.plan_state = {}
    migrados = []
    base_d = v.alta_fecha if v.nuevo else ctx.START.date()
    for p in v.planes:
        if v.nuevo:
            v.plan_state[p.id] = EstadoPlan(v.odo0, v.alta_fecha, draw_delay(rng))
            continue
        frac = rng.uniform(.1, .9)
        if p.km:
            desde_km = frac * p.km
            dias = desde_km / daily
            if p.dias and dias > p.dias * .95:
                dias = p.dias * rng.uniform(.5, .95)
                desde_km = dias * daily
        else:
            dias = frac * p.dias
            desde_km = dias * daily
        last_km = max(0, int(v.odo0 - desde_km))
        last_fecha = base_d - timedelta(days=int(dias))
        v.plan_state[p.id] = EstadoPlan(last_km, last_fecha, draw_delay(rng))
        migrados.append((p, last_km, last_fecha))
    return migrados


def emitir_alta_vehiculo(ctx, v, migrados):
    rng = v.rng
    t_ins = v.alta_dt if v.nuevo else ctx.START - timedelta(hours=18)
    ctx.emit(t_ins, "INSERT INTO vehiculo (id, identificador, tipo_vehiculo_id, estado_id, ubicacion_id, marca, "
                    "modelo, anio, fecha_alta, odometro_km) OVERRIDING SYSTEM VALUE VALUES "
                    f"({v.id}, {sq(v.ident)}, {v.tid}, {EST_DISPONIBLE}, {v.home}, {sq(v.marca)}, {sq(v.modelo)}, "
                    f"{v.anio}, '{v.alta_fecha.isoformat()}', {v.odo0});")
    t_l = t_ins + timedelta(minutes=1)
    ctx.emit(t_l, "INSERT INTO lectura_uso (vehiculo_id, leida_en, odometro_km, fuente) "
                  f"VALUES ({v.id}, {tsq(t_l)}, {v.odo0}, 'IMPORTACION' );")
    if v.nuevo:
        auditar(ctx, t_ins + timedelta(minutes=5), 'ALTA_VEHICULO', 'vehiculo', str(v.id),
                'Incorporación de una unidad nueva a la flota', {'identificador': v.ident})
    # Servicios anteriores al uso del sistema (historia migrada)
    for i, (p, last_km, last_fecha) in enumerate(migrados):
        tecnico, gestor = rng.choice(TECNICOS), rng.choice(GESTORES)
        cerrada = datetime.combine(last_fecha, time(11, 0))
        abierta, iniciada = cerrada - timedelta(hours=4), cerrada - timedelta(hours=3)
        items, _ = items_preventivo(rng, v.tid, p.clave, inflacion(cerrada, ctx.base_infl))
        total = sum(q * u for _, q, u in items)
        oid = ctx.sig_orden
        ctx.sig_orden += 1
        te = t_ins + timedelta(minutes=2 + i)
        ctx.emit(te, "INSERT INTO orden_mantenimiento (id, vehiculo_id, tipo_id, estado_id, plan_id, asignado_a_id, "
                     "taller_id, descripcion, abierta_en, iniciada_en, cerrada_en, odometro_km, costo_total) "
                     f"OVERRIDING SYSTEM VALUE VALUES ({oid}, {v.id}, {TM_PREV}, {ORD_CERRADA}, {p.id}, {tecnico}, "
                     f"{LOC_TALLER}, {sq('Servicio previo a la puesta en marcha del sistema: ' + p.tarea)}, "
                     f"{tsq(abierta)}, {tsq(iniciada)}, {tsq(cerrada)}, {last_km}, {total});")
        for desc_i, qty, unit in items:
            ctx.emit(te, "INSERT INTO orden_item (orden_id, descripcion, cantidad, costo_unitario) "
                         f"VALUES ({oid}, {sq(desc_i)}, {qty}, {unit});")
    v.estado = EST_DISPONIBLE


# --------------------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------------------
def generar(END, seed, meses=24):
    START = datetime.combine((END - timedelta(days=int(meses * 30.4))).date(), time(0, 0))
    ctx = Contexto(END, START, seed)
    ctx.base_infl = START - timedelta(days=0)
    ctx.reemplazos = {}

    # --- Preámbulo: usuarios, ubicaciones, tipos y planes ---
    t0 = START - timedelta(days=3)
    for uid, nom, ape, roles in USUARIOS:
        ctx.emit(t0, "INSERT INTO usuario (id, nombre, apellido, email, activo, creado_en) "
                     f"OVERRIDING SYSTEM VALUE VALUES ({uid}, {sq(nom)}, {sq(ape)}, "
                     f"{sq(slug(nom) + '.' + slug(ape) + '@flota-demo.test')}, TRUE, {tsq(START - timedelta(days=60))});")
        for r in roles:
            ctx.emit(t0, f"INSERT INTO usuario_rol (usuario_id, rol_id) VALUES ({uid}, {r});")
    for lid, nom, tipo in UBICACIONES:
        ctx.emit(t0, f"INSERT INTO ubicacion (id, nombre, tipo) OVERRIDING SYSTEM VALUE VALUES ({lid}, {sq(nom)}, {sq(tipo)});")
    for tid, T in TIPOS.items():
        ctx.emit(t0, "INSERT INTO tipo_vehiculo (id, codigo, nombre, capacidad_personas) OVERRIDING SYSTEM VALUE "
                     f"VALUES ({tid}, {sq(T['codigo'])}, {sq(T['nombre'])}, {T['cap']});")

    flota = construir_flota(ctx, seed)
    for tid, lista in ctx.planes_por_tipo.items():
        for p in lista:
            ctx.emit(t0, "INSERT INTO plan_mantenimiento (id, tipo_vehiculo_id, tarea, cada_km, cada_dias) "
                         f"OVERRIDING SYSTEM VALUE VALUES ({p.id}, {tid}, {sq(p.tarea)}, "
                         f"{p.km if p.km else 'NULL'}, {p.dias if p.dias else 'NULL'});")

    for v in flota:
        migrados = estado_inicial_planes(ctx, v)
        emitir_alta_vehiculo(ctx, v, migrados)
    for v in flota:
        simular(ctx, v)

    # --- Resolver los marcadores de 'resuelta_en' ---
    eventos = []
    for t, seq, sql in sorted(ctx.eventos, key=lambda e: (e[0], e[1])):
        if '@RES' in sql:
            marca = sql[sql.rindex('@RES'):sql.rindex('@') + 1]
            sql = sql.replace(marca, ctx.reemplazos.get(marca, 'NULL'))
        eventos.append(sql)
    return ctx, flota, eventos


def escribir(ctx, flota, eventos, destino, END, seed):
    ult = {'usuario': len(USUARIOS), 'ubicacion': len(UBICACIONES), 'tipo_vehiculo': len(TIPOS),
           'plan_mantenimiento': sum(len(PLANES[t]) for t in PLANES), 'vehiculo': len(flota),
           'asignacion': ctx.sig_asig - 1, 'orden_mantenimiento': ctx.sig_orden - 1,
           'incidencia': ctx.sig_inc - 1}
    cab = [
        "-- Datos sintéticos de la flota con mantenimiento predictivo.",
        f"-- Generado con generar_datos.py (semilla {seed}), historia hasta {END:%Y-%m-%d %H:%M} (UTC-3).",
        "-- Todos los datos son ficticios. Ejecutar sobre una base con db/schema.sql recién cargado.",
        "-- Recomendado: psql -1 -v ON_ERROR_STOP=1 -f seed.sql   (una sola transacción).",
        "SET client_min_messages = warning;",
    ]
    pie = [f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), {n}) ;" for t, n in ult.items()]
    texto = "\n".join(cab + eventos + pie) + "\n"
    if destino.endswith('.gz'):
        with gzip.open(destino, 'wt', encoding='utf-8', compresslevel=9) as f:
            f.write(texto)
    else:
        with open(destino, 'w', encoding='utf-8') as f:
            f.write(texto)


def resumen(ctx, flota, eventos):
    s = ctx.stats
    sys.stderr.write(
        f"Vehículos: {len(flota)} | Sentencias: {len(eventos)}\n"
        f"Viajes: {int(s['viajes'])} | Incidencias: {int(s['incidencias'])} | "
        f"Órdenes preventivas: {int(s['ordenes_prev'])} | correctivas: {int(s['ordenes_corr'])}\n"
        f"Bajas: {int(s['bajas'])} | Lecturas erróneas pendientes: {int(s['typos_pendientes'])} | "
        f"Correcciones auditadas: {int(s['correcciones'])}\n"
        "Señal de diseño: incidencias por 10.000 km según atraso del mantenimiento\n")
    for b in sorted(ctx.expo):
        km, n = ctx.expo[b]
        if km > 0:
            sys.stderr.write(f"  atraso {b:<16} km={km:>10,.0f}  incidencias={n:>4}  tasa={n / km * 10000:.2f}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--hasta', default=datetime.now().strftime('%Y-%m-%dT%H:00'),
                    help='fin de la historia (hora local UTC-3), p. ej. 2026-11-04T12:00')
    ap.add_argument('--semilla', default='2026', help='semilla del generador (reproducible)')
    ap.add_argument('--meses', type=int, default=24, help='meses de historia')
    ap.add_argument('--salida', default='seed.sql', help='archivo de salida (.sql o .sql.gz)')
    a = ap.parse_args()
    END = datetime.strptime(a.hasta, '%Y-%m-%dT%H:%M')
    ctx, flota, eventos = generar(END, a.semilla, a.meses)
    escribir(ctx, flota, eventos, a.salida, END, a.semilla)
    resumen(ctx, flota, eventos)


if __name__ == '__main__':
    main()
