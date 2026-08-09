"""
Connector eBay REALE tramite la Browse API ufficiale (parte delle Buy APIs
di eBay -- non correlata alle API Amazon discusse in precedenza, nessuna
deprecazione nota qui). Gestisce OAuth2 client-credentials con cache token
protetta da lock (evita richieste duplicate concorrenti), e filtro condizione
lato server tramite conditionIds -- solo dove l'ID eBay ha un corrispondente
semanticamente corretto (vedi nota sotto).

NOTA DI ONESTA': i conditionIds sono quelli documentati da eBay. Non ho potuto
verificarli dal vivo in questo ambiente (nessun accesso di rete nel sandbox)
-- testali con una chiamata reale prima di fare pieno affidamento sul filtro.

NOTA SULLA MAPPATURA CONDIZIONI: eBay NON ha tier graduati per articoli usati
(non distingue "molto buono" da "discreto" come fanno le piattaforme fashion
resale). Per questo motivo, i tier 'very_good', 'good' e 'satisfactory' sono
TUTTI mappati sullo stesso conditionId 3000 (Used) -- filtrare per un ID
diverso per ciascuno userebbe programmi eBay semanticamente sbagliati
(Certified Refurbished, For parts/not working), non gradazioni di usato.
"""

import asyncio
import base64
import logging
import time

from datetime import datetime
from typing import Optional

import httpx

from database.models import Item
from connectors.normalize import normalize_condition
from connectors.errors import RateLimitError, BlockedError, AuthError, TimeoutErrorConnector, UnexpectedResponseError
from config.settings import EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_MARKETPLACE_ID

logger = logging.getLogger("connector.ebay")

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

EBAY_BROWSE_API_MAX_LIMIT = 200  # limite documentato eBay per richiesta di ricerca

# Vedi nota nel docstring del modulo sul perche' 'very_good'/'good'/'satisfactory'
# condividono lo stesso ID: eBay non ha gradazioni di usato, solo "Used" generico.
_CONDITION_ID_MAP = {
    "new_with_tags": "1000",     # New
    "new_without_tags": "1500",  # New other (see details)
    "very_good": "3000",         # Used (nessuna distinzione piu' fine disponibile su eBay)
    "good": "3000",              # Used
    "satisfactory": "3000",      # Used
}

_token_cache = {"access_token": None, "expires_at": 0}
_token_lock = asyncio.Lock()  # protegge la cache da richieste concorrenti duplicate


def _resolve_marketplace_id() -> str:
    """
    EBAY_MARKETPLACE_ID esplicito (da env) ha sempre priorita'. Se non
    impostato esplicitamente, deriva dal paese configurato tramite
    config/marketplaces.py -- import locale (non a livello di modulo) per
    evitare qualunque rischio di dipendenza circolare con settings.py.
    """
    if EBAY_MARKETPLACE_ID:
        return EBAY_MARKETPLACE_ID
    from config.marketplaces import get_marketplace_config
    return get_marketplace_config()["ebay_marketplace_id"]


def _normalize_ebay_condition_string(raw_condition: str) -> str:
    """
    L'API Browse restituisce condizioni testuali con spazi/maiuscole
    (es. 'For parts or not working'), mentre normalize_condition si aspetta
    chiavi in stile 'for_parts_or_not_working'. Questa trasformazione allinea
    i due formati per i casi comuni (New, New other, Used, For parts...).
    Varianti non mappate (es. 'Certified - Refurbished') ricadono onestamente
    su 'unknown' invece di essere forzate in una categoria sbagliata.
    """
    return raw_condition.strip().lower().replace(" ", "_").replace("-", "_").replace("__", "_")


async def _get_access_token(client: httpx.AsyncClient) -> str:
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        raise AuthError("ebay", permanent=True, detail="EBAY_CLIENT_ID o EBAY_CLIENT_SECRET non impostati")

    # Prima lettura senza lock: se il token e' valido, evitiamo di acquisire
    # il lock inutilmente ad ogni singola chiamata (fast path comune).
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    async with _token_lock:
        # Secondo controllo DENTRO il lock: un'altra coroutine potrebbe aver
        # gia' rinnovato il token mentre aspettavamo il lock -- evita di
        # richiederne uno nuovo due volte per la stessa scadenza.
        if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]

        credentials = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()

        try:
            response = await client.post(
                TOKEN_URL,
                headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
                timeout=10.0,
            )
        except httpx.TimeoutException:
            raise TimeoutErrorConnector("ebay")
        except httpx.RequestError as e:
            raise UnexpectedResponseError("ebay", f"errore di connessione durante l'autenticazione: {e}")

        if response.status_code in (400, 401, 403):
            raise AuthError("ebay")
        if response.status_code != 200:
            # Errore diverso da un vero problema di credenziali (es. 500 lato eBay):
            # non e' onesto chiamarlo AuthError, confonderebbe la diagnosi
            raise UnexpectedResponseError("ebay", f"token endpoint ha risposto {response.status_code}")

        data = response.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data["expires_in"]
        return _token_cache["access_token"]


async def _invalidate_token() -> None:
    async with _token_lock:
        _token_cache["access_token"] = None


def _parse_item(raw: dict, keyword: str) -> Optional[Item]:
    try:
        price = float(raw["price"]["value"])
        raw_condition = raw.get("condition", "")
        normalized_key = _normalize_ebay_condition_string(raw_condition)

        return Item(
            platform_item_id=raw["itemId"], platform="ebay", keyword=keyword,
            title=raw["title"], price=price,
            condition=normalize_condition(normalized_key),
            url=raw["itemWebUrl"],
            interest_count=None,
            first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(),
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.warning(f"Item eBay scartato per dati incompleti: {e}")
        return None


async def search(keyword: str, limit: int = 50, condition: Optional[str] = None) -> list:
    if limit > EBAY_BROWSE_API_MAX_LIMIT:
        logger.warning(
            f"limit={limit} supera il massimo Browse API ({EBAY_BROWSE_API_MAX_LIMIT}), "
            f"ridotto automaticamente"
        )
        limit = EBAY_BROWSE_API_MAX_LIMIT

    marketplace_id = _resolve_marketplace_id()

    async with httpx.AsyncClient() as client:
        token = await _get_access_token(client)

        params = {"q": keyword, "limit": limit}
        if condition and condition in _CONDITION_ID_MAP:
            params["filter"] = f"conditionIds:{{{_CONDITION_ID_MAP[condition]}}}"

        try:
            response = await client.get(
                SEARCH_URL,
                headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace_id},
                params=params, timeout=15.0,
            )
        except httpx.TimeoutException:
            raise TimeoutErrorConnector("ebay")
        except httpx.RequestError as e:
            raise UnexpectedResponseError("ebay", f"errore di connessione: {e}")

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitError("ebay", retry_after=int(retry_after) if retry_after else None)
        if response.status_code == 401:
            await _invalidate_token()
            raise AuthError("ebay")
        if response.status_code >= 500:
            raise UnexpectedResponseError("ebay", f"errore server, status {response.status_code}")

        try:
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError:
            raise UnexpectedResponseError("ebay", f"status code {response.status_code}")
        except ValueError:
            raise UnexpectedResponseError("ebay", "risposta non in formato JSON valido")

        raw_items = data.get("itemSummaries", [])
        if not isinstance(raw_items, list):
            raise UnexpectedResponseError("ebay", "campo 'itemSummaries' mancante o malformato")

        items = [_parse_item(raw, keyword) for raw in raw_items]
        return [i for i in items if i is not None]
