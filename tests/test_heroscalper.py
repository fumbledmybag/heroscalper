"""Tests de HeroScalper. Ejecutar: python -m pytest tests/ -q"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest

from heroscalper.db import DB
from heroscalper.detector import Detector
from heroscalper.matching import clave_producto, codigo_modelo, fiabilidad
from heroscalper.models import Oferta, ReferenciaReventa, TipoOportunidad
from heroscalper.promos import parsear_multiunidad, clasificar
from heroscalper.runner import cargar_config
from heroscalper.adapters.base import a_float, normaliza_ean
from heroscalper.adapters.shopify import ShopifyAdapter


CFG = cargar_config(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))

# Varios tests comprueban que el detector ENCUENTRA algo, no si supera el
# suelo de beneficio. Para esos se usa la misma config con el suelo a 0.
def _sin_suelo():
    import copy
    c = copy.deepcopy(CFG)
    c["filtros"]["margen_neto_min_eur"] = 0
    return c

CFG_TODO = _sin_suelo()


# ---------------------------------------------------------------- matching
@pytest.mark.parametrize("t1,t2,marca", [
    ("LEGO Technic 42115 Lamborghini Sian", "Set LEGO 42115 Technic Sián FKP 37", "LEGO"),
    ("Sony WH-1000XM5 Auriculares", "Auriculares Sony WH1000XM5 negros", "Sony"),
    ("Bosch Professional GBH 2-28 F", "Martillo Bosch GBH 2-28 F 880W", "Bosch"),
    ("Makita DHP484Z Taladro 18V", "Taladro Makita DHP484Z sin batería", "Makita"),
])
def test_mismo_producto_misma_clave(t1, t2, marca):
    assert clave_producto(marca, t1) == clave_producto(marca, t2)


def test_productos_distintos_claves_distintas():
    assert clave_producto("LEGO", "LEGO Technic 42115") != clave_producto("LEGO", "LEGO Technic 42131")


def test_el_ean_manda_sobre_el_titulo():
    k = clave_producto("LEGO", "cualquier cosa", "5702016616989")
    assert k.startswith("ean:") and fiabilidad(k) == 1.0


def test_el_año_no_es_un_modelo():
    assert codigo_modelo("Nintendo Switch OLED 2024", "Nintendo") is None


def test_texto_suelto_es_poco_fiable():
    assert fiabilidad(clave_producto(None, "Camiseta básica")) < 0.6


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("entrada,esperado", [
    ("1.234,56 €", 1234.56), ("1,234.56", 1234.56), ("59.00", 59.0),
    ("19,99", 19.99), ("", None), (None, None),
])
def test_a_float(entrada, esperado):
    assert a_float(entrada) == esperado


@pytest.mark.parametrize("entrada,esperado", [
    ("5702016616989", "5702016616989"), ("570201661698", "0570201661698"),
    ("abc", None), (None, None), ("12", None),
])
def test_normaliza_ean(entrada, esperado):
    assert normaliza_ean(entrada) == esperado


def test_shopify_parsea_el_esquema_real():
    """Esquema verificado contra tiendas Shopify españolas reales.

    Ojo: `barcode` NO viene en el products.json publico de muchas tiendas;
    por eso existe el matching por marca+modelo.
    """
    producto = {
        "id": 1, "title": "Sudadera Grand Prix", "handle": "sudadera-grand-prix",
        "vendor": "Pompeii", "product_type": "Sudaderas", "tags": ["outlet", "sale"],
        "variants": [{
            "id": 99, "title": "M", "sku": "H10001020W2615221", "available": True,
            "price": "49.00", "compare_at_price": "89.00", "grams": 300,
        }],
    }
    adapter = ShopifyAdapter(cliente=None, config=CFG)
    ofertas = adapter._a_ofertas(producto, "pompeiibrand.com")
    assert len(ofertas) == 1
    o = ofertas[0]
    assert o.precio == 49.0 and o.precio_anterior == 89.0
    assert o.marca == "Pompeii" and o.peso_kg == 0.3
    assert o.es_liquidacion is True                # tag "outlet"
    assert o.ean is None                           # el feed publico no lo trae
    assert abs(o.descuento_declarado - 0.4494) < 0.001


def test_shopify_ignora_precio_tachado_incoherente():
    producto = {"id": 1, "title": "X", "handle": "x", "vendor": "V", "tags": [],
                "variants": [{"id": 1, "price": "50.00", "compare_at_price": "30.00"}]}
    o = ShopifyAdapter(None, CFG)._a_ofertas(producto, "t.es")[0]
    assert o.precio_anterior is None


# ---------------------------------------------------------------- promos
@pytest.mark.parametrize("texto,unidades,factor", [
    ("3x2 en toda la sección", 3, 2/3),
    ("Llévate 2x1", 2, 0.5),
    ("Segunda unidad -50%", 2, 0.75),
])
def test_multiunidad(texto, unidades, factor):
    m = parsear_multiunidad(texto)
    assert m.unidades == unidades
    assert abs(m.factor - factor) < 0.01


def test_sin_promo_no_inventa():
    assert parsear_multiunidad("Producto normal sin ofertas") is None


def test_un_3x2_no_es_un_error_de_precio():
    """Regresión: el 3x2 bajaba el precio efectivo y se clasificaba como error,
    lo que le asignaba un 35% de riesgo de cancelación en vez de un 5%."""
    o = Oferta(tienda="t.es", url="u", titulo="LEGO 42115", precio=99.0,
               marca="LEGO", promo_texto="3x2 en toda la sección")
    tipo, precio_ef, uds, _ = clasificar(o, mediana_mercado=131.97, cfg=CFG)
    assert tipo == TipoOportunidad.PROMO_MULTIUNIDAD
    assert abs(precio_ef - 66.0) < 0.01 and uds == 3


def test_precio_absurdo_si_es_error():
    o = Oferta(tienda="t.es", url="u", titulo="LEGO 42115", precio=19.99, marca="LEGO")
    tipo, _, _, _ = clasificar(o, mediana_mercado=131.97, cfg=CFG)
    assert tipo == TipoOportunidad.ERROR_PRECIO_EXTREMO


# ---------------------------------------------------------------- detector
def _cfg_selectivo():
    import copy
    c = copy.deepcopy(CFG)
    c["modo"] = "selectivo"
    c["filtros"]["requiere_marca"] = True
    c["filtros"]["precio_compra_max_eur"] = 245
    return c


@pytest.fixture
def detector(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    ean = "5702016616989"
    for i, (tienda, precio) in enumerate([("a.es", 129.95), ("b.es", 134.0),
                                          ("c.es", 127.5), ("d.es", 139.99)]):
        db.guardar_lecturas([Oferta(tienda=tienda, url=f"https://{tienda}/{i}",
                                    titulo="LEGO Technic 42115", precio=precio,
                                    ean=ean, marca="LEGO", peso_kg=2.0)])
    db.guardar_reventa(ReferenciaReventa(ean=ean, modelo="LEGO Technic 42115",
                                         precio_venta_mediano=118.0, anuncios_90d=64,
                                         dias_venta_mediano=9.0))
    return Detector(db, CFG)


@pytest.fixture
def selectivo(detector):
    """Mismo dato, pero con los filtros de tipo de producto encendidos."""
    return Detector(detector.db, _cfg_selectivo())


def _oferta(**kw):
    base = dict(tienda="x.es", url="https://x.es/p", titulo="LEGO Technic 42115",
                precio=19.99, ean="5702016616989", marca="LEGO", peso_kg=2.0)
    base.update(kw)
    return Oferta(**base)


def test_detecta_el_chollo_barato(detector):
    """Regresión: un filtro de ticket mínimo sobre el PRECIO DE COMPRA
    descartaba justo el mejor caso (LEGO de 130 € a 19,99 €)."""
    op = detector.evaluar(_oferta())
    assert op is not None
    assert op.tipo == TipoOportunidad.ERROR_PRECIO_EXTREMO
    assert op.margen_neto_eur > 30


def test_descarta_articulo_de_poco_valor(selectivo):
    ok, motivo = selectivo.pasa_filtros_basicos(
        _oferta(titulo="Pack 24 latas de refresco cola", precio=9.9, ean=None))
    assert not ok and "bebidas" in motivo


def test_descarta_voluminoso(selectivo):
    ok, motivo = selectivo.pasa_filtros_basicos(
        _oferta(titulo="Bicicleta elíptica BH Fitness", precio=199.0, peso_kg=45.0))
    assert not ok


def test_descarta_lavadora(selectivo):
    ok, motivo = selectivo.pasa_filtros_basicos(
        _oferta(titulo="Lavadora Bosch 8kg", precio=240.0, peso_kg=None))
    assert not ok and "linea_blanca" in motivo


def test_acepta_aspiradora(selectivo):
    """El usuario dijo explícitamente: una aspiradora sí, una elíptica no."""
    ok, _ = selectivo.pasa_filtros_basicos(
        _oferta(titulo="Aspirador escoba Dyson V15 Detect", precio=240.0,
                marca="Dyson", peso_kg=None))
    assert ok


def test_descarta_sin_marca(selectivo):
    ok, motivo = selectivo.pasa_filtros_basicos(_oferta(marca=None))
    assert not ok and "marca" in motivo


def test_el_presupuesto_se_respeta_en_los_dos_modos(selectivo):
    """El tope de precio es el dinero que tienes, no un criterio afinable.

    Estaba dentro del bloque "solo en modo selectivo", así que en experimental
    —el que usas— no se aplicaba y podían colarse artículos de 3.000 €.
    """
    ok, motivo = selectivo.pasa_filtros_basicos(_oferta(precio=500.0))
    assert not ok and "presupuesto" in motivo

    import copy
    cfg = copy.deepcopy(CFG)
    cfg["modo"] = "experimental"
    cfg["filtros"]["precio_compra_max_eur"] = 1000
    experimental = Detector(selectivo.db, cfg)
    assert experimental.experimental
    ok, motivo = experimental.pasa_filtros_basicos(_oferta(precio=3000.0))
    assert not ok and "presupuesto" in motivo
    # Y por debajo del tope sigue pasando, claro.
    assert experimental.pasa_filtros_basicos(_oferta(precio=300.0))[0]


def test_no_alerta_sin_suficientes_tiendas_cruzadas(tmp_path):
    """Una sola tienda, sin histórico y sin datos de 2ª mano: no hay con qué
    comparar, así que no se afirma nada."""
    db = DB(str(tmp_path / "t2.db"))
    db.guardar_lecturas([Oferta(tienda="a.es", url="https://a.es/1",
                                titulo="LEGO Technic 42115", precio=130.0,
                                ean="5702016616989", marca="LEGO")])
    assert Detector(db, CFG).evaluar(_oferta()) is None


def test_triangulacion_la_reventa_sirve_de_referencia(tmp_path):
    """Aunque ninguna otra tienda lo venda: si en CeX te lo compran por 40 y
    aquí cuesta 10, el margen es real y comprobable."""
    db = DB(str(tmp_path / "tri.db"))
    db.guardar_reventa(ReferenciaReventa(
        ean=None, modelo="Consola Retro X200", precio_venta_mediano=40.0,
        anuncios_90d=1, dias_venta_mediano=1.0, precio_suelo=40.0,
        plataforma="cex"))
    op = Detector(db, CFG_TODO).evaluar(Oferta(
        tienda="rara.es", url="https://rara.es/1",
        titulo="Consola Retro X200", precio=10.0))
    assert op is not None
    assert op.verificado is True                    # está contrastado
    assert "2ª mano" in op.motivo
    assert op.margen_neto_eur > 0


def test_la_reventa_como_referencia_no_es_un_error_de_precio(tmp_path):
    """La 2ª mano ya está por debajo del retail: estar por debajo de ella es
    buen margen, no una anomalía. Y su riesgo de cancelación no es el mismo."""
    db = DB(str(tmp_path / "tri2.db"))
    db.guardar_reventa(ReferenciaReventa(
        ean=None, modelo="Consola Retro X200", precio_venta_mediano=40.0,
        anuncios_90d=1, dias_venta_mediano=1.0, plataforma="cex"))
    op = Detector(db, CFG_TODO).evaluar(Oferta(
        tienda="rara.es", url="https://rara.es/1",
        titulo="Consola Retro X200", precio=10.0))
    assert op.tipo != TipoOportunidad.ERROR_PRECIO_EXTREMO
    assert op.prob_cancelacion < 0.2


def test_ordena_por_margen_por_dia(detector):
    """El KPI es margen/día, no el descuento nominal."""
    ops = detector.evaluar_lote([
        _oferta(tienda="a.es", url="https://a.es/x", precio=19.99),
        _oferta(tienda="b.es", url="https://b.es/x", precio=55.0,
                promo_texto="3x2"),
    ])
    assert len(ops) == 2
    assert ops[0].margen_por_dia >= ops[1].margen_por_dia


def test_margen_descuenta_envio_y_riesgo(detector):
    op = detector.evaluar(_oferta())
    assert op.margen_neto_eur < op.margen_bruto_eur      # porte + overhead
    assert op.margen_esperado_eur < op.margen_neto_eur   # riesgo de cancelación
    assert op.prob_cancelacion == 0.65


def test_evento_tienda_reventada(detector):
    ofertas = [_oferta(tienda="rota.es", url=f"https://rota.es/{i}") for i in range(5)]
    eventos = detector.detectar_eventos(detector.evaluar_lote(ofertas))
    assert len(eventos) == 1 and len(eventos[0].oportunidades) == 5


def test_cooldown_evita_alertas_repetidas(tmp_path):
    db = DB(str(tmp_path / "t3.db"))
    db.registrar_alerta("ean:123", "x.es", "ERROR_PRECIO", 20.0, 50.0)
    assert db.ya_alertado("ean:123", horas=24)
    assert not db.ya_alertado("ean:999", horas=24)


# ------------------------------------------------- modo experimental y descuento
def test_experimental_no_descarta_por_tipo_de_producto(detector):
    """El usuario pidió sin límites: que entre todo y ya decide él."""
    for titulo, peso in [("Bicicleta elíptica BH Fitness", 45.0),
                         ("Lavadora Bosch 8kg", 70.0),
                         ("Pack 24 latas de refresco", 8.0)]:
        ok, motivo = detector.pasa_filtros_basicos(
            _oferta(titulo=titulo, peso_kg=peso, marca=None))
        assert ok, f"{titulo} descartado: {motivo}"


def test_exige_descuento_del_50_por_ciento(detector):
    """-30% no es un chollo, es una rebaja normal."""
    assert detector.evaluar(_oferta(precio=95.0)) is None      # -28%
    assert detector.evaluar(_oferta(precio=45.0)) is not None  # -66%


def test_el_precio_tachado_se_muestra_pero_marcado_sin_verificar(tmp_path):
    """Media internet infla el 'precio original'. No se tira la oferta —
    se enseña avisando, y la compruebas tú en un clic."""
    db = DB(str(tmp_path / "rrp.db"))
    o = Oferta(tienda="infla.es", url="https://infla.es/1", titulo="Reloj XYZ 900",
               precio=29.0, precio_anterior=99.0, marca="XYZ")
    op = Detector(db, CFG_TODO).evaluar(o)
    assert op is not None
    assert op.verificado is False
    assert op.nivel <= 2                    # no puede presumir de ofertón
    assert "SIN VERIFICAR" in op.motivo


def test_sin_verificar_se_puede_desactivar(tmp_path):
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["filtros"]["mostrar_sin_verificar"] = False
    o = Oferta(tienda="infla.es", url="https://infla.es/1", titulo="Reloj XYZ 900",
               precio=29.0, precio_anterior=99.0, marca="XYZ")
    assert Detector(DB(str(tmp_path / "rrp2.db")), cfg).evaluar(o) is None


def test_un_descuento_declarado_flojo_no_entra(tmp_path):
    """Un -20% que dice la tienda y nadie confirma no es nada."""
    o = Oferta(tienda="infla.es", url="https://infla.es/2", titulo="Reloj XYZ 900",
               precio=80.0, precio_anterior=99.0, marca="XYZ")
    assert Detector(DB(str(tmp_path / "rrp3.db")), CFG).evaluar(o) is None


def test_lo_verificado_si_puede_ser_oferton(detector):
    """Con cuatro tiendas cruzadas, el mismo descuento sí sube de nivel."""
    op = detector.evaluar(_oferta(precio=19.99))
    assert op.verificado is True and op.nivel == 4


# ------------------------------------------------- referencia por variantes
def test_anomalia_entre_variantes_sin_datos_de_otras_tiendas(tmp_path):
    """La señal que funciona el primer día: la talla M a 9 € cuando todas
    sus hermanas están a 89 €."""
    db = DB(str(tmp_path / "var.db"))
    det = Detector(db, CFG_TODO)
    hermanas = [
        Oferta(tienda="t.es", url=f"https://t.es/p?v={i}", titulo=f"Chaqueta Norte - {t}",
               precio=p, marca="Norte", grupo_id="t.es:1")
        for i, (t, p) in enumerate([("S", 89.0), ("M", 9.0), ("L", 89.0), ("XL", 92.0)])
    ]
    ops = det.evaluar_lote(hermanas)
    assert len(ops) == 1
    assert ops[0].oferta.precio == 9.0
    assert "variantes" in ops[0].motivo


def test_variante_barata_pero_no_anomala_no_alerta(tmp_path):
    db = DB(str(tmp_path / "var2.db"))
    hermanas = [
        Oferta(tienda="t.es", url=f"https://t.es/p?v={i}", titulo="Chaqueta Norte",
               precio=p, marca="Norte", grupo_id="t.es:1")
        for i, p in enumerate([89.0, 79.0, 89.0, 92.0])
    ]
    assert Detector(db, CFG).evaluar_lote(hermanas) == []


# ------------------------------------------------- referencia por histórico
def test_referencia_por_historico_propio(tmp_path):
    """Con una sola tienda pero histórico suficiente, también detecta.

    Las lecturas van en días distintos porque el histórico guarda una por día
    cuando el precio no se mueve.
    """
    from datetime import datetime, timedelta
    db = DB(str(tmp_path / "hist.db"))
    url = "https://sola.es/p1"
    for d in range(10):
        db.guardar_lecturas([Oferta(
            tienda="sola.es", url=url, titulo="Cafetera Zeta 300", precio=199.0,
            marca="Zeta", visto_en=datetime.utcnow() - timedelta(days=10 - d))])
    op = Detector(db, CFG_TODO).evaluar(
        Oferta(tienda="sola.es", url=url, titulo="Cafetera Zeta 300",
               precio=59.0, marca="Zeta"))
    assert op is not None and "histórico" in op.motivo


# ------------------------------------------------- costes de envío
def test_portes_de_compra_reducen_el_margen(detector):
    op = detector.evaluar(_oferta(precio=20.0))
    assert "portes" in op.motivo
    # 20 € está por debajo del umbral de envío gratis (50 €)
    assert op.margen_neto_eur < op.margen_bruto_eur - 4.0


def test_envio_gratis_por_encima_del_umbral(detector):
    o = _oferta(precio=55.0)
    assert detector.coste_envio_compra(o, 55.0) == 0.0
    assert detector.coste_envio_compra(o, 20.0) == 4.95


def test_envio_de_compra_por_tienda(tmp_path):
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["costes"]["envio_compra"]["por_tienda"] = {"gratis.es": {"coste": 0.0,
                                                                "gratis_desde": 0.0}}
    det = Detector(DB(str(tmp_path / "e.db")), cfg)
    assert det.coste_envio_compra(_oferta(tienda="gratis.es"), 10.0) == 0.0
    assert det.coste_envio_compra(_oferta(tienda="otra.es"), 10.0) == 4.95


# ------------------------------------------------- seguridad operativa
def test_tope_de_unidades_para_no_disparar_cancelaciones(detector):
    """Pedir cantidades anormales de un error de precio dispara la revisión
    manual del pedido. El bot nunca recomienda más de 3 unidades."""
    op = detector.evaluar(_oferta(precio=30.0, promo_texto="6x2"))
    assert op.unidades_recomendadas <= 3


# ------------------------------------------------- ciclo de vida de las ofertas
def _oportunidad(tienda="t.es", precio=70.0, titulo="LEGO Technic 42115"):
    """Oportunidad de prueba con reventa ESTIMADA (el caso por defecto)."""
    from heroscalper.models import Oportunidad
    o = Oferta(tienda=tienda, url=f"https://{tienda}/p", titulo=titulo,
               precio=precio, marca="LEGO", sku="X1")
    return Oportunidad(oferta=o, tipo=TipoOportunidad.ERROR_PRECIO,
                       precio_efectivo=precio, unidades_recomendadas=1,
                       referencia_mercado=200.0, referencia_reventa=165.0,
                       margen_bruto_eur=95.0, margen_neto_eur=84.3,
                       margen_esperado_eur=54.8, margen_pct=1.2,
                       dias_venta_estimados=9, prob_cancelacion=0.35,
                       motivo="referencia: otras tiendas")


def test_oferta_se_crea_y_luego_se_refresca(tmp_path):
    db = DB(str(tmp_path / "v.db"))
    assert db.upsert_oferta(_oportunidad()) is True     # nueva
    assert db.upsert_oferta(_oportunidad()) is False    # ya existía
    assert len(db.ofertas_activas()) == 1


def test_la_oferta_que_deja_de_verse_expira(tmp_path):
    db = DB(str(tmp_path / "e.db"))
    db.upsert_oferta(_oportunidad())
    assert len(db.ofertas_activas()) == 1
    assert db.expirar_ofertas(minutos=0) == 1
    assert db.ofertas_activas() == []          # desaparece del feed sola


def test_las_guardadas_no_expiran(tmp_path):
    """Si le das a la estrella, se queda hasta que tú la sueltes."""
    db = DB(str(tmp_path / "g.db"))
    db.upsert_oferta(_oportunidad())
    db.marcar_guardada("t.es:X1", True)
    assert db.expirar_ofertas(minutos=0) == 0
    assert len(db.ofertas_activas()) == 1


def test_la_oferta_expirada_revive_si_vuelve_a_detectarse(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    db.upsert_oferta(_oportunidad())
    db.expirar_ofertas(minutos=0)
    db.upsert_oferta(_oportunidad())
    assert len(db.ofertas_activas()) == 1


def test_filtro_y_orden_del_feed(tmp_path):
    db = DB(str(tmp_path / "f.db"))
    for i, (t, p) in enumerate([("a.es", 70.0), ("b.es", 30.0), ("c.es", 120.0)]):
        db.upsert_oferta(_oportunidad(tienda=t, precio=p))
    assert len(db.ofertas_activas(tipo="ERROR_PRECIO")) == 3
    assert db.ofertas_activas(tipo="LIQUIDACION") == []
    precios = [o["precio"] for o in db.ofertas_activas(orden="precio")]
    assert precios == sorted(precios, reverse=True)


def test_orden_invalido_no_permite_inyeccion(tmp_path):
    db = DB(str(tmp_path / "i.db"))
    db.upsert_oferta(_oportunidad())
    assert len(db.ofertas_activas(orden="precio; DROP TABLE ofertas")) == 1


def test_el_descuento_se_guarda_calculado(tmp_path):
    db = DB(str(tmp_path / "d.db"))
    db.upsert_oferta(_oportunidad(precio=70.0))     # ref 200
    assert abs(db.ofertas_activas()[0]["descuento"] - 0.65) < 0.01


# ------------------------------------------------- formato de las alertas
def test_la_alerta_lleva_todo_lo_que_hace_falta_para_decidir():
    from heroscalper.notifier import formatear
    op = _oportunidad()
    op.reventa_medida = True
    texto = formatear(op)
    for esperado in ["PRECIO ANÓMALO", "LEGO Technic 42115", "t.es",
                     "Precio ahora", "Precio real", "Descuento",
                     "Reventa 2ª mano", "Beneficio neto"]:
        assert esperado in texto, f"falta «{esperado}» en la alerta"


def test_la_reventa_estimada_se_marca_como_estimada():
    """Un beneficio calculado sobre una suposición no es un beneficio."""
    from heroscalper.notifier import formatear
    op = _oportunidad()
    op.reventa_medida = False
    texto = formatear(op)
    assert "estimada" in texto and "~" in texto
    assert "Reventa 2ª mano" not in texto      # no se presenta como dato


def test_el_boton_de_compra_es_un_boton_no_un_enlace_perdido():
    from heroscalper.notifier import botones
    kb = botones(_oportunidad())["inline_keyboard"]
    assert kb[0][0]["url"] == "https://t.es/p"
    assert "Comprar" in kb[0][0]["text"]
    # y los botones para comprobar el precio de reventa a mano
    assert any("wallapop" in b["url"] for b in kb[1])
    assert any("vinted" in b["url"] for b in kb[1])


def test_la_consulta_de_reventa_no_duplica_la_marca():
    from heroscalper.notifier import botones
    op = _oportunidad(titulo="LEGO Technic 42115")
    url = botones(op)["inline_keyboard"][1][0]["url"]
    assert url.lower().count("lego") == 1


def test_los_importes_van_en_formato_español():
    from heroscalper.notifier import eur
    assert eur(1234.5) == "1.234,50 €"
    assert eur(69) == "69,00 €"
    assert eur(None) == "—"


# ------------------------------------------------- API de la web
def test_la_api_solo_sirve_ofertas_vivas(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    ruta = str(tmp_path / "web.db")
    db = DB(ruta)
    db.upsert_oferta(_oportunidad(tienda="viva.es"))
    db.upsert_oferta(_oportunidad(tienda="muerta.es"))
    db.expirar_ofertas(minutos=0)
    db.upsert_oferta(_oportunidad(tienda="viva.es"))    # solo esta revive
    db.cerrar()

    from heroscalper.web import app as webapp
    monkeypatch.setattr(webapp, "CONFIG", {"base_datos": {"ruta": ruta}})
    monkeypatch.setattr(webapp, "ABIERTA", True)
    r = TestClient(webapp.app).get("/api/ofertas")
    assert r.status_code == 200
    tiendas = [o["tienda"] for o in r.json()["ofertas"]]
    assert tiendas == ["viva.es"]


def _cliente_con_clave(tmp_path, monkeypatch, nombre="p.db",
                       usuarios={"pavel": "clave-larga-y-secreta"}):
    from fastapi.testclient import TestClient
    from heroscalper.web import app as webapp
    monkeypatch.setattr(webapp, "CONFIG",
                        {"base_datos": {"ruta": str(tmp_path / nombre)}})
    monkeypatch.setattr(webapp, "USUARIOS", usuarios)
    monkeypatch.setattr(webapp, "ABIERTA", False)
    monkeypatch.setattr(webapp, "_INTENTOS", __import__("collections").defaultdict(list))
    return TestClient(webapp.app), webapp


def test_la_web_pide_contraseña_si_esta_configurada(tmp_path, monkeypatch):
    c, _ = _cliente_con_clave(tmp_path, monkeypatch)
    assert c.get("/api/ofertas").status_code == 401
    assert c.get("/api/salud").status_code == 401
    assert c.get("/api/historico/x").status_code == 401
    assert c.post("/api/ofertas/x/guardar").status_code == 401
    assert c.get("/", follow_redirects=False).status_code == 303


def test_se_entra_con_la_contraseña_correcta(tmp_path, monkeypatch):
    c, _ = _cliente_con_clave(tmp_path, monkeypatch, "ok.db")
    r = c.post("/login", data={"usuario": "pavel", "clave": "clave-larga-y-secreta"},
               follow_redirects=False)
    assert r.status_code == 303 and "hs_session" in r.cookies
    assert c.get("/api/ofertas").status_code == 200


def test_la_cookie_de_sesion_no_es_accesible_por_javascript(tmp_path, monkeypatch):
    c, _ = _cliente_con_clave(tmp_path, monkeypatch, "cookie.db")
    r = c.post("/login", data={"usuario": "pavel", "clave": "clave-larga-y-secreta"},
               follow_redirects=False)
    cabecera = r.headers["set-cookie"].lower()
    assert "httponly" in cabecera and "samesite=lax" in cabecera


def test_cerrar_sesion_la_revoca_de_verdad(tmp_path, monkeypatch):
    c, _ = _cliente_con_clave(tmp_path, monkeypatch, "logout.db")
    c.post("/login", data={"usuario": "pavel", "clave": "clave-larga-y-secreta"})
    assert c.get("/api/ofertas").status_code == 200
    c.post("/logout")
    assert c.get("/api/ofertas").status_code == 401


def test_el_login_se_bloquea_tras_varios_fallos(tmp_path, monkeypatch):
    """Una IP pública con formulario de contraseña y sin freno es una
    invitación a probar contraseñas toda la noche."""
    c, _ = _cliente_con_clave(tmp_path, monkeypatch, "fuerza.db")
    for _ in range(5):
        r = c.post("/login", data={"usuario": "pavel", "clave": "mal"},
                   follow_redirects=False)
        assert "error=1" in r.headers["location"]
    # sexto intento: bloqueado aunque la contraseña sea correcta
    r = c.post("/login", data={"usuario": "pavel", "clave": "clave-larga-y-secreta"},
               follow_redirects=False)
    assert "error=2" in r.headers["location"]


def test_varios_usuarios_para_compartir_con_un_colega(tmp_path, monkeypatch):
    c, _ = _cliente_con_clave(tmp_path, monkeypatch, "multi.db",
                              usuarios={"pavel": "clave-a", "luis": "clave-b"})
    assert c.post("/login", data={"usuario": "luis", "clave": "clave-b"},
                  follow_redirects=False).status_code == 303
    assert c.get("/api/ofertas").status_code == 200


def test_un_usuario_inexistente_no_entra(tmp_path, monkeypatch):
    c, _ = _cliente_con_clave(tmp_path, monkeypatch, "nadie.db")
    r = c.post("/login", data={"usuario": "intruso", "clave": "loquesea"},
               follow_redirects=False)
    assert "error=1" in r.headers["location"]


def test_cabeceras_de_seguridad(tmp_path, monkeypatch):
    c, _ = _cliente_con_clave(tmp_path, monkeypatch, "cab.db")
    h = c.get("/login").headers
    assert h["x-frame-options"] == "DENY"                    # nada de iframes
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in h["content-security-policy"]


def test_el_endpoint_de_vida_no_filtra_nada(tmp_path, monkeypatch):
    c, _ = _cliente_con_clave(tmp_path, monkeypatch, "vivo.db")
    r = c.get("/api/vivo")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_la_web_pide_no_ser_indexada(tmp_path, monkeypatch):
    c, _ = _cliente_con_clave(tmp_path, monkeypatch, "idx.db")
    assert "noindex" in c.get("/login").text


# ------------------------------------------------- categorías
@pytest.mark.parametrize("titulo,esperada", [
    ("Portátil Lenovo IdeaPad 15 Ryzen 5", "informatica"),
    ("iPhone 15 Pro 256GB", "moviles"),
    ("Sony WH-1000XM5 auriculares", "audio_tv"),
    ("Mando DualSense para PS5", "gaming"),
    ("Makita DHP484Z taladro percutor", "herramientas"),
    ("LEGO Technic 42115", "juguetes"),
    ("Robot aspirador Roomba j7", "hogar"),
    ("Zapatillas Nike Air Max talla 42", "moda"),
    ("Carrito de bebé Bugaboo Fox", "bebe"),
    ("Perfume Dior Sauvage 100ml", "belleza"),
    ("Cámara Sony Alpha 7 IV", "fotografia"),
    ("Bicicleta MTB 29 pulgadas", "deporte"),
    ("Cosa rara sin clasificar", "otros"),
])
def test_clasificacion_por_categoria(titulo, esperada):
    from heroscalper.categorias import clasificar
    assert clasificar(titulo) == esperada


def test_la_categoria_se_guarda_en_la_oferta(tmp_path):
    db = DB(str(tmp_path / "c.db"))
    op = _oportunidad(titulo="Makita DHP484Z taladro")
    op.categoria = "herramientas"
    db.upsert_oferta(op)
    assert db.ofertas_activas()[0]["categoria_clave"] == "herramientas"
    assert [c["clave"] for c in db.categorias_con_ofertas()] == ["herramientas"]


# ------------------------------------------------- ofertas gratis
@pytest.mark.parametrize("titulo,precio,esperado", [
    ("Auriculares AirPods 4 de regalo con tu compra", 29.0, True),
    ("Smartwatch Xiaomi gratis", 0.0, True),
    ("Bono de bienvenida 50 € al registrarte", 0.0, True),
    ("Sony WH-1000XM5 auriculares", 69.0, False),
])
def test_deteccion_de_ofertas_gratuitas(titulo, precio, esperado):
    from heroscalper.promos import es_gratis
    ok, _ = es_gratis(Oferta(tienda="x.es", url="u", titulo=titulo, precio=precio), CFG)
    assert ok is esperado


@pytest.mark.parametrize("titulo", [
    "Móvil gratis contratando fibra con permanencia 24 meses",
    "Smartwatch de regalo al domiciliar tu nómina",
    "Tablet gratis con la contratación de un seguro de hogar",
])
def test_las_promos_con_letra_pequeña_se_muestran_marcadas(titulo):
    """No se descartan: se enseñan avisando de lo que piden, y tú decides."""
    from heroscalper.promos import es_gratis
    ok, motivo = es_gratis(Oferta(tienda="x.es", url="u", titulo=titulo, precio=0.0), CFG)
    assert ok is True
    assert "⚠" in motivo and "exige" in motivo


@pytest.mark.parametrize("titulo", [
    "Móvil gratis contratando fibra con permanencia 24 meses",
])
def test_se_pueden_descartar_si_lo_configuras(titulo):
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["deteccion"]["promos"]["descartar_letra_pequena"] = True
    from heroscalper.promos import es_gratis
    ok, _ = es_gratis(Oferta(tienda="x.es", url="u", titulo=titulo, precio=0.0), cfg)
    assert ok is False


def test_una_oferta_gratis_no_necesita_referencia_de_mercado(tmp_path):
    """Si es gratis, es gratis: no hace falta calcularle un descuento."""
    db = DB(str(tmp_path / "gr.db"))
    op = Detector(db, CFG).evaluar(
        Oferta(tienda="x.es", url="https://x.es/1", marca="Apple",
               titulo="AirPods 4 de regalo con tu compra", precio=0.0))
    assert op is not None and op.tipo == TipoOportunidad.GRATIS


# ------------------------------------------------- histórico
def test_el_historico_guarda_todo_lo_escaneado(tmp_path):
    """Nada se tira: todas las lecturas quedan para consultar después."""
    db = DB(str(tmp_path / "h.db"))
    o = Oferta(tienda="a.es", url="https://a.es/1", titulo="Sony WH-1000XM5",
               precio=289.0, marca="Sony")
    db.guardar_lecturas([o])
    db.guardar_lecturas([Oferta(tienda="b.es", url="https://b.es/1",
                                titulo="Sony WH1000XM5 negros", precio=299.0,
                                marca="Sony")])
    assert len(db.historico_producto(o.clave_match())) == 2


def test_las_expiradas_siguen_consultables_como_historico(tmp_path):
    db = DB(str(tmp_path / "he.db"))
    db.upsert_oferta(_oportunidad())
    db.expirar_ofertas(minutos=0)
    assert db.ofertas_activas(estado="activa") == []
    assert len(db.ofertas_activas(estado="expirada")) == 1
    assert len(db.ofertas_activas(estado="todas")) == 1


# ------------------------------------------------- presupuesto
def test_no_muestra_nada_por_encima_del_presupuesto(detector):
    """Presupuesto real: 1.000 €. Lo que no puedes pagar no es una oportunidad."""
    assert CFG["filtros"]["precio_compra_max_eur"] == 1000
    import copy
    cfg = copy.deepcopy(CFG); cfg["modo"] = "selectivo"
    d = Detector(detector.db, cfg)
    assert d.pasa_filtros_basicos(_oferta(precio=1200.0))[0] is False
    assert d.pasa_filtros_basicos(_oferta(precio=850.0))[0] is True


# ------------------------------------------------- destacados
def test_lo_gordo_se_marca_como_destacado(detector):
    """Un error de precio extremo va siempre arriba y en rojo."""
    op = detector.evaluar(_oferta(precio=19.99))
    assert op.nivel == 4 and op.destacada is True


def test_los_niveles_ordenan_lo_bueno_de_lo_normal(tmp_path):
    """Un -55% con 110 € de ahorro es "buena", no un ofertón."""
    det = Detector(DB(str(tmp_path / "d2.db")), CFG)
    assert det.nivel(_oportunidad(precio=90.0), 0.55) == 2      # buena
    assert det.nivel(_oportunidad(precio=60.0), 0.70) == 3      # muy buena
    assert det.nivel(_oportunidad(precio=30.0), 0.85) == 4      # brutal


def test_el_ahorro_grande_sube_de_nivel_aunque_el_descuento_sea_flojo(tmp_path):
    """-55% sobre 900 € importa igual que -85% sobre 40 €."""
    det = Detector(DB(str(tmp_path / "d5.db")), CFG)
    from heroscalper.models import Oportunidad
    op = _oportunidad(precio=400.0)
    op.referencia_mercado = 900.0        # 500 € de ahorro
    assert det.nivel(op, 0.55) == 4


def test_solo_se_destaca_a_partir_de_muy_buena():
    op = _oportunidad()
    op.nivel = 2
    assert op.destacada is False
    op.nivel = 3
    assert op.destacada is True


def test_lo_gratis_siempre_destaca(tmp_path):
    db = DB(str(tmp_path / "d3.db"))
    op = Detector(db, CFG).evaluar(
        Oferta(tienda="x.es", url="https://x.es/1", marca="Apple",
               titulo="AirPods 4 de regalo con tu compra", precio=0.0))
    assert op.destacada is True


def test_el_nivel_se_guarda_en_la_oferta(tmp_path):
    db = DB(str(tmp_path / "d4.db"))
    op = _oportunidad()
    op.nivel = 4
    db.upsert_oferta(op)
    fila = db.ofertas_activas()[0]
    assert fila["nivel"] == 4 and fila["destacada"] == 1


# ------------------------------------------------- tasación de reventa
def test_cex_lee_el_precio_garantizado():
    """CeX no da lo que alguien pide: da lo que te pagan hoy."""
    from heroscalper.reventa import CeX, Cotizacion

    class RespFalsa:
        status_code = 200
        @staticmethod
        def json():
            return {"response": {"data": {"boxes": [
                {"boxId": "SPS5DIGB", "boxName": "PlayStation 5 Digital",
                 "sellPrice": 320.0, "cashPrice": 180.0, "exchangePrice": 215.0},
                {"boxId": "OTRO", "boxName": "Mando", "sellPrice": 40.0,
                 "cashPrice": 18.0, "exchangePrice": 22.0},
            ]}}}

    class ClienteFalso:
        async def get(self, url): return RespFalsa()

    c = asyncio.run(CeX(ClienteFalso(), {"pais": "es"}).cotizar("PS5"))
    assert isinstance(c, Cotizacion)
    assert c.precio_garantizado == 215.0     # el mejor de los dos, el del vale
    assert c.fuente == "cex" and c.dias_venta == 1.0


def test_wallapop_recorta_los_extremos():
    """Hay anuncios de piezas sueltas a 5 € y de lotes a 900 €: descuadran."""
    from heroscalper.reventa import Wallapop

    precios = [5, 90, 95, 100, 105, 110, 900]
    class RespFalsa:
        status_code = 200
        @staticmethod
        def json(): return {"search_objects": [{"price": p} for p in precios]}
    class ClienteFalso:
        async def get(self, url): return RespFalsa()

    c = asyncio.run(Wallapop(ClienteFalso(), {"factor_cierre": 1.0,
                                              "min_anuncios": 5}).cotizar("x"))
    assert 90 <= c.precio <= 110            # ni 5 ni 900
    assert c.n_muestras == 7


def test_el_precio_garantizado_gana_a_los_anuncios():
    """Entre lo que alguien pide y lo que te pagan seguro, manda lo segundo."""
    from heroscalper.reventa import Cotizacion, Tasador
    cotiz = [
        Cotizacion(fuente="wallapop", consulta="x", precio=205.0, n_muestras=40),
        Cotizacion(fuente="cex", consulta="x", precio=180.0,
                   precio_garantizado=180.0, n_muestras=1),
    ]
    assert Tasador.mejor(cotiz).fuente == "cex"


def test_sin_precio_garantizado_gana_la_muestra_mas_grande():
    from heroscalper.reventa import Cotizacion, Tasador
    cotiz = [Cotizacion(fuente="vinted", consulta="x", precio=40.0, n_muestras=6),
             Cotizacion(fuente="wallapop", consulta="x", precio=55.0, n_muestras=38)]
    assert Tasador.mejor(cotiz).fuente == "wallapop"


def test_la_consulta_de_tasacion_no_duplica_la_marca():
    from heroscalper.reventa import consulta_de
    o = Oferta(tienda="x", url="u", titulo="Sony WH-1000XM5 auriculares", precio=1,
               marca="Sony")
    assert consulta_de(o).lower().count("sony") == 1


def test_las_cotizaciones_se_guardan_como_historico(tmp_path):
    """Con el tiempo esto es tu propia serie de a cuánto se paga cada cosa."""
    from heroscalper.reventa import Cotizacion
    db = DB(str(tmp_path / "cot.db"))
    db.guardar_cotizaciones("mod:sony:wh1000xm5", [
        Cotizacion(fuente="cex", consulta="Sony WH-1000XM5", precio=180.0,
                   precio_garantizado=180.0, n_muestras=1),
        Cotizacion(fuente="wallapop", consulta="Sony WH-1000XM5", precio=205.0,
                   n_muestras=42),
    ])
    assert len(db.historico_cotizaciones("mod:sony:wh1000xm5")) == 2
    assert db.cotizacion_fresca("mod:sony:wh1000xm5", horas=72)
    assert not db.cotizacion_fresca("otra", horas=72)


# ------------------------------------------------- salud visible en la web
def test_la_salud_distingue_tiendas_rotas_de_avisos(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    for dom in ("ok.es", "floja.es", "rota.es"):
        db.registrar_tienda(dom, "shopify")
    db.marcar_exito("ok.es")
    db.marcar_fallo("floja.es")
    for _ in range(3):
        db.marcar_fallo("rota.es")
    salud = db.salud_tiendas()
    assert salud["total"] == 3 and salud["rotas"] == 1 and salud["con_avisos"] == 1
    assert salud["detalle_rotas"][0]["dominio"] == "rota.es"


# ------------------------------------------------- tamaño del histórico
def test_no_se_guarda_la_misma_lectura_dos_veces(tmp_path):
    """Guardar cada lectura de cada ciclo llena el disco de un servidor
    pequeño en dos meses. Solo se guarda si el precio ha cambiado."""
    db = DB(str(tmp_path / "dedupe.db"))
    def leer(p):
        return len(db.guardar_lecturas([Oferta(tienda="a.es", url="https://a.es/1",
                                               titulo="Sony WH-1000XM5", precio=p,
                                               marca="Sony")]))
    assert leer(289.0) == 1        # primera vez
    assert leer(289.0) == 0        # no ha cambiado nada
    assert leer(289.0) == 0
    assert leer(69.0) == 1         # ha cambiado: esto sí importa
    assert leer(69.0) == 0


def test_lecturas_de_productos_distintos_no_se_pisan(tmp_path):
    db = DB(str(tmp_path / "dd2.db"))
    ofertas = [Oferta(tienda="a.es", url=f"https://a.es/{i}", titulo=f"P{i}",
                      precio=10.0 + i) for i in range(5)]
    assert len(db.guardar_lecturas(ofertas)) == 5
    assert len(db.guardar_lecturas(ofertas)) == 0


def test_una_lectura_vieja_se_refresca_aunque_no_cambie(tmp_path):
    """Sin esto, un precio estable dejaría de tener histórico."""
    from datetime import datetime, timedelta
    db = DB(str(tmp_path / "dd3.db"))
    vieja = Oferta(tienda="a.es", url="https://a.es/1", titulo="P", precio=50.0,
                   visto_en=datetime.utcnow() - timedelta(hours=30))
    db.guardar_lecturas([vieja])
    nueva = Oferta(tienda="a.es", url="https://a.es/1", titulo="P", precio=50.0)
    assert len(db.guardar_lecturas([nueva])) == 1


def test_la_poda_deja_una_lectura_por_dia(tmp_path):
    """Más allá de unos meses basta un punto al día para la gráfica."""
    from datetime import datetime, timedelta
    db = DB(str(tmp_path / "poda.db"))
    # Anclado a mediodía para que las 10 lecturas caigan el MISMO día natural
    base = (datetime.utcnow() - timedelta(days=200)).replace(hour=8, minute=0,
                                                            second=0, microsecond=0)
    with db.cursor() as cur:
        for h in range(10):
            cur.execute(
                "INSERT INTO lecturas (url, tienda, precio, visto_en) VALUES (?,?,?,?)",
                ("https://a.es/1", "a.es", 50.0,
                 (base + timedelta(hours=h)).isoformat()))
    resultado = db.podar(dias_detalle=120, dias_maximo=730)
    assert resultado["resumidas"] == 9        # de 10 del mismo día queda 1
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM lecturas")
        assert cur.fetchone()["n"] == 1


def test_la_poda_borra_lo_demasiado_viejo(tmp_path):
    from datetime import datetime, timedelta
    db = DB(str(tmp_path / "poda2.db"))
    with db.cursor() as cur:
        cur.execute("INSERT INTO lecturas (url, tienda, precio, visto_en) VALUES (?,?,?,?)",
                    ("u", "a.es", 9.0,
                     (datetime.utcnow() - timedelta(days=900)).isoformat()))
    assert db.podar(dias_maximo=730)["antiguas"] == 1


# ------------------------------------------------- historial de precio
def test_estadisticas_de_precio(tmp_path):
    """La pregunta que contesta: ¿está barato o es su precio de siempre?"""
    from datetime import datetime, timedelta
    db = DB(str(tmp_path / "est.db"))
    ahora = datetime.utcnow()
    for i, precio in enumerate([300.0, 290.0, 310.0, 295.0, 69.0]):
        db.guardar_lecturas([Oferta(
            tienda="a.es", url="https://a.es/1", titulo="Sony WH-1000XM5",
            precio=precio, marca="Sony",
            visto_en=ahora - timedelta(days=5 - i))])
    est = db.estadisticas_precio("mod:sony:wh1000xm5")
    assert est["n_lecturas"] == 5
    assert est["minimo"] == 69.0 and est["maximo"] == 310.0
    assert 250 < est["media_90d"] < 270
    assert len(est["serie"]) == 5
    assert est["serie"][-1] == 69.0        # el último punto es el actual


def test_sin_historico_las_estadisticas_no_inventan(tmp_path):
    db = DB(str(tmp_path / "est2.db"))
    est = db.estadisticas_precio("mod:nada:000")
    assert est["media_90d"] is None and est["n_lecturas"] == 0


def test_el_historial_se_guarda_en_la_oferta(tmp_path):
    db = DB(str(tmp_path / "est3.db"))
    for p in (289.0, 279.0, 70.0):
        db.guardar_lecturas([Oferta(tienda="t.es", url="https://t.es/p",
                                    titulo="LEGO Technic 42115", precio=p,
                                    marca="LEGO", sku="X1")])
    db.upsert_oferta(_oportunidad())
    fila = db.ofertas_activas()[0]
    assert fila["n_lecturas"] == 3
    assert fila["minimo_historico"] == 70.0
    assert fila["media_90d"] > 200
    assert "289" in fila["serie"]


def test_la_alerta_menciona_el_historico():
    from heroscalper.notifier import formatear
    op = _oportunidad()
    op.historial = {"media_90d": 285.68, "minimo": 69.0, "n_lecturas": 25}
    texto = formatear(op)
    assert "Media histórica" in texto and "285,68" in texto


def test_la_alerta_avisa_de_lo_no_verificado():
    from heroscalper.notifier import formatear
    op = _oportunidad()
    op.verificado = False
    texto = formatear(op)
    assert "SIN VERIFICAR" in texto
    assert "PVP inflado" in texto


def test_el_estado_de_verificacion_se_guarda(tmp_path):
    db = DB(str(tmp_path / "ver.db"))
    op = _oportunidad()
    op.verificado = False
    db.upsert_oferta(op)
    assert db.ofertas_activas()[0]["verificado"] == 0


# ------------------------------------------------- fugas de datos
def test_los_errores_no_filtran_el_token():
    """httpx mete la URL completa en sus excepciones, y la de Telegram lleva
    el token dentro. Un fallo de red no puede escribirlo en los logs."""
    from heroscalper.notifier import censurar
    token = "7654321:AAF-clave-secreta-de-telegram"
    error = f"ConnectError al llamar a https://api.telegram.org/bot{token}/sendMessage"
    limpio = censurar(error, token)
    assert token not in limpio and "***" in limpio


def test_censurar_no_rompe_con_valores_vacios():
    from heroscalper.notifier import censurar
    assert censurar("mensaje normal", None, "", "abc") == "mensaje normal"


def test_la_base_de_datos_no_guarda_datos_personales(tmp_path):
    """Auditoría: ninguna tabla debe tener columnas de datos personales."""
    db = DB(str(tmp_path / "audit.db"))
    prohibidas = {"email", "correo", "telefono", "movil", "direccion", "dni",
                  "nombre_real", "tarjeta", "iban", "password", "contrasena"}
    with db.cursor() as cur:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tablas = [r["name"] for r in cur.fetchall()]
        for tabla in tablas:
            cur.execute(f"PRAGMA table_info({tabla})")
            columnas = {r["name"].lower() for r in cur.fetchall()}
            assert not (columnas & prohibidas), f"{tabla} guarda datos personales"


def test_las_sesiones_guardan_el_hash_no_el_token(tmp_path):
    """Si alguien lee la base de datos, no puede suplantar a nadie."""
    import hashlib
    db = DB(str(tmp_path / "ses.db"))
    token = "token-secreto-de-sesion"
    db.crear_sesion(hashlib.sha256(token.encode()).hexdigest(), "pavel", "1.2.3.4", 60)
    with db.cursor() as cur:
        cur.execute("SELECT token_hash FROM sesiones")
        guardado = cur.fetchone()["token_hash"]
    assert token not in guardado
    assert len(guardado) == 64            # sha256 en hexadecimal


def test_las_sesiones_caducan(tmp_path):
    import hashlib
    db = DB(str(tmp_path / "cad.db"))
    h = hashlib.sha256(b"t").hexdigest()
    db.crear_sesion(h, "pavel", "1.2.3.4", -1)     # ya caducada
    assert db.sesion_valida(h) is None


# ------------------------------------------------- rendimiento del barrido
def test_las_tiendas_se_barren_en_paralelo(tmp_path, monkeypatch):
    """De una en una, 250 tiendas tardan 47 min y no caben en el ciclo de 30."""
    import time
    from heroscalper.runner import HeroScalper
    import copy

    cfg = copy.deepcopy(CFG)
    cfg["base_datos"] = {"ruta": str(tmp_path / "par.db")}
    cfg["rastreo"]["tiendas_a_la_vez"] = 10
    cfg["reventa"]["activo"] = False
    hs = HeroScalper(cfg)

    async def lenta(dominio, plataforma=None, max_productos=2000, solo_outlet=False, desde=0, cambiados_desde=""):
        await asyncio.sleep(0.15)
        return []
    monkeypatch.setattr(hs, "barrer_tienda", lenta)

    dominios = [f"t{i}.es" for i in range(10)]
    # Ya reconocidas: aquí se mide el BARRIDO, no el reconocimiento.
    for d in dominios:
        hs.db.registrar_tienda(d, "shopify")

    inicio = time.monotonic()
    asyncio.run(hs.ciclo(dominios))
    transcurrido = time.monotonic() - inicio
    # Secuencial serían 1,5 s; en paralelo tiene que bajar mucho de eso
    assert transcurrido < 0.8, f"tardó {transcurrido:.2f}s: parece secuencial"


def test_solo_se_leen_las_fichas_que_la_tienda_dice_haber_tocado():
    """No hace falta releer 4.000 fichas para encontrar las 30 que han cambiado.

    Las tiendas publican <lastmod> por producto en su sitemap (lo ponen para
    Google). Es un aviso de cambio gratis, y usarlo es la diferencia entre
    mirar el catálogo entero y mirar solo lo que se ha movido.
    """
    from heroscalper.adapters.jsonld import JsonLdAdapter

    XML = """<?xml version="1.0"?><urlset>
      <url><loc>https://t.es/p/viejo-1</loc><lastmod>2026-01-05</lastmod></url>
      <url><loc>https://t.es/p/viejo-2</loc><lastmod>2026-02-01</lastmod></url>
      <url><loc>https://t.es/p/nuevo-1</loc><lastmod>2026-09-07</lastmod></url>
      <url><loc>https://t.es/p/nuevo-2</loc><lastmod>2026-09-08</lastmod></url>
    </urlset>"""

    entradas = JsonLdAdapter._entradas_sitemap(XML)
    assert len(entradas) == 4
    assert entradas[0] == ("https://t.es/p/viejo-1", "2026-01-05")

    frescos = [u for u, mod in entradas if mod and mod > "2026-09-06"]
    assert frescos == ["https://t.es/p/nuevo-1", "https://t.es/p/nuevo-2"]

    # Un sitemap sin <lastmod> no puede romper nada: se devuelven las urls.
    sin_fecha = """<urlset>
      <url><loc>https://t.es/p/a</loc></url>
      <url><loc>https://t.es/p/b</loc></url></urlset>"""
    assert JsonLdAdapter._entradas_sitemap(sin_fecha) == [
        ("https://t.es/p/a", ""), ("https://t.es/p/b", "")]


