"""
Mappatura centralizzata paese -> identificatori piattaforma. Punto unico
da cui derivare marketplace ID eBay, dominio Vinted, locale Wallapop --
invece di avere ogni connector con la propria idea scollegata di "paese".

NOTA IMPORTANTE: questo modulo NON e' ancora collegato ai connector reali
(vinted.py ha ancora il dominio .it scritto direttamente, wallapop.py non
usa un locale configurabile). E' una predisposizione: il collegamento va
fatto quando rivedremo singolarmente quei file, per non introdurre modifiche
non concordate a moduli gia' "chiusi" in questa sessione di audit.

NOTA DI ONESTA': la presenza/assenza di Vinted in alcuni mercati (es. USA)
non e' stata verificata con una fonte affidabile in questo ambiente senza
rete -- il valore None per vinted_base_url in mercati incerti e' un segnaposto
da confermare, non un dato certo.
"""

from config.settings import COUNTRY


class UnsupportedMarketplaceError(Exception):
    """Sollevata quando il paese configurato non ha una mappatura nota."""
    pass


_SUPPORTED_MARKETPLACES = {
    "IT": {
        "ebay_marketplace_id": "EBAY_IT",
        "vinted_base_url": "https://www.vinted.it",
        "wallapop_locale": "it-IT",
    },
    "GB": {
        "ebay_marketplace_id": "EBAY_GB",
        "vinted_base_url": "https://www.vinted.co.uk",
        "wallapop_locale": "en-GB",
    },
    "DE": {
        "ebay_marketplace_id": "EBAY_DE",
        "vinted_base_url": "https://www.vinted.de",
        "wallapop_locale": "de-DE",
    },
    "FR": {
        "ebay_marketplace_id": "EBAY_FR",
        "vinted_base_url": "https://www.vinted.fr",
        "wallapop_locale": "fr-FR",
    },
    "ES": {
        "ebay_marketplace_id": "EBAY_ES",
        "vinted_base_url": "https://www.vinted.es",
        "wallapop_locale": "es-ES",  # Wallapop nasce in Spagna, locale piu' probabile qui
    },
    "US": {
        "ebay_marketplace_id": "EBAY_US",
        "vinted_base_url": None,  # da verificare: presenza Vinted USA non confermata qui
        "wallapop_locale": "en-US",
    },
}


def get_marketplace_config(country: str | None = None) -> dict:
    """
    Ritorna la mappatura completa per il paese richiesto (default: COUNTRY
    da settings.py). Solleva UnsupportedMarketplaceError con un messaggio
    chiaro se il paese non e' configurato, invece di fallire silenziosamente
    con un KeyError generico da qualche parte più a valle.
    """
    country = country or COUNTRY
    if country not in _SUPPORTED_MARKETPLACES:
        raise UnsupportedMarketplaceError(
            f"Paese '{country}' non configurato in config/marketplaces.py. "
            f"Paesi supportati: {', '.join(sorted(_SUPPORTED_MARKETPLACES))}. "
            f"Aggiungi una voce a _SUPPORTED_MARKETPLACES se vuoi espandere il bot su questo mercato."
        )
    return _SUPPORTED_MARKETPLACES[country]


def validate_marketplace_support() -> None:
    """Da chiamare esplicitamente all'avvio: fallisce subito se COUNTRY non e' supportato."""
    get_marketplace_config()
