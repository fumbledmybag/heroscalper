#!/usr/bin/env python3
"""HeroScalper — linea de comandos.

  python run.py descubrir tiendas.txt     # clasifica dominios por plataforma
  python run.py barrer midominio.es       # un barrido completo de una tienda
  python run.py outlet midominio.es       # solo las paginas de liquidacion
  python run.py ciclo tiendas.txt         # un ciclo completo con alertas
  python run.py bucle tiendas.txt         # en bucle, para el VPS
  python run.py reventa data/reventa.csv  # carga precios de 2a mano
  python run.py demo                      # prueba en seco, sin tocar internet
  python run.py web                       # levanta la web privada
  python run.py demo-web                  # llena la web con ofertas de ejemplo
  python run.py todo tiendas.txt          # bot + web en un solo proceso (Render)
  python run.py importar-tiendas fichero  # añade cientos de tiendas de golpe
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from heroscalper.db import DB
from heroscalper.models import Oferta, ReferenciaReventa
from heroscalper.notifier import formatear, formatear_evento
from heroscalper.runner import HeroScalper, cargar_config


def leer_dominios(ruta: str) -> list[str]:
    p = Path(ruta)
    if not p.exists():
        return [ruta]           # se ha pasado un dominio suelto
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


async def cmd_descubrir(hs: HeroScalper, args: list[str]) -> None:
    dominios = leer_dominios(args[0])
    resultados = await hs.descubrir(dominios)
    vigilables = [r for r in resultados if r["vigilable"]]
    print(f"\n{'DOMINIO':<38} {'PLATAFORMA':<14} {'JSON-LD':<8} {'EAN':<5} VIGILABLE")
    print("-" * 82)
    for r in resultados:
        print(f"{r['dominio'][:37]:<38} {str(r['plataforma'] or '-'):<14} "
              f"{'si' if r['jsonld'] else 'no':<8} {'si' if r['ean'] else 'no':<5} "
              f"{'SI' if r['vigilable'] else 'no'}")
    print(f"\n{len(vigilables)}/{len(resultados)} tiendas vigilables.")


async def cmd_barrer(hs: HeroScalper, args: list[str], solo_outlet=False) -> None:
    dominio = args[0]
    h = await hs.descubrir([dominio])
    plataforma = h[0]["plataforma"] if h else None
    print(f"Plataforma detectada: {plataforma or 'generico (JSON-LD)'}")
    ofertas, _ = await hs.barrer_tienda(dominio, plataforma, solo_outlet=solo_outlet)
    print(f"{len(ofertas)} lecturas de precio guardadas.")
    con_ean = sum(1 for o in ofertas if o.ean)
    con_promo = sum(1 for o in ofertas if o.precio_anterior or o.promo_texto)
    print(f"  con EAN: {con_ean}   con promo/descuento declarado: {con_promo}")
    for o in ofertas[:10]:
        dto = o.descuento_declarado
        print(f"  - {o.titulo[:60]:<62} {o.precio:>8.2f} €"
              + (f"  (-{dto*100:.0f}%)" if dto else ""))


async def cmd_ciclo(hs: HeroScalper, args: list[str]) -> None:
    dominios = leer_dominios(args[0])
    resumen = await hs.ciclo(dominios)
    print(f"\nResumen del ciclo: {resumen}")


async def cmd_bucle(hs: HeroScalper, args: list[str]) -> None:
    await hs.bucle(leer_dominios(args[0]))


def cmd_reventa(cfg: dict, args: list[str]) -> None:
    """CSV: ean,modelo,precio_venta_mediano,anuncios_90d,dias_venta,precio_suelo"""
    db = DB(cfg["base_datos"]["ruta"])
    n = 0
    with open(args[0], newline="", encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            db.guardar_reventa(ReferenciaReventa(
                ean=fila["ean"].strip() or None,
                modelo=fila["modelo"],
                precio_venta_mediano=float(fila["precio_venta_mediano"]),
                anuncios_90d=int(fila["anuncios_90d"]),
                dias_venta_mediano=float(fila["dias_venta"]),
                precio_suelo=float(fila["precio_suelo"]) if fila.get("precio_suelo") else None,
                plataforma=fila.get("plataforma", "wallapop"),
            ))
            n += 1
    print(f"{n} referencias de reventa cargadas.")
    db.cerrar()


def cmd_demo(cfg: dict) -> None:
    """Prueba en seco del detector: sin red, con datos de ejemplo."""
    from heroscalper.detector import Detector

    db = DB("data/demo.db")
    # Mercado: el mismo EAN en 4 tiendas a precio normal
    ean = "5702016616989"
    for i, (tienda, precio) in enumerate([
        ("tienda-a.es", 199.95), ("tienda-b.es", 204.00),
        ("tienda-c.es", 197.50), ("tienda-d.es", 209.99),
    ]):
        db.guardar_lecturas([Oferta(tienda=tienda, url=f"https://{tienda}/p/{i}",
                                    titulo="LEGO Technic 42115", precio=precio,
                                    ean=ean, marca="LEGO", peso_kg=2.0)])
    db.guardar_reventa(ReferenciaReventa(
        ean=ean, modelo="LEGO Technic 42115", precio_venta_mediano=165.0,
        anuncios_90d=64, dias_venta_mediano=9.0, precio_suelo=None))

    # Un producto con variantes, una de ellas mal tarifada
    variantes = [
        Oferta(tienda="ropa.es", url=f"https://ropa.es/chaqueta?v={i}",
               titulo=f"Chaqueta técnica Norte 3000 - {t}", precio=p,
               marca="Norte", grupo_id="ropa.es:77", peso_kg=0.9)
        for i, (t, p) in enumerate([("S", 189.0), ("M", 19.0),
                                    ("L", 189.0), ("XL", 195.0)])
    ]

    detector = Detector(db, cfg)
    casos = [
        # El caso que pediste: algo de 200 € que aparece a 70 €
        Oferta(tienda="tienda-rota.es", url="https://tienda-rota.es/p/1",
               titulo="LEGO Technic 42115 Lamborghini Sian", precio=70.0,
               ean=ean, marca="LEGO", peso_kg=2.0),
        # Rebaja normal: -25%. No es un chollo.
        Oferta(tienda="rebajas.es", url="https://rebajas.es/p/2",
               titulo="LEGO Technic 42115 Lamborghini Sian", precio=150.0,
               ean=ean, marca="LEGO", peso_kg=2.0),
        # Precio tachado inflado por la propia tienda, sin nada que lo respalde
        Oferta(tienda="infla.es", url="https://infla.es/p/3",
               titulo="Reloj deportivo Kappa 700", precio=29.0,
               precio_anterior=149.0, marca="Kappa"),
        # Cosas grandes: en modo experimental YA NO se descartan
        Oferta(tienda="fitness.es", url="https://fitness.es/p/4",
               titulo="Bicicleta elíptica BH Fitness i.Concept", precio=199.0,
               ean="8431624000001", marca="BH", peso_kg=45.0),
    ] + variantes

    print("=" * 70)
    print(f"DEMO — modo: {cfg.get('modo')} · descuento mínimo: "
          f"{cfg['filtros']['descuento_min_vs_mercado']*100:.0f}%")
    print("=" * 70)

    ops = detector.evaluar_lote(casos)
    claves = {o.oferta.url for o in ops}
    for oferta in casos:
        if oferta.url in claves:
            continue
        ok, motivo = detector.pasa_filtros_basicos(oferta)
        razon = motivo if not ok else "no llega al descuento mínimo o sin referencia fiable"
        print(f"\n❌ {oferta.titulo[:52]:<54} {oferta.precio:>8.2f} €\n   → {razon}")

    for op in ops:
        print("\n" + formatear(op).replace("<b>", "").replace("</b>", "")
              .replace("<i>", "").replace("</i>", ""))
        print("-" * 70)

    print(f"\n{len(ops)} oportunidades de {len(casos)} ofertas evaluadas "
          f"(ordenadas por margen/día).")
    db.cerrar()


async def cmd_todo(cfg: dict, args: list[str]) -> None:
    """El bot y la web en UN SOLO proceso.

    Hace falta para Render y para cualquier PaaS parecido: allí un disco
    persistente solo se puede montar en un servicio, y bot y web comparten
    la misma base de datos SQLite. Como dos servicios no pueden tocar el
    mismo disco, van juntos en el mismo proceso.

    En un VPS da igual: puedes usar esto o los dos contenedores por separado.
    """
    import uvicorn

    # Render (y casi todo PaaS) te dice en qué puerto escuchar.
    puerto = int(os.environ.get("PORT") or cfg.get("web", {}).get("puerto", 8080))
    dominios = leer_dominios(args[0] if args else "tiendas.txt")

    hs = HeroScalper(cfg)
    servidor = uvicorn.Server(uvicorn.Config(
        "heroscalper.web.app:app", host="0.0.0.0", port=puerto,
        log_level="warning"))

    print(f"→ web en el puerto {puerto} · vigilando {len(dominios)} tiendas")
    try:
        # Si el bucle del bot se cae, la web tiene que seguir en pie: por eso
        # se envuelve y se reintenta en vez de tumbar el proceso entero.
        await asyncio.gather(servidor.serve(), _bucle_resistente(hs, dominios))
    finally:
        await hs.cerrar()


async def _bucle_resistente(hs: HeroScalper, dominios: list[str]) -> None:
    espera = 60
    while True:
        try:
            await hs.bucle(dominios)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[bot] el ciclo se ha caído: {e}. Reintento en {espera}s")
            await asyncio.sleep(espera)
            espera = min(espera * 2, 900)


def cmd_importar_tiendas(cfg: dict, args: list[str]) -> None:
    """Añade dominios a tiendas.txt desde un fichero o CSV.

    Sirve para volcar de golpe el listado de anunciantes que te descargas de
    una red de afiliación. Acepta dominios sueltos, URLs completas o un CSV
    con una columna de webs: se queda con el dominio y descarta duplicados.
    """
    import re
    origen = Path(args[0]) if args else None
    if not origen or not origen.exists():
        print("Uso: python run.py importar-tiendas <fichero.txt|csv>")
        return

    texto = origen.read_text(encoding="utf-8", errors="ignore")
    patron = re.compile(
        r"\b(?:https?://)?(?:www\.)?"
        r"([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)",
        re.IGNORECASE)
    # Dominios que no son tiendas y siempre se cuelan en estos listados
    basura = re.compile(
        r"(google|facebook|instagram|twitter|x\.com|youtube|linkedin|tiktok|"
        r"gmail|hotmail|outlook|awin|tradedoubler|admitad|daisycon|effiliate|"
        r"cloudflare|wordpress|shopify\.com|gstatic|w3\.org|schema\.org)",
        re.IGNORECASE)
    validos = re.compile(r"\.(es|com|net|eu|org|shop|store|cat|gal|pt|fr|it|de)$",
                         re.IGNORECASE)

    encontrados = []
    for m in patron.finditer(texto):
        d = m.group(1).lower().strip(".")
        if basura.search(d) or not validos.search(d) or len(d) < 5:
            continue
        encontrados.append(d)

    destino = Path("tiendas.txt")
    actual = destino.read_text(encoding="utf-8") if destino.exists() else ""
    ya = {l.strip().lstrip("# ").lower()
          for l in actual.splitlines() if l.strip()}

    nuevos = []
    for d in dict.fromkeys(encontrados):        # dedupe conservando el orden
        if d not in ya:
            nuevos.append(d)
            ya.add(d)

    if not nuevos:
        print(f"Nada nuevo: los {len(set(encontrados))} dominios ya estaban.")
        return

    with destino.open("a", encoding="utf-8") as f:
        f.write(f"\n\n# --- Importadas de {origen.name} "
                f"({datetime.utcnow():%Y-%m-%d}) ---\n")
        f.write("\n".join(nuevos) + "\n")

    print(f"{len(nuevos)} tiendas nuevas añadidas a tiendas.txt "
          f"(de {len(set(encontrados))} dominios encontrados).")
    print("Ahora comprueba cuáles sirven:")
    print("   python run.py descubrir tiendas.txt")


def cmd_web(cfg: dict) -> None:
    import uvicorn
    puerto = int(cfg.get("web", {}).get("puerto", 8080))
    if not os.environ.get("WEB_PASSWORD"):
        print("⚠  Sin WEB_PASSWORD la web queda ABIERTA. Solo para uso local.")
    print(f"→ http://localhost:{puerto}")
    uvicorn.run("heroscalper.web.app:app", host="0.0.0.0", port=puerto, log_level="warning")


def cmd_demo_web(cfg: dict) -> None:
    """Mete ofertas de ejemplo para poder ver la web antes de tener datos reales."""
    from heroscalper.models import Oportunidad, TipoOportunidad

    db = DB(cfg["base_datos"]["ruta"])
    from heroscalper import categorias as cats
    ejemplos = [
        ("AirPods 4 de regalo con tu compra de 29 €",
         "Apple", "electro-garcia.es", 0.0, 149.0, 90.0, TipoOportunidad.GRATIS, 5),
        ("Sony WH-1000XM5 Auriculares Inalámbricos con Cancelación de Ruido",
         "Sony", "audiotienda.es", 69.0, 289.0, 205.0, TipoOportunidad.ERROR_PRECIO, 6),
        ("LEGO Technic 42115 Lamborghini Sián FKP 37",
         "LEGO", "jugueteria-nova.es", 70.0, 201.97, 165.0,
         TipoOportunidad.ERROR_PRECIO_EXTREMO, 9),
        ("Makita DHP484Z Taladro Percutor 18V LXT sin batería",
         "Makita", "herramientas-lopez.es", 48.0, 129.0, 92.0,
         TipoOportunidad.LIQUIDACION, 7),
        ("Chaqueta técnica Norte 3000 impermeable - Talla M",
         "Norte", "ropa-outdoor.es", 19.0, 189.0, 85.0,
         TipoOportunidad.ERROR_PRECIO_EXTREMO, 12),
        ("Dyson V15 Detect Absolute aspirador escoba",
         "Dyson", "electro-garcia.es", 199.0, 599.0, 340.0,
         TipoOportunidad.PROMO_CAMPANA, 10),
        ("Bosch Professional GBH 2-28 F Martillo perforador",
         "Bosch", "ferreteria-sur.es", 74.0, 249.0, 168.0,
         TipoOportunidad.PROMO_MULTIUNIDAD, 11),
    ]
    for i, (titulo, marca, tienda, precio, ref, reventa, tipo, dias) in enumerate(ejemplos):
        oferta = Oferta(tienda=tienda, url=f"https://{tienda}/producto/{i}",
                        titulo=titulo, precio=precio, marca=marca, peso_kg=2.0,
                        sku=f"DEMO{i}")
        neto = reventa - precio - 4.95 - 3.25 - 2.5
        prob = {TipoOportunidad.ERROR_PRECIO_EXTREMO: 0.65,
                TipoOportunidad.ERROR_PRECIO: 0.35,
                TipoOportunidad.GRATIS: 0.15}.get(tipo, 0.04)
        # La mitad con reventa MEDIDA y la otra mitad estimada, para que se
        # vea la diferencia entre un beneficio real y una suposición.
        medida = i % 2 == 0
        ahorro = ref - precio
        dto = 1 - (precio / ref) if ref else 1.0
        nivel = (4 if tipo in (TipoOportunidad.GRATIS,
                               TipoOportunidad.ERROR_PRECIO_EXTREMO)
                      or dto >= 0.80 or ahorro >= 400
                 else 3 if dto >= 0.70 or ahorro >= 200
                 else 2 if dto >= 0.60 or ahorro >= 100
                 else 1)
        op = Oportunidad(
            oferta=oferta, tipo=tipo, precio_efectivo=precio,
            unidades_recomendadas=3 if tipo == TipoOportunidad.PROMO_MULTIUNIDAD else 1,
            referencia_mercado=ref, referencia_reventa=reventa,
            margen_bruto_eur=reventa - precio, margen_neto_eur=neto,
            margen_esperado_eur=neto * (1 - prob),
            margen_pct=neto / precio if precio else 1.0, dias_venta_estimados=dias,
            prob_cancelacion=prob, reventa_medida=medida,

            categoria=cats.clasificar(titulo, None, marca),
            motivo=f"{precio:.2f} € frente a {ref:.2f} € · referencia: otras tiendas")
        op.nivel = nivel
        db.upsert_oferta(op)
        # Historial de ejemplo: precio estable y caída al final
        import json as _json, random as _rnd
        _rnd.seed(i)
        serie = [round(ref * _rnd.uniform(0.93, 1.05), 2) for _ in range(24)]
        serie += [precio]
        with db.cursor() as cur:
            cur.execute("""UPDATE ofertas SET media_90d=?, media_30d=?,
                             minimo_historico=?, n_lecturas=?, serie=?
                           WHERE clave=?""",
                        (round(sum(serie[:-1]) / len(serie[:-1]), 2),
                         round(sum(serie[-8:-1]) / 7, 2), min(serie),
                         len(serie), _json.dumps(serie), oferta.clave()))
    # Una tienda "rota" para que se vea el aviso de salud en la web
    db.registrar_tienda("tienda-que-cambio-su-web.es", "shopify")
    for _ in range(3):
        db.marcar_fallo("tienda-que-cambio-su-web.es")
    for dom in ("audiotienda.es", "jugueteria-nova.es", "ferreteria-sur.es"):
        db.registrar_tienda(dom, "woocommerce")
        db.marcar_exito(dom)

    print(f"{len(ejemplos)} ofertas de ejemplo cargadas. Arranca la web:")
    print("   python run.py web")
    db.cerrar()


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    cfg = cargar_config()

    if cmd == "todo":
        return await cmd_todo(cfg, args)

    hs = HeroScalper(cfg)
    try:
        if cmd == "descubrir":
            await cmd_descubrir(hs, args)
        elif cmd == "barrer":
            await cmd_barrer(hs, args)
        elif cmd == "outlet":
            await cmd_barrer(hs, args, solo_outlet=True)
        elif cmd == "ciclo":
            await cmd_ciclo(hs, args)
        elif cmd == "bucle":
            await cmd_bucle(hs, args)
        else:
            print(__doc__)
    finally:
        await hs.cerrar()


# Los comandos sincronos no pueden ir dentro de asyncio.run(): uvicorn abre
# su propio bucle de eventos y chocaria con el nuestro.
SINCRONOS = {
    "reventa":  lambda cfg, args: cmd_reventa(cfg, args),
    "demo":     lambda cfg, args: cmd_demo(cfg),
    "web":      lambda cfg, args: cmd_web(cfg),
    "demo-web": lambda cfg, args: cmd_demo_web(cfg),
    "importar-tiendas": lambda cfg, args: cmd_importar_tiendas(cfg, args),
}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in SINCRONOS:
        SINCRONOS[sys.argv[1]](cargar_config(), sys.argv[2:])
    else:
        asyncio.run(main())
