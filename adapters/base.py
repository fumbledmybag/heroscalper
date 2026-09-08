"""Base comun de los adapters + cliente HTTP educado.

Toda la fragilidad del sistema vive en los adapters. Cuando una tienda cambia
su web solo hay que tocar un archivo, no el motor.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Iterable, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..models import Oferta


class LimitadorPorDominio:
    """Un token por dominio y segundo, con jitter. Nada de ráfagas."""

    def __init__(self, por_segundo: float = 2.0, jitter=(0.0, 0.35)):
        self.intervalo = 1.0 / max(por_segundo, 0.01)
        self.jitter = jitter
        self._ultimo: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def espera(self, dominio: str) -> None:
        lock = self._locks.setdefault(dominio, asyncio.Lock())
        async with lock:
            ahora = time.monotonic()
            anterior = self._ultimo.get(dominio, 0.0)
            # El jitter va SUMADO al intervalo: con [0.3, 1.2] cada petición
            # esperaba 1,75 s de media en vez de 1, y un catálogo de 40 páginas
            # tardaba 70 s en lugar de 40. Ahora es un pellizco, no otra espera.
            objetivo = anterior + self.intervalo + random.uniform(*self.jitter)
            if objetivo > ahora:
                await asyncio.sleep(objetivo - ahora)
            self._ultimo[dominio] = time.monotonic()


class ClienteHTTP:
    def __init__(self, config: dict):
        r = config.get("rastreo", {})
        self.limitador = LimitadorPorDominio(
            r.get("peticiones_por_segundo_por_dominio", 2.0),
            tuple(r.get("jitter_segundos", [0.0, 0.35])),
        )
        self.timeout = r.get("timeout_segundos", 10)
        self.reintentos = r.get("reintentos", 1)
        self.respetar_robots = r.get("respetar_robots_txt", True)
        self.user_agent = r.get("user_agent", "HeroScalper/0.1")
        self._robots: dict[str, Optional[RobotFileParser]] = {}
        # Último motivo por el que falló cada dominio, para poder contarlo.
        self.ultimo_fallo: dict[str, str] = {}
        self._cliente = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Language": "es-ES,es;q=0.9",
            },
        )

    async def _permitido(self, url: str) -> bool:
        if not self.respetar_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                resp = await self._cliente.get(f"{base}/robots.txt", timeout=8)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp = None
            except Exception:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    async def get(self, url: str) -> Optional[httpx.Response]:
        dominio = urlparse(url).netloc
        if not await self._permitido(url):
            self.ultimo_fallo[dominio] = "su robots.txt no nos deja leerlo"
            return None
        espera = 1.0
        ultimo = "no responde"
        for intento in range(self.reintentos + 1):
            await self.limitador.espera(dominio)
            try:
                resp = await self._cliente.get(url)
            except Exception as e:
                # Guardar POR QUÉ falló. Un fallo mudo no se puede arreglar:
                # no es lo mismo un timeout que un 403 o un dominio muerto.
                ultimo = f"{type(e).__name__}"
                await asyncio.sleep(espera)
                espera *= 2
                continue
            # Backoff exponencial ante rate limiting: si nos piden calma, calma.
            if resp.status_code in (429, 503):
                ultimo = f"nos frena con un HTTP {resp.status_code}"
                retry = resp.headers.get("Retry-After")
                await asyncio.sleep(float(retry) if retry and retry.isdigit() else espera)
                espera *= 2
                continue
            if resp.status_code >= 400:
                self.ultimo_fallo[dominio] = f"HTTP {resp.status_code}"
            else:
                self.ultimo_fallo.pop(dominio, None)
            return resp
        self.ultimo_fallo[dominio] = ultimo
        return None

    async def cerrar(self) -> None:
        await self._cliente.aclose()


class Adapter:
    """Interfaz comun. Un adapter por PLATAFORMA, no por tienda."""

    nombre = "base"

    def __init__(self, cliente: ClienteHTTP, config: dict):
        self.cliente = cliente
        self.config = config

    async def detecta(self, dominio: str) -> bool:
        raise NotImplementedError

    async def catalogo(self, dominio: str, max_productos: int = 5000,
                       desde: int = 0, cambiados_desde: str = "") -> list[Oferta]:
        """Barrido amplio y barato del catalogo.

        `desde` y `cambiados_desde` solo los usan los adapters que tienen que
        visitar los productos uno a uno. `cambiados_desde` es la fecha del
        último barrido: con ella se leen SOLO las fichas que la tienda dice
        haber tocado (el <lastmod> de su sitemap), en vez del catálogo entero.
        `desde` es el plan B cuando la tienda no publica esa fecha.
        """
        raise NotImplementedError

    async def producto(self, url: str) -> Optional[Oferta]:
        """Lectura en vivo de un producto concreto (confirmacion de alerta)."""
        raise NotImplementedError

    async def outlet(self, dominio: str) -> list[Oferta]:
        """Paginas de liquidacion: baratisimas de rastrear y muy rentables."""
        return []


def normaliza_ean(valor) -> Optional[str]:
    """El EAN es la clave de union de todo el sistema. Sin el, no hay cruce."""
    if valor is None:
        return None
    s = "".join(ch for ch in str(valor) if ch.isdigit())
    if len(s) in (8, 12, 13, 14):
        return s.zfill(13) if len(s) in (12, 13) else s
    return None


def a_float(valor) -> Optional[float]:
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip().replace("\xa0", " ")
    s = "".join(ch for ch in s if ch.isdigit() or ch in ".,-")
    if not s:
        return None
    # "1.234,56" -> 1234.56 ; "1,234.56" -> 1234.56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") \
            else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None
