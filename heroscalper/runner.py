"""Orquestador: descubrimiento, barrido, deteccion y alerta."""
from __future__ import annotations

import asyncio
import time
import os
import re
from pathlib import Path
from typing import Optional

import yaml

from .adapters.base import ClienteHTTP
from .adapters.jsonld import JsonLdAdapter
from .adapters.keepa import KeepaAdapter
from .adapters.shopify import ShopifyAdapter
from .adapters.woocommerce import WooCommerceAdapter
from .db import DB
from .detector import Detector
from .fingerprint import adapter_para, huella
from .models import Oferta
from .models import ReferenciaReventa
from .notifier import Telegram, censurar
from .reventa import Tasador


def cargar_config(ruta: str = "config.yaml") -> dict:
    texto = Path(ruta).read_text(encoding="utf-8")
    # Sustituye ${VAR} por variables de entorno
    texto = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), texto)
    return yaml.safe_load(texto)


class HeroScalper:
    def __init__(self, config: dict):
        self.cfg = config
        self.db = DB(config.get("base_datos", {}).get("ruta", "data/heroscalper.db"))
        self.cliente = ClienteHTTP(config)
        self.adapters = {
            "shopify": ShopifyAdapter(self.cliente, config),
            "woocommerce": WooCommerceAdapter(self.cliente, config),
            "jsonld": JsonLdAdapter(self.cliente, config),
            "keepa": KeepaAdapter(self.cliente, config),
        }
        self.detector = Detector(self.db, config)
        self.notifier = Telegram(config)
        self.tasador = Tasador(self.cliente, config)
        self._n_ciclo = 0

    # ------------------------------------------------------------------ #
    async def descubrir(self, dominios: list[str]) -> list[dict]:
        """Clasifica dominios por plataforma y descarta los no vigilables.

        En paralelo, igual que el barrido: reconocer 157 tiendas de una en una
        son varios minutos de reloj, y con una tienda caída cada espera es un
        timeout entero.
        """
        simultaneas = int(self.cfg.get("rastreo", {}).get("tiendas_a_la_vez", 16))
        semaforo = asyncio.Semaphore(max(1, simultaneas))

        async def una(dominio: str):
            async with semaforo:
                return await huella(self.cliente, dominio)

        hechas = 0
        total = len(dominios)

        async def con_cuenta(dominio: str):
            nonlocal hechas
            r = await una(dominio)
            hechas += 1
            if hechas % 20 == 0 or hechas == total:
                print(f"[descubrir] {hechas}/{total} tiendas miradas")
            return r

        huellas = await asyncio.gather(*(con_cuenta(d) for d in dominios),
                                       return_exceptions=True)
        resultados = []
        for dominio, h in zip(dominios, huellas):
            if isinstance(h, BaseException):
                resultados.append({"dominio": dominio, "plataforma": None,
                                   "jsonld": False, "ean": False,
                                   "vigilable": False})
                continue
            resultados.append({
                "dominio": h.dominio,
                "plataforma": h.plataforma,
                "jsonld": h.tiene_jsonld,
                "ean": h.tiene_ean,
                "vigilable": h.vigilable,
            })
            if h.vigilable:
                self.db.registrar_tienda(h.dominio, h.plataforma or "jsonld",
                                         tiene_ean=h.tiene_ean)
        return resultados

    # ------------------------------------------------------------------ #
    async def barrer_tienda(self, dominio: str, plataforma: Optional[str] = None,
                            max_productos: int = 2000,
                            solo_outlet: bool = False,
                            desde: int = 0,
                            cambiados_desde: str = "") -> tuple:
        """Devuelve (todas las ofertas leídas, las que han cambiado de precio).

        La segunda lista es la que importa para el coste: evaluar una oferta
        cuesta varias consultas, y casi ningún precio se mueve entre ciclos.
        """
        adapter = adapter_para(plataforma, self.adapters)
        try:
            if solo_outlet:
                ofertas = await adapter.outlet(dominio)
            else:
                ofertas = await adapter.catalogo(dominio, max_productos,
                                                 desde=desde,
                                                 cambiados_desde=cambiados_desde)
                ofertas += await adapter.outlet(dominio)
        except Exception as e:
            fallos = self.db.marcar_fallo(dominio)
            if fallos >= 3:
                await self.notifier.aviso_tecnico(
                    f"El adapter de {dominio} lleva {fallos} fallos seguidos: "
                    f"{censurar(e, os.environ.get('KEEPA_API_KEY'))}")
            return [], []
        if ofertas:
            self.db.marcar_exito(dominio)
            cambiadas = self.db.guardar_lecturas(ofertas)
        else:
            self.db.marcar_fallo(dominio)
            cambiadas = []
        return ofertas, cambiadas

    # ------------------------------------------------------------------ #
    async def barrer_amazon(self) -> list[Oferta]:
        """Amazon entra por Keepa, no por scraping."""
        keepa = self.adapters["keepa"]
        cfg = (self.cfg.get("fuentes", {}) or {}).get("keepa", {}) or {}
        if not cfg.get("activo") or not keepa.activo:
            return []
        ofertas: list[Oferta] = []
        for dominio in keepa.dominios:
            try:
                nuevas = await keepa.deals(dominio)
            except Exception as e:
                # La URL de Keepa lleva la api_key: fuera antes de contarlo.
                await self.notifier.aviso_tecnico(
                    f"Keepa ({dominio}) falló: {censurar(e, keepa.api_key)}")
                continue
            ofertas.extend(nuevas)
        if ofertas:
            self.db.guardar_lecturas(ofertas)
        return ofertas

    async def tasar_candidatas(self, oportunidades: list,
                               tope: Optional[int] = None) -> int:
        """Consulta CeX / Wallapop / Vinted para las mejores oportunidades.

        Solo las mejores y solo si no se han consultado hace poco: son webs de
        terceros y no hay por qué machacarlas.
        """
        cfg = self.cfg.get("reventa", {}) or {}
        if not cfg.get("activo", True) or not self.tasador.fuentes:
            return 0
        if tope is None:
            tope = int(cfg.get("max_consultas_por_ciclo", 25))
        horas = int(cfg.get("refrescar_cada_horas", 72))

        hechas = 0
        vistas: set[str] = set()
        for op in oportunidades:
            if hechas >= tope:
                break
            clave = op.oferta.clave_match()
            if clave in vistas or self.db.cotizacion_fresca(clave, horas):
                continue
            vistas.add(clave)

            cotizaciones = await self.tasador.tasar(op.oferta)
            hechas += 1
            if not cotizaciones:
                continue
            self.db.guardar_cotizaciones(clave, cotizaciones)

            mejor = self.tasador.mejor(cotizaciones)
            if mejor and mejor.precio:
                self.db.guardar_reventa(ReferenciaReventa(
                    ean=op.oferta.ean,
                    modelo=mejor.consulta,
                    precio_venta_mediano=mejor.precio,
                    anuncios_90d=max(mejor.n_muestras, 1),
                    dias_venta_mediano=mejor.dias_venta or 14.0,
                    precio_suelo=mejor.precio_garantizado,
                    plataforma=mejor.fuente,
                ))
        return hechas

    async def ciclo(self, dominios: list[str], solo_outlet: bool = False,
                    max_productos: Optional[int] = None) -> dict:
        # Cuantos más productos por tienda, más oportunidades: leer 4.000 de
        # una tienda cuesta apenas 16 peticiones (los feeds vienen de 250 en
        # 250), así que subir esto es casi gratis y duplica la cobertura.
        if max_productos is None:
            max_productos = int(self.cfg.get("rastreo", {})
                                .get("max_productos_por_tienda", 4000))

        # Las tiendas se barren EN PARALELO. Ir de una en una parece más
        # prudente y no lo es: el freno de 1 petición/segundo es POR DOMINIO,
        # así que atender a 20 tiendas a la vez no molesta a ninguna y es la
        # diferencia entre barrer 250 tiendas en 3 minutos o en 47.
        # Una tienda muerta cuesta un timeout en CADA ciclo, para siempre.
        # Las que fallan cinco veces seguidas se apagan y se reintentan al día
        # siguiente: por si era una caída temporal y no una web que ya no está.
        horas = int(self.cfg.get("rastreo", {}).get("reintentar_apagadas_horas", 24))
        revividas = self.db.reactivar_caducados(horas)
        if revividas:
            print(f"[tiendas] {revividas} vuelven a probarse tras el descanso")
        apagadas = self.db.dominios_apagados(horas)
        if apagadas:
            dominios = [d for d in dominios if d not in apagadas]
            print(f"[tiendas] {len(apagadas)} apagadas por fallos: no se barren")

        plataformas = {r["dominio"]: r["plataforma"]
                       for r in self.db.tiendas_activas()}

        # Reconocimiento automático. Una tienda cuya plataforma no conocemos cae
        # al lector genérico, que saca mucho menos que el de Shopify o el de
        # WooCommerce. En un servidor gestionado (Render y compañía) nadie va a
        # abrir una terminal para lanzar "descubrir", así que se hace solo: la
        # primera vez que aparece un dominio nuevo, se le mira la huella.
        tope_nuevas = int(self.cfg.get("rastreo", {})
                          .get("descubrir_por_ciclo", 60))
        desconocidas = [d for d in dominios if d not in plataformas][:tope_nuevas]
        if desconocidas:
            print(f"[descubrir] {len(desconocidas)} tiendas nuevas por reconocer")
            try:
                hallazgos = await self.descubrir(desconocidas)
                vigilables = sum(1 for h in hallazgos if h["vigilable"])
                print(f"[descubrir] {vigilables}/{len(hallazgos)} vigilables")
                plataformas = {r["dominio"]: r["plataforma"]
                               for r in self.db.tiendas_activas()}
            except Exception as e:
                print(f"[descubrir] ha fallado, se sigue con el lector genérico: {e}")
        simultaneas = int(self.cfg.get("rastreo", {}).get("tiendas_a_la_vez", 16))
        semaforo = asyncio.Semaphore(max(1, simultaneas))

        offsets = self.db.offsets_catalogo()
        ultimos = self.db.ultimos_barridos()
        paso = int(self.cfg.get("rastreo", {}).get("max_productos_generico", 200))

        # Cada cuántos ciclos se relee el catálogo COMPLETO de una tienda. Con 1
        # se relee siempre (lo que quieres con 170 tiendas). Si algún día pones
        # 500, ponlo en 4: el outlet y las rebajas se siguen mirando cada media
        # hora, que es donde aparece casi todo, y el catálogo entero cada dos.
        cada = max(1, int(self.cfg.get("rastreo", {})
                          .get("catalogo_cada_ciclos", 1)))
        self._n_ciclo += 1
        toca_catalogo = (self._n_ciclo % cada == 1) or cada == 1

        async def una(dominio: str):
            async with semaforo:
                todas, cambiadas = await self.barrer_tienda(
                    dominio, plataformas.get(dominio), max_productos,
                    solo_outlet or not toca_catalogo,
                    desde=offsets.get(dominio, 0),
                    cambiados_desde=ultimos.get(dominio, ""))
                # Solo avanza el trozo de sitemap el lector genérico: los de
                # Shopify y WooCommerce leen el catálogo entero de una vez.
                if not solo_outlet and toca_catalogo and \
                        plataformas.get(dominio) in (None, "jsonld", "prestashop"):
                    self.db.avanzar_offset(dominio, paso)
                return todas, cambiadas

        # EN TUBERÍA, no por tandas. Antes se barrían 25 tiendas y se esperaba
        # a que TERMINARAN LAS 25 antes de empezar la siguiente hornada: una
        # tienda lenta dejaba a otras veinticuatro esperando de brazos
        # cruzados. Ahora, en cuanto una acaba entra otra, y el resultado se
        # evalúa y se tira al momento. Mismo tope de memoria, la mitad de
        # tiempo o menos.
        cfg_rev = self.cfg.get("reventa", {}) or {}
        presupuesto = int(cfg_rev.get("max_consultas_por_ciclo", 25))

        # Cada N ciclos se reevalúa TODO aunque no haya cambiado el precio:
        # la referencia de mercado puede haberse movido por otras tiendas.
        reev = max(1, int(self.cfg.get("rastreo", {})
                          .get("reevaluar_todo_cada_ciclos", 8)))
        reevaluar_todo = (self._n_ciclo % reev == 0)

        oportunidades: list = []
        n_lecturas = 0
        n_evaluadas = 0
        vivas = self.db.urls_ofertas_activas()
        refrescar: list[str] = []

        async def procesar(lote, cambiadas):
            """Evalúa un lote y se queda solo con las oportunidades."""
            nonlocal presupuesto, n_lecturas, n_evaluadas
            if not lote:
                return
            n_lecturas += len(lote)

            # AQUÍ está el ahorro grande. Evaluar una oferta son varias
            # consultas a la base de datos, y el 97 % de los precios no se
            # mueve entre ciclos. Se evalúa lo que ha cambiado y lo que nunca
            # habíamos visto; el resto solo se "toca" para que no caduque.
            if reevaluar_todo:
                a_evaluar = lote
            else:
                mueve = {o.url for o in cambiadas}
                a_evaluar = [o for o in lote if o.url in mueve]
                refrescar.extend(o.url for o in lote
                                 if o.url not in mueve and o.url in vivas)
            if not a_evaluar:
                return
            n_evaluadas += len(a_evaluar)

            # Las variantes hermanas se comparan contra el lote COMPLETO de la
            # tienda: si solo cambió la talla M, hay que poder compararla con
            # la S y la L aunque esas no se hayan movido.
            ops = self.detector.evaluar_lote(a_evaluar, contexto=lote)
            if presupuesto > 0:
                hechas = await self.tasar_candidatas(ops, tope=presupuesto)
                presupuesto -= hechas
                if hechas:
                    ops = self.detector.evaluar_lote(a_evaluar, contexto=lote)
            oportunidades.extend(ops)

        # Amazon (vía Keepa) va primero: son pocas ofertas.
        amazon, amazon_cambiadas = [], []
        try:
            amazon = await self.barrer_amazon()
            amazon_cambiadas = amazon
        except Exception as e:
            print(f"[keepa] {e}")
        await procesar(amazon, amazon_cambiadas)
        del amazon, amazon_cambiadas

        print(f"[barrido] empieza · {len(dominios)} tiendas, "
              f"{simultaneas} a la vez")
        arranque = time.monotonic()
        listas = 0
        tareas = [asyncio.ensure_future(una(d)) for d in dominios]
        for terminada in asyncio.as_completed(tareas):
            try:
                lote, cambiadas = await terminada
            except Exception:
                lote, cambiadas = [], []
            await procesar(lote, cambiadas)
            del lote, cambiadas          # la siguiente tienda entra con sitio
            listas += 1
            # Un barrido de 172 tiendas son minutos sin decir nada, y un bot
            # callado parece un bot muerto. Cada 25 tiendas, señal de vida.
            if listas % 25 == 0 or listas == len(dominios):
                print(f"[barrido] {listas}/{len(dominios)} tiendas · "
                      f"{(time.monotonic() - arranque)/60:.1f} min · "
                      f"{n_lecturas:,} lecturas")

        tocadas = self.db.refrescar_vistas(refrescar)
        oportunidades.sort(key=lambda o: self.detector.prioridad(o), reverse=True)
        print(f"[ciclo] {n_lecturas} lecturas · {n_evaluadas} evaluadas · "
              f"{tocadas} ofertas vivas refrescadas")

        # Cooldown: no repetir la misma alerta en 24 h
        horas = self.cfg.get("notificaciones", {}).get("cooldown_horas_por_producto", 24)
        # La web lo enseña todo; Telegram solo lo que llegue al nivel mínimo,
        # para que el móvil no se convierta en ruido.
        nivel_min = self.cfg.get("notificaciones", {}).get("nivel_minimo_telegram", 1)
        nuevas = []
        for o in oportunidades:
            clave = o.oferta.clave()
            if self.db.ya_alertado(clave, horas):
                continue
            if o.nivel < nivel_min:
                continue
            nuevas.append(o)

        # Persistir TODAS las oportunidades vivas (no solo las que se alertan):
        # la web muestra el conjunto completo, Telegram solo las novedades.
        for o in oportunidades:
            self.db.upsert_oferta(o)
        minutos = self.cfg.get("web", {}).get("expirar_tras_minutos", 180)
        expiradas = self.db.expirar_ofertas(minutos)

        eventos = self.detector.detectar_eventos(nuevas)
        tiendas_evento = {e.tienda for e in eventos}

        for ev in eventos:
            await self.notifier.alerta_evento(ev)
            for o in ev.oportunidades:
                self.db.registrar_alerta(o.oferta.clave(), o.oferta.tienda,
                                         o.tipo, o.precio_efectivo, o.margen_neto_eur)

        enviadas = 0
        for o in nuevas:
            if o.oferta.tienda in tiendas_evento:
                continue      # ya ha ido en el mensaje del evento
            await self.notifier.alerta(o)
            self.db.registrar_alerta(o.oferta.clave(), o.oferta.tienda,
                                     o.tipo, o.precio_efectivo, o.margen_neto_eur)
            enviadas += 1

        return {
            "lecturas": n_lecturas,
            "oportunidades": len(oportunidades),
            "alertas_enviadas": enviadas,
            "eventos_tienda": len(eventos),
            "ofertas_expiradas": expiradas,
            "ofertas_activas": self.db.resumen().get("activas", 0),
            "destacadas": sum(1 for o in oportunidades if o.destacada),
        }

    # ------------------------------------------------------------------ #
    async def bucle(self, dominios: list[str]) -> None:
        tiers = self.cfg.get("rastreo", {}).get("tiers", {})
        intervalo_warm = tiers.get("warm_minutos", 30) * 60
        ciclos_por_dia = max(1, int(24 * 3600 / intervalo_warm))
        n = 0
        while True:
            resumen = await self.ciclo(dominios)
            print(f"[ciclo] {resumen}")
            n += 1
            # Una vez al día se adelgaza el histórico: sin esto el disco de un
            # servidor pequeño se llena en un par de meses.
            if n % ciclos_por_dia == 0:
                print(f"[poda] {self.db.podar()}")
            await asyncio.sleep(intervalo_warm)

    async def cerrar(self) -> None:
        await self.cliente.cerrar()
        await self.notifier.cerrar()
        self.db.cerrar()
