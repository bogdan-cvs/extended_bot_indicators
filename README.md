# Extended Exchange Market Making Bot

Bot conservator de market making pentru Extended Exchange cu control strict de risc și inventory management.

## 📋 Cuprins

1. [Prezentare Generală](#prezentare-generală)
2. [Cerințe Sistem](#cerințe-sistem)
3. [Instalare](#instalare)
4. [Configurare](#configurare)
5. [Pornire și Utilizare](#pornire-și-utilizare)
6. [Profile de Configurație](#profile-de-configurație)
7. [Verificări Pre-Lansare](#verificări-pre-lansare)
8. [Oprire în Siguranță](#oprire-în-siguranță)
9. [Troubleshooting](#troubleshooting)
10. [Parametri și Ajustări](#parametri-și-ajustări)
11. [Avertismente de Risc](#avertismente-de-risc)
12. [Arhitectură](#arhitectură)

---

## Prezentare Generală

### Ce face acest bot?

Acest bot implementează o **strategie conservatoare de market making**:

- **Cotare pe ambele părți** (bid și ask) în jurul prețului mid
- **Spread dinamic** care se lărgește în condiții de volatilitate
- **Inventory skew** - ajustează cotările pentru a menține expunerea neutră
- **Kill switch** - oprește automat la atingerea limitei de drawdown
- **POST_ONLY orders** - asigură că suntem mereu maker (fees mai mici)

### Ce NU face acest bot?

❌ Nu face "volume churning" sau wash trading  
❌ Nu plasează ordine care să se execute între ele (anti-self-trade)  
❌ Nu folosește leverage agresiv  
❌ Nu urmărește puncte/rewards în detrimentul profitabilității  

---

## Cerințe Sistem

### Software

```
Python >= 3.10
pip >= 23.0
```

### Verificare versiune Python

```bash
python3 --version
# Output: Python 3.10.x sau mai nou
```

### Sistem de operare

- Linux (recomandat pentru producție)
- macOS
- Windows (cu WSL2 pentru producție)

---

## Instalare

### 1. Clonare Repository

```bash
git clone <repository_url>
cd extended-mm-bot
```

### 2. Creare Virtual Environment

```bash
# Creare venv
python3 -m venv venv

# Activare pe Linux/macOS
source venv/bin/activate

# Activare pe Windows
.\venv\Scripts\activate
```

### 3. Instalare Dependențe

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Verificare Instalare

```bash
python -c "import aiohttp, websockets, yaml, pydantic; print('OK')"
```

---

## Configurare

### 1. Creare Fișier .env

```bash
cp .env.example .env
```

### 2. Obținere Credențiale

#### API Key

1. Conectează-te la Extended Exchange
2. Navighează la **Settings** → **API Keys**
3. Creează o cheie nouă cu permisiuni:
   - ✅ Read
   - ✅ Trade
   - ❌ Withdraw (NU activa!)
4. Salvează API Key-ul

#### Stark Private Key

1. Din wallet-ul tău StarkNet sau
2. Din Extended Exchange → **Account** → **Export Private Key**
3. Formatul este hex: `0x...`

#### Account Address

1. Adresa contului tău StarkNet
2. Vizibilă în Extended Exchange sau în wallet

### 3. Editare .env

```bash
nano .env
```

Completează:

```env
EXTENDED_API_KEY=sk_live_xxxxxxxxxxxxx
EXTENDED_STARK_PRIVATE_KEY=0x123abc...
EXTENDED_ACCOUNT_ADDRESS=0x456def...
BOT_ENV=testnet
BOT_PROFILE=TESTNET_SAFE
```

### 4. Securitate .env

```bash
# Asigură-te că .env nu e în git
echo ".env" >> .gitignore

# Setează permisiuni restrictive
chmod 600 .env
```

---

## Pornire și Utilizare

### Comenzi Principale

#### 1. Check-Only Mode (verificare configurație)

```bash
python -m src.main --env testnet --profile TESTNET_SAFE --check-only
```

Verifică:
- ✅ Conectivitate API
- ✅ Credențiale valide
- ✅ Balance disponibil
- ✅ Market info și fees
- ✅ WebSocket connectivity

#### 2. Dry-Run Mode (fără ordine reale)

```bash
python -m src.main --env testnet --profile TESTNET_SAFE --dry-run
```

Botul rulează normal dar:
- Loghează "Would create order..." în loc să plaseze
- Loghează "Would cancel order..." în loc să anuleze
- Ideal pentru testarea logicii

#### 3. Live Testnet

```bash
python -m src.main --env testnet --profile TESTNET_SAFE
```

⚠️ Ordine REALE pe testnet (bani de test)

#### 4. Live Mainnet

```bash
python -m src.main --env mainnet --profile MAINNET_CONSERVATIVE_200USD
```

⚠️ **BANI REALI** - verifică de 3 ori înainte!

### Opțiuni CLI Complete

```bash
python -m src.main --help
```

| Opțiune | Descriere | Default |
|---------|-----------|---------|
| `--env` | `testnet` sau `mainnet` | `testnet` |
| `--profile` | Nume profil din configs/ | `TESTNET_SAFE` |
| `--config` | Path către config YAML | - |
| `--dry-run` | Simulare fără ordine | `false` |
| `--check-only` | Doar verificări, fără rulare | `false` |
| `--market` | Override market din config | - |
| `--log-level` | DEBUG/INFO/WARNING/ERROR | `INFO` |

---

## Profile de Configurație

### TESTNET_SAFE (configs/TESTNET_SAFE.yaml)

**Scop:** Validare logică pe testnet cu risc minim

| Parametru | Valoare | Explicație |
|-----------|---------|------------|
| `order_notional_usd` | $4 | Ordine foarte mici |
| `spread_min_bps` | 30 | Spread larg (0.30%) |
| `spread_max_bps` | 150 | Max 1.50% |
| `max_inventory_usd` | $15 | Inventar limitat |
| `kill_switch_drawdown` | $8 | Oprire la -$8 |
| `dry_run` | true | Default dry run |

### MAINNET_CONSERVATIVE_200USD (configs/MAINNET_CONSERVATIVE_200USD.yaml)

**Scop:** Producție conservatoare cu buget $200

| Parametru | Valoare | Explicație |
|-----------|---------|------------|
| `order_notional_usd` | $10 | ~5% din buget per ordine |
| `spread_min_bps` | 12 | Spread competitiv (0.12%) |
| `spread_max_bps` | 100 | Max 1.00% |
| `max_inventory_usd` | $50 | Max 25% din buget |
| `kill_switch_drawdown` | $20 | Oprire la -$20 (~10%) |
| `use_taker_for_risk_off` | true | IOC pentru flatten urgent |

---

## Verificări Pre-Lansare

### Checklist înainte de TESTNET

```bash
# 1. Verifică credențiale și connectivity
python -m src.main --env testnet --check-only

# 2. Verifică că market-ul există
# În logs ar trebui să vezi: "Market ETH-USDC found, tick_size=X, min_size=Y"

# 3. Rulează în dry-run 5-10 minute
python -m src.main --env testnet --profile TESTNET_SAFE --dry-run

# 4. Verifică logs pentru:
#    - "Would create BID order at X.XX"
#    - "Would create ASK order at Y.YY"
#    - Spread calculat corect
#    - Nu există erori de signing/API
```

### Checklist înainte de MAINNET

```bash
# 1. Rulează pe testnet MINIM 24 ore fără erori

# 2. Verifică balance pe mainnet
python -m src.main --env mainnet --check-only

# 3. Dry-run pe mainnet 15-30 minute
python -m src.main --env mainnet --profile MAINNET_CONSERVATIVE_200USD --dry-run

# 4. Prima rulare live - monitorizează ACTIV
python -m src.main --env mainnet --profile MAINNET_CONSERVATIVE_200USD

# 5. Setează alarme pentru:
#    - Drawdown > $10
#    - Inventory > $30
#    - Mai mult de 3 pause-uri în 10 minute
```

---

## Oprire în Siguranță

### 1. Graceful Shutdown (recomandat)

```bash
# În terminal unde rulează botul:
Ctrl+C

# Sau din alt terminal:
kill -SIGTERM <pid>
```

Botul va:
1. Anula toate ordinele deschise
2. Salva starea curentă
3. Loga metrici finale
4. Ieși curat

### 2. Emergency Stop

```bash
# Forțează oprirea imediată
kill -SIGKILL <pid>
```

⚠️ Ordinele pot rămâne deschise! Anulează manual din UI.

### 3. Mass Cancel Manual

Dacă botul a crashuit fără să anuleze ordinele:

```bash
# Script de emergency cancel (dacă e disponibil)
python -m src.main --env mainnet --cancel-all-only
```

Sau anulează din interfața Extended Exchange.

---

## Troubleshooting

### Erori Comune

#### 1. "Rate limit exceeded" (429)

```
Cauză: Prea multe request-uri API
Soluție:
- Botul are auto-backoff, așteaptă 30-60s
- Crește refresh_sec în config
- Verifică că nu rulezi multiple instanțe
```

#### 2. "Invalid signature"

```
Cauză: Stark private key greșit sau format incorect
Soluție:
- Verifică că private key-ul e în format hex (0x...)
- Verifică că e pentru account-ul corect
- Testează cu check-only mai întâi
```

#### 3. "Invalid fee"

```
Cauză: Fee trimis nu corespunde cu fee-ul expected
Soluție:
- Botul fetchează automat fees din /user/fees
- Verifică că endpoint-ul fees e accesibil
- Check-only ar trebui să arate fees actuale
```

#### 4. "Invalid expiration"

```
Cauză: Timestamp expirare în trecut sau prea îndepărtat
Soluție:
- Verifică că ceasul sistemului e sincronizat (NTP)
- Ajustează order_expiration_sec în config
```

#### 5. WebSocket Disconnect

```
Cauză: Conexiune instabilă sau server restart
Soluție:
- Botul are auto-reconnect cu exponential backoff
- Dacă se deconectează frecvent, verifică rețeaua
- Crește WS_RECONNECT_DELAY_SEC
```

#### 6. "Insufficient balance"

```
Cauză: Nu ai destul collateral pentru ordine
Soluție:
- Verifică balance în UI
- Reduce order_notional_usd
- Verifică că nu ai ordine blocate în alte markets
```

### Logs pentru Debugging

```bash
# Rulare cu log level DEBUG
python -m src.main --env testnet --profile TESTNET_SAFE --log-level DEBUG

# Salvare logs în fișier
python -m src.main --env testnet --profile TESTNET_SAFE 2>&1 | tee bot.log

# Căutare erori în log
grep -i "error\|exception\|fail" bot.log
```

---

## Parametri și Ajustări

### Parametri de Start Recomandați

| Parametru | Testnet | Mainnet Inițial | După Stabilizare |
|-----------|---------|-----------------|------------------|
| `order_notional_usd` | $4 | $10 | $12-15 |
| `spread_min_bps` | 30 | 12 | 8-10 |
| `max_inventory_usd` | $15 | $50 | $60-80 |
| `refresh_sec` | 6 | 4 | 3 |

### Cum Ajustezi în Siguranță

1. **O singură modificare la un moment dat**
2. **Rulează minim 4 ore după fiecare modificare**
3. **Monitorizează metrici înainte/după**
4. **Revert dacă performance scade**

### Metrici de Urmărit

```
1. Fill Rate = fills / quotes (target: 5-15%)
2. Spread Capture = avg(fill_price - mid) (target: > 50% din spread)
3. Inventory Utilization = avg(|inventory|) / max_inventory (target: 20-50%)
4. PnL per Fill = realized_pnl / num_fills (target: > 0)
5. Pause Frequency = pauses / hour (target: < 5)
```

---

## Avertismente de Risc

### ⚠️ RISC FINANCIAR

```
Acest software tranzacționează cu BANI REALI pe mainnet.
Poți PIERDE o parte sau TOATĂ suma investită.
Nu investi mai mult decât îți permiți să pierzi.
```

### ⚠️ FĂRĂ GARANȚII

```
Acest software este oferit "AS IS" fără nicio garanție.
Performance trecută nu garantează rezultate viitoare.
Tu ești singurul responsabil pentru pierderile tale.
```

### ⚠️ BUNE PRACTICI

1. **Începe cu testnet** - minimum 24-48 ore
2. **Dry-run pe mainnet** - minimum 30-60 minute
3. **Monitorizare activă** - primele zile, verifică la fiecare 30 min
4. **Kill switch setat corect** - nu-l dezactiva!
5. **Backup credențiale** - în loc sigur, offline
6. **Nu share private key** - niciodată, cu nimeni

### ⚠️ COMPLIANCE

- Acest bot NU face wash trading
- Nu are logică pentru "volume churning"
- Respectă regulile Extended Exchange
- Folosește exclusiv market making legitim

---

## Arhitectură

### Structura Proiectului

```
extended-mm-bot/
├── src/
│   ├── __init__.py       # Package init
│   ├── config.py         # Configurare și profile loader
│   ├── signing.py        # Stark signatures pentru ordine
│   ├── api_client.py     # REST API client async
│   ├── ws_client.py      # WebSocket streaming
│   ├── risk.py           # Risk manager, kill switch
│   ├── order_manager.py  # Order lifecycle management
│   ├── strategy_mm.py    # Logica de market making
│   ├── metrics.py        # Metrics și reporting
│   └── main.py           # Entry point, orchestrator
├── configs/
│   ├── TESTNET_SAFE.yaml
│   └── MAINNET_CONSERVATIVE_200USD.yaml
├── tests/
│   ├── test_strategy.py
│   ├── test_risk.py
│   └── test_order_manager.py
├── requirements.txt
├── .env.example
└── README.md
```

### Flux de Date

```
[Extended WS] → [ws_client] → [OrderBook/Trades]
                     ↓
              [strategy_mm] ← mid price, volatility
                     ↓
              [risk.py] ← check limits
                     ↓
              [order_manager] → [signing] → [api_client] → [Extended REST]
                     ↓
              [metrics.py] → logs, reports
```

### States Bot

```
STARTING → RUNNING ↔ PAUSED ↔ COOLDOWN
              ↓
           KILLED (kill switch)
              ↓
           STOPPED (graceful shutdown)
```

---

## Suport și Contribuții

### Raportare Bug-uri

1. Verifică logs și troubleshooting
2. Creează un issue cu:
   - Descriere problemă
   - Logs relevante (fără credentials!)
   - Configurația folosită
   - Pași de reproducere

### Contribuții

1. Fork repository
2. Creează branch: `git checkout -b feature/...`
3. Commit: `git commit -m "Add ..."`
4. Push: `git push origin feature/...`
5. Deschide Pull Request

---

## Licență

[MIT License sau conform specificației proiectului]

---

**Ultima actualizare:** 2026-01-01