def test_el_catalogo_se_puede_releer_cada_n_ciclos(tmp_path, monkeypatch):
    """Con 500 tiendas no se puede releer todo cada media hora.

    El outlet se sigue mirando siempre; el catálogo entero, cada N ciclos.
    """
    from heroscalper.runner import HeroScalper
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["base_datos"] = {"ruta": str(tmp_path / "ciclos.db")}
    cfg["reventa"]["activo"] = False
    cfg["rastreo"]["catalogo_cada_ciclos"] = 3
    hs = HeroScalper(cfg)
    hs.db.registrar_tienda("t.es", "shopify")

    modos = []
    async def espia(dominio, plataforma=None, max_productos=2000,
                    solo_outlet=False, desde=0, cambiados_desde=""):
        modos.append(solo_outlet)
        return []
    monkeypatch.setattr(hs, "barrer_tienda", espia)

    for _ in range(6):
        asyncio.run(hs.ciclo(["t.es"]))

    # Ciclo 1 y 4: catálogo completo. El resto, solo outlet.
    assert modos == [False, True, True, False, True, True], modos


def test_el_catalogo_generico_visita_solo_lo_cambiado(monkeypatch):
    """De 4.000 peticiones a 2: el ahorro real de leer el <lastmod>."""
    from heroscalper.adapters.jsonld import JsonLdAdapter
    from heroscalper.models import Oferta
    import copy

    cfg = copy.deepcopy(CFG)
    cfg.setdefault("rastreo", {})["max_productos_generico"] = 200
    ad = JsonLdAdapter(cliente=None, config=cfg)

    catalogo = [(f"https://t.es/p/{i}", "2026-01-01") for i in range(4000)]
    catalogo[7] = ("https://t.es/p/7", "2026-09-08")
    catalogo[999] = ("https://t.es/p/999", "2026-09-08")

    async def sitemap(dominio, limite):
        return catalogo
    monkeypatch.setattr(ad, "_urls_sitemap", sitemap)

    visitadas = []
    async def producto(url):
        visitadas.append(url)
        return Oferta(tienda="t.es", url=url, titulo="Cosa", precio=10.0)
    monkeypatch.setattr(ad, "producto", producto)

    # Con fecha de último barrido: solo las dos que la tienda dice haber tocado.
    asyncio.run(ad.catalogo("t.es", 4000, cambiados_desde="2026-09-01"))
    assert visitadas == ["https://t.es/p/7", "https://t.es/p/999"]

    # Sin fecha (primer barrido): ventana rotatoria, nunca el catálogo entero.
    visitadas.clear()
    asyncio.run(ad.catalogo("t.es", 4000, desde=0))
    assert len(visitadas) == 200
    visitadas.clear()
    asyncio.run(ad.catalogo("t.es", 4000, desde=200))
    assert visitadas[0] == "https://t.es/p/200"


