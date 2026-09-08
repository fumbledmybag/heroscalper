"""Adapter WooCommerce (Store API publica).

41.006 tiendas en España: es la plataforma mas numerosa con diferencia.
La Store API en /wp-json/wc/store/products devuelve JSON limpio con
`regular_price` y `sale_price`, o sea el descuento oficial servido en bandeja.
"""
from __future__ import annotations

import json
from typing import Optional

from ..models import Oferta
from .base import Adapter, a_float, normaliza_ean

RUTA = "/wp-json/wc/store/v1/products"
RUTA_LEGACY = "/wp-json/wc/store/products"


class WooCommerceAdapter(Adapter):
    nombre = "woocommerce"

    async def _ruta_valida(self, dominio: str) -> Optional[str]:
        for ruta in (RUTA, RUTA_LEGACY):
            resp = await self.cliente.get(f"https://{dominio}{ruta}?per_page=1")
            if resp is not None and resp.status_code == 200:
                try:
                    if isinstance(resp.json(), list):
                        return ruta
                except (json.JSONDecodeError, ValueError):
                    continue
        return None

    async def detecta(self, dominio: str) -> bool:
        return await self._ruta_valida(dominio) is not None

    async def catalogo(self, dominio: str, max_productos: int = 5000) -> list[Oferta]:
        ruta = await self._ruta_valida(dominio)
        if not ruta:
            return []
        ofertas: list[Oferta] = []
        pagina = 1
        while len(ofertas) < max_productos:
            resp = await self.cliente.get(
                f"https://{dominio}{ruta}?per_page=100&page={pagina}"
            )
            if resp is None or resp.status_code != 200:
                break
            try:
                productos = resp.json()
            except (json.JSONDecodeError, ValueError):
                break
            if not productos:
                break
            for p in productos:
                o = self._a_oferta(p, dominio)
                if o:
                    ofertas.append(o)
            pagina += 1
            if pagina > 60:
                break
        return ofertas[:max_productos]

    async def outlet(self, dominio: str) -> list[Oferta]:
        """Woo marca `on_sale`: filtramos por ahi directamente."""
        ruta = await self._ruta_valida(dominio)
        if not ruta:
            return []
        resp = await self.cliente.get(f"https://{dominio}{ruta}?on_sale=true&per_page=100")
        if resp is None or resp.status_code != 200:
            return []
        try:
            productos = resp.json()
        except (json.JSONDecodeError, ValueError):
            return []
        ofertas = []
        for p in productos:
            o = self._a_oferta(p, dominio)
            if o:
                o.es_liquidacion = True
                ofertas.append(o)
        return ofertas

    async def producto(self, url: str) -> Optional[Oferta]:
        # La Store API no resuelve por URL; se cae al adapter JSON-LD.
        return None

    # ------------------------------------------------------------------ #
    def _a_oferta(self, p: dict, dominio: str) -> Optional[Oferta]:
        precios = p.get("prices", {}) or {}
        # Woo devuelve enteros en la menor unidad monetaria.
        divisor = 10 ** int(precios.get("currency_minor_unit", 2) or 2)
        precio = a_float(precios.get("price"))
        if precio is None:
            return None
        precio = precio / divisor
        regular = a_float(precios.get("regular_price"))
        regular = regular / divisor if regular else None
        if precio <= 0:
            return None

        cats = [c.get("name", "") for c in (p.get("categories") or [])]
        marca = None
        for attr in (p.get("attributes") or []):
            if str(attr.get("name", "")).lower() in ("marca", "brand"):
                terms = attr.get("terms") or []
                if terms:
                    marca = terms[0].get("name")

        return Oferta(
            tienda=dominio,
            url=p.get("permalink", f"https://{dominio}"),
            titulo=p.get("name", ""),
            precio=precio,
            precio_anterior=regular if regular and regular > precio else None,
            ean=normaliza_ean(p.get("sku")),   # muchas tiendas ES usan el EAN como SKU
            marca=marca,
            sku=str(p.get("sku") or p.get("id") or ""),
            disponible=bool(p.get("is_in_stock", True)),
            categoria=", ".join(cats) or None,
            imagen=((p.get('images') or [{}])[0] or {}).get('src'),
            es_liquidacion=bool(p.get("on_sale")),
            plataforma="woocommerce",
            grupo_id=f"{dominio}:{p.get('parent') or p.get('id')}",
        )
