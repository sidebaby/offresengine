"""
Configurazione centrale. I segreti (token/credenziali) vengono letti SOLO da
variabili d'ambiente, mai scritti qui. Il file .env (non versionato) viene
caricato automaticamente da questo modulo stesso.

Il mascheramento segreti vive in config/security.py (riusabile da tutto il
progetto). La mappatura paese->piattaforme vive in config/marketplaces.py
(dipende da questo modulo, mai il contrario -- nessun import circolare).
"""

import os
import logging

from config.security import mask_secret, check_file_permissions

logger = logging.getLogger("config.settings")

# --- Caricamento .env automatico e sicuro ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Warning raccolti durante il parsing, mostrati da print_startup_summary()
# DOPO che logging.basicConfig() e' stato chiamato altrove -- mai loggare
# direttamente qui a livello di import.
_startup_warnings: list[str] = []


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        _startup_warnings.append(f"{name}='{raw}' non e' un numero valido, uso il default {default}")
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _startup_warnings.append(f"{name}='{raw}' non e' un intero valido, uso il default {default}")
        return default


# --- Ambiente di esecuzione e mercato di riferimento ---
APP_ENV = os.environ.get("APP_ENV", "production")
COUNTRY = os.environ.get("COUNTRY", "IT")  # usato da config/marketplaces.py

# Wallapop e' un marketplace geolocalizzato: senza coordinate, le ricerche
# potrebbero risultare vuote o centrate su una localita' irrilevante.
# Default: Roma (centro Italia approssimativo) -- sovrascrivibile via env
# se vuoi centrare le ricerche su una citta' specifica.
WALLAPOP_LATITUDE = _env_float("WALLAPOP_LATITUDE", 41.9028)
WALLAPOP_LONGITUDE = _env_float("WALLAPOP_LONGITUDE", 12.4964)

# --- Configurazione di business (override via env opzionale, con default nel codice) ---
SCAN_INTERVAL_HOURS = _env_float("SCAN_INTERVAL_HOURS", 2.5)
ALERT_THRESHOLD = _env_float("ALERT_THRESHOLD", 0.55)
INTEREST_WARNING_MIN = _env_int("INTEREST_WARNING_MIN", 3)

# Quanti giorni di osservazioni contano per calcolare la baseline di prezzo di
# un segmento (keyword+piattaforma+condizione). Osservazioni piu' vecchie di
# questa finestra non influenzano piu' la media -- risolve il problema della
# 'memoria infinita' che degradava silenziosamente rarity_score nel tempo.
STATS_ROLLING_WINDOW_DAYS = _env_int("STATS_ROLLING_WINDOW_DAYS", 30)

# KEYWORDS e' il SEED iniziale, non la fonte di verita' a regime.
# Quando costruiremo la tabella "keywords" nel database (sessione dedicata,
# non questa), get_active_keywords() sotto verra' aggiornata per leggere da
# li' invece che da questa lista statica -- i chiamanti (scheduler, bootstrap)
# devono usare SEMPRE get_active_keywords(), mai KEYWORDS direttamente, cosi'
# quel giorno cambia una sola funzione, non ogni punto di chiamata.
KEYWORDS = ["nike air max 90", "giacca carhartt"]


def get_active_keywords() -> list[str]:
    """
    Punto di estensione unico per ottenere le keyword attive. Oggi ritorna
    semplicemente KEYWORDS; in futuro potra' leggere da un database che
    cresce nel tempo (es. aggiunte dal bootstrap o da un pannello utente)
    senza richiedere modifiche a chi la chiama.
    """
    return KEYWORDS


_KNOWN_EBAY_MARKETPLACES = {
    "EBAY_IT", "EBAY_US", "EBAY_GB", "EBAY_DE", "EBAY_FR", "EBAY_ES", "EBAY_AT", "EBAY_CH",
}

# --- Segreti: letti da env, MAI hardcoded ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET")
EBAY_MARKETPLACE_ID = os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_IT")


def _is_valid_telegram_chat_id(value: str) -> bool:
    if value.startswith("@"):
        return len(value) > 1
    try:
        int(value)
        return True
    except ValueError:
        return False


def validate_required_secrets() -> None:
    """
    Da chiamare esplicitamente all'avvio: valida presenza, formato e range
    di segreti e configurazione, fallendo con un messaggio chiaro.
    """
    errors = []

    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"):
        if not globals().get(name):
            errors.append(f"variabile mancante: {name}")

    if TELEGRAM_CHAT_ID and not _is_valid_telegram_chat_id(TELEGRAM_CHAT_ID):
        errors.append(
            f"TELEGRAM_CHAT_ID='{TELEGRAM_CHAT_ID}' non sembra un formato valido "
            f"(atteso un numero, anche negativo per i gruppi, oppure @username)"
        )

    if EBAY_MARKETPLACE_ID not in _KNOWN_EBAY_MARKETPLACES:
        errors.append(
            f"EBAY_MARKETPLACE_ID='{EBAY_MARKETPLACE_ID}' non e' tra i marketplace noti "
            f"({', '.join(sorted(_KNOWN_EBAY_MARKETPLACES))})."
        )

    if not (0 < ALERT_THRESHOLD <= 1):
        errors.append(f"ALERT_THRESHOLD={ALERT_THRESHOLD} fuori range valido (0, 1]")

    if SCAN_INTERVAL_HOURS <= 0:
        errors.append(f"SCAN_INTERVAL_HOURS={SCAN_INTERVAL_HOURS} deve essere positivo")

    if STATS_ROLLING_WINDOW_DAYS <= 0:
        errors.append(f"STATS_ROLLING_WINDOW_DAYS={STATS_ROLLING_WINDOW_DAYS} deve essere positivo")

    if INTEREST_WARNING_MIN < 0:
        errors.append(f"INTEREST_WARNING_MIN={INTEREST_WARNING_MIN} non puo' essere negativo")

    if not get_active_keywords():
        errors.append("get_active_keywords() e' vuota: il bot non avrebbe nulla da cercare")

    if errors:
        raise RuntimeError(
            "Configurazione non valida, correggi prima dell'avvio:\n  - " + "\n  - ".join(errors)
        )


def print_startup_summary() -> None:
    """Logga la configurazione attiva in modo sicuro. Chiamare dopo logging.basicConfig()."""
    for warning in _startup_warnings:
        logger.warning(f"[CONFIG] {warning}")

    for warning in check_file_permissions(".env"):
        logger.warning(f"[SICUREZZA] {warning}")

    logger.info(f"Ambiente: {APP_ENV} | Paese/mercato: {COUNTRY}")
    keywords = get_active_keywords()
    logger.info(f"Keyword monitorate: {len(keywords)} ({', '.join(keywords)})")
    logger.info(f"Intervallo scan: {SCAN_INTERVAL_HOURS}h | Soglia alert: {ALERT_THRESHOLD}")
    logger.info(f"Marketplace eBay: {EBAY_MARKETPLACE_ID}")
    logger.info(f"Telegram bot token: {mask_secret(TELEGRAM_BOT_TOKEN)}")
    logger.info(f"Telegram chat id: {mask_secret(TELEGRAM_CHAT_ID, visible_chars=2)}")
    logger.info(f"eBay client id: {mask_secret(EBAY_CLIENT_ID)}")