def test_una_tienda_muerta_se_apaga_y_deja_de_costar_timeouts(tmp_path):
    """Un dominio caído cuesta un timeout en CADA ciclo si nadie lo apaga."""
    from heroscalper.db import DB
    db = DB(str(tmp_path / "apagado.db"))
    db.registrar_tienda("muerta.es", "shopify")
    db.registrar_tienda("viva.es", "shopify")

    for i in range(4):
        db.marcar_fallo("muerta.es")
        assert db.dominios_apagados() == set(), f"apagada demasiado pronto ({i+1})"
    db.marcar_fallo("muerta.es")            # la quinta
    assert db.dominios_apagados() == {"muerta.es"}
    assert [r["dominio"] for r in db.tiendas_activas()] == ["viva.es"]

    # Pasado el plazo se vuelve a probar: pudo ser una caída temporal.
    with db.cursor() as cur:
        cur.execute("UPDATE tiendas SET apagada_en = '2020-01-01T00:00:00' "
                    "WHERE dominio = 'muerta.es'")
    assert db.reactivar_caducados(24) == 1
    assert db.dominios_apagados() == set()
    db.cerrar()


def test_el_lector_generico_rota_el_catalogo(tmp_path):
    """Visitar 4.000 productos de uno en uno es casi 2 h para una tienda.

    Se lee un trozo por ciclo y se va rotando: sin esto el ciclo no termina.
    """
    from heroscalper.db import DB
    db = DB(str(tmp_path / "offset.db"))
    db.registrar_tienda("generica.es", "jsonld")
    assert db.offsets_catalogo()["generica.es"] == 0
    db.avanzar_offset("generica.es", 200)
    db.avanzar_offset("generica.es", 200)
    assert db.offsets_catalogo()["generica.es"] == 400
    db.cerrar()


