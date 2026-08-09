"""
Script di bootstrap: costruisce la baseline iniziale (keyword_stats segmentate
per condizione) scansionando un campione ridotto per ogni combinazione
keyword x piattaforma x condizione, invece di aspettare che la baseline si
formi naturalmente nel tempo con gli scan periodici leggeri.

Punto chiave: NON tutti i prezzi raccolti finiscono nella media. Ogni prezzo
viene confrontato con la MEDIANA del proprio piccolo campione (più robusta
della media quando i campioni sono di sole 2-3 osservazioni): chi si scosta
troppo (oltre OUTLIER_TOLERANCE_PCT) non entra nella baseline, ma viene
salvato subito come "opportunità" (troppo sotto) o "sovrapprezzo" (troppo
sopra) -- esattamente la logica richiesta.
"""

import asyncio
import logging
from statistics import median

from connectors import ebay, wallapop, vinted
from connectors.errors import ConnectorError
from database import queries
from config.bootstrap_keywords import (
    BOOTSTRAP_KEYWORDS, CONDITION_TIERS, SAMPLES_PER_SEGMENT,
    BOOTSTRAP_DELAY_SECONDS, OUTLIER_TOLERANCE_PCT,
    validate_bootstrap_config, estimate_total_requests,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bootstrap")

CONNECTORS = {"ebay": ebay.search, "wallapop": wallapop.search, "vinted": vinted.search}


def classify_prices(items: list) -> tuple[list, list, list]:
    """
    Divide gli item del segmento in tre gruppi: normali (baseline),
    occasioni (troppo sotto la mediana), sovrapprezzo (troppo sopra).
    Con campioni molto piccoli (2-3) usiamo la mediana come riferimento,
    più resistente agli outlier stessi rispetto alla media.
    """
    if len(items) < 2:
        # campione troppo piccolo per distinguere outlier in modo sensato:
        # li trattiamo tutti come "normali", dichiarandolo esplicitamente nel log
        return items, [], []

    prices = [i.price for i in items]
    ref = median(prices)

    normal, opportunities, overpriced = [], [], []
    for item in items:
        deviation = (item.price - ref) / ref
        if deviation < -OUTLIER_TOLERANCE_PCT:
            opportunities.append(item)
        elif deviation > OUTLIER_TOLERANCE_PCT:
            overpriced.append(item)
        else:
            normal.append(item)

    return normal, opportunities, overpriced


async def _save_item_safely(item, keyword: str, platform: str) -> None:
    """
    Salva un item gestendo sia il caso nuovo (INSERT) sia quello gia' visto
    (UPDATE) -- necessario perche' il bootstrap puo' incontrare lo stesso
    articolo reale sotto piu' tier di condizione (es. eBay non distingue
    finemente 'very_good'/'good'/'satisfactory', vedi audit di ebay.py),
    quindi un INSERT incondizionato fallirebbe con un errore di vincolo UNIQUE
    alla seconda occorrenza.
    """
    existing = await queries.get_item(item.platform, item.platform_item_id)
    if existing is None:
        await queries.insert_item(item, keyword, platform)
    else:
        await queries.update_item_price(item, alert_eligible=False)


async def bootstrap_segment(keyword: str, platform: str, condition: str, search_fn) -> None:
    """Scansiona un singolo segmento (keyword+piattaforma+condizione) e classifica i risultati."""
    try:
        items = await search_fn(keyword, limit=SAMPLES_PER_SEGMENT, condition=condition)
    except ConnectorError as e:
        logger.warning(f"Segmento saltato per errore connector: {e}")
        return
    except Exception as e:
        logger.warning(f"[{platform}/{keyword}/{condition}] errore non classificato: {e}")
        return

    if not items:
        logger.info(f"[{platform}] '{keyword}' ({condition}): nessun risultato, segmento vuoto")
        return

    normal, opportunities, overpriced = classify_prices(items)

    for item in opportunities:
        await _save_item_safely(item, keyword, platform)
        logger.info(
            f"🔥 OCCASIONE rilevata durante il bootstrap: '{item.title}' a {item.price}EUR "
            f"(sotto la mediana del segmento di oltre {OUTLIER_TOLERANCE_PCT*100:.0f}%)"
        )

    for item in overpriced:
        await _save_item_safely(item, keyword, platform)
        logger.info(
            f"💰 SOVRAPPREZZO rilevato durante il bootstrap: '{item.title}' a {item.price}EUR "
            f"(escluso dalla baseline, salvato per riferimento)"
        )

    if normal:
        for item in normal:
            await _save_item_safely(item, keyword, platform)

        # Usiamo la vera funzione del database (finestra mobile su
        # segment_observations), non piu' un calcolo manuale iniettato
        # direttamente in un attributo privato che non esiste nella
        # versione SQLite -- questo E' il modo corretto di registrare
        # una baseline, identico a quello usato dallo scheduler periodico.
        await queries.update_keyword_stats(keyword, platform, normal)

        stats = await queries.get_keyword_stats(keyword, platform, condition)
        logger.info(
            f"[BASELINE] {platform}/{condition}/'{keyword}': media={stats.avg_price:.2f}EUR "
            f"da {stats.sample_size} campioni nella finestra mobile (di cui {len(normal)} appena aggiunti)"
        )
    else:
        logger.info(
            f"[BASELINE] {platform}/{condition}/'{keyword}': nessun campione 'normale' "
            f"(tutti outlier o campione insufficiente) — baseline non costruita per questo segmento"
        )


async def run_bootstrap():
    validate_bootstrap_config()  # fallisce subito se la config e' pericolosa o incoerente

    total_segments = len(BOOTSTRAP_KEYWORDS) * len(CONDITION_TIERS) * len(CONNECTORS)
    total_requests = estimate_total_requests(num_platforms=len(CONNECTORS))
    estimated_minutes = (total_segments * BOOTSTRAP_DELAY_SECONDS) / 60
    logger.info(
        f"=== Avvio bootstrap: {total_segments} segmenti, ~{total_requests} richieste totali, "
        f"tempo stimato ~{estimated_minutes:.1f} minuti (con delay={BOOTSTRAP_DELAY_SECONDS}s) ==="
    )

    done = 0
    for keyword in BOOTSTRAP_KEYWORDS:
        for platform, search_fn in CONNECTORS.items():
            for condition in CONDITION_TIERS:
                await bootstrap_segment(keyword, platform, condition, search_fn)
                done += 1
                if done % 10 == 0:
                    logger.info(f"Progresso bootstrap: {done}/{total_segments} segmenti completati")
                await asyncio.sleep(BOOTSTRAP_DELAY_SECONDS)

    logger.info("=== Bootstrap completato ===")


if __name__ == "__main__":
    asyncio.run(run_bootstrap())
