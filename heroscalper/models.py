"""Tipos de datos que circulan por el sistema."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class TipoOportunidad(str, Enum):
    """El tipo condiciona el riesgo de cancelacion y la urgencia."""

    ERROR_PRECIO_EXTREMO = "ERROR_PRECIO_EXTREMO"   # 200 -> 20, decimal desplazado
    ERROR_PRECIO = "ERROR_PRECIO"                   # anomalia clara pero no absurda
    PROMO_MULTIUNIDAD = "PROMO_MULTIUNIDAD"         # 3x2, 2x1, 2a unidad -50%
    PROMO_CAMPANA = "PROMO_CAMPANA"                 # descuento oficial fuerte
    LIQUIDACION = "LIQUIDACION"                     # outlet, fin de serie
    DESCATALOGADO = "DESCATALOGADO"                 # sube de valor en 2a mano
    GRATIS = "GRATIS"                               # regalo, 0 EUR, cashback


# Urgencia: cuanto dura tipicamente la ventana antes de que se corrija/agote.
URGENCIA_MINUTOS = {
    TipoOportunidad.ERROR_PRECIO_EXTREMO: 15,
    TipoOportunidad.ERROR_PRECIO: 60,
    TipoOportunidad.PROMO_MULTIUNIDAD: 60 * 24,
    TipoOportunidad.PROMO_CAMPANA: 60 * 24 * 3,
    TipoOportunidad.LIQUIDACION: 60 * 24 * 7,
    TipoOportunidad.DESCATALOGADO: 60 * 24 * 14,
    TipoOportunidad.GRATIS: 60 * 12,
}


@dataclass
class Oferta:
    """Una lectura de precio de un producto en una tienda concreta."""

    tienda: str                       # dominio
    url: str
    titulo: str
    precio: float
    moneda: str = "EUR"
    ean: Optional[str] = None
    marca: Optional[str] = None
    sku: Optional[str] = None
    disponible: bool = True
    stock: Optional[int] = None
    # Precio tachado que declara la propia tienda (compare_at / regular_price).
    precio_anterior: Optional[float] = None
    # Texto de promo detectado en la ficha ("3x2", "2a unidad -50%"...)
    promo_texto: Optional[str] = None
    # Marcadores de liquidacion (categoria outlet, badge de ultimas unidades)
    es_liquidacion: bool = False
    peso_kg: Optional[float] = None
    dimension_max_cm: Optional[float] = None
    categoria: Optional[str] = None
    imagen: Optional[str] = None
    condicion: str = "nuevo"          # nuevo | reacondicionado | usado
    plataforma: Optional[str] = None  # shopify | woocommerce | prestashop | jsonld
    # Id del producto padre: permite comparar variantes hermanas entre si
    grupo_id: Optional[str] = None
    visto_en: datetime = field(default_factory=datetime.utcnow)

    @property
    def descuento_declarado(self) -> Optional[float]:
        """Descuento oficial segun la propia tienda, 0-1."""
        if not self.precio_anterior or self.precio_anterior <= 0:
            return None
        if self.precio_anterior <= self.precio:
            return None
        return 1.0 - (self.precio / self.precio_anterior)

    def clave(self) -> str:
        """Identidad de ESTA oferta (para dedupe de alertas)."""
        return self.ean or f"{self.tienda}:{self.sku or self.url}"

    def clave_match(self) -> str:
        """Identidad del PRODUCTO, para cruzarlo entre tiendas."""
        from .matching import clave_producto
        return clave_producto(self.marca, self.titulo, self.ean)

    def fiabilidad_match(self) -> float:
        from .matching import fiabilidad
        return fiabilidad(self.clave_match())


@dataclass
class ReferenciaReventa:
    """Lo que se paga de verdad por este producto en 2a mano."""

    ean: Optional[str]
    modelo: str
    # Mediana de precios de venta CONFIRMADA (no de anuncios activos).
    precio_venta_mediano: float
    anuncios_90d: int
    dias_venta_mediano: float
    plataforma: str = "wallapop"       # wallapop | vinted | cex | manual
    # Suelo garantizado (p. ej. precio de compra de CeX), si existe.
    precio_suelo: Optional[float] = None
    actualizado: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Oportunidad:
    """Una oferta que ha pasado todos los filtros y merece una alerta."""

    oferta: Oferta
    tipo: TipoOportunidad
    precio_efectivo: float             # tras promo multi-unidad
    unidades_recomendadas: int
    referencia_mercado: float          # mediana retail del mismo EAN
    referencia_reventa: Optional[float]
    margen_bruto_eur: float
    margen_neto_eur: float             # tras porte, comisiones y overhead
    margen_esperado_eur: float         # tras descontar riesgo de cancelacion
    margen_pct: float
    dias_venta_estimados: float
    prob_cancelacion: float
    motivo: str = ""
    # ¿La reventa es un dato MEDIDO (Wallapop/CeX cargados) o una estimación?
    # Importa: un beneficio calculado sobre una estimación no es un beneficio.
    reventa_medida: bool = False
    categoria: str = "otros"
    # ¿El precio de referencia viene de una fuente INDEPENDIENTE (otras
    # tiendas, histórico, variantes) o solo del precio tachado de la tienda?
    # Si es lo segundo, la oferta se enseña igual pero avisando.
    verificado: bool = True
    # 1 normal · 2 buena · 3 muy buena · 4 brutal
    nivel: int = 1
    # {media_30d, media_90d, minimo, maximo, n_lecturas, serie}
    historial: dict = field(default_factory=dict)

    @property
    def margen_por_dia(self) -> float:
        """El KPI que de verdad importa cuando reinviertes el capital."""
        return self.margen_esperado_eur / max(self.dias_venta_estimados, 1.0)

    @property
    def destacada(self) -> bool:
        """A partir de "muy buena" va resaltada arriba del todo."""
        return self.nivel >= 3

    @property
    def capital_necesario(self) -> float:
        return self.precio_efectivo * self.unidades_recomendadas


@dataclass
class EventoTienda:
    """Fallo masivo de catalogo en una tienda: varias anomalias a la vez."""

    tienda: str
    oportunidades: list
    detectado_en: datetime = field(default_factory=datetime.utcnow)

    @property
    def margen_total(self) -> float:
        return sum(o.margen_esperado_eur for o in self.oportunidades)
