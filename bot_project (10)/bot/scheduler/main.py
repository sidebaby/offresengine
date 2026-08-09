import asyncio
import logging
from datetime import datetime

from connectors import ebay, wallapop, vinted
from connectors.errors import RateLimitError, BlockedError, AuthError, TimeoutErrorConnector, UnexpectedResponseError
from database import queries
from alerting.scoring import opportunity_score
from alerting.notifier import send_alert, flush_retry_queue
from config.settings import (
    SCAN_INTERVAL_HOURS, ALERT_THRESHOLD, INTEREST_WARNING_MIN,
    validate_required_secrets, print_startup_summary, get_active_keywords,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scheduler")

CONNECTORS = {"ebay": ebay.search, "wallapop": wallapop.search, "vinted": vinted.search}

# Sconto di soglia per priorita': "bassa" resta la piu' permissiva (segnale debole ma informativo),
# ma ora anche "alta" (prezzo appena sceso) ha uno sconto, perche' un prezzo in discesa
# e' il segnale piu' importante per un reseller e non deve essere silenziato solo per
# via di un item non freschissimo.
THRESHOLD_MULTIPLIER = {"nuovo": 1.0, "alta": 0.85, "bassa": 0.7}


async def scan_keyword_on_platform(keyword: str, platform: str, search_fn):
    try:
        items = await search_fn(keyword)
        logger.info(f"[{platform}] '{keyword}': {len(items)} risultati trovati")
        return items
    except RateLimitError as e:
        logger.warning(str(e))
    except BlockedError as e:
        logger.warning(str(e))
    except AuthError as e:
        logger.warning(str(e))
    except TimeoutErrorConnector as e:
        logger.warning(str(e))
    except UnexpectedResponseError as e:
        logger.warning(str(e))
    except Exception as e:
        logger.warning(f"[{platform}] '{keyword}': errore non classificato ({e}), riprovero' al prossimo ciclo")
    return []


def build_alert_message(item, score: float, priority: str, price_history) -> str:
    lines = [
        f"{'🔥' if priority == 'alta' else '👀'} {item.title}",
        f"Prezzo: {item.price}EUR | Piattaforma: {item.platform} | Keyword: {item.keyword} | Condizione: {item.condition}",
        f"Score occasione: {score:.2f}",
        f"Link: {item.url}",
    ]

    if price_history is not None:
        discount_pct = price_history.cumulative_discount_pct
        if discount_pct is not None and abs(discount_pct) >= 1:
            direction = "sceso" if discount_pct > 0 else "salito"
            lines.append(
                f"📉 Prezzo {direction} del {abs(discount_pct):.1f}% dal primo avvistamento "
                f"({price_history.initial_price}EUR -> {price_history.current_price}EUR)"
            )

    if item.interest_count is not None and item.interest_count >= INTEREST_WARNING_MIN:
        lines.append(
            f"⚠️ Attenzione: gia' {item.interest_count} persone hanno mostrato interesse — "
            f"valuta di offrire un prezzo leggermente piu' alto se lo vuoi davvero."
        )
    return "\n".join(lines)


async def evaluate_and_alert(item, keyword: str, platform: str, priority_hint: str):
    condition = item.condition or "unknown"
    stats = await queries.get_keyword_stats(keyword, platform, condition)
    minutes_since_listed = (datetime.utcnow() - item.first_seen_at).total_seconds() / 60

    if not stats.has_baseline:
        # Nessuno storico per questa condizione specifica: non possiamo valutare
        # onestamente se sia un affare. Logghiamo e salviamo, niente alert forzati.
        logger.info(
            f"[SCORE] '{item.title}' (condizione={condition}): nessuno storico segmentato disponibile, "
            f"salvato per costruire la baseline futura, nessun alert"
        )
        return

    score = opportunity_score(
        price=item.price, avg_price=stats.avg_price, stddev_price=stats.stddev_price,
        sample_size=stats.sample_size, minutes_since_listed=minutes_since_listed,
        tolerance_pct=stats.tolerance_pct, trend_slope=stats.trend_slope,
    )

    threshold = ALERT_THRESHOLD * THRESHOLD_MULTIPLIER[priority_hint]
    logger.info(
        f"[SCORE] '{item.title}' (cond={condition}) -> score={score:.2f} (soglia={threshold:.2f}, "
        f"hint={priority_hint}, avg_segmento={stats.avg_price:.2f}, trend={stats.trend_label})"
    )

    if score >= threshold:
        priority = "alta" if priority_hint in ("nuovo", "alta") else "bassa"
        price_history = await queries.get_price_history(item.platform, item.platform_item_id)
        message = build_alert_message(item, score, priority, price_history)
        await send_alert(message, priority=priority)
        await queries.record_alert(item.platform, item.platform_item_id, score, priority=priority)
    else:
        logger.info(f"[SCORE] Sotto soglia, nessun alert per '{item.title}'")


async def process_items(items: list, keyword: str, platform: str):
    for item in items:
        existing = await queries.get_item(item.platform, item.platform_item_id)

        if existing is None:
            await queries.insert_item(item, keyword, platform)
            await evaluate_and_alert(item, keyword, platform, priority_hint="nuovo")
            continue

        if item.price < existing.price:
            await queries.update_item_price(item)
            await evaluate_and_alert(item, keyword, platform, priority_hint="alta")
        elif item.price > existing.price:
            condition = item.condition or "unknown"
            stats = await queries.get_keyword_stats(keyword, platform, condition)
            if stats.has_baseline and item.price < stats.avg_price:
                await queries.update_item_price(item)
                await evaluate_and_alert(item, keyword, platform, priority_hint="bassa")
            else:
                await queries.update_item_price(item, alert_eligible=False)
        else:
            await queries.touch_item(item)

    await queries.update_keyword_stats(keyword, platform, items)


async def _scan_and_process(keyword, platform, search_fn):
    items = await scan_keyword_on_platform(keyword, platform, search_fn)
    if items:
        await process_items(items, keyword, platform)


async def run_cycle():
    logger.info("=== Inizio ciclo di scansione ===")
    await flush_retry_queue()

    tasks = []
    for keyword in get_active_keywords():
        for platform, search_fn in CONNECTORS.items():
            tasks.append(_scan_and_process(keyword, platform, search_fn))

    await asyncio.gather(*tasks)
    logger.info("=== Ciclo completato ===")


async def main_loop(cycles: int = 1):
    last_prune = datetime.utcnow()
    for i in range(cycles):
        start = datetime.utcnow()
        await run_cycle()

        # Pulizia storico prezzi una volta al giorno, non ad ogni ciclo
        if (datetime.utcnow() - last_prune).total_seconds() >= 86400:
            await queries.prune_old_price_history()
            last_prune = datetime.utcnow()

        elapsed = (datetime.utcnow() - start).total_seconds()
        logger.info(f"Ciclo {i+1}/{cycles} completato in {elapsed:.2f}s")


async def shutdown():
    logger.info("Arresto in corso, chiusura browser Vinted e database...")
    await vinted.close_browser()
    await queries.close_db()


async def startup_and_run(cycles: int = 1):
    validate_required_secrets()
    print_startup_summary()
    await queries.init_db()
    try:
        await main_loop(cycles=cycles)
    finally:
        await shutdown()


if __name__ == "__main__":
    asyncio.run(startup_and_run(cycles=1))
