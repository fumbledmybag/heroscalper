"""Adapter Shopify.

Casi todas las tiendas Shopify exponen /products.json publicamente: catalogo
completo con variantes, precios y `compare_at_price` (el precio tachado, que
nos da el descuento OFICIAL sin tener que calcularlo). ~3.900 tiendas en España.
"""
from __future__ import annotations

import json
from typing import Optional

from ..models import Oferta
from .base import Adapter, a_float, normaliza_ean

KEYWORDS_LIQUIDACION_DEFAULT = ("outlet", "liquidacion", "liquidación", "sale",
                                "clearance", "ultimas-unidades", "descatalogado")


class ShopifyAdapter(Adapter):
    nombre = "shopify"

    async def detecta(self, dominio: str) -> bool:
        resp = await self.cliente.get(f"https://{dominio}/products.json?limit=1")
        if resp is None or resp.status_code != 200:
            return False
        try:
            return "products" in resp.json()
        except (json.JSONDecodeError, ValueError):
            return False

    async def catalogo(self, dominio: str, max_productos: int = 5000) -> list[Oferta]:
        ofertas: list[Oferta] = []
        pagina = 1
        while len(ofertas) < max_productos:
            url = f"https://{dominio}/products.json?limit=250&page={pagina}"
            resp = await self.cliente.get(url)
            if resp is None or resp.status_code != 200:
                break
            try:
                productos = resp.json().get("products", [])
            except (json.JSONDecodeError, ValueError):
                break
            if not productos:
                break
            for p in productos:
                ofertas.extend(self._a_ofertas(p, dominio))
            pagina += 1
            if pagina > 40:   # cortafuegos
                break
        return ofertas[:max_productos]

    async def outlet(self, dominio: str) -> list[Oferta]:
        """Las colecciones de outlet en Shopify tambien son JSON publico."""
        ofertas: list[Oferta] = []
        rutas = self.config.get("deteccion", {}).get("promos", {}).get("rutas_outlet", [])
        for ruta in rutas:
            if not ruta.startswith("/collections"):
                continue
            resp = await self.cliente.get(f"https://{dominio}{ruta}/products.json?limit=250")
            if resp is None or resp.status_code != 200:
                continue
            try:
                productos = resp.json().get("products", [])
            except (json.JSONDecodeError, ValueError):
                continue
            for p in productos:
                for o in self._a_ofertas(p, dominio):
                    o.es_liquidacion = True
                    ofertas.append(o)
        return ofertas

    async def producto(self, url: str) -> Optional[Oferta]:
        base = url.split("?")[0].rstrip("/")
        resp = await self.cliente.get(f"{base}.json")
        if resp is None or resp.status_code != 200:
            return None
        try:
            p = resp.json().get("product")
        except (json.JSONDecodeError, ValueError):
            return None
        if not p:
            return None
        dominio = url.split("/")[2]
        ofertas = self._a_ofertas(p, dominio)
        return ofertas[0] if ofertas else None

    # ------------------------------------------------------------------ #
    def _a_ofertas(self, p: dict, dominio: str) -> list[Oferta]:
        ofertas = []
        handle = p.get("handle", "")
        tags = [str(t).lower() for t in (p.get("tags") or [])]
        tipo = str(p.get("product_type") or "")
        liquidacion = any(k in " ".join(tags + [tipo, handle]).lower()
                          for k in KEYWORDS_LIQUIDACION_DEFAULT)

        imagenes = p.get("images") or []
        img_principal = imagenes[0].get("src") if imagenes else None

        for v in p.get("variants", []):
            precio = a_float(v.get("price"))
            if precio is None or precio <= 0:
                continue
            anterior = a_float(v.get("compare_at_price"))
            titulo = p.get("title", "")
            if v.get("title") and v["title"] != "Default Title":
                titulo = f"{titulo} - {v['title']}"
            ofertas.append(
                Oferta(
                    tienda=dominio,
                    url=f"https://{dominio}/products/{handle}?variant={v.get('id')}",
                    titulo=titulo,
                    precio=precio,
                    precio_anterior=anterior if anterior and anterior > precio else None,
                    ean=normaliza_ean(v.get("barcode")),
                    marca=p.get("vendor"),
                    sku=str(v.get("sku") or v.get("id") or ""),
                    disponible=bool(v.get("available", True)),
                    peso_kg=(v.get("grams") or 0) / 1000.0 or None,
                    categoria=tipo or None,
                    imagen=(v.get('featured_image') or {}).get('src') or img_principal,
                    es_liquidacion=liquidacion,
                    plataforma="shopify",
                    grupo_id=f"{dominio}:{p.get('id')}",
                )
            )
        return ofertas
