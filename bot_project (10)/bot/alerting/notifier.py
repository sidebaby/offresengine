"""
Invio REALE degli alert su Telegram. Rispetta il rate limit di ~1 messaggio
al secondo verso la stessa chat, mantiene una coda di retry ordinata per
priorita' e persistita su disco (sopravvive ai riavvii del processo), tronca
in modo sicuro i messaggi troppo lunghi, ed e' pronto per una futura
formattazione Markdown senza rompersi sui caratteri speciali.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import httpx

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("alerting.notifier")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

MIN_SECONDS_BETWEEN_MESSAGES = 1.0
MAX_RETRY_ATTEMPTS = 3
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TRUNCATION_SUFFIX = "\n\n[...] messaggio troncato, apri il link per i dettagli completi"

# Priorita' usata per ordinare la coda di retry: valori piu' bassi = piu' urgenti
_PRIORITY_ORDER = {"alta": 0, "bassa": 1}

# Persistenza della coda: solo libreria standard (json/pathlib), nessuna nuova
# dipendenza esterna. Un file accanto al modulo, non nel vero database, perche'
# e' uno stato transitorio (alert in attesa di reinvio), non un dato di business.
_PENDING_QUEUE_FILE = Path(__file__).parent / "pending_alerts.json"

_retry_queue: list["PendingAlert"] = []
_send_lock = asyncio.Lock()
_last_sent_at = 0.0

_http_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


@dataclass
class PendingAlert:
    message: str
    priority: str
    attempts: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PendingAlert":
        return cls(
            message=d["message"], priority=d["priority"],
            attempts=d.get("attempts", 0),
            created_at=datetime.fromisoformat(d["created_at"]),
        )


def _mask_token(text: str) -> str:
    """
    Rimuove il token Telegram da qualunque stringa prima di loggarla, nel caso
    un'eccezione di rete includa l'URL completo nel proprio messaggio -- protegge
    da leak accidentali del token nei log del server.
    """
    if TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_TOKEN in text:
        return text.replace(TELEGRAM_BOT_TOKEN, "***TOKEN_NASCOSTO***")
    return text


def _truncate_message(message: str) -> str:
    if len(message) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return message
    available = TELEGRAM_MAX_MESSAGE_LENGTH - len(TRUNCATION_SUFFIX)
    logger.warning(f"Messaggio alert troppo lungo ({len(message)} caratteri), troncato a {available}")
    return message[:available] + TRUNCATION_SUFFIX


def _escape_for_future_markdown(text: str) -> str:
    """
    Non usata oggi (inviamo testo semplice), ma pronta per quando/se attiveremo
    parse_mode Markdown: MarkdownV2 richiede l'escape di caratteri speciali,
    altrimenti l'invio fallisce se un titolo scaricato dal marketplace li contiene.
    """
    special_chars = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special_chars else c for c in text)


def _load_persisted_queue() -> None:
    """Ricarica la coda di retry da disco all'avvio, se un riavvio l'aveva interrotta."""
    global _retry_queue
    if not _PENDING_QUEUE_FILE.exists():
        return
    try:
        raw = json.loads(_PENDING_QUEUE_FILE.read_text())
        _retry_queue = [PendingAlert.from_dict(d) for d in raw]
        if _retry_queue:
            logger.info(f"Ripristinati {len(_retry_queue)} alert in coda da un riavvio precedente")
    except (json.JSONDecodeError, KeyError, OSError) as e:
        logger.warning(f"Impossibile leggere la coda persistita ({e}), riparto con coda vuota")
        _retry_queue = []


def _persist_queue() -> None:
    """Salva lo stato attuale della coda su disco, cosi' sopravvive a un riavvio del processo."""
    try:
        _PENDING_QUEUE_FILE.write_text(json.dumps([a.to_dict() for a in _retry_queue]))
    except OSError as e:
        logger.warning(f"Impossibile persistere la coda di retry su disco: {e}")


async def _get_http_client() -> httpx.AsyncClient:
    """Client HTTP condiviso e riusato, invece di aprirne uno nuovo per ogni messaggio."""
    global _http_client
    async with _client_lock:
        if _http_client is None:
            _http_client = httpx.AsyncClient(timeout=10.0)
    return _http_client


async def close_notifier() -> None:
    """Da chiamare allo shutdown del processo, per chiudere il client HTTP condiviso."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


async def send_alert(message: str, priority: str = "alta") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning(
            "TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti: alert NON inviato. "
            "Imposta le variabili d'ambiente prima dell'avvio in produzione."
        )
        return False

    safe_message = _truncate_message(message)
    pending = PendingAlert(message=safe_message, priority=priority)
    success = await _try_send(pending)

    if not success:
        _enqueue_by_priority(pending)
        _persist_queue()
        logger.warning(f"Alert non inviato, aggiunto alla coda di retry ({len(_retry_queue)} in attesa)")

    return success


def _enqueue_by_priority(pending: "PendingAlert") -> None:
    insert_at = len(_retry_queue)
    for i, existing in enumerate(_retry_queue):
        if _PRIORITY_ORDER.get(pending.priority, 1) < _PRIORITY_ORDER.get(existing.priority, 1):
            insert_at = i
            break
    _retry_queue.insert(insert_at, pending)


async def _try_send(pending: PendingAlert) -> bool:
    global _last_sent_at
    async with _send_lock:
        loop_time = asyncio.get_event_loop().time()
        wait = MIN_SECONDS_BETWEEN_MESSAGES - (loop_time - _last_sent_at)
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            client = await _get_http_client()
            response = await client.post(
                TELEGRAM_API_URL,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": pending.message,
                    "disable_web_page_preview": False,
                },
            )
        except httpx.TimeoutException:
            logger.warning("Timeout durante l'invio a Telegram")
            return False
        except httpx.RequestError as e:
            logger.warning(f"Errore di rete verso Telegram: {_mask_token(str(e))}")
            return False

        _last_sent_at = asyncio.get_event_loop().time()

        if response.status_code == 200:
            logger.info(f"Alert inviato (priorita': {pending.priority})")
            return True

        if response.status_code == 429:
            retry_after = response.json().get("parameters", {}).get("retry_after", 5)
            logger.warning(f"Rate limit Telegram, attendo {retry_after}s")
            await asyncio.sleep(retry_after)
            return False

        if response.status_code == 400:
            logger.warning(
                f"Telegram ha rifiutato il messaggio (400 Bad Request): "
                f"{_mask_token(response.text)}. Non verra' ritentato."
            )
            return True  # non ha senso ritentare un messaggio malformato

        logger.warning(f"Telegram ha risposto con status {response.status_code}: {_mask_token(response.text)}")
        return False


async def flush_retry_queue():
    if not _retry_queue:
        _load_persisted_queue()  # copre il caso di un riavvio: ricarica prima di dire "niente da fare"
        if not _retry_queue:
            return

    logger.info(f"Elaborazione coda retry: {len(_retry_queue)} alert in attesa (ordinati per priorita')")
    still_pending = []

    for pending in _retry_queue:
        pending.attempts += 1
        success = await _try_send(pending)

        if not success and pending.attempts < MAX_RETRY_ATTEMPTS:
            still_pending.append(pending)
        elif not success:
            logger.warning(
                f"Alert scartato dopo {MAX_RETRY_ATTEMPTS} tentativi falliti "
                f"(priorita'={pending.priority}, creato alle {pending.created_at})"
            )

    _retry_queue[:] = still_pending
    _persist_queue()


def get_pending_count() -> int:
    return len(_retry_queue)
