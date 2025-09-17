# TASER 4.5 — Smart Bot (Delta/ccxt)

- Strict .env config
- Dynamic avoid zones from structure (no hard targets)
- AI **audit** + AI **risk manager** (runtime hold/exit/extend)
- Memory: liquidity heat-map snapshots & lessons from past trades
- Dashboard (FastAPI): / shows trades + events
- 5m scan loop; 1m/5m/15m/1h data for decisions
RU
Run:
```bash
pip install -r requirements.txt
cp .env.example .env  # fill values
python3 -m venv .venv
source .venv/bin/activate

python main.py
python main.py --DRY_RUN
DRY_RUN=true python main.py
# Dashboard in another terminal:
uvicorn app.dashboard:app --reload --port 8000
```
