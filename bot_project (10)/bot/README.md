# Bot di monitoraggio marketplace

## Setup

1. `pip install -r requirements.txt`
2. `playwright install chromium` (scarica il browser usato dal connector Vinted)
3. Copia `.env.example` in `.env` e compila i valori reali (token Telegram, credenziali eBay)
4. Carica le variabili d'ambiente prima dell'avvio, ad esempio con `python-dotenv`
   aggiungendo in cima a `scheduler/main.py`:
   ```python
   from dotenv import load_dotenv
   load_dotenv()
   ```
   oppure esportandole manualmente nel terminale/servizio di hosting (es. Fly.io secrets).

## Avvio

- Scan periodico continuo: `python -m scheduler.main` (di default esegue 1 ciclo, vedi `cycles=` in `startup_and_run`)
- Bootstrap iniziale (baseline segmentata): `python -m scheduler.bootstrap`

## Note

- Il connector Vinted richiede Chromium installato via Playwright (vedi setup).
- I selettori CSS di Vinted e i conditionIds di eBay sono stati scritti senza possibilità di
  verifica dal vivo (ambiente di sviluppo senza rete) — testali con una chiamata reale prima
  di considerarli definitivi.
- Il database SQLite viene creato automaticamente al primo avvio in `database/bot.db`.
