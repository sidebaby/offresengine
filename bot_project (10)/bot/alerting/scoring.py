"""
Scoring: confronto SEGMENTATO per condizione (non media generica), con banda
di tolleranza (ne' affare ne' sovrapprezzo) e correzione per trend di mercato
(bearish/bullish), oltre a rarita' e freschezza.

NOTA IMPORTANTE SU 'rarity_score': il parametro sample_size dovrebbe idealmente
rappresentare un conteggio RECENTE/FINESTRATO (es. ultimi 30 giorni), non un
totale cumulativo a vita. Oggi database/queries.py accumula sample_size senza
mai resettarlo: questo e' un problema da correggere in quel file (finestra
mobile), perche' altrimenti la rarita' calcolata qui perde progressivamente
significato man mano che il bot accumula storico nel tempo.
"""

import logging

logger = logging.getLogger("alerting.scoring")


def discount_score(price: float, avg_price: float, stddev_price: float, tolerance_pct: float) -> float:
    """
    Punteggio di sconto, con una banda di tolleranza: un prezzo entro +/- tolerance_pct
    dalla media e' considerato 'normale' (score vicino a 0), non un'occasione.
    Solo sotto la banda inizia a contare come vero sconto.
    """
    if price < 0:
        logger.warning(f"discount_score ricevuto un prezzo negativo ({price}), trattato come 0")
        price = 0.0

    if avg_price <= 0:
        return 0.0

    tolerance_band = avg_price * tolerance_pct
    effective_avg = avg_price - tolerance_band

    if stddev_price <= 0:
        stddev_price = avg_price * 0.1

    z = (effective_avg - price) / stddev_price
    return max(0.0, min(1.0, z / 3))


def rarity_score(sample_size: int, max_expected_sample: int = 50) -> float:
    """
    ATTENZIONE: vedi nota nel docstring del modulo -- sample_size dovrebbe essere
    un conteggio finestrato nel tempo, non cumulativo a vita.
    """
    if sample_size < 0:
        logger.warning(f"rarity_score ricevuto sample_size negativo ({sample_size}), trattato come 0")
        sample_size = 0

    return max(0.0, min(1.0, 1 - (sample_size / max_expected_sample)))


def freshness_score(minutes_since_listed: float, decay_minutes: float = 60) -> float:
    if minutes_since_listed < 0:
        logger.warning(
            f"freshness_score ricevuto minutes_since_listed negativo ({minutes_since_listed:.1f}), "
            f"possibile disallineamento di orologio -- trattato come 0 (massima freschezza)"
        )
        minutes_since_listed = 0.0

    return max(0.0, min(1.0, 1 - (minutes_since_listed / decay_minutes)))


def trend_adjustment(trend_slope: float) -> float:
    """
    Corregge lo score in base al trend di mercato per quella keyword/condizione:
    - trend BEARISH (prezzi in discesa nel tempo): lo sconto rilevato potrebbe
      essere semplicemente il mercato che scende (es. articolo fuori moda),
      non una vera occasione -> penalizziamo leggermente
    - trend BULLISH (prezzi in salita): un prezzo scontato ora e' probabilmente
      un'occasione piu' genuina (va controcorrente) -> bonus leggero
    Il fattore e' intenzionalmente contenuto (max +/-15%) per non dominare lo score.
    """
    normalized = max(-1.0, min(1.0, trend_slope))
    return 1.0 + (normalized * 0.15)


def opportunity_score(
    price: float, avg_price: float, stddev_price: float,
    sample_size: int, minutes_since_listed: float,
    tolerance_pct: float = 0.08, trend_slope: float = 0.0,
    weights: dict = None
) -> float:
    weights = weights or {"discount": 0.5, "rarity": 0.2, "freshness": 0.3}

    d = discount_score(price, avg_price, stddev_price, tolerance_pct)
    r = rarity_score(sample_size)
    f = freshness_score(minutes_since_listed)

    base_score = d * weights["discount"] + r * weights["rarity"] + f * weights["freshness"]
    return max(0.0, min(1.0, base_score * trend_adjustment(trend_slope)))


def score_breakdown(
    price: float, avg_price: float, stddev_price: float,
    sample_size: int, minutes_since_listed: float,
    tolerance_pct: float = 0.08, trend_slope: float = 0.0,
    weights: dict = None
) -> dict:
    """
    Come opportunity_score, ma ritorna ogni componente separatamente -- utile
    per il tuning manuale dei pesi osservando casi reali, senza dover
    ricalcolare a mano ogni singolo fattore.
    """
    weights = weights or {"discount": 0.5, "rarity": 0.2, "freshness": 0.3}

    d = discount_score(price, avg_price, stddev_price, tolerance_pct)
    r = rarity_score(sample_size)
    f = freshness_score(minutes_since_listed)
    trend_mult = trend_adjustment(trend_slope)

    base_score = d * weights["discount"] + r * weights["rarity"] + f * weights["freshness"]
    final_score = max(0.0, min(1.0, base_score * trend_mult))

    return {
        "discount_score": d,
        "rarity_score": r,
        "freshness_score": f,
        "trend_multiplier": trend_mult,
        "base_score_pre_trend": base_score,
        "final_score": final_score,
    }
