"""Precio real de reventa: cuánto te van a pagar de verdad por esto.

Hasta ahora el beneficio era una estimación (un % del precio de mercado), y
una estimación no sirve para decidir una compra. Este módulo lo sustituye por
datos medidos de las plataformas donde se vende de verdad en España:

  · CeX (es.webuy.com) — precio de compra GARANTIZADO en efectivo o en vale.
    Es el mejor dato de todos: no es lo que alguien pide, es lo que te pagan
    hoy. Funciona para electrónica, consolas, videojuegos y móviles, y tienen
    tienda física (Málaga incluida), así que es un suelo real.
  · Wallapop — el mercado general. Da precios PEDIDOS, no cerrados, así que se
    aplica un factor de cierre para aproximar la venta real.
  · Vinted — moda, calzado y accesorios.

Todo lo consultado se guarda en la base de datos como histórico propio: con el
tiempo tienes tu propia serie de a cuánto se paga cada cosa, que es un activo
que ninguna de estas plataformas te da.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus

from .adapters.base import ClienteHTTP
from .models import Oferta


@dataclass
class Cotizacion:
    """Lo que dice UNA plataforma sobre lo que vale este producto."""

    fuente: str                 # cex | wallapop | vinted
    consulta: str
    precio: Optional[float]     # precio de venta estimado en esa plataforma
    n_muestras: int = 0
    # Solo CeX: lo que te pagan HOY, garantizado. Es un suelo, no una hipótesis.
    precio_garantizado: Optional[float] = None
    dias_venta: Optional[float] = None
    url: Optional[str] = None
    consultada: datetime = field(default_factory=datetime.utcnow)

    @property
    def fiable(self) -> bool:
        return self.precio is not None and self.precio > 0


def consulta_de(oferta: Oferta) -> str:
    """Texto de búsqueda: marca + modelo, sin duplicar la marca."""
    titulo = " ".join(str(oferta.titulo or "").split()[:7])
    marca = str(oferta.marca or "")
    if marca and marca.lower() not in titulo.lower():
        titulo = f"{marca} {titulo}"
    return titulo.strip()[:70]


# --------------------------------------------------------------------- #
# CeX / WeBuy — el dato duro
# --------------------------------------------------------------------- #
class CeX:
    """API pública de CeX. Devuelve el precio al que TE COMPRAN el artículo."""

    nombre = "cex"
    BASE = "https://wss2.cex.{pais}.webuy.io/v3"

    def __init__(self, cliente: ClienteHTTP, cfg: dict):
        self.cliente = cliente
        self.cfg = cfg or {}
        self.pais = self.cfg.get("pais", "es")

    async def cotizar(self, consulta: str) -> Optional[Cotizacion]:
        url = f"{self.BASE.format(pais=self.pais)}/boxes?q={quote_plus(consulta)}"
        resp = await self.cliente.get(url)
        if resp is None or resp.status_code != 200:
            return None
        try:
            datos = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None

        cajas = (((datos.get("response") or {}).get("data") or {}).get("boxes")) or []
        if not cajas:
            return None

        # Se coge la variante con mejor precio de compra: es el suelo que te
        # interesa conocer, no la media de todas las capacidades y colores.
        mejor = None
        for c in cajas[:8]:
            efectivo = _num(c.get("cashPrice"))
            vale = _num(c.get("exchangePrice"))
            venta = _num(c.get("sellPrice"))
            if efectivo is None and vale is None:
                continue
            garantizado = max(x for x in (efectivo, vale) if x is not None)
            if mejor is None or garantizado > mejor[0]:
                mejor = (garantizado, venta, c.get("boxName"), c.get("boxId"))
        if mejor is None:
            return None

        garantizado, venta, nombre_caja, box_id = mejor
        return Cotizacion(
            fuente="cex",
            consulta=consulta,
            # Lo que puedes sacar tú vendiéndolo a particular está entre el
            # precio de compra de CeX y su precio de venta. Nos quedamos con
            # el de compra: es el único que está garantizado.
            precio=garantizado,
            precio_garantizado=garantizado,
            n_muestras=1,
            dias_venta=1.0,      # CeX te lo compra en el momento
            url=(f"https://es.webuy.com/product-detail?id={box_id}"
                 if box_id else None),
        )


# --------------------------------------------------------------------- #
# Wallapop — el mercado general
# --------------------------------------------------------------------- #
class Wallapop:
    """Busca anuncios activos y toma la mediana.

    Ojo con lo que significa: son precios PEDIDOS, no cerrados. La gente pide
    de más. Por eso se aplica `factor_cierre` (0,85 por defecto) para
    aproximar lo que realmente se paga.
    """

    nombre = "wallapop"
    ENDPOINTS = [
        "https://api.wallapop.com/api/v3/search?source=search_box&keywords={q}",
        "https://api.wallapop.com/api/v3/general/search?keywords={q}",
    ]

    def __init__(self, cliente: ClienteHTTP, cfg: dict):
        self.cliente = cliente
        self.cfg = cfg or {}
        self.factor = float(self.cfg.get("factor_cierre", 0.85))
        self.minimo = int(self.cfg.get("min_anuncios", 5))

    async def cotizar(self, consulta: str) -> Optional[Cotizacion]:
        for plantilla in self.ENDPOINTS:
            resp = await self.cliente.get(plantilla.format(q=quote_plus(consulta)))
            if resp is None or resp.status_code != 200:
                continue
            try:
                datos = resp.json()
            except (json.JSONDecodeError, ValueError):
                continue
            precios = _precios_wallapop(datos)
            if len(precios) >= self.minimo:
                precios.sort()
                # Se recorta el 15 % de los extremos: hay anuncios de piezas
                # sueltas por 5 € y de lotes enteros por 900 € que descuadran.
                recorte = max(1, len(precios) // 7)
                central = precios[recorte:-recorte] or precios
                return Cotizacion(
                    fuente="wallapop",
                    consulta=consulta,
                    precio=round(statistics.median(central) * self.factor, 2),
                    n_muestras=len(precios),
                    url=("https://es.wallapop.com/app/search?keywords="
                         + quote_plus(consulta)),
                )
        return None


def _precios_wallapop(datos) -> list[float]:
    """Wallapop ha cambiado la forma de la respuesta varias veces.

    En vez de acoplarnos a una versión concreta, se recorre el JSON buscando
    objetos que tengan precio. Así sobrevive al próximo cambio de esquema.
    """
    precios: list[float] = []

    def recorrer(nodo):
        if isinstance(nodo, dict):
            for clave in ("price", "sale_price", "amount"):
                valor = nodo.get(clave)
                if isinstance(valor, dict):
                    valor = valor.get("amount")
                n = _num(valor)
                if n is not None and 1 < n < 100000:
                    precios.append(n)
                    break
            for v in nodo.values():
                recorrer(v)
        elif isinstance(nodo, list):
            for v in nodo:
                recorrer(v)

    recorrer(datos)
    return precios


# --------------------------------------------------------------------- #
# Vinted — moda y calzado
# --------------------------------------------------------------------- #
class Vinted:
    """Catálogo de Vinted. Necesita una cookie anónima de la portada."""

    nombre = "vinted"
    PORTADA = "https://www.vinted.es/"
    BUSQUEDA = ("https://www.vinted.es/api/v2/catalog/items"
                "?search_text={q}&per_page=40&order=newest_first")

    def __init__(self, cliente: ClienteHTTP, cfg: dict):
        self.cliente = cliente
        self.cfg = cfg or {}
        self.factor = float(self.cfg.get("factor_cierre", 0.90))
        self.minimo = int(self.cfg.get("min_anuncios", 5))
        self._preparado = False

    async def _preparar(self) -> None:
        if not self._preparado:
            await self.cliente.get(self.PORTADA)   # recoge cookies de sesión
            self._preparado = True

    async def cotizar(self, consulta: str) -> Optional[Cotizacion]:
        await self._preparar()
        resp = await self.cliente.get(self.BUSQUEDA.format(q=quote_plus(consulta)))
        if resp is None or resp.status_code != 200:
            return None
        try:
            datos = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None

        precios = []
        for art in (datos.get("items") or []):
            precio = art.get("price")
            if isinstance(precio, dict):
                precio = precio.get("amount")
            n = _num(precio)
            if n is not None and n > 1:
                precios.append(n)
        if len(precios) < self.minimo:
            return None
        return Cotizacion(
            fuente="vinted",
            consulta=consulta,
            precio=round(statistics.median(precios) * self.factor, 2),
            n_muestras=len(precios),
            url="https://www.vinted.es/catalog?search_text=" + quote_plus(consulta),
        )


# --------------------------------------------------------------------- #
# Orquestador
# --------------------------------------------------------------------- #
class Tasador:
    """Consulta las fuentes y decide cuál manda."""

    def __init__(self, cliente: ClienteHTTP, config: dict):
        cfg = config.get("reventa", {}) or {}
        self.cfg = cfg
        self.activo = cfg.get("activo", True)
        disponibles = {
            "cex": CeX(cliente, cfg.get("cex", {})),
            "wallapop": Wallapop(cliente, cfg.get("wallapop", {})),
            "vinted": Vinted(cliente, cfg.get("vinted", {})),
        }
        self.fuentes = [
            disponibles[n] for n in cfg.get("fuentes", ["cex", "wallapop", "vinted"])
            if n in disponibles and (cfg.get(n, {}) or {}).get("activo", True)
        ]

    async def tasar(self, oferta: Oferta) -> list[Cotizacion]:
        """Pregunta a todas las fuentes. Guarda todo, decide después."""
        if not self.activo:
            return []
        consulta = consulta_de(oferta)
        salida = []
        for fuente in self.fuentes:
            try:
                c = await fuente.cotizar(consulta)
            except Exception:
                c = None
            if c and c.fiable:
                salida.append(c)
        return salida

    @staticmethod
    def mejor(cotizaciones: list[Cotizacion]) -> Optional[Cotizacion]:
        """CeX manda si existe: es el único precio garantizado.

        Si no hay CeX, gana la fuente con más muestras: más anuncios mirados,
        menos casualidad.
        """
        if not cotizaciones:
            return None
        garantizadas = [c for c in cotizaciones if c.precio_garantizado]
        if garantizadas:
            return max(garantizadas, key=lambda c: c.precio_garantizado or 0)
        return max(cotizaciones, key=lambda c: c.n_muestras)


def _num(valor) -> Optional[float]:
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return None
    return None if n <= 0 else round(n, 2)