def test_la_comision_de_la_plataforma_se_descuenta():
    """Una venta de 165 € en Wallapop no te deja 165 €.

    Faltaba por completo y el beneficio salía inflado justo en la cifra que
    decide si compras o no.
    """
    from heroscalper.detector import Detector
    import copy
    cfg = copy.deepcopy(CFG)
    cfg.setdefault("costes", {})["comision_venta"] = {
        "cex":      {"pct": 0.0,  "fija_eur": 0.0},
        "wallapop": {"pct": 0.05, "fija_eur": 0.0},
        "por_defecto": {"pct": 0.05, "fija_eur": 0.0},
    }
    det = Detector.__new__(Detector)
    det.costes = cfg["costes"]

    # CeX te compra: sin comisión.
    assert det.comision_venta(165.0, "cex") == 0.0
    # Wallapop: 5 %.
    assert round(det.comision_venta(165.0, "wallapop"), 2) == 8.25
    # Plataforma desconocida: se usa el por defecto, no cero.
    assert round(det.comision_venta(165.0, "loquesea"), 2) == 8.25
    assert round(det.comision_venta(165.0, None), 2) == 8.25
    # Sin ingreso no hay comisión ni parte fija.
    assert det.comision_venta(0.0, "wallapop") == 0.0


def test_el_barrido_va_en_tuberia_y_no_acumula_todo(tmp_path, monkeypatch):
    """Dos cosas a la vez: memoria acotada y ninguna tienda esperando.

    Si se barre todo de golpe, 172 tiendas x 4.000 productos son 1,5 GB y el
    servidor de 512 MB muere. Y si se barre por tandas esperando a que
    terminen todas, una tienda lenta deja a otras veinticuatro paradas.
    """
    from heroscalper.runner import HeroScalper
    from heroscalper.models import Oferta
    import copy

    cfg = copy.deepcopy(CFG)
    cfg["base_datos"] = {"ruta": str(tmp_path / "tuberia.db")}
    cfg["reventa"]["activo"] = False
    cfg["rastreo"]["tiendas_a_la_vez"] = 5
    hs = HeroScalper(cfg)

    dominios = [f"t{i}.es" for i in range(20)]
    for d in dominios:
        hs.db.registrar_tienda(d, "shopify")

    vivas = 0
    pico_vivas = 0
    picos_lote = []

    async def cien(dominio, plataforma=None, max_productos=2000,
                   solo_outlet=False, desde=0, cambiados_desde=""):
        nonlocal vivas, pico_vivas
        vivas += 1
        pico_vivas = max(pico_vivas, vivas)
        await asyncio.sleep(0.01)
        lote = [Oferta(tienda=dominio, url=f"https://{dominio}/p/{i}",
                       titulo=f"Cosa {i}", precio=10.0 + i, marca="M")
                for i in range(100)]
        vivas -= 1
        return lote, lote
    monkeypatch.setattr(hs, "barrer_tienda", cien)

    original = hs.detector.evaluar_lote
    def espia(lote, contexto=None):
        picos_lote.append(len(lote))
        return original(lote, contexto)
    monkeypatch.setattr(hs.detector, "evaluar_lote", espia)

    resumen = asyncio.run(hs.ciclo(dominios))

    assert resumen["lecturas"] == 2000            # 20 x 100
    # Nunca más de `tiendas_a_la_vez` tiendas en el aire.
    assert pico_vivas <= 5, f"{pico_vivas} tiendas a la vez: sin freno"
    # Y se evalúa tienda a tienda, no un montón acumulado.
    assert picos_lote == [100] * 20, picos_lote


