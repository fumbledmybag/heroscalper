"""Alertas por Telegram.

Objetivo del formato: que de un vistazo sepas QUÉ es, DÓNDE está, CUÁNTO
cuesta, CUÁNTO vale de verdad y CUÁNTO ganas. Sin tener que interpretar nada.
Los números van en bloque monoespaciado para que queden alineados en columna.
"""
from __future__ import annotations

import html
import os
from typing import Optional

import httpx

from .models import EventoTienda, Oportunidad, TipoOportunidad

# Círculo de color por nivel: lo primero que ve el ojo al abrir Telegram.
NIVELES = {
    4: ("🔴", "BRUTAL"),
    3: ("🟠", "MUY BUENA"),
    2: ("🟡", "BUENA"),
    1: ("🟢", "NORMAL"),
}

CABECERA = {
    TipoOportunidad.ERROR_PRECIO_EXTREMO: ("🚨", "ERROR DE PRECIO"),
    TipoOportunidad.ERROR_PRECIO:         ("⚠️", "PRECIO ANÓMALO"),
    TipoOportunidad.PROMO_MULTIUNIDAD:    ("🧮", "PROMO MULTI-UNIDAD"),
    TipoOportunidad.PROMO_CAMPANA:        ("🏷️", "PROMOCIÓN"),
    TipoOportunidad.LIQUIDACION:          ("📦", "LIQUIDACIÓN"),
    TipoOportunidad.DESCATALOGADO:        ("🔚", "DESCATALOGADO"),
    TipoOportunidad.GRATIS:               ("🎁", "GRATIS"),
}


def eur(valor: Optional[float]) -> str:
    """Formato español: 1.234,56 €"""
    if valor is None:
        return "—"
    entero, decimal = f"{valor:,.2f}".split(".")
    return f"{entero.replace(',', '.')},{decimal} €"


def _fila(etiqueta: str, valor: str, ancho: int = 30) -> str:
    return f"{etiqueta}{valor.rjust(max(ancho - len(etiqueta), 1))}"


def formatear(o: Oportunidad) -> str:
    emoji_tipo, titulo_tipo = CABECERA.get(o.tipo, ("•", o.tipo.value))
    of = o.oferta
    # El círculo de color va primero: es lo que distingue de un vistazo una
    # rebaja decente de un pelotazo, sin leer un solo número.
    circulo, etiqueta_nivel = NIVELES.get(o.nivel, NIVELES[1])
    if o.nivel >= 4:
        cabecera = f"{circulo}{circulo}{circulo} <b>{etiqueta_nivel}</b> · {titulo_tipo}"
    elif o.nivel == 3:
        cabecera = f"{circulo}{circulo} <b>{etiqueta_nivel}</b> · {titulo_tipo}"
    else:
        cabecera = f"{circulo} <b>{etiqueta_nivel}</b> · {emoji_tipo} {titulo_tipo}"

    descuento = ""
    if o.referencia_mercado:
        pct = (1 - o.precio_efectivo / o.referencia_mercado) * 100
        descuento = f"−{pct:.0f} %"

    subtitulo = " · ".join(x for x in [of.marca, of.tienda] if x)
    if not getattr(o, "verificado", True):
        cabecera += "  ⚠️ <b>SIN VERIFICAR</b>"

    lineas = [
        cabecera,
        "",
        f"<b>{html.escape(of.titulo[:120])}</b>",
        f"<i>{html.escape(subtitulo)}</i>",
        "",
        "<pre>",
        _fila("Precio ahora", eur(o.precio_efectivo)),
    ]
    if abs(o.precio_efectivo - of.precio) > 0.01:
        lineas.append(_fila("  (etiqueta)", eur(of.precio)))
    if o.referencia_mercado:
        lineas.append(_fila("Precio real", eur(o.referencia_mercado)))
    if descuento:
        lineas.append(_fila("Descuento", descuento))
    if o.unidades_recomendadas > 1:
        lineas.append(_fila("Unidades", f"{o.unidades_recomendadas}"))
        lineas.append(_fila("Desembolso", eur(o.capital_necesario)))

    # La reventa solo se presenta como dato cuando ESTÁ MEDIDA. Si es una
    # estimación se dice, porque un beneficio sobre una suposición no es un
    # beneficio: es una hipótesis, y decidir con ella es decidir a ciegas.
    if o.referencia_reventa:
        lineas.append("─" * 30)
        if o.reventa_medida:
            lineas.append(_fila("Reventa 2ª mano", eur(o.referencia_reventa)))
            lineas.append(_fila("Beneficio neto", eur(o.margen_neto_eur)))
            lineas.append(_fila("Ajustado riesgo", eur(o.margen_esperado_eur)))
        else:
            lineas.append(_fila("Reventa (estimada)", "~" + eur(o.referencia_reventa)))
            lineas.append(_fila("Beneficio (estim.)", "~" + eur(o.margen_neto_eur)))
    lineas.append("</pre>")

    hist = getattr(o, "historial", None) or {}
    if hist.get("media_90d"):
        lineas.append("")
        media = eur(hist["media_90d"])
        minimo = eur(hist.get("minimo"))
        texto = f"📉 Media histórica {media} · más barato visto {minimo}"
        if hist.get("minimo") is not None and o.precio_efectivo <= hist["minimo"] + 0.01:
            texto += "  ← nunca ha estado más barato"
        lineas.append(f"<i>{texto}</i>")

    if not getattr(o, "verificado", True):
        lineas.append("<i>⚠️ El descuento lo declara la propia tienda y no hay "
                      "forma de contrastarlo. Puede ser un PVP inflado: "
                      "compruébalo con los botones de abajo.</i>")
    elif o.referencia_reventa and not o.reventa_medida:
        lineas.append("<i>⚠️ Reventa estimada sobre el precio de mercado, "
                      "no medida en Wallapop. Compruébalo antes de comprar.</i>")
    if o.motivo:
        lineas += ["", f"<i>{html.escape(o.motivo[:220])}</i>"]
    return "\n".join(lineas)


