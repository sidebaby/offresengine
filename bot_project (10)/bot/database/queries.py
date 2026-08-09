"""
Persistenza reale con SQLite (via aiosqlite).

CAMBIAMENTO STRUTTURALE PIU' IMPORTANTE: keyword_stats non e' piu' un
aggregato "in-place" aggiornato incrementalmente (che cresceva all'infinito
e comprimeva artificialmente la varianza). Ogni prezzo osservato viene
registrato in segment_observations; keyword_stats viene RICALCOLATA da una
finestra mobile temporale su quella tabella -- una vera cache derivata,
non uno stato che degrada nel tempo.
"""

import asyncio
import logging
from pathlib import Path
from statistics import mean, stdev
from datetime import datetime, timedelta

import aiosqlite

from database.models import Item, KeywordStats, PricePoint, ItemPriceHistory
from config.settings import STATS_ROLLING_WINDOW_DAYS

logger = logging.getLogger("database.sqlite")

DB_PATH = Path(__file__).parent / "bot.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

TREND_WINDOW = 5
PRICE_HISTORY_MAX_AGE_DAYS = 90
# Le osservazioni di segmento sopravvivono un po' oltre la finestra mobile
# usata per il calcolo (2x), cosi' un cambio futuro di STATS_ROLLING_WINDOW_DAYS
# verso l'alto non si ritrova subito senza dati storici sufficienti.
SEGMENT_OBSERVATIONS_MAX_AGE_DAYS = STATS_ROLLING_WINDOW_DAYS * 2

_connection: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


async def init_db() -> None:
    global _connection
    if _connection is not None:
        return

    _connection = await aiosqlite.connect(DB_PATH)
    _connection.row_factory = aiosqlite.Row

    await _connection.execute("PRAGMA journal_mode=WAL;")
    await _connection.execute("PRAGMA foreign_keys=ON;")

    schema_sql = SCHEMA_PATH.read_text()
    await _connection.executescript(schema_sql)
    await _connection.commit()
    logger.info(f"Database inizializzato: {DB_PATH} (finestra statistiche: {STATS_ROLLING_WINDOW_DAYS} giorni)")


async def close_db() -> None:
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