def test_solo_se_evalua_lo_que_ha_cambiado_de_precio(tmp_path, monkeypatch):
    """Evaluar una oferta cuesta varias consultas; casi ningún precio se mueve.

    Sin esto, 172 tiendas x 4.000 productos son 688.000 evaluaciones cada
    media hora en un servidor de medio procesador.
    """
    from heroscalper.runner import HeroScalper
    from heroscalper.models import Oferta
    import copy

    cfg = copy.deepcopy(CFG)
    cfg["base_datos"] = {"ruta": str(tmp_path / "cambios.db")}
    cfg["reventa"]["activo"] = False
    cfg["rastreo"]["reevaluar_todo_cada_ciclos"] = 99   # que no toque el repaso
    hs = HeroScalper(cfg)
    hs.db.registrar_tienda("t.es", "shopify")

    lote = [Oferta(tienda="t.es", url=f"https://t.es/p/{i}", titulo=f"P{i}",
                   precio=10.0 + i, marca="M") for i in range(50)]

    cambiadas = list(lote)          # la primera vez cambia todo
    async def barrido(dominio, plataforma=None, max_productos=2000,
                      solo_outlet=False, desde=0, cambiados_desde=""):
        return lote, cambiadas
    monkeypatch.setattr(hs, "barrer_tienda", barrido)

    evaluadas = []
    original = hs.detector.evaluar_lote
    def espia(l, contexto=None):
        evaluadas.append(len(l))
        return original(l, contexto)
    monkeypatch.setattr(hs.detector, "evaluar_lote", espia)

    asyncio.run(hs.ciclo(["t.es"]))
    assert evaluadas == [50]                    # primera vez: todo

    # Segundo ciclo: solo se ha movido un precio.
    evaluadas.clear()
    cambiadas = [lote[7]]
    asyncio.run(hs.ciclo(["t.es"]))
    assert evaluadas == [1], f"evaluó {evaluadas} en vez de solo lo que cambió"

    # Tercero: no se mueve nada. Cero evaluaciones, cero consultas.
    evaluadas.clear()
    cambiadas = []
    asyncio.run(hs.ciclo(["t.es"]))
    assert evaluadas == []


