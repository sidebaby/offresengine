"""
Connector Wallapop REALE: endpoint pubblico interno (api.wallapop.com),
non autenticato, usato dal sito stesso per la ricerca. Nessun OAuth richiesto.

NOTA DI ONESTA' IMPORTANTE: questo e' il connector meno verificato del
progetto. Non e' stato possibile testarlo con una vera risposta HTTP in
nessuna fase di questo sviluppo (ambiente senza rete). In particolare:
- I nomi esatti dei parametri di geolocalizzazione (latitude/longitude
  sotto) sono una STIMA basata su convenzioni comuni per API di questo tipo,
  NON confermata contro la documentazione reale (che non esiste, essendo un
  endpoint interno non ufficiale).
- La struttura del campo 'condition' (stringa semplice vs oggetto annidato)
  e' gestita in modo difensivo per ENTRAMBI i casi, proprio perche' non e'
  verificabile quale sia quella vera finche' non parte il primo test reale.
Al primo lancio reale, controllare i log per warning su formati inattesi:
sono il modo piu' rapido per scoprire la struttura JSON effettiva e
correggere questo file di conseguenza.
"""

import logging
from datetime import datetime
from typing import Optional

import httpx

from database.models import Item
from connectors.normalize import normalize_condition
from connectors.errors import RateLimitError, BlockedError, TimeoutErrorConnector, UnexpectedResponseError
from config.settings import WALLAPOP_LATITUDE, WALLAPOP_LONGITUDE

logger = logging.getLogger("connector.wallapop")

SEARCH_URL = "https://api.wallapop.com/api/v3/search"

# Wallapop non ha un limite massimo documentato pubblicamente (endpoint non
# ufficiale) -- questo e' un tetto PRUDENZIALE scelto per non rischiare di
# esagerare con la dimensione della risposta, non un valore confermato da eBay.
_CONSERVATIVE_MAX_LIMIT = 100

_condition_format_warned = False  # per loggare il formato inatteso una sola volta, non ad ogni item


def _resolve_locale() -> str:
    """
    Deriva il locale Wallapop da config/marketplaces.py in base al paese
    configurato -- import locale per evitare rischi di dipendenza circolare,
    stesso pattern gia' usato in connectors/ebay.py.
    """
    try:
        from config.marketplaces import get_marketplace_config
        return get_marketplace_config()["wallapop_locale"]
    except Exception as e:
        logger.warning(f"Impossibile derivare il locale Wallapop da config/marketplaces.py: {e}, uso 'it-IT'")
        return "it-IT"


def _extract_condition_string(raw_condition) -> str:
    """
    Gestisce raw_condition sia come stringa semplice sia come oggetto
    annidato (es. {'id': 1, 'title': 'Buono stato'}), dato che la struttura
    reale non e' verificabile in questo ambiente. Logga il formato incontrato
    una sola volta, per rendere visibile al primo test reale quale dei due
    casi si applica davvero.
    """
    global _condition_format_warned

    if isinstance(raw_condition, str):
        return raw_condition

    if isinstance(raw_condition, dict):
        if not _condition_format_warned:
            logger.warning(
                f"Campo 'condition' Wallapop e' un oggetto annidato, non una stringa: {raw_condition}. "
                f"Estraggo un campo testuale plausibile ('title'/'name'/'id') -- verificare e adattare "
                f"questa logica dopo aver visto la struttura reale."
            )
            _condition_format_warned = True
        return str(raw_condition.get("title") or raw_condition.get("name") or raw_condition.get("id") or "")

    return ""


def _parse_item(raw: dict, keyword: str) -> Optional[Item]:
    try:
        price = float(raw["price"]["cash"]["amount"])
        raw_condition = _extract_condition_string(raw.get("condition", ""))

        return Item(
            platform_item_id=str(raw["id"]), platform="wallapop", keyword=keyword,
            title=raw.get("title", "senza titolo"), price=price,
            condition=normalize_condition(raw_condition),
            url=f"https://it.wallapop.com/item/{raw.get('web_slug', raw['id'])}",
            interest_count=None,
            first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(),
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"Item Wallapop scartato per dati incompleti: {e}")
        return None
    except Exception as e:
        # Cattura ampia DELIBERATA: la struttura reale della risposta non e'
        # verificata, quindi un errore imprevisto (es. AttributeError da un
        # tipo inatteso non gia' gestito sopra) deve scartare SOLO questo
        # item, non far fallire l'intera ricerca per tutti gli altri.
        logger.warning(f"Item Wallapop scartato per errore imprevisto durante il parsing: {e}")
        return None


async def search(keyword: str, limit: int = 40, condition: Optional[str] = None) -> list:
    """
    NOTA: l'endpoint pubblico di Wallapop potrebbe non supportare un filtro
    condizione lato server in modo affidabile (non documentato). Filtriamo
    anche lato client come rete di sicurezza.
    """
    if limit > _CONSERVATIVE_MAX_LIMIT:
        logger.warning(f"limit={limit} ridotto al tetto prudenziale {_CONSERVATIVE_MAX_LIMIT}")
        limit = _CONSERVATIVE_MAX_LIMIT

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": _resolve_locale(),
    }

    # NOTA DI ONESTA': 'latitude'/'longitude' sono i nomi di parametro piu'
    # comuni per API di ricerca geolocalizzata, ma NON confermati contro
    # una risposta reale di Wallapop -- da verificare al primo test dal vivo
    # (se la ricerca ritorna risultati irrilevanti/vuoti, controllare qui per primo).
    params = {
        "keywords": keyword,
        "limit": limit,
        "latitude": WALLAPOP_LATITUDE,
        "longitude": WALLAPOP_LONGITUDE,
    }

    async with httpx.AsyncClient(headers=headers) as client:
        try:
            response = await client.get(SEARCH_URL, params=params, timeout=15.0)
        except httpx.TimeoutException:
            raise TimeoutErrorConnector("wallapop")
        except httpx.RequestError as e:
            raise UnexpectedResponseError("wallapop", f"errore di connessione: {e}")

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitError("wallapop", retry_after=int(retry_after) if retry_after else None)
        if response.status_code in (403, 401):
            raise BlockedError("wallapop", detail=f"status code {response.status_code}")

        try:
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError:
            raise UnexpectedResponseError("wallapop", f"status code {response.status_code}")
        except ValueError:
            raise UnexpectedResponseError("wallapop", "risposta non in formato JSON, possibile pagina di blocco")

        raw_items = data.get("search_objects", [])
        if not isinstance(raw_items, list):
            raise UnexpectedResponseError("wallapop", "campo 'search_objects' mancante o malformato")

        items = [_parse_item(raw, keyword) for raw in raw_items]
        items = [i for i in items if i is not None]

        if condition:
            # Filtro lato client: i.condition e' gia' normalizzato da _parse_item,
            # quindi il confronto corretto e' solo con normalize_condition(condition)
            # -- nessuna seconda condizione ridondante necessaria.
            expected = normalize_condition(condition)
            items = [i for i in items if i.condition == expected]

        return items
