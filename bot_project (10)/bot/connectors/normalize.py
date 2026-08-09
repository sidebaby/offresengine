"""
Normalizza le etichette di condizione, diverse da sito a sito, in un set
standard usato per la segmentazione delle statistiche di prezzo.

IMPORTANTE -- USO DUALE DI QUESTO MODULO: normalize_condition() riceve DUE
tipi di input diversi a seconda del chiamante:
1. Stringhe GREZZE restituite dai siti (es. "For parts or not working" da
   eBay, "molto buono" da Vinted) -- il caso "tipico".
2. I NOSTRI STESSI nomi di tier interni (es. "very_good", "good") quando un
   connector ha gia' filtrato la ricerca per condizione lato server (vedi
   connectors/vinted.py: se la ricerca era per il tier 'very_good', l'item
   eredita quella condizione chiamando normalize_condition('very_good')).
Per questola mappa contiene ANCHE i nomi dei tier come chiavi identita'
(es. "very_good": "very_good") -- non e' ridondanza, e' necessario per il
caso 2. Se mai rinomini un tier, aggiorna la mappa in entrambi i punti.

IMPORTANTE -- 5 TIER, NON 4: CANONICAL_CONDITIONS ora vive in database/models.py
(fonte di verita' dello SCHEMA dati), non piu' qui -- questo file possiede la
logica di TRASFORMAZIONE (come mappare stringhe grezze dei siti in quei tier),
non la definizione di quali tier sono validi. Una versione precedente di
questo file duplicava la lista localmente, disallineandosi silenziosamente
dal resto del progetto -- vedi audit precedente per i dettagli del bug.
"""

import logging

from database.models import CANONICAL_CONDITIONS

logger = logging.getLogger("connectors.normalize")

_CONDITION_MAP = {
    # --- Identita': i nostri stessi nomi di tier (vedi nota "USO DUALE" sopra) ---
    "new_with_tags": "new_with_tags",
    "new_without_tags": "new_without_tags",
    "very_good": "very_good",
    "good": "good",
    "satisfactory": "satisfactory",

    # --- eBay (stringhe gia' trasformate spazi->underscore da connectors/ebay.py) ---
    "new": "new_with_tags",              # eBay "New" -- l'analogo piu' vicino a "nuovo con cartellino"
    "new_other": "new_without_tags",     # eBay "New other" -- nuovo ma senza confezione/cartellino originali
    "used": "good",                      # eBay non distingue gradazioni di usato: bucket intermedio onesto
    "for_parts_or_not_working": "satisfactory",  # analogo piu' vicino al tier piu' basso disponibile

    # --- Vinted / Wallapop (etichette utente in italiano) ---
    "nuovo con cartellino": "new_with_tags",
    "nuovo senza cartellino": "new_without_tags",
    "molto buono": "very_good",
    "buono": "good",
    "soddisfacente": "satisfactory",

    # --- Vinted / Wallapop (possibili varianti inglesi -- NON verificate dal vivo,
    # vedi nota di onesta' sotto) ---
    "new with tags": "new_with_tags",
    "new without tags": "new_without_tags",
    "very good": "very_good",
}

# Stringhe gia' segnalate come non riconosciute in questo processo, per evitare
# di inondare i log ripetendo lo stesso avviso per ogni singolo articolo con
# lo stesso formato inatteso -- un avviso per stringa distinta e' sufficiente
# per farti sapere che la mappa va aggiornata.
_already_warned: set[str] = set()


def normalize_condition(raw: str | None) -> str:
    """
    NOTA DI ONESTA': la corrispondenza esatta delle stringhe restituite da
    Wallapop non e' stata verificata con una chiamata reale (nessun accesso
    di rete in questo ambiente di sviluppo) -- se dopo il primo test dal vivo
    vedi molti item Wallapop finire su "unknown", e' il primo posto da
    controllare: probabilmente il formato reale differisce da quanto assunto qui.
    """
    if not raw:
        return "unknown"

    key = raw.strip().lower()
    result = _CONDITION_MAP.get(key)

    if result is None:
        if key not in _already_warned:
            logger.warning(
                f"Condizione non riconosciuta: '{raw}' (normalizzata a '{key}') -- "
                f"trattata come 'unknown'. Se questo si ripete spesso, aggiungi una "
                f"voce a _CONDITION_MAP in questo file."
            )
            _already_warned.add(key)
        return "unknown"

    return result
