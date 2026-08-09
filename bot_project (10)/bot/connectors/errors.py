"""
Eccezioni condivise da tutti i connector (eBay, Wallapop, Vinted). Lo
scheduler le intercetta per TIPO (non piu' per testo) e decide il log/retry
di conseguenza.

NOTA SU str(exception) vs .message: str(exception) include il prefisso
"[platform]" (utile per i log), mentre .message contiene solo il testo senza
prefisso (utile se un chiamante vuole ricomporre il messaggio diversamente).
La differenza e' intenzionale, non un'inconsistenza.
"""

__all__ = [
    "ConnectorError", "RateLimitError", "BlockedError", "AuthError",
    "TimeoutErrorConnector", "UnexpectedResponseError",
]

# Limite di sanita' per retry_after: oltre 1 ora non ha senso operativo per
# questo bot (i cicli di scan sono ogni 2-3 ore), e un valore negativo indica
# quasi certamente un errore di parsing a monte, non un'attesa reale richiesta.
_MAX_SANE_RETRY_AFTER_SECONDS = 3600


class ConnectorError(Exception):
    """Classe base: tutte le eccezioni dei connector ereditano da questa."""

    is_retryable: bool = True  # di default assumiamo che valga la pena ritentare al prossimo ciclo

    def __init__(self, platform: str, message: str):
        self.platform = platform
        self.message = message
        super().__init__(f"[{platform}] {message}")


class RateLimitError(ConnectorError):
    """Troppe richieste: il sito chiede di rallentare."""

    is_retryable = True

    def __init__(self, platform: str, retry_after: int | None = None):
        if retry_after is not None:
            if retry_after < 0 or retry_after > _MAX_SANE_RETRY_AFTER_SECONDS:
                retry_after = None  # valore non sensato, meglio ignorarlo che propagarlo

        self.retry_after = retry_after
        msg = "troppe richieste, riprovero' al prossimo ciclo"
        if retry_after:
            msg += f" (il sito suggerisce di attendere {retry_after}s)"
        super().__init__(platform, msg)


class BlockedError(ConnectorError):
    """Accesso negato: probabile blocco anti-bot (403, captcha, Datadome, ecc.)."""

    is_retryable = True  # il blocco potrebbe essere temporaneo

    def __init__(self, platform: str, detail: str = "", blocking_mechanism: str | None = None):
        self.blocking_mechanism = blocking_mechanism
        parts = []
        if blocking_mechanism:
            parts.append(f"meccanismo: {blocking_mechanism}")
        if detail:
            parts.append(detail)
        suffix = f": {', '.join(parts)}" if parts else ""
        super().__init__(platform, f"accesso bloccato (anti-bot){suffix}")


class AuthError(ConnectorError):
    """
    Autenticazione fallita. Distingue esplicitamente due scenari con
    conseguenze molto diverse:
    - permanent=True: credenziali mancanti/non valide -- NON si risolve
      riprovando, serve intervento manuale sulla configurazione
    - permanent=False (default): token scaduto/rifiutato -- probabilmente
      transitorio, si risolve da solo ottenendo un nuovo token
    """

    def __init__(self, platform: str, permanent: bool = False, detail: str = ""):
        self.permanent = permanent
        self.is_retryable = not permanent  # sovrascrive l'attributo di classe a livello di istanza

        if permanent:
            msg = "credenziali mancanti o non valide: richiede intervento manuale, NON si risolvera' da solo riprovando"
        else:
            msg = "autenticazione fallita (probabile token scaduto/rifiutato), verra' rinnovato al prossimo tentativo"
        if detail:
            msg += f" ({detail})"
        super().__init__(platform, msg)


class TimeoutErrorConnector(ConnectorError):
    """
    Il sito non ha risposto in tempo. Chiamata 'TimeoutErrorConnector' e non
    semplicemente 'TimeoutError' DELIBERATAMENTE: TimeoutError e' un'eccezione
    builtin di Python -- riusare quel nome qui la ombrerebbe nello scope del
    modulo, causando bug sottili se mai un except TimeoutError (builtin)
    altrove nel codice si aspettasse di catturare quella invece di questa.
    """

    is_retryable = True

    def __init__(self, platform: str):
        super().__init__(platform, "timeout durante la richiesta")


class UnexpectedResponseError(ConnectorError):
    """Risposta ricevuta ma in un formato inatteso (struttura JSON cambiata, ecc.)."""

    is_retryable = True

    def __init__(self, platform: str, detail: str):
        super().__init__(platform, f"risposta inattesa dal sito: {detail}")
