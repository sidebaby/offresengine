"""
Modulo di sicurezza centralizzato: mascheramento segreti, redazione di
dizionari per log sicuri, controllo permessi file sensibili. Riusabile da
qualunque modulo del progetto (settings, bootstrap, database, ecc.),
invece di duplicare questa logica in ogni file.
"""

import os
import stat
import logging

logger = logging.getLogger("config.security")

_SENSITIVE_KEY_MARKERS = ("token", "secret", "password", "chat_id", "client_id", "key")


def mask_secret(value: str | None, visible_chars: int = 4) -> str:
    """Mostra solo gli ultimi N caratteri di un segreto, per log/debug sicuri."""
    if not value:
        return "(non impostato)"
    if len(value) <= visible_chars:
        return "*" * len(value)
    return "*" * (len(value) - visible_chars) + value[-visible_chars:]


def redact_dict(d: dict) -> dict:
    """
    Ritorna una copia del dizionario con i valori delle chiavi 'sensibili'
    (token, secret, password, chat_id, client_id, key) mascherati -- utile
    per loggare interi oggetti di configurazione/richiesta senza rischiare
    di stampare segreti per errore.
    """
    redacted = {}
    for k, v in d.items():
        if any(marker in k.lower() for marker in _SENSITIVE_KEY_MARKERS):
            redacted[k] = mask_secret(str(v)) if v else v
        else:
            redacted[k] = v
    return redacted


def check_file_permissions(path: str) -> list[str]:
    """
    Controlla che un file sensibile (es. .env) non sia leggibile da altri
    utenti del sistema. Ritorna una lista di warning testuali (non logga
    direttamente, per rispettare l'ordine di inizializzazione del logging
    nel resto del progetto -- il chiamante decide quando mostrarli).
    Su Windows i permessi POSIX non si applicano: il controllo viene saltato.
    """
    warnings = []
    if not os.path.exists(path):
        return warnings

    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            warnings.append(
                f"Il file '{path}' e' leggibile da altri utenti del sistema "
                f"(permessi attuali: {oct(mode)}). Su Linux/server esegui: chmod 600 {path}"
            )
    except OSError as e:
        warnings.append(f"Impossibile controllare i permessi di '{path}': {e}")

    return warnings
