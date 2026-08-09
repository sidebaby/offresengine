"""
Connector Vinted REALE tramite Playwright (browser Chromium reale headless).
Nessun tentativo di aggirare protezioni anti-bot: navighiamo come farebbe
un utente reale, lasciando che il JS della pagina esegua normalmente.

GESTIONE SESSIONE ("cookie factory"): un CONTEXT persistente viene riusato
tra tutte le ricerche (non solo il browser), cosi' i cookie di sessione reali
si accumulano nel tempo invece che ogni ricerca sembrare una visita anonima
di prima volta -- l'esatto comportamento di un utente reale che torna piu'
volte sullo stesso sito. I cookie vengono anche salvati su disco, cosi'
sopravvivono a un riavvio del processo. NESSUN cookie/token viene generato
o falsificato: sono cookie reali, ottenuti da un browser reale che naviga
normalmente -- resta fuori discussione qualunque tecnica di aggiramento
attivo delle protezioni, come stabilito fin dall'inizio del progetto.

NOTA DI ONESTA': i selettori CSS/data-testid e gli status_ids sotto sono
basati sulla struttura nota della pagina di ricerca Vinted al momento della
progettazione. Non ho potuto verificarli dal vivo in questo ambiente
(nessuna rete nel sandbox) -- vanno controllati/aggiornati da te al primo
test reale.
"""

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from database.models import Item
from connectors.normalize import normalize_condition
from connectors.errors import RateLimitError, BlockedError, TimeoutErrorConnector, UnexpectedResponseError

logger = logging.getLogger("connector.vinted")

SEARCH_URL = "https://www.vinted.it/catalog?search_text={query}"

_CONDITION_STATUS_ID_MAP = {
    "new_with_tags": "6", "new_without_tags": "1",
    "very_good": "2", "good": "3", "satisfactory": "4",
}

# Pattern atteso per l'ID Vinted nell'URL: un numero all'inizio dell'ultimo
# segmento di path (es. /items/123456789-nome-articolo). Se il formato reale
# differisce, logghiamo un warning invece di fallire silenziosamente con un
# ID potenzialmente sbagliato/duplicato.
_ITEM_ID_PATTERN = re.compile(r"^(\d+)")

_STORAGE_STATE_FILE = Path(__file__).parent / "vinted_session.json"

_browser = None
_playwright_instance = None
_context = None  # context PERSISTENTE, riusato tra le ricerche (non piu' creato/chiuso ad ogni search)
_init_lock = asyncio.Lock()  # protegge l'inizializzazione pigra da race condition tra coroutine concorrenti


async def _get_context():
    """
    Ritorna il context Playwright condiviso, inizializzandolo (browser +
    context) una sola volta anche se piu' coroutine lo richiedono
    contemporaneamente -- stesso pattern check-lock-check gia' usato per
    la cache token in connectors/ebay.py.
    """
    global _browser, _playwright_instance, _context

    if _context is not None:
        return _context

    async with _init_lock:
        if _context is not None:  # un'altra coroutine potrebbe aver gia' inizializzato mentre aspettavamo
            return _context

        _playwright_instance = await async_playwright().start()
        _browser = await _playwright_instance.chromium.launch(headless=True)
        logger.info("Istanza Chromium avviata per Vinted")

        context_kwargs = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "locale": "it-IT",
        }

        if _STORAGE_STATE_FILE.exists():
            context_kwargs["storage_state"] = str(_STORAGE_STATE_FILE)
            logger.info("Sessione Vinted precedente ripristinata da disco")

        _context = await _browser.new_context(**context_kwargs)
        return _context


async def _persist_session() -> None:
    """Salva i cookie/storage attuali su disco, cosi' sopravvivono a un riavvio del processo."""
    if _context is None:
        return
    try:
        await _context.storage_state(path=str(_STORAGE_STATE_FILE))
    except Exception as e:
        logger.warning(f"Impossibile salvare la sessione Vinted su disco: {e}")


async def close_browser():
    """Chiude context, browser e istanza Playwright, salvando prima la sessione."""
    global _browser, _playwright_instance, _context

    await _persist_session()

    if _context:
        await _context.close()
        _context = None
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None