def test_lo_que_no_cambia_no_caduca_por_silencio(tmp_path):
    """Saltarse la evaluación no puede hacer que una oferta buena desaparezca."""
    from heroscalper.db import DB
    from datetime import datetime, timedelta
    db = DB(str(tmp_path / "refresco.db"))
    with db.cursor() as cur:
        cur.execute("""INSERT INTO ofertas
            (clave, url, tienda, titulo, precio, tipo, nivel, vista_primera,
             vista_ultima, estado)
            VALUES ('k1','https://t.es/1','t.es','P',10.0,'LIQUIDACION',2,?,?,
                    'activa')""",
            ((datetime.utcnow() - timedelta(hours=9)).isoformat(),
             (datetime.utcnow() - timedelta(hours=9)).isoformat()))

    assert db.urls_ofertas_activas() == {"https://t.es/1"}
    # Sin refrescar, una oferta vista hace 9 h caduca con el umbral de 3 h.
    assert db.refrescar_vistas(["https://t.es/1"]) == 1
    assert db.expirar_ofertas(180) == 0, "ha caducado pese a haberla vuelto a ver"
    db.cerrar()


def test_una_tienda_nueva_se_reconoce_sola(tmp_path, monkeypatch):
    """En Render nadie lanza «descubrir» a mano: el ciclo tiene que hacerlo.

    Sin esto toda tienda se leería con el lector genérico, que saca una
    fracción de lo que sacan los de Shopify y WooCommerce.
    """
    from heroscalper.runner import HeroScalper
    from heroscalper import runner as mod
    import copy

    cfg = copy.deepcopy(CFG)
    cfg["base_datos"] = {"ruta": str(tmp_path / "desc.db")}
    cfg["reventa"]["activo"] = False
    hs = HeroScalper(cfg)

    class HuellaFalsa:
        def __init__(self, dominio):
            self.dominio = dominio; self.plataforma = "shopify"
            self.tiene_jsonld = True; self.tiene_ean = True; self.vigilable = True

    async def huella_falsa(cliente, dominio):
        return HuellaFalsa(dominio)
    monkeypatch.setattr(mod, "huella", huella_falsa)

    usadas = []
    async def anota(dominio, plataforma=None, max_productos=2000, solo_outlet=False, desde=0, cambiados_desde=""):
        usadas.append(plataforma)
        return []
    monkeypatch.setattr(hs, "barrer_tienda", anota)

    asyncio.run(hs.ciclo(["tiendanueva.es"]))

    # Se ha reconocido ANTES de barrer, así que ya se lee con el lector bueno.
    assert usadas == ["shopify"]
    assert [r["dominio"] for r in hs.db.tiendas_activas()] == ["tiendanueva.es"]


