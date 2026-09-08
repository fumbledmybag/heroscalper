"""Adapter generico schema.org / JSON-LD.

Es el comodin: casi todas las tiendas incrustan un bloque
<script type="application/ld+json"> con precio, disponibilidad y GTIN porque
lo necesitan para Google Shopping. Cubre PrestaShop (9.017 tiendas en España)
y todo el long tail. Si una tienda no tiene ni plataforma reconocible ni
JSON-LD, se descarta: no merece el mantenimiento.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser  # type: ignore

from ..models import Oferta
from .base import Adapter, a_float, normaliza_ean

RE_MULTIUNIDAD = re.compile(
    r"(\d\s?x\s?\d)"                                   # 3x2, 2x1
    r"|(segunda\s+unidad\s+(?:al\s+)?-?\s?\d{1,2}\s?%)"  # segunda unidad -50%
    r"|(\d{1,2}\s?%\s+en\s+la\s+segunda)",
    re.IGNORECASE,
)


class JsonLdAdapter(Adapter):
    """Funciona sobre cualquier tienda que publique schema.org/Product."""

    nombre = "jsonld"

    async def detecta(self, dominio: str) -> bool:
        resp = await self.cliente.get(f"https://{dominio}/")
        if resp is None or resp.status_code != 200:
            return False
        return "application/ld+json" in resp.text

    async def producto(self, url: str) -> Optional[Oferta]:
        resp = await self.cliente.get(url)
        if resp is None or resp.status_code != 200:
            return None
        return self.parsear(resp.text, url)

    async def catalogo(self, dominio: str, max_productos: int = 5000,
                       desde: int = 0, cambiados_desde: str = "") -> list[Oferta]:
        """Sin feed no hay barrido barato: hay que visitar producto por producto.

        Y aquí estaba el cuello de botella del sistema entero. Shopify y
        WooCommerce sueltan 250 y 100 productos por petición; aquí es UNA
        petición por producto. Con 4.000 productos son 4.000 peticiones: casi
        dos horas para una sola tienda, y el ciclo no termina nunca.

        La solución no es leer menos catálogo, es leerlo a trozos: cada ciclo
        se lee un tramo distinto del sitemap, y en unas horas se ha recorrido
        entero. Mientras tanto la sección de outlet, que es donde están las
        gangas de verdad, se lee siempre completa.
        """
        tope = int(self.config.get("rastreo", {})
                   .get("max_productos_generico", 200))
        tope = max(1, min(tope, max_productos))

        entradas = await self._urls_sitemap(dominio, 20000)
        if not entradas:
            return []

        # PRIMERO: lo que la tienda dice que ha tocado desde la última vez.
        # El sitemap trae <lastmod> por producto — lo ponen para Google, y a
        # nosotros nos sirve de aviso de cambio. Un catálogo de 4.000 fichas
        # cambia 30 o 40 al día: leer solo esas es la diferencia entre mirar
        # el catálogo entero cada vez y mirar lo que de verdad se ha movido.
        frescos = []
        if cambiados_desde:
            frescos = [u for u, mod in entradas if mod and mod > cambiados_desde]

        if frescos:
            trozo = frescos[:tope]
        else:
            # Sin lastmod utilizable (o primera pasada): ventana circular, para
            # ir recorriendo el catálogo entero a lo largo de unas horas.
            urls = [u for u, _ in entradas]
            inicio = desde % len(urls)
            trozo = urls[inicio:inicio + tope]
            if len(trozo) < tope:
                trozo += urls[:tope - len(trozo)]

        ofertas = []
        for u in trozo:
            o = await self.producto(u)
            if o:
                ofertas.append(o)
        return ofertas

    async def outlet(self, dominio: str) -> list[Oferta]:
        """Las páginas de liquidación: poco rastreo y mucha oportunidad.

        Ojo con el tope: aquí también hay que visitar ficha por ficha. Con 60
        productos por ruta y cuatro rutas eran 240 peticiones por tienda EN
        CADA CICLO, más caras que el propio catálogo. El tope es del total, y
        las rutas suelen repetir producto: se descartan los repetidos antes de
        gastar una petición en ellos.
        """
        cfg = self.config.get("deteccion", {}).get("promos", {})
        tope = int(self.config.get("rastreo", {})
                   .get("max_outlet_generico", 80))
        ofertas: list[Oferta] = []
        vistos: set[str] = set()

        for ruta in cfg.get("rutas_outlet", []):
            if len(vistos) >= tope:
                break
            url = f"https://{dominio}{ruta}"
            resp = await self.cliente.get(url)
            if resp is None or resp.status_code != 200:
                continue
            for enlace in self._enlaces_producto(resp.text, url):
                if len(vistos) >= tope:
                    break
                if enlace in vistos:
                    continue
                vistos.add(enlace)
                o = await self.producto(enlace)
                if o:
                    o.es_liquidacion = True
                    ofertas.append(o)
        return ofertas

    # ------------------------------------------------------------------ #
    def parsear(self, html: str, url: str) -> Optional[Oferta]:
        dominio = urlparse(url).netloc
        arbol = HTMLParser(html)
        producto = None
        for nodo in arbol.css('script[type="application/ld+json"]'):
            try:
                datos = json.loads(nodo.text())
            except (json.JSONDecodeError, ValueError):
                continue
            producto = self._buscar_producto(datos)
            if producto:
                break
        if not producto:
            return None

        oferta_ld = producto.get("offers")
        if isinstance(oferta_ld, list):
            oferta_ld = oferta_ld[0] if oferta_ld else {}
        if not isinstance(oferta_ld, dict):
            oferta_ld = {}

        precio = a_float(oferta_ld.get("price") or oferta_ld.get("lowPrice"))
        if precio is None or precio <= 0:
            return None

        disponibilidad = str(oferta_ld.get("availability", "")).lower()
        disponible = "outofstock" not in disponibilidad and "discontinued" not in disponibilidad

        marca = producto.get("brand")
        if isinstance(marca, dict):
            marca = marca.get("name")

        texto_plano = arbol.text()[:20000].lower()
        promo = RE_MULTIUNIDAD.search(texto_plano)
        keywords = self.config.get("deteccion", {}).get("promos", {}).get(
            "keywords_liquidacion", [])
        liquidacion = any(k in texto_plano or k in url.lower() for k in keywords)

        imagen = producto.get("image")
        if isinstance(imagen, list):
            imagen = imagen[0] if imagen else None
        if isinstance(imagen, dict):
            imagen = imagen.get("url")

        return Oferta(
            tienda=dominio,
            url=url,
            titulo=str(producto.get("name", ""))[:300],
            precio=precio,
            moneda=str(oferta_ld.get("priceCurrency", "EUR")),
            ean=normaliza_ean(producto.get("gtin13") or producto.get("gtin")
                              or producto.get("gtin14") or producto.get("gtin8")
                              or producto.get("mpn")),
            marca=str(marca) if marca else None,
            sku=str(producto.get("sku") or ""),
            disponible=disponible,
            promo_texto=promo.group(0) if promo else None,
            es_liquidacion=liquidacion,
            imagen=str(imagen) if imagen else None,
            plataforma="jsonld",
        )

    def _buscar_producto(self, datos: Any) -> Optional[dict]:
        """El JSON-LD puede venir suelto, en lista o dentro de un @graph."""
        if isinstance(datos, list):
            for d in datos:
                r = self._buscar_producto(d)
                if r:
                    return r
            return None
        if not isinstance(datos, dict):
            return None
        tipo = datos.get("@type", "")
        tipos = tipo if isinstance(tipo, list) else [tipo]
        if any(str(t).lower() == "product" for t in tipos):
            return datos
        if "@graph" in datos:
            return self._buscar_producto(datos["@graph"])
        return None

    # <url> con su <lastmod> si lo trae. Las tiendas lo ponen para Google.
    _ENTRADA = re.compile(
        r"<url>(.*?)</url>", re.S | re.I)
    _LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
    _LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", re.I)

    @classmethod
    def _entradas_sitemap(cls, xml: str) -> list[tuple[str, str]]:
        """Devuelve [(url, lastmod)] — lastmod puede venir vacío."""
        salida = []
        for bloque in cls._ENTRADA.findall(xml):
            loc = cls._LOC.search(bloque)
            if not loc:
                continue
            mod = cls._LASTMOD.search(bloque)
            salida.append((loc.group(1), mod.group(1) if mod else ""))
        if not salida:                      # sitemap sin <url>, formato raro
            salida = [(u, "") for u in cls._LOC.findall(xml)]
        return salida

    async def _urls_sitemap(self, dominio: str,
                            limite: int) -> list[tuple[str, str]]:
        urls: list[tuple[str, str]] = []
        resp = await self.cliente.get(f"https://{dominio}/sitemap.xml")
        if resp is None or resp.status_code != 200:
            return urls
        sitemaps = self._LOC.findall(resp.text)
        candidatos = [s for s in sitemaps if "produc" in s.lower()] or sitemaps[:5]
        for sm in candidatos[:10]:
            r = await self.cliente.get(sm)
            if r is None or r.status_code != 200:
                continue
            urls.extend(self._entradas_sitemap(r.text))
            if len(urls) >= limite:
                break
        return urls[:limite]

    def _enlaces_producto(self, html: str, base: str) -> list[str]:
        arbol = HTMLParser(html)
        vistos, salida = set(), []
        for a in arbol.css("a[href]"):
            href = a.attributes.get("href") or ""
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            absoluta = urljoin(base, href)
            if urlparse(absoluta).netloc != urlparse(base).netloc:
                continue
            if absoluta in vistos:
                continue
            vistos.add(absoluta)
            salida.append(absoluta)
        return salida
