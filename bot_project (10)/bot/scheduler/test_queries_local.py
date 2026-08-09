"""
Script di verifica locale per database/queries.py.

DA ESEGUIRE IN LOCALE (non nel sandbox di sviluppo, dove aiosqlite non e'
disponibile): pip install aiosqlite, poi python test_queries_local.py

Usa un database temporaneo separato da bot.db (quello di produzione), cosi'
puoi eseguirlo quante volte vuoi senza sporcare i dati reali. Il file di
test viene cancellato automaticamente alla fine.
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Assicurati di eseguire questo script dalla cartella bot/ (o aggiusta il path sotto)
sys.path.insert(0, str(Path(__file__).parent))

import database.queries as queries
from database.models import Item

TEST_DB_PATH = Path(__file__).parent / "test_bot.db"

PASSED = 0
FAILED = 0


def check(description: str, condition: bool):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  OK: {description}")
    else:
        FAILED += 1
        print(f"  FALLITO: {description}")


def make_item(item_id: str, price: float, condition: str = "good", keyword: str = "test keyword",
              platform: str = "ebay", days_ago: float = 0) -> Item:
    ts = datetime.utcnow() - timedelta(days=days_ago)
    return Item(
        platform_item_id=item_id, platform=platform, keyword=keyword,
        title=f"Item di test {item_id}", price=price, condition=condition,
        url=f"https://example.com/{item_id}", interest_count=None,
        first_seen_at=ts, last_seen_at=ts,
    )


async def main():
    # Usiamo un DB temporaneo separato, monkey-patchando i path del modulo
    queries.DB_PATH = TEST_DB_PATH
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()

    print("=== Inizializzazione database di test ===")
    await queries.init_db()
    check("database inizializzato", TEST_DB_PATH.exists())

    print("\n=== Test 1: insert_item + get_item ===")
    item1 = make_item("item001", 100.0, condition="good")
    await queries.insert_item(item1, item1.keyword, item1.platform)
    fetched = await queries.get_item("ebay", "item001")
    check("item recuperato correttamente", fetched is not None and fetched.price == 100.0)

    print("\n=== Test 2: update_item_price + get_price_history ===")
    item1_updated = make_item("item001", 85.0, condition="good")
    await queries.update_item_price(item1_updated)
    history = await queries.get_price_history("ebay", "item001")
    check("storico prezzi ha 2 punti", history is not None and len(history.points) == 2)
    check("prezzo iniziale corretto", history.initial_price == 100.0)
    check("prezzo attuale corretto", history.current_price == 85.0)
    check("sconto cumulativo calcolato", history.cumulative_discount_pct == 15.0)

    print("\n=== Test 3: update_keyword_stats + get_keyword_stats (baseline semplice) ===")
    items_batch = [make_item(f"batch{i}", 90.0 + i, condition="good") for i in range(5)]
    await queries.update_keyword_stats("test keyword", "ebay", items_batch)
    stats = await queries.get_keyword_stats("test keyword", "ebay", "good")
    check("has_baseline e' True dopo l'inserimento", stats.has_baseline)
    check("sample_size riflette le osservazioni inserite", stats.sample_size == 5)
    check("last_updated_at popolato (non None)", stats.last_updated_at is not None)
    print(f"  -> media calcolata: {stats.avg_price:.2f}, campioni: {stats.sample_size}")

    print("\n=== Test 4: FINESTRA MOBILE -- osservazioni vecchie non devono contare ===")
    # Inseriamo manualmente osservazioni "vecchie" (oltre la finestra) direttamente
    # nella tabella, per simulare dati che si sono accumulati settimane fa
    old_date = (datetime.utcnow() - timedelta(days=queries.STATS_ROLLING_WINDOW_DAYS + 10)).isoformat()
    await queries._connection.execute(
        """INSERT INTO segment_observations (keyword, platform, condition, price, observed_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("finestra_test", "ebay", "good", 9999.0, old_date),  # prezzo assurdo, facile da individuare se trapela
    )
    await queries._connection.commit()

    # Ora aggiungiamo un'osservazione FRESCA per lo stesso segmento
    fresh_items = [make_item("fresh1", 50.0, condition="good", keyword="finestra_test")]
    await queries.update_keyword_stats("finestra_test", "ebay", fresh_items)

    stats_window = await queries.get_keyword_stats("finestra_test", "ebay", "good")
    check(
        "la media NON include l'osservazione vecchia (9999.0)",
        stats_window.avg_price < 1000.0
    )
    check("sample_size conta solo l'osservazione fresca (1, non 2)", stats_window.sample_size == 1)
    print(f"  -> media (deve essere ~50.0, non influenzata da 9999.0): {stats_window.avg_price:.2f}")

    print("\n=== Test 5: segmentazione per condizione (very_good vs good separati) ===")
    item_very_good = make_item("vg1", 200.0, condition="very_good", keyword="segmentazione test")
    item_good = make_item("g1", 100.0, condition="good", keyword="segmentazione test")
    await queries.update_keyword_stats("segmentazione test", "ebay", [item_very_good])
    await queries.update_keyword_stats("segmentazione test", "ebay", [item_good])

    stats_vg = await queries.get_keyword_stats("segmentazione test", "ebay", "very_good")
    stats_g = await queries.get_keyword_stats("segmentazione test", "ebay", "good")
    check("very_good e good hanno medie DIVERSE (non mescolate)", stats_vg.avg_price != stats_g.avg_price)
    check("very_good media corretta", stats_vg.avg_price == 200.0)
    check("good media corretta", stats_g.avg_price == 100.0)

    print("\n=== Test 6: record_alert con platform + priority ===")
    await queries.record_alert("ebay", "item001", 0.75, priority="alta")
    async with queries._connection.execute("SELECT * FROM alerts_sent WHERE platform_item_id = 'item001'") as cursor:
        row = await cursor.fetchone()
    check("alert salvato con platform corretto", row is not None and row["platform"] == "ebay")
    check("alert salvato con priority corretta", row["priority"] == "alta")

    print("\n=== Test 7: prune_old_price_history e prune_old_segment_observations ===")
    # L'osservazione vecchia inserita nel Test 4 e' oltre SEGMENT_OBSERVATIONS_MAX_AGE_DAYS?
    # (STATS_ROLLING_WINDOW_DAYS + 10 potrebbe non superare 2x la finestra di default 30 -> 60gg)
    # Forziamone una molto più vecchia per testare la pulizia in modo affidabile
    very_old_date = (datetime.utcnow() - timedelta(days=queries.SEGMENT_OBSERVATIONS_MAX_AGE_DAYS + 30)).isoformat()
    await queries._connection.execute(
        """INSERT INTO segment_observations (keyword, platform, condition, price, observed_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("da_rimuovere", "ebay", "good", 1.0, very_old_date),
    )
    await queries._connection.commit()

    await queries.prune_old_segment_observations()

    async with queries._connection.execute(
        "SELECT COUNT(*) as c FROM segment_observations WHERE keyword = 'da_rimuovere'"
    ) as cursor:
        row = await cursor.fetchone()
    check("l'osservazione molto vecchia e' stata rimossa dalla pulizia", row["c"] == 0)

    print("\n=== Test 8: validazione modello (Item con dati invalidi deve fallire) ===")
    try:
        make_item("bad", -50.0)  # prezzo negativo
        check("Item con prezzo negativo doveva sollevare ValueError", False)
    except ValueError:
        check("Item con prezzo negativo correttamente rifiutato", True)

    await queries.close_db()
    TEST_DB_PATH.unlink()

    print(f"\n{'='*50}")
    print(f"RISULTATO: {PASSED} test superati, {FAILED} falliti")
    print(f"{'='*50}")

    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