def _row_to_item(row: aiosqlite.Row) -> Item:
    return Item(
        platform_item_id=row["platform_item_id"], platform=row["platform"],
        keyword=row["keyword"], title=row["title"], price=row["price"],
        condition=row["condition"], url=row["url"], interest_count=row["interest_count"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
    )


async def get_item(platform: str, platform_item_id: str) -> Item | None:
    async with _connection.execute(
        "SELECT * FROM items WHERE platform = ? AND platform_item_id = ?",
        (platform, platform_item_id),
    ) as cursor:
        row = await cursor.fetchone()
        return _row_to_item(row) if row else None


async def insert_item(item: Item, keyword: str, platform: str) -> None:
    # Un solo commit per l'intera operazione (item + primo punto storico),
    # invece di due commit separati come nella versione precedente.
    await _connection.execute(
        """INSERT INTO items
           (platform, platform_item_id, keyword, title, price, condition, url,
            interest_count, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (item.platform, item.platform_item_id, keyword, item.title, item.price,
         item.condition, item.url, item.interest_count,
         item.first_seen_at.isoformat(), item.last_seen_at.isoformat()),
    )
    await _record_price_point_no_commit(item)
    await _connection.commit()
    logger.info(f"[DB] Nuovo item salvato: {item.title} ({platform}, condizione={item.condition})")


async def update_item_price(item: Item, alert_eligible: bool = True) -> None:
    await _connection.execute(
        """UPDATE items SET price = ?, last_seen_at = ?
           WHERE platform = ? AND platform_item_id = ?""",
        (item.price, item.last_seen_at.isoformat(), item.platform, item.platform_item_id),
    )
    await _record_price_point_no_commit(item)
    await _connection.commit()
    tag = "ALERT" if alert_eligible else "no-alert"
    logger.info(f"[DB] Prezzo aggiornato ({tag}): {item.title} -> {item.price}{getattr(item, 'currency', 'EUR')}")


async def touch_item(item: Item) -> None:
    await _connection.execute(
        "UPDATE items SET last_seen_at = ? WHERE platform = ? AND platform_item_id = ?",
        (item.last_seen_at.isoformat(), item.platform, item.platform_item_id),
    )
    await _connection.commit()


async def _record_price_point_no_commit(item: Item) -> None:
    """Versione senza commit, per essere combinata in una singola transazione dal chiamante."""
    await _connection.execute(
        """INSERT INTO price_history (platform, platform_item_id, price, observed_at)
           VALUES (?, ?, ?, ?)""",
        (item.platform, item.platform_item_id, item.price, item.last_seen_at.isoformat()),
    )


async def record_price_point(item: Item) -> None:
    """Punto di ingresso pubblico (con commit), per chiamate isolate al di fuori di insert/update_item."""
    await _record_price_point_no_commit(item)
    await _connection.commit()


async def get_price_history(platform: str, platform_item_id: str) -> ItemPriceHistory | None:
    async with _connection.execute(
        """SELECT price, observed_at FROM price_history
           WHERE platform = ? AND platform_item_id = ? ORDER BY observed_at ASC""",
        (platform, platform_item_id),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        return None

    history = ItemPriceHistory(platform=platform, platform_item_id=platform_item_id)
    for r in rows:
        history.add_point(price=r["price"], observed_at=datetime.fromisoformat(r["observed_at"]))
    return history


async def get_keyword_stats(keyword: str, platform: str, condition: str) -> KeywordStats:
    async with _connection.execute(
        "SELECT * FROM keyword_stats WHERE keyword = ? AND platform = ? AND condition = ?",
        (keyword, platform, condition),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return KeywordStats(keyword=keyword, platform=platform, condition=condition,
                             avg_price=0.0, stddev_price=0.0, sample_size=0)

    return KeywordStats(
        keyword=row["keyword"], platform=row["platform"], condition=row["condition"],
        avg_price=row["avg_price"], stddev_price=row["stddev_price"], sample_size=row["sample_size"],
        tolerance_pct=row["tolerance_pct"], trend_slope=row["trend_slope"],
        last_updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


async def update_keyword_stats(keyword: str, platform: str, items: list) -> None:
    """
    Registra ogni prezzo osservato in segment_observations, poi RICALCOLA
    (non aggiorna incrementalmente) keyword_stats per ogni condizione
    coinvolta, usando solo le osservazioni entro STATS_ROLLING_WINDOW_DAYS.
    """
    if not items:
        return

    by_condition: dict[str, list[float]] = {}
    for i in items:
        by_condition.setdefault(i.condition or "unknown", []).append(i.price)

    now_dt = datetime.utcnow()
    now = now_dt.isoformat()

    async with _write_lock:
        for condition, prices in by_condition.items():
            # 1. Registriamo OGNI osservazione grezza (fonte di verita')
            for price in prices:
                await _connection.execute(
                    """INSERT INTO segment_observations (keyword, platform, condition, price, observed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (keyword, platform, condition, price, now),
                )
            await _connection.commit()

            # 2. Ricalcoliamo la baseline da zero, filtrando solo la finestra mobile
            new_avg, new_std, new_sample = await _recompute_segment_stats(keyword, platform, condition, now_dt)

            existing = await get_keyword_stats(keyword, platform, condition)

            await _connection.execute(
                """INSERT INTO keyword_stats
                   (keyword, platform, condition, avg_price, stddev_price, sample_size,
                    tolerance_pct, trend_slope, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(keyword, platform, condition) DO UPDATE SET
                       avg_price=excluded.avg_price, stddev_price=excluded.stddev_price,
                       sample_size=excluded.sample_size, updated_at=excluded.updated_at""",
                (keyword, platform, condition, new_avg, new_std, new_sample,
                 existing.tolerance_pct, existing.trend_slope, now),
            )

            await _connection.execute(
                """INSERT INTO stats_history (keyword, platform, condition, avg_price, observed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (keyword, platform, condition, new_avg, now),
            )
            await _connection.commit()

            trend = await _calculate_trend(keyword, platform, condition)
            await _connection.execute(
                "UPDATE keyword_stats SET trend_slope = ? WHERE keyword = ? AND platform = ? AND condition = ?",
                (trend, keyword, platform, condition),
            )
            await _connection.commit()


async def _recompute_segment_stats(
    keyword: str, platform: str, condition: str, now_dt: datetime
) -> tuple[float, float, int]:
    """
    Ricalcola media/deviazione standard/campione REALI a partire dalle
    osservazioni grezze entro la finestra mobile -- sostituisce la vecchia
    tecnica di ricostruzione artificiale della distribuzione.
    """
    cutoff = (now_dt - timedelta(days=STATS_ROLLING_WINDOW_DAYS)).isoformat()

    async with _connection.execute(
        """SELECT price FROM segment_observations
           WHERE keyword = ? AND platform = ? AND condition = ? AND observed_at >= ?""",
        (keyword, platform, condition, cutoff),
    ) as cursor:
        rows = await cursor.fetchall()

    prices = [r["price"] for r in rows]
    if not prices:
        return 0.0, 0.0, 0

    avg = mean(prices)
    std = stdev(prices) if len(prices) > 1 else avg * 0.1
    return avg, std, len(prices)


async def _calculate_trend(keyword: str, platform: str, condition: str) -> float:
    async with _connection.execute(
        """SELECT avg_price FROM stats_history
           WHERE keyword = ? AND platform = ? AND condition = ?
           ORDER BY observed_at DESC LIMIT ?""",
        (keyword, platform, condition, TREND_WINDOW),
    ) as cursor:
        rows = await cursor.fetchall()

    if len(rows) < 3:
        return 0.0

    prices = [r["avg_price"] for r in reversed(rows)]
    older_half = prices[: len(prices) // 2] or [prices[0]]
    recent_half = prices[len(prices) // 2:] or [prices[-1]]

    older_avg = mean(older_half)
    recent_avg = mean(recent_half)
    if older_avg == 0:
        return 0.0

    change_pct = (recent_avg - older_avg) / older_avg
    return max(-1.0, min(1.0, change_pct * 5))


async def record_alert(platform: str, platform_item_id: str, score: float, priority: str | None = None) -> None:
    """
    NOTA: la firma ora richiede anche 'platform' (prima mancava, un gap di
    integrita' referenziale reale -- vedi audit). Lo scheduler e' stato
    aggiornato di conseguenza.
    """
    await _connection.execute(
        "INSERT INTO alerts_sent (platform, platform_item_id, score, priority, sent_at) VALUES (?, ?, ?, ?, ?)",
        (platform, platform_item_id, score, priority, datetime.utcnow().isoformat()),
    )
    await _connection.commit()


async def prune_old_price_history() -> None:
    cutoff = (datetime.utcnow() - timedelta(days=PRICE_HISTORY_MAX_AGE_DAYS)).isoformat()
    cursor = await _connection.execute("DELETE FROM price_history WHERE observed_at < ?", (cutoff,))
    await _connection.commit()
    if cursor.rowcount:
        logger.info(f"[DB] Pulizia storico prezzi: rimossi {cursor.rowcount} punti piu' vecchi di {PRICE_HISTORY_MAX_AGE_DAYS} giorni")

    await prune_old_segment_observations()


async def prune_old_segment_observations() -> None:
    """
    Rimuove le osservazioni di segmento troppo vecchie per essere utili
    anche a un futuro aumento della finestra mobile (vedi SEGMENT_OBSERVATIONS_MAX_AGE_DAYS).
    """
    cutoff = (datetime.utcnow() - timedelta(days=SEGMENT_OBSERVATIONS_MAX_AGE_DAYS)).isoformat()
    cursor = await _connection.execute("DELETE FROM segment_observations WHERE observed_at < ?", (cutoff,))
    await _connection.commit()
    if cursor.rowcount:
        logger.info(
            f"[DB] Pulizia osservazioni di segmento: rimossi {cursor.rowcount} punti "
            f"piu' vecchi di {SEGMENT_OBSERVATIONS_MAX_AGE_DAYS} giorni"
        )
