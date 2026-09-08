"""Detecta que plataforma usa cada dominio y le asigna el adapter correcto.

Es el multiplicador del sistema: con 3 adapters de plataforma se cubre el 98%
del ecommerce español (41.006 WooCommerce + 9.017 PrestaShop + 3.879 Shopify),
sin escribir una linea de codigo por tienda.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .adapters.base import ClienteHTTP

SEÑALES = {
    "shopify": [r"cdn\.shopify\.com", r"Shopify\.theme", r"shopify-features"],
    "woocommerce": [r"/wp-content/plugins/woocommerce", r"woocommerce-page",
                    r"wc-add-to-cart", r"generator\" content=\"WooCommerce"],
    "prestashop": [r"/modules/ps_", r"prestashop", r"var prestashop ="],
    "magento": [r"/static/version\d+/frontend/", r"Magento_", r"mage/cookies"],
}
SEÑALES_COMPILADAS = {k: [re.compile(p, re.I) for p in v] for k, v in SEÑALES.items()}


@dataclass
class Huella:
    dominio: str
    plataforma: Optional[str]
    tiene_jsonld: bool
    tiene_ean: bool
    alcanzable: bool

    @property
    def vigilable(self) -> bool:
        """Sin plataforma reconocible ni JSON-LD no merece el mantenimiento."""
        return self.alcanzable and (self.plataforma is not None or self.tiene_jsonld)


async def huella(cliente: ClienteHTTP, dominio: str) -> Huella:
    dominio = dominio.replace("https://", "").replace("http://", "").strip("/")
    resp = await cliente.get(f"https://{dominio}/")
    if resp is None or resp.status_code >= 400:
        return Huella(dominio, None, False, False, alcanzable=False)

    html = resp.text
    plataforma = None
    for nombre, patrones in SEÑALES_COMPILADAS.items():
        if any(p.search(html) for p in patrones):
            plataforma = nombre
            break

    # Cabeceras y cookies delatan tambien
    if plataforma is None:
        cookies = " ".join(resp.cookies.keys()).lower()
        if "shopify" in cookies or "_shopify_y" in cookies:
            plataforma = "shopify"
        elif "woocommerce" in cookies or "wp_woocommerce" in cookies:
            plataforma = "woocommerce"
        elif "prestashop" in cookies:
            plataforma = "prestashop"

    tiene_jsonld = "application/ld+json" in html
    tiene_ean = bool(re.search(r'"gtin1?[348]?"\s*:', html))

    return Huella(dominio, plataforma, tiene_jsonld, tiene_ean, alcanzable=True)


def adapter_para(plataforma: Optional[str], adapters: dict):
    """Shopify y Woo tienen API propia; el resto cae al generico JSON-LD."""
    if plataforma == "keepa":
        return adapters.get("keepa", adapters.get("jsonld"))
    if plataforma == "shopify":
        return adapters.get("shopify")
    if plataforma == "woocommerce":
        return adapters.get("woocommerce")
    return adapters.get("jsonld")
