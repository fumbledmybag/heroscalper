"""El detector: convierte lecturas de precio en oportunidades accionables.

Criterio principal (modo experimental): descuento >= 50% respecto al PRECIO
REAL DE MERCADO. "Algo de 200 EUR que de repente esta a 70 EUR".

Deliberadamente NO se mide el descuento contra el precio tachado de la propia
tienda: media internet infla el "precio original" para que todo parezca un
-50% permanente. La referencia valida es, por este orden:

  1. cross_tienda      lo que cuesta el mismo producto en otras tiendas
  2. historico_propio  lo que costaba en ESA tienda las ultimas semanas
  3. variantes_hermanas  lo que cuestan las otras tallas/colores del mismo
                       producto (la unica señal que funciona el primer dia,
                       cuando todavia no hay historico de nada)
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta
from typing import Optional

from . import categorias
from .db import DB
from .models import (EventoTienda, Oferta, Oportunidad, ReferenciaReventa,
                     TipoOportunidad)
from .promos import clasificar, es_gratis

# --------------------------------------------------------------------- #
# Clasificacion por volumen. "Una tele o una aspiradora si, una eliptica no."
# --------------------------------------------------------------------- #
PATRONES_VETO = {
    "fitness_grande": r"\b(elipt|cinta de correr|multiestac|banco de muscul|remo|"
                      r"bicicleta est[aá]tica|spinning|jaula de sentadilla|"
                      r"press banca|saco de boxeo)\b",
    "muebles": r"\b(sof[aá]|armario|estanter[ií]a|c[oó]moda|canap[eé]|somier|"
               r"cabecero|mesa de comedor|silla de oficina|escritorio)\b",
    "colchones": r"\b(colch[oó]n|almohada viscoel)\b",
    "electrodomestico_linea_blanca": r"\b(lavadora|lavavajillas|frigor[ií]fico|nevera|"
                                     r"congelador|secadora|horno encastr|vitrocer[aá]m|"
                                     r"campana extractora|placa de inducci)\b",
    "alimentacion": r"\b(caf[eé] en grano|galletas|conserva|aceite de oliva|snack|"
                    r"cereales|pasta|arroz|chocolatina)\b",
    "bebidas": r"\b(refresco|cerveza|agua mineral|zumo|lata[s]? de|bebida energ)\b",
    "droguería": r"\b(detergente|suavizante|lej[ií]a|friegasuelos|papel higi)\b",
    "higiene": r"\b(champ[uú]|gel de ba[nñ]o|pasta de dientes|desodorante|compresa|"
               r"pa[nñ]al)\b",
    "consumibles": r"\b(recambio|c[aá]psula[s]? de caf|filtro de repuesto|bolsa[s]? "
                   r"de aspirador|cartucho de tinta|t[oó]ner)\b",
    "jardineria_grande": r"\b(cortac[eé]sped|motoazada|piscina desmontable|caseta de "
                         r"jard[ií]n|invernadero|barbacoa de obra)\b",
    "sanitarios": r"\b(plato de ducha|mampara|inodoro|lavabo|ba[nñ]era)\b",
}
PATRONES_VETO_COMPILADOS = {k: re.compile(v, re.IGNORECASE) for k, v in PATRONES_VETO.items()}

# Peso estimado por familia cuando la tienda no lo declara (kg).
PESO_ESTIMADO = [
    (r"\b(port[aá]til|laptop|tablet|monitor)\b", 4.0),
    (r"\b(televis|tv |smart tv)\b", 14.0),
    (r"\b(aspirador|robot aspirador)\b", 8.0),
    (r"\b(cafetera|freidora|batidora|microondas)\b", 7.0),
    (r"\b(taladro|atornillador|amoladora|sierra|lijadora)\b", 3.5),
    (r"\b(consola|playstation|xbox|nintendo)\b", 4.5),
    (r"\b(auricular|altavoz|c[aá]mara|objetivo)\b", 1.5),
    (r"\b(lego|playmobil|puzzle)\b", 2.0),
    (r"\b(juego|videojuego|blu-?ray|libro)\b", 0.4),
    (r"\b(camiseta|pantal[oó]n|chaqueta|zapatilla|abrigo)\b", 0.8),
]
PESO_COMPILADO = [(re.compile(p, re.IGNORECASE), kg) for p, kg in PESO_ESTIMADO]


FUENTES = {
    "cross_tienda": "otras tiendas",
    "historico_propio": "su propio histórico",
    "variantes_hermanas": "otras variantes del mismo producto",
    "sin_verificar": "la propia tienda (sin contrastar)",
    "reventa_2a_mano": "lo que se paga en 2ª mano",
}


class Detector:
    def __init__(self, db: DB, config: dict):
        self.db = db
        self.cfg = config
        self.f = config.get("filtros", {})
        self.costes = config.get("costes", {})
        self.riesgo = config.get("riesgo", {}).get("prob_cancelacion", {})
        self.modo = config.get("modo", "experimental")

    @property
    def experimental(self) -> bool:
        """En experimental no se descarta nada por tipo de producto."""
        return self.modo == "experimental"

    # ------------------------------------------------------------------ #
    # Filtros duros (baratos): descartan antes de calcular nada
    # ------------------------------------------------------------------ #
    def categoria_vetada(self, oferta: Oferta) -> Optional[str]:
        texto = f"{oferta.titulo} {oferta.categoria or ''}"
        for categoria in self.f.get("categorias_veto", []):
            patron = PATRONES_VETO_COMPILADOS.get(categoria)
            if patron and patron.search(texto):
                return categoria
        return None

    def peso_estimado(self, oferta: Oferta) -> float:
        if oferta.peso_kg and oferta.peso_kg > 0:
            return oferta.peso_kg
        for patron, kg in PESO_COMPILADO:
            if patron.search(oferta.titulo):
                return kg
        return 3.0   # asuncion prudente

    def pasa_filtros_basicos(self, oferta: Oferta) -> tuple[bool, str]:
        # Estos dos se aplican SIEMPRE: no son criterio comercial, son sanidad.
        if not oferta.disponible:
            return False, "sin stock"
        if oferta.condicion != "nuevo":
            return False, f"condicion {oferta.condicion}"
        # Un precio de 0,01 EUR suele ser ruido de parseo... salvo que sea una
        # oferta gratuita de verdad, que es justo lo que mas nos interesa.
        if oferta.precio < self.f.get("precio_compra_min_eur", 1):
            gratis, _ = es_gratis(oferta, self.cfg)
            if not gratis:
                return False, "precio irrisorio: casi seguro ruido de parseo"

        # El presupuesto NO es un criterio comercial afinable: es el dinero que
        # tienes. Estaba dentro del bloque "solo en modo selectivo", así que en
        # experimental no se aplicaba y podían colarse artículos de 3.000 €.
        # Además descarta media tienda antes de tocar la base de datos, que es
        # lo caro de evaluar.
        tope = self.f.get("precio_compra_max_eur", 0)
        if tope and oferta.precio > tope:
            return False, "por encima de tu presupuesto"

        if self.experimental:
            return True, ""

        # --- A partir de aqui, solo en modo selectivo ---
        vetada = self.categoria_vetada(oferta)
        if vetada:
            return False, f"categoria vetada: {vetada}"
        peso = self.peso_estimado(oferta)
        if peso > self.f.get("peso_max_kg", 20.0):
            return False, f"demasiado pesado ({peso:.1f} kg)"
        if oferta.dimension_max_cm and oferta.dimension_max_cm > self.f.get(
                "dimension_max_cm", 120.0):
            return False, "voluminoso para Wallapop"
        if self.f.get("requiere_marca", False) and not oferta.marca:
            return False, "sin marca reconocible: no hay demanda en 2a mano"
        return True, ""

    # ------------------------------------------------------------------ #
    # Costes: comprar cuesta portes, y revender tambien
    # ------------------------------------------------------------------ #
    def coste_envio_compra(self, oferta: Oferta, importe_pedido: float) -> float:
        """Portes que te cobra la tienda. Se come el margen de los chollos
        pequeños: 4,95 EUR sobre una compra de 20 EUR es un 25%."""
        cfg = self.costes.get("envio_compra", {}) or {}
        por_tienda = (cfg.get("por_tienda") or {}).get(oferta.tienda, {})
        coste = float(por_tienda.get("coste", cfg.get("por_defecto_eur", 0.0)))
        gratis = por_tienda.get("gratis_desde", cfg.get("gratis_desde_eur"))
        if gratis is not None and importe_pedido >= float(gratis):
            return 0.0
        return coste

    def coste_envio_venta(self, peso_kg: float) -> float:
        tramos = self.costes.get("envio_por_tramo_eur", {})
        for limite in sorted(tramos, key=lambda x: float(x)):
            if peso_kg <= float(limite):
                return float(tramos[limite])
        return float(self.costes.get("voluminoso_eur", 19.05))

    # Alias historico
    def coste_envio(self, peso_kg: float) -> float:
        return self.coste_envio_venta(peso_kg)

    def comision_venta(self, ingreso: float, plataforma: Optional[str]) -> float:
        """Lo que se queda el sitio donde vendes.

        Faltaba, y el beneficio salía inflado: en Wallapop una venta de 165 €
        no te deja 165 €. CeX es la excepción de verdad — ahí te COMPRAN
        ellos, así que el precio que te dan es limpio.
        """
        tabla = self.costes.get("comision_venta", {}) or {}
        regla = tabla.get((plataforma or "").lower()) or tabla.get("por_defecto") or {}
        pct = float(regla.get("pct", 0.0))
        fija = float(regla.get("fija_eur", 0.0))
        return max(0.0, ingreso * pct + (fija if ingreso else 0.0))

    # ------------------------------------------------------------------ #
    # Precio de referencia: de donde sale el "precio original"
    # ------------------------------------------------------------------ #
    def referencia_mercado(
        self, oferta: Oferta, hermanas: Optional[list[Oferta]] = None
    ) -> tuple[Optional[float], str, int]:
        """Devuelve (precio_referencia, fuente, n_observaciones)."""
        fuentes = self.f.get("fuentes_referencia",
                             ["cross_tienda", "historico_propio", "variantes_hermanas"])
        det = self.cfg.get("deteccion", {})

        for fuente in fuentes:
            if fuente == "cross_tienda":
                clave = oferta.clave_match()
                precios = self.db.precios_cross_tienda(clave, oferta.tienda)
                minimo = det.get("min_tiendas_para_cruce", 3)
                if len(precios) >= minimo and oferta.fiabilidad_match() >= det.get(
                        "fiabilidad_match_min_error", 0.6):
                    return statistics.median(precios), "cross_tienda", len(precios)

            elif fuente == "historico_propio":
                cfg_h = det.get("historico", {})
                if not cfg_h.get("activo", True):
                    continue
                hist = self.db.historico_url(oferta.url, det.get("dias_historico", 90))
                # Solo cuentan las lecturas ANTERIORES a esta caida
                hist = [h for h in hist if h > oferta.precio]
                if len(hist) >= cfg_h.get("min_lecturas", 8):
                    return statistics.median(hist), "historico_propio", len(hist)

            elif fuente == "reventa":
                # Triangulación: aunque ninguna otra tienda venda esto, si en
                # CeX te lo compran por 40 y aquí cuesta 10, el margen es real
                # y comprobable. CeX además es precio garantizado, no una
                # opinión: es la referencia más sólida que existe.
                ref = (self.db.reventa(oferta.ean)
                       or self.db.reventa_por_clave(oferta.clave_match()))
                if ref and ref.precio_venta_mediano > 0:
                    return (ref.precio_venta_mediano, "reventa_2a_mano",
                            max(ref.anuncios_90d, 1))

            elif fuente == "variantes_hermanas" and hermanas:
                cfg_v = det.get("variantes", {})
                if not cfg_v.get("activo", True):
                    continue
                precios = [h.precio for h in hermanas
                           if h.url != oferta.url and h.precio > 0]
                if len(precios) >= cfg_v.get("min_variantes", 3) - 1:
                    med = statistics.median(precios)
                    if oferta.precio <= med * cfg_v.get("ratio_vs_hermanas", 0.55):
                        return med, "variantes_hermanas", len(precios)

        return None, "", 0

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # El embudo: por qué NO sale nada
    # ------------------------------------------------------------------ #
    def reiniciar_diagnostico(self) -> None:
        self._emb: dict[str, int] = {}
        self._roza: list[dict] = []

    def _descarta(self, motivo: str) -> None:
        """Apunta por qué se ha caído una oferta.

        Una web vacía no distingue «funciona pero hoy no hay nada» de «está
        roto», y eso es inaceptable: si no sale nada tienes que poder ver en
        qué escalón se cae todo y qué mando tocar.
        """
        if not hasattr(self, "_emb"):
            self.reiniciar_diagnostico()
        self._emb[motivo] = self._emb.get(motivo, 0) + 1

    def _casi(self, oferta: Oferta, margen: float, referencia: float,
              descuento: float) -> None:
        """Guarda las que se han quedado a las puertas por poco margen."""
        if not hasattr(self, "_roza"):
            self.reiniciar_diagnostico()
        self._roza.append({
            "titulo": (oferta.titulo or "")[:90], "tienda": oferta.tienda,
            "url": oferta.url, "precio": round(oferta.precio, 2),
            "referencia": round(referencia, 2),
            "descuento": round(descuento, 3),
            "margen_neto": round(margen, 2),
        })
        self._roza.sort(key=lambda x: x["margen_neto"], reverse=True)
        del self._roza[8:]

    def diagnostico(self) -> dict:
        if not hasattr(self, "_emb"):
            self.reiniciar_diagnostico()
        return {"embudo": dict(self._emb), "a_punto": list(self._roza)}

    # ------------------------------------------------------------------ #
    def candidatas(self, ofertas: list[Oferta],
                   contexto: Optional[list[Oferta]] = None,
                   tope: int = 40) -> list[Oferta]:
        """Lo que MERECE que le preguntemos a CeX y a Wallapop cuánto vale.

        Sin esto el sistema no arranca nunca, y es un círculo vicioso de manual:
        para pasar el filtro de 50 € limpios hace falta saber a cuánto se
        revende, pero solo se consultaba la reventa de lo que YA había pasado
        el filtro. Con la estimación conservadora del 45 %, un −50 % no llega a
        50 € limpios a ningún precio, así que no pasaba nada, así que no se
        consultaba nada, así que nunca había datos de reventa.

        Aquí se seleccionan las que cumplen lo BARATO de comprobar —hay
        referencia y el descuento es claro— sin mirar todavía el margen. De
        esas se averigua la reventa de verdad, y ya con ese dato se evalúan.
        """
        grupos: dict[str, list[Oferta]] = {}
        for o in (contexto if contexto is not None else ofertas):
            if o.grupo_id:
                grupos.setdefault(o.grupo_id, []).append(o)

        marcadas: list[tuple[float, Oferta]] = []
        for o in ofertas:
            ok, _ = self.pasa_filtros_basicos(o)
            if not ok:
                continue
            referencia, fuente, _ = self.referencia_mercado(
                o, grupos.get(o.grupo_id or ""))
            if not referencia or referencia <= 0:
                continue
            if fuente == "reventa_2a_mano":
                continue          # ya sabemos lo que vale: no hay que preguntar
            descuento = 1.0 - (o.precio / referencia)
            if descuento < self.f.get("descuento_min_vs_mercado", 0.50):
                continue
            # Primero las que más euros de diferencia tienen: son las que más
            # posibilidades tienen de dejar los 50 € cuando sepamos la reventa.
            marcadas.append((referencia - o.precio, o))

        marcadas.sort(key=lambda par: par[0], reverse=True)
        return [o for _, o in marcadas[:tope]]

    # ------------------------------------------------------------------ #
    # Evaluacion completa
    # ------------------------------------------------------------------ #
    def evaluar(self, oferta: Oferta,
                hermanas: Optional[list[Oferta]] = None) -> Optional[Oportunidad]:
        ok, motivo = self.pasa_filtros_basicos(oferta)
        if not ok:
            self._descarta(motivo)
            return None
        self._descarta("*leidas")

        referencia, fuente, n_obs = self.referencia_mercado(oferta, hermanas)
        gratis, _ = es_gratis(oferta, self.cfg)
        # Una oferta gratuita no necesita verificarse: no hay precio que
        # contrastar. Las demas, si no hay fuente independiente, van marcadas.
        verificado = bool(referencia and referencia > 0) or gratis

        # Sin fuente independiente: en vez de tirar la oferta, se enseña con
        # el descuento que declara la tienda y marcada como SIN VERIFICAR.
        # Puede ser un PVP inflado — de eso avisa la tarjeta — pero es mejor
        # que no verla: entras, lo miras en un clic y decides tú.
        if not verificado and not gratis:
            if not self.f.get("mostrar_sin_verificar", True):
                self._descarta("sin precio de referencia")
                return None
            declarado = oferta.descuento_declarado
            minimo_declarado = self.f.get("descuento_declarado_min", 0.50)
            if declarado and declarado >= minimo_declarado:
                referencia = oferta.precio_anterior
                fuente = "sin_verificar"
                n_obs = 0
            else:
                self._descarta("sin precio de referencia")
                return None

        # Si la referencia es un precio de 2ª mano, NO se puede llamar a esto
        # "error de precio": la 2ª mano ya está por debajo del retail, así que
        # estar por debajo de ella es un buen margen, no una anomalía. Y su
        # riesgo de cancelación tampoco es el de un error.
        referencia_para_tipo = referencia if fuente != "reventa_2a_mano" else None
        tipo, precio_efectivo, unidades, motivo = clasificar(
            oferta, referencia_para_tipo, self.cfg)

        # --- CRITERIO PRINCIPAL: el descuento tiene que ser claro ---
        if referencia and referencia > 0:
            descuento = 1.0 - (precio_efectivo / referencia)
            minimo = self.f.get("descuento_min_vs_mercado", 0.50)
            if descuento < minimo and tipo != TipoOportunidad.GRATIS:
                self._descarta("descuento por debajo del mínimo")
                return None
            self._descarta("*con descuento suficiente")
        else:
            descuento = 1.0

        # Nunca pedir cantidades que disparen la revision manual del pedido.
        unidades = min(unidades, int(self.f.get("max_unidades_por_pedido", 3)))

        # --- Reventa ---
        clave = oferta.clave_match()
        ref = self.db.reventa(oferta.ean) or self.db.reventa_por_clave(clave)
        # `medida` = viene de datos reales de 2a mano que has cargado tu.
        # Si no, lo que se calcula es una ESTIMACION y hay que decirlo: un
        # beneficio calculado sobre una suposicion no es un beneficio.
        reventa_medida = ref is not None
        referencia_reventa = ref.precio_venta_mediano if ref else None
        dias_venta = ref.dias_venta_mediano if ref else 14.0

        if ref:
            venta_esperada = ref.precio_venta_mediano
            if not self.experimental:
                if ref.anuncios_90d < self.f.get("anuncios_min_90d", 15):
                    return None
                if dias_venta > self.f.get("dias_venta_max", 21):
                    return None
        elif referencia:
            # Sin datos de 2a mano, estimacion conservadora sobre el mercado.
            venta_esperada = referencia * self.f.get("ratio_reventa_min", 0.45)
        else:
            venta_esperada = 0.0

        # --- Margen: descontando portes de COMPRA y de VENTA ---
        peso = self.peso_estimado(oferta)
        importe_pedido = precio_efectivo * unidades
        envio_compra = self.coste_envio_compra(oferta, importe_pedido)
        envio_venta = self.coste_envio_venta(peso) * unidades
        overhead = float(self.costes.get("overhead_por_operacion_eur", 2.5)) * unidades

        coste_total = importe_pedido + envio_compra
        ingreso_total = venta_esperada * unidades
        plataforma_venta = ref.plataforma if ref else None
        comision = self.comision_venta(ingreso_total, plataforma_venta)
        margen_bruto = ingreso_total - importe_pedido
        margen_neto = (ingreso_total - coste_total - envio_venta
                       - overhead - comision)

        # Lo gratis no pasa por el filtro de margen: pediste verlo todo, y
        # además ahí no arriesgas dinero, solo tiempo.
        suelo = self.f.get("margen_neto_min_eur", 0)
        if suelo and tipo != TipoOportunidad.GRATIS and margen_neto < suelo:
            # Esta es la caída que más importa: si todo muere aquí, el sistema
            # está funcionando y lo que sobra es el listón. Se guardan las que
            # más cerca se han quedado para que puedas juzgarlo tú.
            self._descarta("no llega al margen mínimo")
            if referencia:
                self._casi(oferta, margen_neto, referencia, descuento)
            return None
        ahorro_min = self.f.get("ahorro_min_eur", 0)
        if ahorro_min and (referencia - precio_efectivo) * unidades < ahorro_min:
            self._descarta("ahorro en euros insuficiente")
            return None
        margen_pct = margen_neto / coste_total if coste_total else 0.0
        pct_min = self.f.get("margen_pct_min", 0.0)
        if pct_min and margen_pct < pct_min:
            self._descarta("retorno porcentual insuficiente")
            return None
        self._descarta("*oportunidades")

        # Si ya has comprado varias veces en esta tienda, su tasa real de
        # cancelación sustituye a mi estimación. Esto es lo que hace que el
        # sistema mejore con el tiempo en vez de quedarse en suposiciones.
        prob = float(self.riesgo.get(tipo.value, 0.1))
        real = self.db.tasa_cancelacion_tienda(oferta.tienda)
        if real is not None:
            # Media entre mi estimación por tipo y tu dato de esa tienda.
            prob = round((prob + real) / 2, 3)
        margen_esperado = margen_neto * (1 - prob)

        if fuente == "reventa_2a_mano":
            motivo = (f"Cuesta {precio_efectivo:.2f} € y en 2ª mano se paga "
                      f"{referencia:.2f} €")
        elif not motivo:
            motivo = (f"{precio_efectivo:.2f} € frente a {referencia:.2f} € "
                      f"({descuento*100:.0f}% de descuento)")
        if verificado:
            motivo += f" · referencia: {FUENTES.get(fuente, fuente)} ({n_obs} obs.)"
        else:
            motivo += (" · SIN VERIFICAR: el descuento lo declara la propia "
                       "tienda y nadie más lo confirma")
        if envio_compra:
            motivo += f" · +{envio_compra:.2f} € de portes"

        op = Oportunidad(
            oferta=oferta,
            tipo=tipo,
            precio_efectivo=precio_efectivo,
            unidades_recomendadas=unidades,
            referencia_mercado=referencia,
            referencia_reventa=referencia_reventa,
            margen_bruto_eur=margen_bruto,
            margen_neto_eur=margen_neto,
            margen_esperado_eur=margen_esperado,
            margen_pct=margen_pct,
            dias_venta_estimados=dias_venta,
            prob_cancelacion=prob,
            motivo=motivo,
            reventa_medida=reventa_medida,
            verificado=verificado,
            categoria=categorias.clasificar(oferta.titulo, oferta.categoria, oferta.marca),
        )
        op.nivel = self.nivel(op, descuento)
        # Un descuento que solo dice la tienda no puede presumir de nivel:
        # se queda en "buena" como mucho hasta que lo confirmes tú.
        if not verificado:
            op.nivel = min(op.nivel, 2)
        op.historial = self.db.estadisticas_precio(clave)
        return op

    def nivel(self, op: Oportunidad, descuento: float) -> int:
        """Del 1 al 4. Es lo que decide el color con el que lo ves.

        Se mira el descuento Y el ahorro en euros, porque son cosas distintas:
        un -85% sobre 40 € y un -55% sobre 700 € no se parecen en nada, y los
        dos merecen que te fijes.
        """
        cfg = self.cfg.get("niveles", {}) or {}
        ahorro = ((op.referencia_mercado - op.precio_efectivo) * op.unidades_recomendadas
                  if op.referencia_mercado else 0.0)

        for valor, clave in ((4, "brutal"), (3, "muy_buena"), (2, "buena")):
            regla = cfg.get(clave, {}) or {}
            if op.tipo.value in (regla.get("tipos") or []):
                return valor
            if regla.get("descuento") is not None and descuento >= regla["descuento"]:
                return valor
            if regla.get("ahorro_eur") is not None and ahorro >= regla["ahorro_eur"]:
                return valor
        return 1

    # ------------------------------------------------------------------ #
    def evaluar_lote(self, ofertas: list[Oferta],
                     contexto: Optional[list[Oferta]] = None) -> list[Oportunidad]:
        """Evalúa `ofertas`; las variantes hermanas salen de `contexto`.

        Sirve para evaluar solo lo que ha cambiado de precio sin perder la
        comparación entre variantes: si únicamente se ha movido la talla M,
        hay que poder compararla con la S y la L, que no se han movido.
        """
        # Agrupar por producto padre para poder comparar variantes hermanas.
        grupos: dict[str, list[Oferta]] = {}
        for o in (contexto if contexto is not None else ofertas):
            if o.grupo_id:
                grupos.setdefault(o.grupo_id, []).append(o)

        salida = []
        for o in ofertas:
            op = self.evaluar(o, hermanas=grupos.get(o.grupo_id or ""))
            if op:
                salida.append(op)

        salida.sort(key=lambda o: self.prioridad(o), reverse=True)
        return salida

    def prioridad(self, op: Oportunidad) -> float:
        """Con qué criterio se ordenan las oportunidades.

        Por defecto, BENEFICIO NETO: cada venta en Wallapop o Vinted cuesta el
        mismo rato la ganes de 15 o de 150 €, así que lo que manda es cuánto
        deja cada operación, no cuántas caben en un mes.

        Con mucho más capital tendría sentido `margen_por_dia`, que optimiza la
        rotación en vez del importe. Se cambia en `orden_prioridad`.
        """
        criterio = self.cfg.get("orden_prioridad", "margen_neto")
        if criterio == "margen_por_dia":
            base = op.margen_por_dia
        elif criterio == "margen_esperado":
            base = op.margen_esperado_eur
        else:
            base = op.margen_neto_eur
        cfg = self.cfg.get("deteccion", {}).get("ventana_caliente", {})
        if cfg.get("activo", True) and op.oferta.visto_en.weekday() in cfg.get("dias", []):
            base *= float(cfg.get("bonus_prioridad", 1.0))
        return base

    def detectar_eventos(self, oportunidades: list[Oportunidad]) -> list[EventoTienda]:
        """Varias anomalias en la misma tienda = fallo masivo de catalogo.

        Es donde se hace el dinero de golpe: no compras una unidad, compras
        el lote entero antes de que lo arreglen.
        """
        cfg = self.cfg.get("deteccion", {}).get("evento_tienda", {})
        if not cfg.get("activo", True):
            return []
        minimo = cfg.get("min_anomalias", 4)
        ventana = timedelta(minutes=cfg.get("ventana_minutos", 60))
        ahora = datetime.utcnow()

        por_tienda: dict[str, list[Oportunidad]] = {}
        for o in oportunidades:
            if o.tipo not in (TipoOportunidad.ERROR_PRECIO,
                              TipoOportunidad.ERROR_PRECIO_EXTREMO):
                continue
            if ahora - o.oferta.visto_en > ventana:
                continue
            por_tienda.setdefault(o.oferta.tienda, []).append(o)

        return [
            EventoTienda(tienda=t, oportunidades=sorted(
                ops, key=lambda o: o.margen_esperado_eur, reverse=True))
            for t, ops in por_tienda.items() if len(ops) >= minimo
        ]

    # ------------------------------------------------------------------ #
    def zscore_historico(self, oferta: Oferta) -> Optional[float]:
        """Desviacion contra la mediana movil del propio producto."""
        dias = self.cfg.get("deteccion", {}).get("dias_historico", 90)
        historico = self.db.historico_url(oferta.url, dias)
        if len(historico) < 8:
            return None
        med = statistics.median(historico)
        try:
            desv = statistics.stdev(historico)
        except statistics.StatisticsError:
            return None
        if desv == 0:
            return None
        return (med - oferta.precio) / desv
