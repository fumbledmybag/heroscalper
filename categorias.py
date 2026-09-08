"""Clasificación por categoría de producto.

La web se organiza por CATEGORÍAS, como cualquier tienda, no por el tipo
técnico de anomalía. Que algo sea un "error de precio" o una "liquidación" es
un detalle interno: lo que buscas cuando abres el feed es «a ver qué hay de
informática», no «a ver qué errores de tipo 2 hay».

El tipo sigue existiendo, pero como distintivo de la tarjeta, no como filtro
principal.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Orden importante: gana la primera que casa, así que las específicas van
# antes que las genéricas.
CATEGORIAS: list[tuple[str, str, str]] = [
    ("informatica", "Informática", r"""
        portatil|laptop|ordenador|pc gaming|sobremesa|monitor|teclado|raton\b|mouse|
        tarjeta grafica|grafica\b|rtx|radeon|geforce|procesador|ryzen|core i[3579]|
        placa base|ssd|nvme|disco duro|memoria ram|ddr[45]|fuente de alimentacion|
        router|impresora|escaner|webcam|nas\b|switch de red|tablet grafica"""),

    ("moviles", "Móviles y tablets", r"""
        movil|smartphone|iphone|galaxy s\d|galaxy a\d|pixel \d|xiaomi|redmi|
        poco x|oneplus|tablet|ipad|smartwatch|apple watch|galaxy watch|
        funda de movil|cargador|power ?bank|airtag"""),

    ("audio_tv", "Audio, TV e imagen", r"""
        auricular|cascos|airpods|altavoz|barra de sonido|sonos|bose|marshall|jbl|
        televis|smart ?tv|oled|qled|proyector|home cinema|tocadiscos|
        amplificador|equipo de musica|soundbar"""),

    ("gaming", "Consolas y videojuegos", r"""
        playstation|ps[45]\b|xbox|nintendo|switch\b|steam deck|consola|
        videojuego|mando de|dualsense|joy-?con|silla gaming|
        \bjuego (de|para) (ps|xbox|switch|pc)"""),

    ("herramientas", "Herramientas y bricolaje", r"""
        taladro|atornillador|amoladora|sierra|lijadora|martillo perforador|
        makita|bosch professional|milwaukee|dewalt|festool|einhell|
        caja de herramientas|juego de llaves|soldador|compresor|
        nivel laser|multiherramienta|desbrozadora"""),

    ("fotografia", "Fotografía y drones", r"""
        camara|reflex|mirrorless|objetivo \d|teleobjetivo|gopro|dji|dron\b|
        tripode|gimbal|flash de estudio|sony alpha|canon eos|nikon z"""),

    ("hogar", "Hogar y electrodomésticos", r"""
        aspirador|robot aspirador|roomba|dyson|cafetera|nespresso|freidora|
        airfryer|batidora|thermomix|microondas|licuadora|plancha|
        humidificador|purificador|ventilador|calefactor|aire acondicionado|
        lavadora|lavavajillas|frigorifico|nevera|horno|vitroceramica|
        colchon|sofa|estanteria|lampara|sabanas|menaje|sarten|olla"""),

    ("deporte", "Deporte y aire libre", r"""
        bicicleta|patinete|mtb\b|zapatillas running|garmin|polar\b|
        mancuerna|pesas|cinta de correr|eliptica|tienda de campa|
        saco de dormir|mochila de montan|esqui|snowboard|surf|padel|
        raqueta|balon|casco de"""),

    ("moda", "Moda y calzado", r"""
        camiseta|sudadera|pantalon|vaquero|jeans|chaqueta|abrigo|plumifero|
        zapatilla|zapato|bota\b|sandalia|vestido|falda|jersey|polo\b|
        the north face|patagonia|arc.?teryx|nike|adidas|levi.?s|
        reloj\b|gafas de sol|bolso|mochila|cinturon|cartera"""),

    ("juguetes", "Juguetes y LEGO", r"""
        lego|playmobil|puzzle|juguete|peluche|nerf|barbie|funko|
        maqueta|juego de mesa|monopoly|catan|patinete infantil"""),

    ("bebe", "Bebé y puericultura", r"""
        carrito de bebe|cochecito|silla de coche|maxi.?cosi|cybex|bugaboo|
        stokke|chicco|trona|cuna\b|hamaca de bebe|mochila portabebe|
        esterilizador|sacaleches|panal"""),

    ("belleza", "Belleza y salud", r"""
        perfume|colonia|eau de (toilette|parfum)|crema facial|serum|
        maquillaje|secador de pelo|plancha de pelo|dyson airwrap|
        afeitadora|depiladora|cepillo de dientes electrico|oral-?b|
        tensiometro|bascula"""),
]

COMPILADAS = [
    (clave, nombre, re.compile(patron, re.IGNORECASE | re.VERBOSE))
    for clave, nombre, patron in CATEGORIAS
]

NOMBRES = {clave: nombre for clave, nombre, _ in CATEGORIAS}
NOMBRES["otros"] = "Otros"


def _limpiar(texto: str) -> str:
    t = unicodedata.normalize("NFD", str(texto or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def clasificar(titulo: str, categoria_tienda: Optional[str] = None,
               marca: Optional[str] = None) -> str:
    """Devuelve la clave de categoría. Nunca falla: por defecto 'otros'."""
    texto = _limpiar(f"{titulo} {categoria_tienda or ''} {marca or ''}")
    for clave, _, patron in COMPILADAS:
        if patron.search(texto):
            return clave
    return "otros"


def nombre(clave: str) -> str:
    return NOMBRES.get(clave, "Otros")
