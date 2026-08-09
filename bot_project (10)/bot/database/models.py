"""
Modelli dati condivisi da tutto il progetto. Questo file e' la fonte di
verita' per "cos'e' un dato valido" nel sistema -- la validazione avviene
qui (in __post_init__), non duplicata nei singoli connector.

DIREZIONE DI DIPENDENZA: questo file NON importa nulla da connectors/ --
e' connectors/ a dipendere da database/ (per Item e CANONICAL_CONDITIONS),
mai il contrario. "Quali condizioni esistono" e' un concetto di dominio
dati (schema), non un dettaglio implementativo di un singolo connector --
per questo CANONICAL_CONDITIONS vive qui, non in connectors/normalize.py.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Fonte di verita' UNICA per i tier di condizione validi. connectors/normalize.py
# e config/bootstrap_keywords.py importano questa lista (direttamente o
# transitivamente) invece di mantenerne copie proprie -- un disallineamento
# simile aveva gia' causato un bug critico (vedi audit di normalize.py).
CANONICAL_CONDITIONS = ["new_with_tags", "new_without_tags", "very_good", "good", "satisfactory"]
ALL_CONDITIONS = CANONICAL_CONDITIONS + ["unknown"]

DEFAULT_CURRENCY = "EUR"


@dataclass(frozen=True)
class Item:
    """
    Un singolo articolo osservato su una piattaforma in un momento preciso.
    Immutabile: rappresenta uno SNAPSHOT, non un oggetto che cambia nel tempo
    (un prezzo aggiornato e' un nuovo Item, non una mutazione di uno esistente).
    condition, se presente, deve essere uno dei tier in ALL_CONDITIONS --
    MAI una stringa grezza del sito (quella normalizzazione e' responsabilita'
    del connector, tramite normalize_condition, PRIMA di costruire questo oggetto).
    """
    platform_item_id: str
    platform: str
    keyword: str
    title: str
    price: float
    condition: Optional[str]
    url: str
    interest_count: Optional[int]
    first_seen_at: datetime
    last_seen_at: datetime
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self):
        if self.price < 0:
            raise ValueError(f"Item '{self.title}': price negativo ({self.price})")

        for field_name in ("platform_item_id", "platform", "title", "url"):
            value = getattr(self, field_name)
            if not value or not value.strip():
                raise ValueError(f"Item.{field_name} non puo' essere vuoto")

        if self.interest_count is not None and self.interest_count < 0:
            raise ValueError(f"Item '{self.title}': interest_count negativo ({self.interest_count})")

        if self.condition is not None and self.condition not in ALL_CONDITIONS:
            raise ValueError(
                f"Item '{self.title}': condition {self.condition!r} non e' tra i valori validi "
                f"{ALL_CONDITIONS} -- probabile bug: il connector ha passato una stringa grezza "
                f"senza chiamare normalize_condition() prima."
            )

        if self.first_seen_at > self.last_seen_at:
            raise ValueError(
                f"Item '{self.title}': first_seen_at ({self.first_seen_at}) non puo' essere "
                f"successivo a last_seen_at ({self.last_seen_at})"
            )

    @property
    def age_minutes(self) -> float:
        """Minuti trascorsi da quando l'item e' stato visto la prima volta, ad ora."""
        return (datetime.utcnow() - self.first_seen_at).total_seconds() / 60


@dataclass(frozen=True)
class PricePoint:
    """Un singolo prezzo osservato nel tempo, per un item specifico."""
    price: float
    observed_at: datetime

    def __post_init__(self):
        if self.price < 0:
            raise ValueError(f"PricePoint: price negativo ({self.price})")


@dataclass(frozen=True)
class KeywordStats:
    """
    Statistiche SEGMENTATE: la chiave reale e' (keyword, platform, condition),
    non solo (keyword, platform) -- evita di mescolare prezzi di condizioni
    diverse nella stessa media.

    avg_price=0.0 con sample_size=0 e' il valore SENTINELLA per "nessun dato
    ancora disponibile per questo segmento" -- usato deliberatamente in tutto
    il progetto, non un errore di validazione.
    """
    keyword: str
    platform: str
    condition: str
    avg_price: float
    stddev_price: float
    sample_size: int
    tolerance_pct: float = 0.08
    trend_slope: float = 0.0
    currency: str = DEFAULT_CURRENCY
    last_updated_at: Optional[datetime] = None  # colma il gap con la colonna SQL 'updated_at'

    def __post_init__(self):
        if self.avg_price < 0:
            raise ValueError(f"KeywordStats '{self.keyword}': avg_price negativo ({self.avg_price})")
        if self.stddev_price < 0:
            raise ValueError(f"KeywordStats '{self.keyword}': stddev_price negativo ({self.stddev_price})")
        if self.sample_size < 0:
            raise ValueError(f"KeywordStats '{self.keyword}': sample_size negativo ({self.sample_size})")
        if not (0 <= self.tolerance_pct < 1):
            raise ValueError(f"KeywordStats '{self.keyword}': tolerance_pct fuori range [0,1) ({self.tolerance_pct})")
        if not (-1.0 <= self.trend_slope <= 1.0):
            raise ValueError(f"KeywordStats '{self.keyword}': trend_slope fuori range [-1,1] ({self.trend_slope})")

    @property
    def has_baseline(self) -> bool:
        """True se questo segmento ha almeno un campione su cui basare un confronto onesto."""
        return self.sample_size > 0

    @property
    def is_empty(self) -> bool:
        """Inverso di has_baseline -- entrambi disponibili per leggibilita' a seconda del contesto."""
        return not self.has_baseline

    @property
    def trend_label(self) -> str:
        """
        Classificazione leggibile del trend, centralizzata qui invece che
        duplicata (con le stesse soglie magiche) in scheduler.py.
        """
        if self.trend_slope < -0.05:
            return "bearish"
        if self.trend_slope > 0.05:
            return "bullish"
        return "stabile"

    @property
    def staleness_hours(self) -> Optional[float]:
        """Da quante ore questa statistica non viene aggiornata (None se mai aggiornata)."""
        if self.last_updated_at is None:
            return None
        return (datetime.utcnow() - self.last_updated_at).total_seconds() / 3600


@dataclass
class ItemPriceHistory:
    """
    Storico completo dei prezzi visti per un singolo item nel tempo.
    NON immutabile (a differenza di Item/PricePoint/KeywordStats): questo e'
    un AGGREGATO che cresce nel tempo (nuovi PricePoint vengono aggiunti man
    mano che il bot rileva nuove osservazioni sullo stesso articolo).
    """
    platform: str
    platform_item_id: str
    points: list[PricePoint] = field(default_factory=list)

    def add_point(self, price: float, observed_at: datetime) -> None:
        """Aggiunge un nuovo punto storico, mantenendo l'incapsulamento invece
        di manipolare .points direttamente dall'esterno."""
        self.points.append(PricePoint(price=price, observed_at=observed_at))

    @property
    def initial_price(self) -> Optional[float]:
        return self.points[0].price if self.points else None

    @property
    def current_price(self) -> Optional[float]:
        return self.points[-1].price if self.points else None

    @property
    def highest_price_seen(self) -> Optional[float]:
        return max((p.price for p in self.points), default=None)

    @property
    def lowest_price_seen(self) -> Optional[float]:
        return min((p.price for p in self.points), default=None)

    @property
    def tracking_duration_days(self) -> Optional[float]:
        """Per quanti giorni questo item e' stato osservato, dal primo all'ultimo avvistamento."""
        if len(self.points) < 2:
            return None
        delta = self.points[-1].observed_at - self.points[0].observed_at
        return delta.total_seconds() / 86400

    @property
    def cumulative_discount_pct(self) -> Optional[float]:
        """
        Quanto e' sceso (o salito) rispetto al primo prezzo visto, in percentuale.
        Ritorna None se ci sono meno di 2 punti O se il prezzo iniziale era 0
        (articolo gratuito -- evitiamo la divisione per zero).
        """
        if not self.points or len(self.points) < 2:
            return None
        if self.initial_price == 0:
            return None
        return (self.initial_price - self.current_price) / self.initial_price * 100


@dataclass(frozen=True)
class MonitoredKeyword:
    """
    PREPARAZIONE per la lista keyword dinamica discussa in config/settings.py
    (get_active_keywords()). Non ancora collegato a query reali nel database
    -- puro schema, in attesa della sessione dedicata alla tabella 'keywords'.
    """
    keyword: str
    added_at: datetime
    is_bootstrap_seed: bool = False
    is_active: bool = True

    def __post_init__(self):
        if not self.keyword or not self.keyword.strip():
            raise ValueError("MonitoredKeyword: keyword non puo' essere vuota")


@dataclass(frozen=True)
class AlertRecord:
    """
    Rappresenta una riga della tabella alerts_sent -- oggi gestita in
    database/queries.py in modo grezzo, modellata qui per coerenza con il
    resto del sistema (ogni dato persistito ha un dataclass corrispondente).
    """
    platform_item_id: str
    score: float
    sent_at: datetime
    priority: Optional[str] = None

    def __post_init__(self):
        if not (0 <= self.score <= 1):
            raise ValueError(f"AlertRecord: score fuori range [0,1] ({self.score})")
