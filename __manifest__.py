{
    "name": "BOSON - GRE: sincronización con el VPS",
    "version": "18.0.1.0.1",
    "category": "Inventory/EDI",
    "summary": "Envía al VPS las guías de remisión y los despachos de la sede (en vivo y carga histórica)",
    "author": "BOSON INNOVATION",
    "maintainer": "BOSON INNOVATION",
    "website": "https://bosoninnovation.com",
    "license": "LGPL-3",
    "depends": [
        "boson_gre_sunat",
        "boson_sync_base",
    ],
    "data": [
        "data/ir_cron.xml",
        "views/boson_gre_views.xml",
    ],
    "installable": True,
    "application": False,
}
