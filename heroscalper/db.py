"""Persistencia: historico de precios, tiendas, referencias de reventa y alertas."""
from __future__ import annotations

import json
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .matching import clave_producto
from .models import Oferta, ReferenciaReventa


def _clave_modelo(modelo: str) -> str:
    return clave_producto(None, modelo, None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tiendas (
    dominio        TEXT PRIMARY KEY,
    plataforma     TEXT,
    envia_espana   INTEGER DEFAULT 1,
    activa         INTEGER DEFAULT 1,
    tiene_ean      INTEGER DEFAULT 0,
    n_productos    INTEGER DEFAULT 0,
    fallos_seguidos INTEGER DEFAULT 0,
    descubierta    TEXT,
    ultimo_barrido TEXT,
    apagada_en     TEXT,
    offset_catalogo INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lecturas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ean            TEXT,
    clave_match    TEXT,
    tienda         TEXT NOT NULL,
    sku            TEXT,
    url            TEXT NOT NULL,
    titulo         TEXT,
    marca          TEXT,
    precio         REAL NOT NULL,
    precio_anterior REAL,
    disponible     INTEGER DEFAULT 1,
    promo_texto    TEXT,
    es_liquidacion INTEGER DEFAULT 0,
    categoria      TEXT,
    condicion      TEXT DEFAULT 'nuevo',
    visto_en       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lecturas_ean   ON lecturas(ean, visto_en);
CREATE INDEX IF NOT EXISTS idx_lecturas_match ON lecturas(clave_match, visto_en);
CREATE INDEX IF NOT EXISTS idx_lecturas_tienda ON lecturas(tienda, visto_en);
CREATE INDEX IF NOT EXISTS idx_lecturas_url   ON lecturas(url, visto_en);

CREATE TABLE IF NOT EXISTS reventa (
    ean                  TEXT,
    modelo               TEXT,
    clave_match          TEXT,
    plataforma           TEXT,
    precio_venta_mediano REAL,
    anuncios_90d         INTEGER,
    dias_venta_mediano   REAL,
    precio_suelo         REAL,
    actualizado          TEXT,
    PRIMARY KEY (ean, plataforma)
);

CREATE TABLE IF NOT EXISTS alertas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    clave         TEXT NOT NULL,
    tienda        TEXT,
    tipo          TEXT,
    precio        REAL,
    margen_neto   REAL,
    enviada_en    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alertas_clave ON alertas(clave, enviada_en);

-- Sesiones de la web. En base de datos y no en memoria para que sobrevivan
-- a un reinicio y para poder revocarlas de verdad al cerrar sesion.
-- Se guarda el HASH del token, nunca el token: si alguien lee la base de
-- datos no puede suplantar a nadie.
CREATE TABLE IF NOT EXISTS sesiones (
    token_hash TEXT PRIMARY KEY,
    usuario    TEXT NOT NULL,
    ip         TEXT,
    creada     TEXT NOT NULL,
    expira     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sesiones_exp ON sesiones(expira);

-- Todas las consultas de precio de reventa que se han hecho nunca.
-- Con el tiempo esto es tu propia serie historica de a cuanto se paga cada
-- cosa en 2a mano, que no te la da ninguna plataforma.
CREATE TABLE IF NOT EXISTS reventa_cotizaciones (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    clave_match        TEXT,
    consulta           TEXT,
    fuente             TEXT,
    precio             REAL,
    precio_garantizado REAL,
    n_muestras         INTEGER,
    dias_venta         REAL,
    url                TEXT,
    consultada         TEXT
);
CREATE INDEX IF NOT EXISTS idx_cotiz ON reventa_cotizaciones(clave_match, consultada);

-- Ofertas vivas. Es lo que alimenta la web: mientras `estado` sea 'activa'
-- la oferta se ve; cuando deja de detectarse o sube de precio, se marca
-- 'expirada' y desaparece del feed sola.
CREATE TABLE IF NOT EXISTS ofertas (
    clave             TEXT PRIMARY KEY,
    tienda            TEXT,
    titulo            TEXT,
    url               TEXT,
    imagen            TEXT,
    marca             TEXT,
    ean               TEXT,
    categoria         TEXT,
    categoria_clave   TEXT,
    tipo              TEXT,
    precio            REAL,
    precio_anterior   REAL,
    precio_referencia REAL,
    descuento         REAL,
    precio_reventa    REAL,
    reventa_medida    INTEGER DEFAULT 0,
    verificado        INTEGER DEFAULT 1,
    margen_neto       REAL,
    margen_esperado   REAL,
    margen_pct        REAL,
    margen_por_dia    REAL,
    unidades          INTEGER DEFAULT 1,
    dias_venta        REAL,
    prob_cancelacion  REAL,
    fuente_referencia TEXT,
    motivo            TEXT,
    vista_primera     TEXT,
    vista_ultima      TEXT,
    estado            TEXT DEFAULT 'activa',
    expirada_en       TEXT,
    razon_expiracion  TEXT,
    guardada          INTEGER DEFAULT 0,
    destacada         INTEGER DEFAULT 0,
    nivel             INTEGER DEFAULT 1,
    media_30d         REAL,
    media_90d         REAL,
    minimo_historico  REAL,
    n_lecturas        INTEGER DEFAULT 0,
    serie             TEXT
);
CREATE INDEX IF NOT EXISTS idx_ofertas_estado ON ofertas(estado, margen_por_dia);
CREATE INDEX IF NOT EXISTS idx_ofertas_vista  ON ofertas(vista_ultima);
CREATE INDEX IF NOT EXISTS idx_ofertas_cat    ON ofertas(categoria_clave, estado);

-- Resultado real de cada compra. A partir del mes 3 esto sustituye a las
-- estimaciones y el bot empieza a puntuar con datos propios.
CREATE TABLE IF NOT EXISTS operaciones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ean            TEXT,
    titulo         TEXT,
    tienda         TEXT,
    precio_compra  REAL,
    unidades       INTEGER DEFAULT 1,
    comprada_en    TEXT,
    cancelada      INTEGER DEFAULT 0,
    precio_venta   REAL,
    vendida_en     TEXT,
    plataforma_venta TEXT,
    notas          TEXT
);
"""


class DB:
    def __init__(self, ruta: str = "data/heroscalper.db"):
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.ruta, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrar()
        self._conn.commit()

    def _migrar(self) -> None:
        """CREATE TABLE IF NOT EXISTS no añade columnas a una tabla que ya existe.

        Sin esto, una base de datos creada con una versión anterior seguiría
        sin las columnas nuevas y el bot fallaría en producción justo donde
        no se puede depurar.
        """
        nuevas = {
            "tiendas": [("apagada_en", "TEXT"),
                        ("offset_catalogo", "INTEGER DEFAULT 0")],
        }
        cur = self._conn.cursor()
        for tabla, columnas in nuevas.items():
            cur.execute(f"PRAGMA table_info({tabla})")
            existentes = {r["name"] for r in cur.fetchall()}
            for nombre, tipo in columnas:
                if nombre not in existentes:
                    cur.execute(f"ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}")

    @contextmanager
    def cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        finally:
            cur.close()

    # ------------------------------------------------------------------ #
    # Lecturas de precio
    # ------------------------------------------------------------------ #
    def guardar_lecturas(self, ofertas: Iterable[Oferta],
                         horas_min: int = 20) -> list:
        """Igual que antes, pero devuelve LAS ofertas cuyo precio ha cambiado.

        Saberlo cambia el coste del sistema entero: evaluar una oferta cuesta
        varias consultas a la base de datos, y el 97 % de los precios no se
        mueve de un ciclo a otro. Con esta lista se evalúa solo lo que se ha
        movido en vez de las 600.000 fichas de cada barrido.
        """
        return self._guardar_lecturas(ofertas, horas_min)

    def _guardar_lecturas(self, ofertas: Iterable[Oferta],
                          horas_min: int = 20) -> list:
        """Guarda solo lo que aporta algo: precios que han CAMBIADO.

        Guardar cada lectura de cada ciclo parece inofensivo y no lo es: con
        45 tiendas y un barrido cada 30 minutos son 4,3 millones de filas al
        día, que llenan el disco de un servidor pequeño en dos meses. Como el
        97 % de los precios no se mueve de un día para otro, se guarda una
        fila solo si el precio cambió o si han pasado ~20 h desde la última.
        """
        ofertas = list(ofertas)
        ultimas = self._ultimas_por_url([o.url for o in ofertas])
        corte = datetime.utcnow() - timedelta(hours=horas_min)

        nuevas = []
        for o in ofertas:
            anterior = ultimas.get(o.url)
            if anterior:
                precio_ant, visto_ant = anterior
                if abs(precio_ant - o.precio) < 0.005 and visto_ant > corte:
                    continue          # ni ha cambiado ni toca refrescar
            nuevas.append(o)

        self._insertar_lecturas(nuevas)
        return nuevas

    def _ultimas_por_url(self, urls: list[str]) -> dict:
        """Último precio conocido de cada URL, en bloques para no ahogar SQLite."""
        salida: dict[str, tuple] = {}
        unicas = list({u for u in urls if u})
        for i in range(0, len(unicas), 400):
            trozo = unicas[i:i + 400]
            marcas = ",".join("?" * len(trozo))
            with self.cursor() as cur:
                cur.execute(
                    f"""SELECT url, precio, visto_en FROM (
                            SELECT url, precio, visto_en,
                                   ROW_NUMBER() OVER (PARTITION BY url
                                       ORDER BY visto_en DESC) AS rn
                            FROM lecturas WHERE url IN ({marcas})
                        ) WHERE rn = 1""",
                    trozo,
                )
                for r in cur.fetchall():
                    salida[r["url"]] = (r["precio"],
                                        datetime.fromisoformat(r["visto_en"]))
        return salida

    def _insertar_lecturas(self, ofertas: Iterable[Oferta]) -> int:
        filas = [
            (
                o.ean, o.clave_match(), o.tienda, o.sku, o.url, o.titulo, o.marca, o.precio,
                o.precio_anterior, int(o.disponible), o.promo_texto,
                int(o.es_liquidacion), o.categoria, o.condicion,
                o.visto_en.isoformat(),
            )
            for o in ofertas
        ]
        if not filas:
            return 0
        with self.cursor() as cur:
            cur.executemany(
                """INSERT INTO lecturas
                   (ean, clave_match, tienda, sku, url, titulo, marca, precio,
                    precio_anterior, disponible, promo_texto, es_liquidacion,
                    categoria, condicion, visto_en)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                filas,
            )
        return len(filas)

    def historico_url(self, url: str, dias: int = 90) -> list[float]:
        desde = (datetime.utcnow() - timedelta(days=dias)).isoformat()
        with self.cursor() as cur:
            cur.execute(
                "SELECT precio FROM lecturas WHERE url = ? AND visto_en >= ?",
                (url, desde),
            )
            return [r["precio"] for r in cur.fetchall()]

    def precios_cross_tienda(
        self, clave: str, excluir_tienda: Optional[str] = None, horas: int = 24
    ) -> list[float]:
        """Ultimo precio de este producto en cada una de las demas tiendas.

        Se cruza por `clave_match`, que es el EAN cuando existe y la clave
        marca+modelo cuando la tienda no lo publica (caso de Shopify).
        """
        if not clave:
            return []
        desde = (datetime.utcnow() - timedelta(hours=horas)).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """SELECT tienda, precio, MAX(visto_en) AS ultima FROM lecturas
                   WHERE clave_match = ? AND visto_en >= ? AND disponible = 1
                     AND condicion = 'nuevo'
                   GROUP BY tienda""",
                (clave, desde),
            )
            return [
                r["precio"] for r in cur.fetchall()
                if r["tienda"] != excluir_tienda
            ]

    def mediana_mercado(self, clave: str, excluir_tienda: Optional[str] = None) -> Optional[float]:
        precios = self.precios_cross_tienda(clave, excluir_tienda)
        return statistics.median(precios) if precios else None

    # ------------------------------------------------------------------ #
    # Referencias de reventa
    # ------------------------------------------------------------------ #
    def guardar_reventa(self, ref: ReferenciaReventa) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO reventa
                   (ean, modelo, clave_match, plataforma, precio_venta_mediano,
                    anuncios_90d, dias_venta_mediano, precio_suelo, actualizado)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (ref.ean, ref.modelo, _clave_modelo(ref.modelo),
                 ref.plataforma, ref.precio_venta_mediano,
                 ref.anuncios_90d, ref.dias_venta_mediano, ref.precio_suelo,
                 ref.actualizado.isoformat()),
            )

    def reventa(self, ean: Optional[str]) -> Optional[ReferenciaReventa]:
        if not ean:
            return None
        with self.cursor() as cur:
            cur.execute(
                """SELECT * FROM reventa WHERE ean = ?
                   ORDER BY anuncios_90d DESC LIMIT 1""",
                (ean,),
            )
            r = cur.fetchone()
        if not r:
            return None
        return ReferenciaReventa(
            ean=r["ean"], modelo=r["modelo"], plataforma=r["plataforma"],
            precio_venta_mediano=r["precio_venta_mediano"],
            anuncios_90d=r["anuncios_90d"],
            dias_venta_mediano=r["dias_venta_mediano"],
            precio_suelo=r["precio_suelo"],
            actualizado=datetime.fromisoformat(r["actualizado"]),
        )

    # ------------------------------------------------------------------ #
    # Alertas (cooldown / dedupe)
    # ------------------------------------------------------------------ #
    def ya_alertado(self, clave: str, horas: int = 24) -> bool:
        desde = (datetime.utcnow() - timedelta(hours=horas)).isoformat()
        with self.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM alertas WHERE clave = ? AND enviada_en >= ? LIMIT 1",
                (clave, desde),
            )
            return cur.fetchone() is not None

    def registrar_alerta(self, clave, tienda, tipo, precio, margen_neto) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO alertas (clave, tienda, tipo, precio, margen_neto, enviada_en)
                   VALUES (?,?,?,?,?,?)""",
                (clave, tienda, str(tipo), precio, margen_neto,
                 datetime.utcnow().isoformat()),
            )

    # ------------------------------------------------------------------ #
    # Tiendas
    # ------------------------------------------------------------------ #
    def registrar_tienda(self, dominio: str, plataforma: str, n_productos: int = 0,
                         tiene_ean: bool = False) -> None:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO tiendas (dominio, plataforma, n_productos, tiene_ean, descubierta)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(dominio) DO UPDATE SET
                     plataforma=excluded.plataforma,
                     n_productos=excluded.n_productos,
                     tiene_ean=excluded.tiene_ean""",
                (dominio, plataforma, n_productos, int(tiene_ean),
                 datetime.utcnow().isoformat()),
            )

    def tiendas_activas(self) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM tiendas WHERE activa = 1")
            return cur.fetchall()

    def marcar_fallo(self, dominio: str, umbral_apagado: int = 5) -> int:
        """Watchdog: un adapter roto en silencio es el fallo nº1 de estos bots.

        Y una tienda muerta no solo no aporta: cuesta un timeout entero en
        cada ciclo, para siempre. A partir del umbral se apaga y se vuelve a
        probar al día siguiente.
        """
        with self.cursor() as cur:
            cur.execute(
                "UPDATE tiendas SET fallos_seguidos = fallos_seguidos + 1 WHERE dominio = ?",
                (dominio,),
            )
            cur.execute("SELECT fallos_seguidos FROM tiendas WHERE dominio = ?", (dominio,))
            r = cur.fetchone()
            fallos = r["fallos_seguidos"] if r else 0
            if fallos >= umbral_apagado:
                cur.execute(
                    "UPDATE tiendas SET activa = 0, apagada_en = ? WHERE dominio = ?",
                    (datetime.utcnow().isoformat(), dominio),
                )
            return fallos

    def dominios_apagados(self, reintentar_tras_horas: int = 24) -> set[str]:
        """Los que se saltan este ciclo. Se reintentan pasado el plazo."""
        limite = (datetime.utcnow()
                  - timedelta(hours=reintentar_tras_horas)).isoformat()
        with self.cursor() as cur:
            cur.execute("""SELECT dominio FROM tiendas
                           WHERE activa = 0
                             AND (apagada_en IS NULL OR apagada_en > ?)""",
                        (limite,))
            return {r["dominio"] for r in cur.fetchall()}

    def reactivar_caducados(self, reintentar_tras_horas: int = 24) -> int:
        """Vuelve a encender las apagadas hace más de un día: quizá ya van."""
        limite = (datetime.utcnow()
                  - timedelta(hours=reintentar_tras_horas)).isoformat()
        with self.cursor() as cur:
            cur.execute("""UPDATE tiendas
                           SET activa = 1, fallos_seguidos = 0, apagada_en = NULL
                           WHERE activa = 0 AND apagada_en IS NOT NULL
                             AND apagada_en <= ?""", (limite,))
            return cur.rowcount

    def ultimos_barridos(self) -> dict[str, str]:
        """Cuándo se leyó por última vez cada tienda, para pedirle solo lo nuevo."""
        with self.cursor() as cur:
            cur.execute("SELECT dominio, ultimo_barrido FROM tiendas")
            return {r["dominio"]: (r["ultimo_barrido"] or "")
                    for r in cur.fetchall()}

    def urls_ofertas_activas(self) -> set:
        with self.cursor() as cur:
            cur.execute("SELECT url FROM ofertas WHERE estado = 'activa'")
            return {r["url"] for r in cur.fetchall()}

    def refrescar_vistas(self, urls: list) -> int:
        """«La he vuelto a ver y sigue igual»: que no caduque por silencio.

        Sin esto, saltarse la evaluación de lo que no ha cambiado haría que
        las ofertas buenas desaparecieran solas a las 3 horas.
        """
        if not urls:
            return 0
        ahora = datetime.utcnow().isoformat()
        total = 0
        for i in range(0, len(urls), 400):
            trozo = urls[i:i + 400]
            marcas = ",".join("?" * len(trozo))
            with self.cursor() as cur:
                cur.execute(f"""UPDATE ofertas SET vista_ultima = ?
                                WHERE url IN ({marcas}) AND estado = 'activa'""",
                            [ahora] + trozo)
                total += cur.rowcount
        return total

    def offsets_catalogo(self) -> dict[str, int]:
        with self.cursor() as cur:
            cur.execute("SELECT dominio, offset_catalogo FROM tiendas")
            return {r["dominio"]: (r["offset_catalogo"] or 0)
                    for r in cur.fetchall()}

    def avanzar_offset(self, dominio: str, cuanto: int) -> None:
        with self.cursor() as cur:
            cur.execute("""UPDATE tiendas
                           SET offset_catalogo = COALESCE(offset_catalogo,0) + ?
                           WHERE dominio = ?""", (cuanto, dominio))

    def marcar_exito(self, dominio: str) -> None:
        with self.cursor() as cur:
            cur.execute(
                """UPDATE tiendas SET fallos_seguidos = 0, ultimo_barrido = ?
                   WHERE dominio = ?""",
                (datetime.utcnow().isoformat(), dominio),
            )

    def reventa_por_clave(self, clave: Optional[str]) -> Optional[ReferenciaReventa]:
        """Fallback cuando la tienda no publica EAN: busca por marca+modelo."""
        if not clave or clave.startswith("txt:"):
            return None
        with self.cursor() as cur:
            cur.execute(
                """SELECT * FROM reventa WHERE clave_match = ?
                   ORDER BY anuncios_90d DESC LIMIT 1""",
                (clave,),
            )
            r = cur.fetchone()
        if not r:
            return None
        return ReferenciaReventa(
            ean=r["ean"], modelo=r["modelo"], plataforma=r["plataforma"],
            precio_venta_mediano=r["precio_venta_mediano"],
            anuncios_90d=r["anuncios_90d"],
            dias_venta_mediano=r["dias_venta_mediano"],
            precio_suelo=r["precio_suelo"],
            actualizado=datetime.fromisoformat(r["actualizado"]),
        )

    # ------------------------------------------------------------------ #
    # Ofertas vivas (lo que alimenta la web)
    # ------------------------------------------------------------------ #
    def upsert_oferta(self, op) -> bool:
        """Guarda o refresca una oferta. Devuelve True si es nueva."""
        o = op.oferta
        clave = o.clave()
        ahora = datetime.utcnow().isoformat()
        descuento = (1.0 - (op.precio_efectivo / op.referencia_mercado)
                     if op.referencia_mercado else None)
        fuente = ""
        for marca_f in ("otras tiendas", "su propio histórico",
                        "otras variantes del mismo producto"):
            if marca_f in (op.motivo or ""):
                fuente = marca_f
                break

        # Historial de precio: es lo que contesta a "¿esto está barato de
        # verdad o es su precio de siempre?"
        est = self.estadisticas_precio(o.clave_match())
        serie = json.dumps(est["serie"]) if est["serie"] else None

        with self.cursor() as cur:
            cur.execute("SELECT clave FROM ofertas WHERE clave = ?", (clave,))
            existe = cur.fetchone() is not None
            if existe:
                cur.execute(
                    """UPDATE ofertas SET
                         precio=?, precio_referencia=?, descuento=?, precio_reventa=?,
                         margen_neto=?, margen_esperado=?, margen_pct=?, margen_por_dia=?,
                         unidades=?, tipo=?, motivo=?, categoria_clave=?,
                         reventa_medida=?, verificado=?, destacada=?, nivel=?,
                         media_30d=?, media_90d=?, minimo_historico=?,
                         n_lecturas=?, serie=?, vista_ultima=?,
                         estado='activa', expirada_en=NULL, razon_expiracion=NULL
                       WHERE clave=?""",
                    (op.precio_efectivo, op.referencia_mercado, descuento,
                     op.referencia_reventa, op.margen_neto_eur, op.margen_esperado_eur,
                     op.margen_pct, op.margen_por_dia, op.unidades_recomendadas,
                     str(op.tipo.value), op.motivo, op.categoria,
                     int(op.reventa_medida), int(op.verificado),
                     int(op.destacada), int(op.nivel),
                     est["media_30d"], est["media_90d"], est["minimo"],
                     est["n_lecturas"], serie, ahora, clave),
                )
            else:
                cur.execute(
                    """INSERT INTO ofertas
                       (clave, tienda, titulo, url, imagen, marca, ean, categoria,
                        categoria_clave, tipo, precio, precio_anterior,
                        precio_referencia, descuento, precio_reventa, reventa_medida, verificado,
                        margen_neto, margen_esperado, margen_pct,
                        margen_por_dia, unidades, dias_venta, prob_cancelacion,
                        fuente_referencia, motivo, destacada, nivel,
                        media_30d, media_90d, minimo_historico, n_lecturas, serie,
                        vista_primera, vista_ultima, estado)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                               ?,?,?,?,?, 'activa')""",
                    (clave, o.tienda, o.titulo, o.url, o.imagen, o.marca, o.ean,
                     o.categoria, op.categoria, str(op.tipo.value), op.precio_efectivo,
                     o.precio_anterior, op.referencia_mercado, descuento,
                     op.referencia_reventa, int(op.reventa_medida),
                     int(op.verificado), op.margen_neto_eur, op.margen_esperado_eur,
                     op.margen_pct, op.margen_por_dia, op.unidades_recomendadas,
                     op.dias_venta_estimados, op.prob_cancelacion, fuente, op.motivo,
                     int(op.destacada), int(op.nivel),
                     est["media_30d"], est["media_90d"], est["minimo"],
                     est["n_lecturas"], serie, ahora, ahora),
                )
        return not existe

    def expirar_ofertas(self, minutos: int = 180) -> int:
        """Una oferta que lleva rato sin volver a detectarse ya no existe."""
        limite = (datetime.utcnow() - timedelta(minutes=minutos)).isoformat()
        ahora = datetime.utcnow().isoformat()
        with self.cursor() as cur:
            cur.execute(
                """UPDATE ofertas SET estado='expirada', expirada_en=?,
                       razon_expiracion='ya no se detecta'
                   WHERE estado='activa' AND vista_ultima < ? AND guardada = 0""",
                (ahora, limite),
            )
            return cur.rowcount

    def ofertas_activas(self, limite: int = 200, tipo: Optional[str] = None,
                        orden: str = "margen_por_dia",
                        categoria: Optional[str] = None,
                        estado: str = "activa") -> list[dict]:
        # Lista blanca: el nombre de columna no puede parametrizarse en SQL,
        # asi que se valida contra un conjunto cerrado.
        columnas_validas = {"margen_por_dia", "margen_neto", "descuento",
                            "precio", "vista_primera", "vista_ultima", "nivel"}
        if orden not in columnas_validas:
            orden = "margen_por_dia"
        estado = estado if estado in ("activa", "expirada", "todas") else "activa"

        sql = "SELECT * FROM ofertas WHERE 1=1"
        params: list = []
        if estado != "todas":
            sql += " AND estado = ?"
            params.append(estado)
        if tipo:
            sql += " AND tipo = ?"
            params.append(tipo)
        if categoria:
            sql += " AND categoria_clave = ?"
            params.append(categoria)
        sql += f" ORDER BY {orden} DESC LIMIT ?"
        params.append(limite)
        with self.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def categorias_con_ofertas(self, estado: str = "activa") -> list[dict]:
        """Para pintar las pestañas con su contador, como una tienda."""
        with self.cursor() as cur:
            cur.execute(
                """SELECT categoria_clave AS clave, COUNT(*) AS n
                   FROM ofertas WHERE estado = ?
                   GROUP BY categoria_clave ORDER BY n DESC""",
                (estado,),
            )
            return [dict(r) for r in cur.fetchall()]

    def historico_producto(self, clave_match: str, dias: int = 365) -> list[dict]:
        """Todo lo que se ha visto de un producto. Nada se tira."""
        desde = (datetime.utcnow() - timedelta(days=dias)).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """SELECT tienda, precio, disponible, visto_en FROM lecturas
                   WHERE clave_match = ? AND visto_en >= ?
                   ORDER BY visto_en DESC LIMIT 2000""",
                (clave_match, desde),
            )
            return [dict(r) for r in cur.fetchall()]

    def marcar_guardada(self, clave: str, guardada: bool = True) -> None:
        """Las guardadas no expiran solas: te las quedas hasta que decidas."""
        with self.cursor() as cur:
            cur.execute("UPDATE ofertas SET guardada=? WHERE clave=?",
                        (int(guardada), clave))

    def resumen(self) -> dict:
        with self.cursor() as cur:
            cur.execute("""SELECT
                 (SELECT COUNT(*) FROM ofertas WHERE estado='activa') AS activas,
                 (SELECT COUNT(*) FROM ofertas WHERE estado='expirada') AS expiradas,
                 (SELECT COUNT(*) FROM lecturas) AS lecturas,
                 (SELECT COUNT(DISTINCT url) FROM lecturas) AS articulos,
                 (SELECT COUNT(*) FROM tiendas WHERE activa = 1) AS tiendas_lista,
                 (SELECT COUNT(*) FROM tiendas
                   WHERE activa = 1 AND fallos_seguidos >= 3) AS tiendas_rotas,
                 (SELECT COUNT(DISTINCT tienda) FROM ofertas WHERE estado='activa')
                   AS tiendas_con_oferta,
                 (SELECT COUNT(DISTINCT tienda) FROM lecturas) AS tiendas_vigiladas,
                 (SELECT IFNULL(SUM(margen_esperado),0) FROM ofertas WHERE estado='activa')
                   AS margen_total,
                 (SELECT MAX(vista_ultima) FROM ofertas) AS ultima""")
            r = dict(cur.fetchone())
        r["tiendas"] = r.get("tiendas_vigiladas") or r.get("tiendas_con_oferta") or 0
        return r

    def podar(self, dias_detalle: int = 120, dias_maximo: int = 730) -> dict:
        """Adelgaza el histórico sin perder la serie de precios.

        Más allá de `dias_detalle` se queda UNA lectura por producto y día
        (la primera), que es lo que necesita una gráfica. Más allá de
        `dias_maximo` se borra. Conviene ejecutarlo una vez al día.
        """
        limite_detalle = (datetime.utcnow() - timedelta(days=dias_detalle)).isoformat()
        limite_maximo = (datetime.utcnow() - timedelta(days=dias_maximo)).isoformat()
        with self.cursor() as cur:
            cur.execute("DELETE FROM lecturas WHERE visto_en < ?", (limite_maximo,))
            borradas_viejas = cur.rowcount
            cur.execute(
                """DELETE FROM lecturas WHERE id IN (
                       SELECT id FROM (
                           SELECT id, ROW_NUMBER() OVER (
                               PARTITION BY url, substr(visto_en, 1, 10)
                               ORDER BY visto_en) AS rn
                           FROM lecturas WHERE visto_en < ?
                       ) WHERE rn > 1)""",
                (limite_detalle,),
            )
            borradas_detalle = cur.rowcount
        # VACUUM no puede ir dentro de una transacción: devuelve al disco el
        # espacio de lo borrado, que es justo el objetivo de todo esto.
        if borradas_viejas or borradas_detalle:
            self._conn.isolation_level = None
            try:
                self._conn.execute("VACUUM")
            finally:
                self._conn.isolation_level = ""
        return {"antiguas": borradas_viejas, "resumidas": borradas_detalle}

    # ------------------------------------------------------------------ #
    # Estadisticas de precio: ¿esta oferta es buena de verdad?
    # ------------------------------------------------------------------ #
    def estadisticas_precio(self, clave_match: str, dias: int = 90) -> dict:
        """Media, mínimo y serie de precios de un producto.

        Es lo que contesta a "¿esto está barato o es su precio de siempre?".
        """
        vacio = {"media_30d": None, "media_90d": None, "minimo": None,
                 "maximo": None, "n_lecturas": 0, "serie": []}
        if not clave_match:
            return vacio
        desde = (datetime.utcnow() - timedelta(days=dias)).isoformat()
        desde30 = (datetime.utcnow() - timedelta(days=30)).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """SELECT precio, visto_en FROM lecturas
                   WHERE clave_match = ? AND visto_en >= ? AND disponible = 1
                   ORDER BY visto_en""",
                (clave_match, desde),
            )
            filas = cur.fetchall()
        if not filas:
            return vacio

        precios = [r["precio"] for r in filas]
        precios30 = [r["precio"] for r in filas if r["visto_en"] >= desde30]

        # Serie compacta para la mini-gráfica: 30 puntos como mucho.
        paso = max(1, len(precios) // 30)
        serie = [round(p, 2) for p in precios[::paso]][-30:]

        return {
            "media_30d": round(statistics.mean(precios30), 2) if precios30 else None,
            "media_90d": round(statistics.mean(precios), 2),
            "minimo": round(min(precios), 2),
            "maximo": round(max(precios), 2),
            "n_lecturas": len(precios),
            "serie": serie,
        }

    # ------------------------------------------------------------------ #
    # Cotizaciones de reventa (historico propio)
    # ------------------------------------------------------------------ #
    def guardar_cotizaciones(self, clave_match: str, cotizaciones: list) -> int:
        filas = [
            (clave_match, c.consulta, c.fuente, c.precio, c.precio_garantizado,
             c.n_muestras, c.dias_venta, c.url, c.consultada.isoformat())
            for c in cotizaciones
        ]
        if not filas:
            return 0
        with self.cursor() as cur:
            cur.executemany(
                """INSERT INTO reventa_cotizaciones
                   (clave_match, consulta, fuente, precio, precio_garantizado,
                    n_muestras, dias_venta, url, consultada)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                filas,
            )
        return len(filas)

    def cotizacion_fresca(self, clave_match: str, horas: int = 72) -> bool:
        """¿Ya hemos preguntado por esto hace poco? No repetir sin necesidad."""
        desde = (datetime.utcnow() - timedelta(hours=horas)).isoformat()
        with self.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM reventa_cotizaciones
                   WHERE clave_match = ? AND consultada >= ? LIMIT 1""",
                (clave_match, desde),
            )
            return cur.fetchone() is not None

    def historico_cotizaciones(self, clave_match: str, limite: int = 200) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(
                """SELECT * FROM reventa_cotizaciones WHERE clave_match = ?
                   ORDER BY consultada DESC LIMIT ?""",
                (clave_match, limite),
            )
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Salud del sistema (para enseñarla tambien en la web, no solo en Telegram)
    # ------------------------------------------------------------------ #
    def salud_tiendas(self) -> dict:
        with self.cursor() as cur:
            cur.execute(
                """SELECT dominio, plataforma, fallos_seguidos, ultimo_barrido
                   FROM tiendas WHERE activa = 1
                   ORDER BY fallos_seguidos DESC, ultimo_barrido ASC""")
            filas = [dict(r) for r in cur.fetchall()]
        rotas = [f for f in filas if (f["fallos_seguidos"] or 0) >= 3]
        flojas = [f for f in filas if 0 < (f["fallos_seguidos"] or 0) < 3]
        return {
            "total": len(filas),
            "ok": len(filas) - len(rotas) - len(flojas),
            "con_avisos": len(flojas),
            "rotas": len(rotas),
            "detalle_rotas": rotas[:30],
            "detalle_avisos": flojas[:30],
        }

    # ------------------------------------------------------------------ #
    # Sesiones de la web
    # ------------------------------------------------------------------ #
    def crear_sesion(self, token_hash: str, usuario: str, ip: str,
                     duracion_seg: int) -> None:
        ahora = datetime.utcnow()
        with self.cursor() as cur:
            cur.execute("DELETE FROM sesiones WHERE expira < ?", (ahora.isoformat(),))
            cur.execute(
                """INSERT OR REPLACE INTO sesiones
                   (token_hash, usuario, ip, creada, expira) VALUES (?,?,?,?,?)""",
                (token_hash, usuario, ip, ahora.isoformat(),
                 (ahora + timedelta(seconds=duracion_seg)).isoformat()),
            )

    def sesion_valida(self, token_hash: str) -> Optional[str]:
        with self.cursor() as cur:
            cur.execute(
                "SELECT usuario FROM sesiones WHERE token_hash = ? AND expira > ?",
                (token_hash, datetime.utcnow().isoformat()),
            )
            r = cur.fetchone()
        return r["usuario"] if r else None

    def cerrar_sesion(self, token_hash: str) -> None:
        with self.cursor() as cur:
            cur.execute("DELETE FROM sesiones WHERE token_hash = ?", (token_hash,))

    # ------------------------------------------------------------------ #
    # Registro de compras: el bucle que hace que el sistema aprenda
    # ------------------------------------------------------------------ #
    def registrar_compra(self, oferta: dict, precio: float, unidades: int = 1,
                         notas: str = "") -> int:
        with self.cursor() as cur:
            cur.execute(
                """INSERT INTO operaciones
                   (ean, titulo, tienda, precio_compra, unidades, comprada_en, notas)
                   VALUES (?,?,?,?,?,?,?)""",
                (oferta.get("ean"), oferta.get("titulo"), oferta.get("tienda"),
                 precio, unidades, datetime.utcnow().isoformat(), notas),
            )
            return cur.lastrowid

    def actualizar_compra(self, id_op: int, cancelada: Optional[bool] = None,
                          precio_venta: Optional[float] = None,
                          plataforma: Optional[str] = None) -> None:
        campos, valores = [], []
        if cancelada is not None:
            campos.append("cancelada = ?")
            valores.append(int(cancelada))
        if precio_venta is not None:
            campos += ["precio_venta = ?", "vendida_en = ?"]
            valores += [precio_venta, datetime.utcnow().isoformat()]
        if plataforma:
            campos.append("plataforma_venta = ?")
            valores.append(plataforma)
        if not campos:
            return
        valores.append(id_op)
        with self.cursor() as cur:
            cur.execute(f"UPDATE operaciones SET {', '.join(campos)} WHERE id = ?",
                        valores)

    def compras(self, limite: int = 200) -> list[dict]:
        with self.cursor() as cur:
            cur.execute("SELECT * FROM operaciones ORDER BY comprada_en DESC LIMIT ?",
                        (limite,))
            filas = [dict(r) for r in cur.fetchall()]
        for f in filas:
            f["estado"] = ("cancelada" if f["cancelada"]
                           else "vendida" if f["precio_venta"] else "en stock")
            if f["precio_venta"]:
                f["beneficio"] = round(
                    f["precio_venta"] - f["precio_compra"] * (f["unidades"] or 1), 2)
                if f["vendida_en"] and f["comprada_en"]:
                    f["dias"] = (datetime.fromisoformat(f["vendida_en"])
                                 - datetime.fromisoformat(f["comprada_en"])).days
        return filas

    def balance(self) -> dict:
        """Lo que de verdad has ganado, no lo que el bot estimó."""
        with self.cursor() as cur:
            cur.execute("""SELECT
                COUNT(*) AS total,
                SUM(cancelada) AS canceladas,
                SUM(CASE WHEN precio_venta IS NOT NULL THEN 1 ELSE 0 END) AS vendidas,
                SUM(CASE WHEN cancelada = 0 AND precio_venta IS NULL
                         THEN precio_compra * unidades ELSE 0 END) AS capital_parado,
                SUM(CASE WHEN precio_venta IS NOT NULL
                         THEN precio_venta - precio_compra * unidades
                         ELSE 0 END) AS beneficio_real
                FROM operaciones""")
            r = dict(cur.fetchone())
        r = {k: (v or 0) for k, v in r.items()}
        r["beneficio_real"] = round(r["beneficio_real"], 2)
        r["capital_parado"] = round(r["capital_parado"], 2)
        r["tasa_cancelacion"] = (round(r["canceladas"] / r["total"], 3)
                                 if r["total"] else None)
        return r

    def tasa_cancelacion_tienda(self, tienda: str, minimo: int = 5) -> Optional[float]:
        """Con suficientes compras registradas, la probabilidad de cancelación
        de ESA tienda deja de ser una estimación mía y pasa a ser tu dato."""
        with self.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS n, SUM(cancelada) AS canceladas
                   FROM operaciones WHERE tienda = ?""", (tienda,))
            r = cur.fetchone()
        if not r or (r["n"] or 0) < minimo:
            return None
        return round((r["canceladas"] or 0) / r["n"], 3)

    def cerrar(self) -> None:
        self._conn.close()
