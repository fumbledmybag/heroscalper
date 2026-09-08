from .base import Adapter, ClienteHTTP
from .jsonld import JsonLdAdapter
from .shopify import ShopifyAdapter
from .woocommerce import WooCommerceAdapter

__all__ = ["Adapter", "ClienteHTTP", "ShopifyAdapter",
           "WooCommerceAdapter", "JsonLdAdapter"]