def test_una_tienda_rota_no_tumba_el_ciclo(tmp_path, monkeypatch):
    from heroscalper.runner import HeroScalper
    import copy
    cfg = copy.deepcopy(CFG)
    cfg["base_datos"] = {"ruta": str(tmp_path / "rota.db")}
    cfg["reventa"]["activo"] = False
    hs = HeroScalper(cfg)

    async def a_veces_falla(dominio, plataforma=None, max_productos=2000,
                            solo_outlet=False):
        if dominio == "mala.es":
            raise RuntimeError("la tienda explotó")
        return []
    monkeypatch.setattr(hs, "barrer_tienda", a_veces_falla)

    resumen = asyncio.run(hs.ciclo(["buena.es", "mala.es", "otra.es"]))
    assert resumen["lecturas"] == 0        # el ciclo termina, no revienta


# ------------------------------------------------- suelo de beneficio
def test_no_llega_nada_por_debajo_del_beneficio_minimo(tmp_path):
    """50 € netos: por debajo no compensa comprar, empaquetar y responder."""
    assert CFG["filtros"]["margen_neto_min_eur"] == 50
    db = DB(str(tmp_path / "suelo.db"))
    db.guardar_reventa(ReferenciaReventa(
        ean=None, modelo="Consola Retro X200", precio_venta_mediano=40.0,
        anuncios_90d=1, dias_venta_mediano=1.0, plataforma="cex"))
    # 10 € y se revende a 40 -> unos 18 € de margen: no llega
    o = Oferta(tienda="rara.es", url="https://rara.es/1",
               titulo="Consola Retro X200", precio=10.0)
    assert Detector(db, CFG).evaluar(o) is None
    assert Detector(db, CFG_TODO).evaluar(o) is not None   # sin suelo, sí


