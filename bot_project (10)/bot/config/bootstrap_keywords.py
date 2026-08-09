"""
Lista di partenza per il bootstrap, basata su dati di mercato reali (non una
classifica ufficiale, che non esiste per queste piattaforme) -- modificabile
liberamente in base alla conoscenza diretta del mercato reseller.

RELAZIONE CON settings.py: questa lista (BOOTSTRAP_KEYWORDS) serve a costruire
la BASELINE iniziale di prezzi (scansione profonda, una tantum o periodica),
mentre KEYWORDS/get_active_keywords() in settings.py serve al monitoraggio
CONTINUO per gli alert. Sono intenzionalmente separate: puoi voler costruire
una baseline ampia (tanti modelli, per capire il mercato) mentre monitori
attivamente solo un sottoinsieme più mirato per gli alert. Se in futuro vuoi
che ogni keyword monitorata riceva automaticamente anche un bootstrap, andra'
costruita un'unione esplicita altrove (es. in scheduler/bootstrap.py), non
qui: questo file resta la sorgente del bootstrap, non del monitoraggio.

CONDITION_TIERS ora importato da connectors/normalize.py (CANONICAL_CONDITIONS),
non piu' una copia locale -- evita il disallineamento silenzioso che aveva
causato la vanificazione della segmentazione a 5 tier (vedi audit di normalize.py).
"""

from connectors.normalize import CANONICAL_CONDITIONS

_RAW_BOOTSTRAP_KEYWORDS = [
    "nike air force 1",
    "nike dunk low",
    "adidas samba",
    "new balance 550",
    "levi's 501",
    "carhartt detroit jacket",
    "the north face nuptse",
    "dr martens 1460",
    "ralph lauren polo",
    "patagonia retro pile",
]

# Normalizzate (lowercase, spazi ripuliti, deduplicate) per evitare che un
# errore di battitura o maiuscole incoerenti creino segmenti duplicati distinti
BOOTSTRAP_KEYWORDS = sorted(set(k.strip().lower() for k in _RAW_BOOTSTRAP_KEYWORDS))

CONDITION_TIERS = CANONICAL_CONDITIONS

SAMPLES_PER_SEGMENT = 3          # quante inserzioni raccogliere per ogni keyword+piattaforma+condizione

# ATTENZIONE: questo valore era stato abbassato a 0.02 DURANTE I TEST in sandbox
# per velocizzare le prove con connector finti (nessuna rete reale coinvolta).
# Ripristinato al valore di produzione sicuro -- NON abbassarlo per un lancio
# reale, anche "solo per una prova veloce": e' il modo piu' diretto per farsi
# bloccare l'IP su tutte le piattaforme nel giro di pochi secondi.
BOOTSTRAP_DELAY_SECONDS = 1.5

OUTLIER_TOLERANCE_PCT = 0.30     # oltre il 30% di scostamento dalla mediana del segmento = outlier

# Numero di piattaforme note dal resto del sistema -- usato solo per la stima
# del costo qui sotto, non e' la fonte di verita' (quella e' in scheduler/bootstrap.py)
_KNOWN_PLATFORM_COUNT = 3


def estimate_total_requests(num_platforms: int = _KNOWN_PLATFORM_COUNT) -> int:
    """
    Quante richieste HTTP genererebbe un bootstrap completo con la
    configurazione attuale. Utile da controllare PRIMA di lanciare,
    specialmente dopo aver modificato la lista keyword o i tier di condizione.
    """
    return len(BOOTSTRAP_KEYWORDS) * len(CONDITION_TIERS) * num_platforms * SAMPLES_PER_SEGMENT


def validate_bootstrap_config() -> None:
    """
    Da chiamare esplicitamente prima di avviare scheduler/bootstrap.py --
    stesso pattern di validate_required_secrets() in settings.py.
    """
    errors = []

    if not BOOTSTRAP_KEYWORDS:
        errors.append("BOOTSTRAP_KEYWORDS e' vuota: nessun bootstrap da eseguire")

    if not CONDITION_TIERS:
        errors.append("CONDITION_TIERS e' vuota: nessuna condizione da segmentare")

    if SAMPLES_PER_SEGMENT <= 0:
        errors.append(f"SAMPLES_PER_SEGMENT={SAMPLES_PER_SEGMENT} deve essere positivo")

    if BOOTSTRAP_DELAY_SECONDS < 0.5:
        errors.append(
            f"BOOTSTRAP_DELAY_SECONDS={BOOTSTRAP_DELAY_SECONDS} e' pericolosamente basso per l'uso "
            f"su siti reali (rischio concreto di blocco IP). Valore minimo consigliato: 0.5s, "
            f"raccomandato 1.5s o superiore."
        )

    if not (0 < OUTLIER_TOLERANCE_PCT < 1):
        errors.append(f"OUTLIER_TOLERANCE_PCT={OUTLIER_TOLERANCE_PCT} deve essere tra 0 e 1 (escluso)")

    if errors:
        raise RuntimeError(
            "Configurazione bootstrap non valida, correggi prima di avviare:\n  - " + "\n  - ".join(errors)
        )