def _parse_price(price_text: str) -> Optional[float]:
    try:
        cleaned = price_text.replace("€", "").replace(",", ".").strip()
        return float(cleaned)
    except (ValueError, AttributeError):
        return None


def _extract_item_id(relative_url: str) -> Optional[str]:
    """
    Estrae l'ID numerico dall'ultimo segmento di path. Se il formato non
    corrisponde al pattern atteso (numero all'inizio), logga un warning
    esplicito invece di ritornare silenziosamente un frammento potenzialmente
    sbagliato che potrebbe collidere con un altro articolo.
    """
    last_segment = relative_url.rstrip("/").split("/")[-1]
    match = _ITEM_ID_PATTERN.match(last_segment)
    if not match:
        logger.warning(
            f"Formato URL Vinted inatteso, impossibile estrarre un ID affidabile: '{relative_url}'. "
            f"Possibile cambio di struttura del sito -- verificare _ITEM_ID_PATTERN."
        )
        return None
    return match.group(1)


async def search(keyword: str, limit: int = 30, condition: Optional[str] = None) -> list:
    context = await _get_context()
    page = await context.new_page()

    try:
        url = SEARCH_URL.format(query=keyword)
        if condition and condition in _CONDITION_STATUS_ID_MAP:
            url += f"&status_ids[]={_CONDITION_STATUS_ID_MAP[condition]}"

        response = await page.goto(url, timeout=20000, wait_until="domcontentloaded")

        if response is None:
            raise TimeoutErrorConnector("vinted")
        if response.status == 429:
            raise RateLimitError("vinted")
        if response.status in (403, 401):
            raise BlockedError("vinted", detail=f"status code {response.status}", blocking_mechanism="datadome_sospetto")
        if response.status >= 500:
            raise UnexpectedResponseError("vinted", f"errore server, status {response.status}")

        try:
            await page.wait_for_selector("[data-testid='item-box']", timeout=8000)
        except PlaywrightTimeoutError:
            content = await page.content()
            if "nessun risultato" in content.lower() or "no results" in content.lower():
                logger.info(f"[vinted] '{keyword}' ({condition}): nessun risultato trovato")
                return []
            raise UnexpectedResponseError(
                "vinted", "struttura pagina non riconosciuta, possibile cambio layout del sito"
            )

        item_elements = await page.query_selector_all("[data-testid='item-box']")
        items = []
        for element in item_elements[:limit]:
            item = await _parse_element(element, keyword, condition)
            if item:
                items.append(item)

        return items

    except PlaywrightTimeoutError:
        raise TimeoutErrorConnector("vinted")
    finally:
        # Chiudiamo solo la PAGINA, non il context: il context (e i suoi
        # cookie) resta vivo e condiviso tra tutte le ricerche successive.
        await page.close()


async def _parse_element(element, keyword: str, requested_condition: Optional[str] = None) -> Optional[Item]:
    try:
        title_el = await element.query_selector("[data-testid='item-title']")
        price_el = await element.query_selector("[data-testid='item-price']")
        link_el = await element.query_selector("a")

        if not (title_el and price_el and link_el):
            return None

        title = await title_el.inner_text()
        price_text = await price_el.inner_text()
        relative_url = await link_el.get_attribute("href")

        price = _parse_price(price_text)
        if price is None:
            return None

        item_id = _extract_item_id(relative_url)
        if item_id is None:
            return None  # gia' loggato in _extract_item_id, scartiamo l'item invece di rischiare un ID sbagliato

        condition_value = normalize_condition(requested_condition) if requested_condition else "unknown"

        return Item(
            platform_item_id=item_id, platform="vinted", keyword=keyword,
            title=title.strip(), price=price,
            condition=condition_value,
            url=f"https://www.vinted.it{relative_url}" if relative_url.startswith("/") else relative_url,
            interest_count=None,
            first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(),
        )
    except Exception as e:
        logger.warning(f"Elemento Vinted scartato per errore di parsing: {e}")
        return None