def test_si_llega_lo_que_deja_mas_de_50(tmp_path):
    db = DB(str(tmp_path / "suelo2.db"))
    db.guardar_reventa(ReferenciaReventa(
        ean=None, modelo="Auriculares Zeta 900", precio_venta_mediano=180.0,
        anuncios_90d=30, dias_venta_mediano=7.0, plataforma="wallapop"))
    op = Detector(db, CFG).evaluar(Oferta(
        tienda="t.es", url="https://t.es/1", titulo="Auriculares Zeta 900",
        precio=60.0))
    assert op is not None and op.margen_neto_eur >= 50


def test_lo_gratis_esta_exento_del_suelo(tmp_path):
    """Pediste ver todas las gratis: ahí no arriesgas dinero, solo tiempo."""
    db = DB(str(tmp_path / "suelo3.db"))
    op = Detector(db, CFG).evaluar(Oferta(
        tienda="x.es", url="https://x.es/1", marca="Apple",
        titulo="Auriculares de regalo con tu compra", precio=0.0))
    assert op is not None and op.tipo == TipoOportunidad.GRATIS


# ------------------------------------------------- registro de compras
def test_apuntar_una_compra_y_venderla(tmp_path):
    """El bucle que hace que el sistema aprenda: sin esto todo son estimaciones."""
    db = DB(str(tmp_path / "compras.db"))
    oid = db.registrar_compra(
        {"titulo": "LEGO 42115", "tienda": "t.es", "ean": None}, 70.0)
    b = db.balance()
    assert b["total"] == 1 and b["capital_parado"] == 70.0
    db.actualizar_compra(oid, precio_venta=165.0, plataforma="wallapop")
    b = db.balance()
    assert b["vendidas"] == 1 and b["beneficio_real"] == 95.0
    assert b["capital_parado"] == 0
    c = db.compras()[0]
    assert c["estado"] == "vendida" and c["beneficio"] == 95.0


def test_una_compra_cancelada_no_cuenta_como_capital(tmp_path):
    db = DB(str(tmp_path / "canc.db"))
    oid = db.registrar_compra({"titulo": "x", "tienda": "t.es"}, 200.0)
    db.actualizar_compra(oid, cancelada=True)
    b = db.balance()
    assert b["canceladas"] == 1 and b["capital_parado"] == 0
    assert db.compras()[0]["estado"] == "cancelada"


def test_la_tasa_real_de_la_tienda_sustituye_a_la_estimacion(tmp_path):
    """Si una tienda te cancela siempre, el bot tiene que saberlo."""
    db = DB(str(tmp_path / "tasa.db"))
    assert db.tasa_cancelacion_tienda("mala.es") is None      # sin datos aún
    for i in range(6):
        oid = db.registrar_compra({"titulo": f"p{i}", "tienda": "mala.es"}, 50.0)
        db.actualizar_compra(oid, cancelada=(i < 5))          # 5 de 6 canceladas
    assert db.tasa_cancelacion_tienda("mala.es") == 0.833


def test_el_detector_usa_la_tasa_real_si_la_hay(tmp_path):
    db = DB(str(tmp_path / "tasa2.db"))
    db.guardar_reventa(ReferenciaReventa(
        ean=None, modelo="Auriculares Zeta 900", precio_venta_mediano=180.0,
        anuncios_90d=30, dias_venta_mediano=7.0, plataforma="wallapop"))
    oferta = Oferta(tienda="fiable.es", url="https://fiable.es/1",
                    titulo="Auriculares Zeta 900", precio=60.0)

    antes = Detector(db, CFG).evaluar(oferta).prob_cancelacion
    # Esta tienda nunca cancela: 6 compras, 0 canceladas
    for i in range(6):
        oid = db.registrar_compra({"titulo": f"p{i}", "tienda": "fiable.es"}, 50.0)
        db.actualizar_compra(oid, cancelada=False)
    despues = Detector(db, CFG).evaluar(oferta).prob_cancelacion
    assert despues < antes, "la tasa real de la tienda debería bajar el riesgo"


def test_la_api_de_compras_funciona_de_punta_a_punta(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from heroscalper.web import app as webapp
    ruta = str(tmp_path / "apic.db")
    db = DB(ruta)
    db.upsert_oferta(_oportunidad())
    db.cerrar()
    monkeypatch.setattr(webapp, "CONFIG", {"base_datos": {"ruta": ruta}})
    monkeypatch.setattr(webapp, "ABIERTA", True)
    c = TestClient(webapp.app)

    r = c.post("/api/compras", data={"clave": "t.es:X1", "precio": "70"})
    assert r.status_code == 200 and r.json()["ok"]
    id_op = r.json()["id"]
    assert c.get("/api/compras").json()["balance"]["total"] == 1
    r = c.post(f"/api/compras/{id_op}?precio_venta=165")
    assert r.json()["balance"]["beneficio_real"] == 95.0


def test_no_se_puede_apuntar_una_compra_de_una_oferta_inexistente(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from heroscalper.web import app as webapp
    monkeypatch.setattr(webapp, "CONFIG",
                        {"base_datos": {"ruta": str(tmp_path / "x.db")}})
    monkeypatch.setattr(webapp, "ABIERTA", True)
    r = TestClient(webapp.app).post("/api/compras",
                                    data={"clave": "no-existe", "precio": "10"})
    assert r.status_code == 404


def test_se_ordena_por_beneficio_no_por_rotacion(tmp_path):
    """En Europa cada venta cuesta el mismo rato: manda cuánto deja, no
    cuántas caben en un mes."""
    import copy
    db = DB(str(tmp_path / "orden.db"))
    det = Detector(db, CFG)
    assert CFG.get("orden_prioridad") == "margen_neto"

    mucho_lento = _oportunidad()          # 84 € en 9 días
    mucho_lento.margen_neto_eur, mucho_lento.dias_venta_estimados = 200.0, 30
    poco_rapido = _oportunidad()
    poco_rapido.margen_neto_eur, poco_rapido.dias_venta_estimados = 60.0, 2

    assert det.prioridad(mucho_lento) > det.prioridad(poco_rapido)

    # Con el criterio de rotación se invierte, y es configurable
    cfg = copy.deepcopy(CFG); cfg["orden_prioridad"] = "margen_por_dia"
    det2 = Detector(db, cfg)
    assert det2.prioridad(poco_rapido) > det2.prioridad(mucho_lento)


def test_el_lote_llega_ordenado_por_beneficio(detector):
    ops = detector.evaluar_lote([
        _oferta(tienda="a.es", url="https://a.es/1", precio=19.99),
        _oferta(tienda="b.es", url="https://b.es/1", precio=55.0),
    ])
    beneficios = [o.margen_neto_eur for o in ops]
    assert beneficios == sorted(beneficios, reverse=True)
