"""Promociones: liquidaciones, campañas y promos multi-unidad.

La parte interesante es la multi-unidad. Un 3x2 sobre algo con un 20% de
margen lo convierte en un 46% efectivo, y casi ningun bot lo calcula porque
mira el precio de etiqueta en vez del precio por unidad realmente pagado.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .models import Oferta, TipoOportunidad

RE_NXM = re.compile(r"\b(\d)\s*x\s*(\d)\b", re.IGNORECASE)

# --- Ofertas gratuitas y de dinero fácil ---
RE_GRATIS = re.compile(
    r"\b(gratis|de regalo|te regalamos|0[,.]00\s*€|sin coste|"
    r"llevate.{0,15}gratis|segunda unidad gratis|"
    r"te devolvemos|cashback|reembolso del 100|bono de bienvenida|"
    r"bonus de registro)\b",
    re.IGNORECASE,
)
# Lo que convierte una "promo gratis" en un marrón: permanencias, nóminas,
# contrataciones. Eso NO es dinero fácil y no queremos verlo.
RE_LETRA_PEQUENA = re.compile(
    r"\b(nomina|n[oó]mina|domicilia|permanencia|alta de l[ií]nea|"
    r"contrataci[oó]n|hipoteca|seguro de|plan de pensiones|"
    r"aportaci[oó]n m[ií]nima|financiaci[oó]n|tarjeta de cr[eé]dito|"
    r"12 meses|24 meses|cuota mensual)\b",
    re.IGNORECASE,
)
RE_SEGUNDA = re.compile(
    r"segunda\s+unidad\s+(?:a[l]?\s+)?(?:-\s*)?(\d{1,2})\s*%", re.IGNORECASE
)
RE_PCT_SEGUNDA = re.compile(r"(\d{1,2})\s*%\s+(?:de\s+dto\.?\s+)?en\s+la\s+segunda",
                            re.IGNORECASE)


@dataclass
class Multiunidad:
    unidades: int
    unidades_pagadas: float
    etiqueta: str

    @property
    def factor(self) -> float:
        """Precio efectivo por unidad = precio * factor."""
        return self.unidades_pagadas / self.unidades


def parsear_multiunidad(texto: Optional[str]) -> Optional[Multiunidad]:
    """Convierte '3x2' o 'segunda unidad -50%' en un factor de precio real."""
    if not texto:
        return None
    t = texto.lower()

    m = RE_NXM.search(t)
    if m:
        llevas, pagas = int(m.group(1)), int(m.group(2))
        if 1 < llevas <= 6 and 0 < pagas < llevas:
            return Multiunidad(llevas, float(pagas), f"{llevas}x{pagas}")

    for regex in (RE_SEGUNDA, RE_PCT_SEGUNDA):
        m = regex.search(t)
        if m:
            dto = int(m.group(1))
            # "segunda unidad al 70%" = pagas el 70%; "-50%" = pagas el 50%.
            paga_pct = dto / 100.0 if "al " in t or "a l" in t else 1 - dto / 100.0
            paga_pct = min(max(paga_pct, 0.0), 1.0)
            return Multiunidad(2, 1.0 + paga_pct, f"2ª unidad {dto}%")

    return None


def es_gratis(oferta: Oferta, cfg: dict) -> tuple[bool, str]:
    """¿Es una oferta gratuita de las buenas, o de las que llevan letra pequeña?"""
    promos = cfg.get("deteccion", {}).get("promos", {})
    if not promos.get("detectar_gratis", True):
        return False, ""
    texto = f"{oferta.titulo} {oferta.promo_texto or ''} {oferta.categoria or ''}"

    gratis_por_precio = oferta.precio <= float(promos.get("umbral_gratis_eur", 0.5))
    marca_gratis = RE_GRATIS.search(texto)
    if not (gratis_por_precio or marca_gratis):
        return False, ""

    etiqueta = marca_gratis.group(0) if marca_gratis else "precio 0 €"
    trampa = RE_LETRA_PEQUENA.search(texto)
    if trampa:
        # Nómina, permanencia, contratación: se ENSEÑA igualmente, pero
        # avisando de lo que pide. Tú decides si te compensa.
        if promos.get("descartar_letra_pequena", False):
            return False, f"descartada: exige {trampa.group(0)}"
        return True, f"Oferta gratuita ({etiqueta}) ⚠ exige {trampa.group(0)}"

    return True, f"Oferta gratuita ({etiqueta})"


def clasificar(
    oferta: Oferta,
    mediana_mercado: Optional[float],
    cfg: dict,
) -> tuple[TipoOportunidad, float, int, str]:
    """Decide el tipo de oportunidad y el precio efectivo por unidad.

    Devuelve (tipo, precio_efectivo, unidades_recomendadas, motivo).
    """
    det = cfg.get("deteccion", {})
    promos_cfg = det.get("promos", {})
    precio = oferta.precio
    unidades = 1
    motivo = ""

    # --- 0. Gratis: va primero, es la categoría más golosa y la más rara ---
    gratis, nota = es_gratis(oferta, cfg)
    if gratis:
        return TipoOportunidad.GRATIS, precio, 1, nota

    # --- 1. Promo multi-unidad: recalcula el precio REAL por unidad ---
    multi = parsear_multiunidad(oferta.promo_texto)
    if multi:
        precio = oferta.precio * multi.factor
        unidades = multi.unidades
        motivo = f"Promo {multi.etiqueta}: {oferta.precio:.2f} € → {precio:.2f} €/ud"

    # --- 2. Error de precio (cruce con el resto del mercado) ---
    # Se compara el precio DE ETIQUETA, no el efectivo tras promo: si la caida
    # la explica un 3x2, es una promocion legitima y no un error, y su riesgo
    # de cancelacion es completamente distinto.
    if mediana_mercado and mediana_mercado > 0:
        ratio = oferta.precio / mediana_mercado
        if ratio <= det.get("ratio_anomalia_extrema", 0.25):
            return (
                TipoOportunidad.ERROR_PRECIO_EXTREMO, precio, unidades,
                f"{oferta.precio:.2f} € frente a {mediana_mercado:.2f} € de mediana "
                f"({(1-ratio)*100:.0f}% por debajo) — probable error de precio",
            )
        if ratio <= det.get("ratio_anomalia_cross_tienda", 0.55) and not oferta.es_liquidacion:
            return (
                TipoOportunidad.ERROR_PRECIO, precio, unidades,
                f"{oferta.precio:.2f} € frente a {mediana_mercado:.2f} € de mediana "
                f"({(1-ratio)*100:.0f}% por debajo)",
            )

    # --- 3. Liquidación / outlet ---
    if oferta.es_liquidacion:
        return (
            TipoOportunidad.LIQUIDACION, precio, unidades,
            motivo or "Marcado como outlet/liquidación por la tienda",
        )

    # --- 4. Promo multi-unidad sin anomalía de precio ---
    if multi:
        return TipoOportunidad.PROMO_MULTIUNIDAD, precio, unidades, motivo

    # --- 5. Campaña: descuento oficial declarado por la tienda ---
    dto = oferta.descuento_declarado
    if dto and dto >= promos_cfg.get("descuento_campana_min", 0.40):
        return (
            TipoOportunidad.PROMO_CAMPANA, precio, unidades,
            f"Descuento oficial del {dto*100:.0f}% "
            f"({oferta.precio_anterior:.2f} € → {oferta.precio:.2f} €)",
        )

    return TipoOportunidad.PROMO_CAMPANA, precio, unidades, motivo


def es_descatalogado(historico_disponibilidad: list[bool], min_lecturas: int = 5) -> bool:
    """Producto que desaparece del catálogo: en 2ª mano suele subir de precio.

    Es la señal que hace rentable el LEGO descatalogado.
    """
    if len(historico_disponibilidad) < min_lecturas:
        return False
    return historico_disponibilidad[-1] is False and any(historico_disponibilidad[:-2])