def botones(o: Oportunidad) -> dict:
    """Botón de compra directa bajo el mensaje, no un enlace perdido en el texto."""
    fila = [{"text": f"🛒 Comprar en {o.oferta.tienda[:26]}", "url": o.oferta.url}]
    titulo = " ".join(o.oferta.titulo.split()[:6])
    marca = o.oferta.marca or ""
    # No repetir la marca si ya viene en el título ("Sony Sony WH-1000XM5")
    consulta = titulo if marca.lower() in titulo.lower() else f"{marca} {titulo}"
    segunda = [
        {"text": "🔎 Wallapop",
         "url": f"https://es.wallapop.com/app/search?keywords={_q(consulta)}"},
        {"text": "🔎 Vinted",
         "url": f"https://www.vinted.es/catalog?search_text={_q(consulta)}"},
    ]
    return {"inline_keyboard": [fila, segunda]}


def _q(texto: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(texto.strip()[:60])


def formatear_evento(ev: EventoTienda) -> str:
    lineas = [
        "🔥 <b>TIENDA REVENTADA</b>",
        "",
        f"<b>{html.escape(ev.tienda)}</b>",
        f"<i>{len(ev.oportunidades)} anomalías en la última hora · "
        f"{eur(ev.margen_total)} de beneficio estimado</i>",
        "",
        "Fallo masivo de catálogo. Suele durar poco.",
        "",
    ]
    for i, o in enumerate(ev.oportunidades[:12], 1):
        lineas.append(
            f'{i}. <a href="{html.escape(o.oferta.url)}">'
            f"{html.escape(o.oferta.titulo[:55])}</a>"
        )
        lineas.append(f"    {eur(o.precio_efectivo)} → +{eur(o.margen_neto_eur)}")
    if len(ev.oportunidades) > 12:
        lineas.append(f"\n… y {len(ev.oportunidades) - 12} más en la web")
    return "\n".join(lineas)


def formatear_resumen(resumen: dict, url_web: Optional[str] = None) -> str:
    lineas = [
        "📊 <b>Resumen</b>",
        "",
        "<pre>",
        _fila("Ofertas activas", str(resumen.get("activas", 0))),
        _fila("Beneficio en juego", eur(resumen.get("margen_total"))),
        _fila("Tiendas vigiladas", str(resumen.get("tiendas", 0))),
        _fila("Lecturas de precio", f"{resumen.get('lecturas', 0):,}".replace(",", ".")),
        "</pre>",
    ]
    if url_web:
        lineas += ["", f'🌐 <a href="{html.escape(url_web)}">Ver todas en la web</a>']
    return "\n".join(lineas)


class Telegram:
    def __init__(self, config: dict):
        cfg = config.get("notificaciones", {}).get("telegram", {})
        self.activo = cfg.get("activo", True)
        self.token = _resolver(cfg.get("bot_token", ""))
        self.chat_id = _resolver(cfg.get("chat_id", ""))
        self.url_web = _resolver(
            config.get("web", {}).get("url_publica", "")) or None
        self._cliente = httpx.AsyncClient(timeout=20)

    async def enviar(self, texto: str, teclado: Optional[dict] = None) -> bool:
        if not self.activo or not self.token or not self.chat_id:
            print(texto)          # modo consola: util en la fase 0
            return False
        try:
            r = await self._cliente.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": texto,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    **({"reply_markup": teclado} if teclado else {}),
                },
            )
            return r.status_code == 200
        except Exception as e:
            print(f"[notifier] fallo al enviar: "
                  f"{censurar(e, self.token, self.chat_id)}")
            return False

    async def alerta(self, o: Oportunidad) -> bool:
        return await self.enviar(formatear(o), botones(o))

    async def alerta_evento(self, ev: EventoTienda) -> bool:
        return await self.enviar(formatear_evento(ev))

    async def resumen(self, datos: dict) -> bool:
        return await self.enviar(formatear_resumen(datos, self.url_web))

    async def aviso_tecnico(self, texto: str) -> bool:
        """Watchdog: un scraper roto en silencio es el fallo nº1 de estos bots."""
        return await self.enviar(f"🔧 <b>Aviso técnico</b>\n{html.escape(texto)}")

    async def cerrar(self) -> None:
        await self._cliente.aclose()


def censurar(texto: str, *secretos: Optional[str]) -> str:
    """Quita claves de los mensajes de error.

    httpx incluye la URL completa en sus excepciones, y la URL de Telegram
    lleva el token dentro. Sin esto, un fallo de red te escribe el token en
    los logs del servidor — y de ahí a cualquiera que los lea.
    """
    salida = str(texto)
    for secreto in secretos:
        if secreto and len(secreto) > 6:
            salida = salida.replace(secreto, "***")
    return salida


def _resolver(valor: str) -> Optional[str]:
    """Permite ${VARIABLE_DE_ENTORNO} en el config."""
    if not valor:
        return None
    if valor.startswith("${") and valor.endswith("}"):
        return os.environ.get(valor[2:-1])
    return valor
