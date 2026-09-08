"""Adapter de Amazon vía Keepa.

Amazon no se puede rastrear directamente de forma fiable: es la tienda con más
protecciones y la que antes te bloquea. La vía practicable es Keepa, que lleva
años guardando el histórico de precios de todos los Amazon europeos y expone
un endpoint de "deals" — caídas de precio recientes — ya calculado.

Ventaja añadida: Keepa te da el precio medio histórico real del producto, que
es exactamente la referencia que necesitamos y la que Amazon nunca enseña.

Requiere una API key de pago (https://keepa.com/#!api). Sin ella, el adapter
se desactiva solo y el resto del bot sigue funcionando igual.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from ..models import Oferta
from .base import Adapter

# Códigos de dominio de Keepa. Verificar contra la documentación al activarlo.
DOMINIOS = {
    "amazon.com": 1, "amazon.co.uk": 2, "amazon.de": 3, "amazon.fr": 4,
    "amazon.it": 8, "amazon.es": 9, "amazon.nl": 12, "amazon.pl": 21,
}


class KeepaAdapter(Adapter):
    """Cubre los Amazon europeos. No scrapea: consulta la API de Keepa."""

    nombre = "keepa"

    def __init__(self, cliente, config: dict):
        super().__init__(cliente, config)
        cfg = (config.get("fuentes", {}) or {}).get("keepa", {}) or {}
        self.api_key = _resolver(cfg.get("api_key", "")) or os.environ.get("KEEPA_API_KEY")
        self.dominios = cfg.get("dominios", ["amazon.es"])
        self.caida_min = float(cfg.get("caida_min_pct", 50))
        self.max_deals = int(cfg.get("max_deals", 150))

    @property
    def activo(self) -> bool:
        return bool(self.api_key)

    async def detecta(self, dominio: str) -> bool:
        return dominio in DOMINIOS and self.activo

    async def catalogo(self, dominio: str, max_productos: int = 5000) -> list[Oferta]:
        """En Keepa el 'catálogo' útil son directamente las caídas de precio."""
        return await self.deals(dominio, max_productos)

    async def producto(self, url: str) -> Optional[Oferta]:
        return None

    # ------------------------------------------------------------------ #
    async def deals(self, dominio: str = "amazon.es",
                    limite: Optional[int] = None) -> list[Oferta]:
        if not self.activo:
            return []
        cod = DOMINIOS.get(dominio)
        if cod is None:
            return []

        seleccion = {
            "domainId": cod,
            "priceTypes": [0],               # 0 = precio Amazon
            "deltaPercentRange": [int(self.caida_min), 100],
            "sortType": 4,                   # por porcentaje de caída
            "isRangeEnabled": True,
            "isFilterEnabled": False,
            "page": 0,
        }
        url = (f"https://api.keepa.com/deal?key={self.api_key}"
               f"&selection={json.dumps(seleccion, separators=(',', ':'))}")
        resp = await self.cliente.get(url)
        if resp is None or resp.status_code != 200:
            return []
        try:
            datos = resp.json()
        except (json.JSONDecodeError, ValueError):
            return []

        crudos = (datos.get("deals") or {}).get("dr") or []
        tope = limite or self.max_deals
        return [o for o in (self._a_oferta(d, dominio) for d in crudos[:tope]) if o]

    # ------------------------------------------------------------------ #
    def _a_oferta(self, d: dict, dominio: str) -> Optional[Oferta]:
        asin = d.get("asin")
        if not asin:
            return None

        # Keepa devuelve los precios en céntimos; -1 significa "sin dato".
        actual = _centimos(_primero(d.get("current")))
        if actual is None:
            return None
        # `avg` trae las medias por ventana (día, semana, mes, trimestre).
        # La del trimestre es la referencia honesta: no la infla una campaña.
        medias = d.get("avg") or []
        referencia = None
        for ventana in (3, 2, 1, 0):
            if len(medias) > ventana:
                referencia = _centimos(_primero(medias[ventana]))
                if referencia:
                    break

        imagen = d.get("image")
        if isinstance(imagen, (bytes, bytearray)):
            imagen = imagen.decode("utf-8", "ignore")
        img_url = (f"https://m.media-amazon.com/images/I/{imagen}"
                   if imagen and not str(imagen).startswith("http") else imagen)

        return Oferta(
            tienda=dominio,
            url=f"https://www.{dominio}/dp/{asin}",
            titulo=str(d.get("title") or asin)[:300],
            precio=actual,
            # El "precio anterior" aquí NO es el PVP inflado del vendedor:
            # es la media histórica real que ha medido Keepa.
            precio_anterior=referencia,
            ean=None,
            marca=None,
            sku=asin,
            disponible=True,
            categoria=str(d.get("rootCat") or "") or None,
            imagen=str(img_url) if img_url else None,
            plataforma="keepa",
            grupo_id=f"{dominio}:{asin}",
        )


def _primero(valor):
    if isinstance(valor, list):
        return valor[0] if valor else None
    return valor


def _centimos(valor) -> Optional[float]:
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return None
    return None if v < 0 else round(v / 100.0, 2)


def _resolver(valor: str) -> Optional[str]:
    if not valor:
        return None
    if valor.startswith("${") and valor.endswith("}"):
        return os.environ.get(valor[2:-1])
    return valor
