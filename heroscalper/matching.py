"""Emparejar el mismo producto entre tiendas.

El EAN es la clave ideal, pero comprobado contra tiendas reales: el
/products.json publico de Shopify NO devuelve `barcode` en muchas tiendas.
Sin plan B, todo el cruce cross-tienda se cae en silencio justo en la
plataforma que mejor datos daba.

Plan B: clave normalizada marca + modelo extraida del titulo.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Ruido tipico de los titulos de ecommerce español
RUIDO = re.compile(
    r"\b(nuevo|nueva|oferta|rebajado|env[ií]o gratis|original|oficial|"
    r"pack|unidad(es)?|ud\.?|uds\.?|color|talla|cm|mm|gr|kg|ml|"
    r"para|con|de|del|la|el|los|las|y|en|a)\b",
    re.IGNORECASE,
)
NO_ALFANUM = re.compile(r"[^a-z0-9]+")

# Un codigo de modelo (42115, WH-1000XM5, GBH 2-28) identifica el producto
# mucho mejor que el titulo entero. Se coge el que aparece MAS A LA IZQUIERDA:
# las marcas ponen el modelo al principio y las referencias sueltas (FKP 37,
# medidas, colores) van despues.
RE_MODELO_ALFA = re.compile(
    r"\b[a-z]{1,6}(?:[-\s]?\d{1,5}){1,3}(?:[-\s]?[a-z]{1,3}\d{0,3})?\b",
    re.IGNORECASE,
)
RE_MODELO_NUM = re.compile(r"\b\d{4,6}\b")

# Palabras que preceden a un numero que NO es un modelo
PALABRAS_MEDIDA = re.compile(
    r"\b(talla|cm|mm|ml|gr|kg|w|v|mah|gb|tb|pulgadas|uds?|pack|x)\b", re.IGNORECASE
)


def _es_año(token: str) -> bool:
    return token.isdigit() and len(token) == 4 and 1900 <= int(token) <= 2100


def sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def normaliza(texto: str) -> str:
    texto = sin_acentos(str(texto or "").lower())
    texto = RUIDO.sub(" ", texto)
    texto = NO_ALFANUM.sub(" ", texto)
    return " ".join(texto.split())


RE_AÑO = re.compile(r"\b(19|20)\d{2}\b")


def codigo_modelo(titulo: str, marca: Optional[str] = None) -> Optional[str]:
    """Extrae el codigo de modelo del titulo, si lo hay.

    Se queda con el candidato mas a la izquierda que no sea un año ni una
    medida: 'LEGO Technic 42115 Lamborghini FKP 37' -> 42115, no fkp37.
    """
    limpio = sin_acentos(str(titulo or "").lower())
    # La marca delante del numero lo contamina ("lego 42115" -> "lego42115").
    if marca:
        limpio = re.sub(rf"\b{re.escape(sin_acentos(marca.lower()))}\b", " ", limpio)
    limpio = RE_AÑO.sub(" ", limpio)     # 2024 no es un modelo
    candidatos = []
    for regex in (RE_MODELO_ALFA, RE_MODELO_NUM):
        for m in regex.finditer(limpio):
            token = m.group(0).strip()
            if _es_año(NO_ALFANUM.sub("", token)):
                continue
            if PALABRAS_MEDIDA.fullmatch(token.split()[0] if token.split() else ""):
                continue
            # Tiene que llevar al menos un digito y no ser solo una medida
            if not any(c.isdigit() for c in token):
                continue
            if len(NO_ALFANUM.sub("", token)) < 3:
                continue
            candidatos.append((m.start(), token))
    if not candidatos:
        return None
    candidatos.sort(key=lambda c: (c[0], -len(c[1])))
    return NO_ALFANUM.sub("", candidatos[0][1]) or None


def clave_producto(marca: Optional[str], titulo: str, ean: Optional[str] = None) -> str:
    """Clave de emparejamiento, en orden de fiabilidad decreciente."""
    if ean:
        return f"ean:{ean}"
    m = normaliza(marca or "")
    cod = codigo_modelo(titulo, marca)
    if m and cod:
        return f"mod:{m}:{cod}"
    if cod:
        return f"mod:{cod}"
    # Ultimo recurso: marca + las 5 primeras palabras significativas
    palabras = normaliza(titulo).split()[:5]
    return f"txt:{m}:{'_'.join(palabras)}" if palabras else f"txt:{m}"


def fiabilidad(clave: str) -> float:
    """Cuanta confianza merece un cruce hecho con esta clave."""
    if clave.startswith("ean:"):
        return 1.0
    if clave.startswith("mod:") and clave.count(":") == 2:
        return 0.85     # marca + codigo de modelo
    if clave.startswith("mod:"):
        return 0.6      # solo codigo de modelo
    return 0.35         # texto: no fiable para disparar una alerta de error
