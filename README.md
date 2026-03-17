# APEX — Multi-Coin Crypto Trading Bot

Asyncio tabanlı, 20 coin izleyen, orkestratör mimarili trading botu.
Binance **Spot Testnet** üzerinde çalışır. Railway'e tek komutla deploy edilir.

---

## Mimari

```
WebSocket Feed (20 coin, 1m kline + bookTicker)
        │
        ▼
  CandleBuffer (her coin için rolling 100-bar window)
        │
        ▼  (her candle kapanışında tetiklenir)
┌───────────────────────────────────┐
│  Coin başına Ajan Grubu (paralel) │
│  ├── MomentumSniper (0.35)        │
│  ├── OrderFlowAgent (0.30)        │
│  ├── FundingAgent   (0.20)        │
│  └── LiquidationHunter (0.15)     │
└───────────────────────────────────┘
        │
        ▼
  Orkestratör — en yüksek coin_score seç
        │
        ▼  (score ≥ 75?)
  Risk Manager — korelasyon, drawdown, pozisyon limiti
        │
        ▼
  ExecutionEngine — Binance REST (HMAC imzalı)
        │
        ▼
  Position Monitor — 3 kademeli TP, ATR-bazlı SL
```

---

## Hızlı başlangıç (local)

```bash
git clone <repo>
cd apex-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env içine testnet API key'lerini gir (aşağıya bak)

python main.py
```

Health check: http://localhost:8000/health  
Anlık durum: http://localhost:8000/state

---

## Testnet API Key alma

1. https://testnet.binance.vision/ adresine git
2. **Log In with GitHub** ile giriş yap
3. **Generate HMAC API Key** butonuna bas
4. `API_KEY` ve `API_SECRET` değerlerini `.env`'e kopyala
5. Testnet'te varsayılan 1 BTC + 100,000 USDT bakiye gelir

---

## Railway Deploy

### 1. GitHub'a push et
```bash
git init
git add .
git commit -m "initial"
git remote add origin <your-repo-url>
git push -u origin main
```

### 2. Railway'de proje oluştur
1. https://railway.app → **New Project** → **Deploy from GitHub repo**
2. Repoyu seç → Railway `Dockerfile`'ı otomatik bulur

### 3. Environment Variables ekle
Railway dashboard → **Variables** sekmesi → aşağıdaki değişkenleri ekle:

| Variable | Değer |
|----------|-------|
| `API_KEY` | testnet api key |
| `API_SECRET` | testnet api secret |
| `TESTNET` | `true` |
| `EXCHANGE` | `binance` |
| `SCORE_THRESHOLD` | `75` |
| `MAX_OPEN_POSITIONS` | `3` |
| `RISK_PER_TRADE_PCT` | `1.5` |

Diğer değişkenler için `.env.example`'a bak — hepsinin varsayılan değeri var.

### 4. Deploy
Variables kaydedilince Railway otomatik deploy eder.  
**Logs** sekmesinden canlı log'ları izleyebilirsin.

Health check URL: `https://<proje>.railway.app/health`

---

## Konfigürasyon referansı

| Değişken | Varsayılan | Açıklama |
|----------|-----------|----------|
| `SCORE_THRESHOLD` | 75 | İşlem açmak için minimum ajan skoru |
| `MAX_OPEN_POSITIONS` | 3 | Aynı anda maksimum açık pozisyon |
| `RISK_PER_TRADE_PCT` | 1.5 | İşlem başına bakiyenin %'si |
| `MAX_DRAWDOWN_PCT` | 8.0 | Bu eşikte sistem durur |
| `CORRELATION_THRESHOLD` | 0.85 | Bu üstü korelasyonda ikinci işlem bloke |
| `MOMENTUM_VOLUME_MULTIPLIER` | 1.8 | Hacim spike için minimum çarpan |
| `TP1_PCT / TP2_PCT / TP3_PCT` | 0.8 / 1.6 / 2.8 | Üç kademeli kar alma seviyeleri |
| `ATR_SL_MULTIPLIER` | 1.5 | Stop-loss = ATR × bu çarpan |

---

## Dashboard

Bot çalışırken aşağıdaki adreste canlı terminal dashboard açılır:

```
http://localhost:8000/          ← Dashboard (siyah/turuncu terminal UI)
http://localhost:8000/health    ← JSON health check
http://localhost:8000/state     ← Tam bot durumu (JSON)
```

Railway'de deploy edildikten sonra:
```
https://<proje>.railway.app/
```

Dashboard her 5 saniyede `/state` endpoint'ini polling yaparak canlı veriyi gösterir.
API key yoksa (dry-run modunda) simülasyon verileriyle çalışmaya devam eder.

---

## API Endpointleri

### `GET /health`
```json
{
  "status": "ok",
  "exchange": "binance",
  "testnet": true,
  "active_coins": 20,
  "open_positions": 1,
  "daily_pnl": 47.32
}
```

### `GET /state`
```json
{
  "balance": 1047.32,
  "daily_pnl": 47.32,
  "drawdown_pct": 0.0,
  "open_positions": {
    "SOLUSDT": {
      "direction": "long",
      "entry": 185.42,
      "qty": 0.8041,
      "sl": 183.10,
      "tp1": 186.90, "tp2": 188.39, "tp3": 190.60,
      "pnl": 12.45
    }
  },
  "last_signals": {
    "BTCUSDT": { "score": 88.2, "direction": "long", ... },
    "ETHUSDT": { "score": 61.5, "direction": "long", ... }
  }
}
```

---

## Proje yapısı

```
apex-bot/
├── main.py                  # FastAPI app + lifespan
├── requirements.txt
├── Dockerfile
├── railway.toml
├── .env.example
├── core/
│   ├── config.py            # Tüm ayarlar env'den
│   ├── models.py            # Candle, CandleBuffer, OrderBook
│   ├── feed.py              # Binance WebSocket feed manager
│   ├── risk.py              # RiskManager + Position
│   ├── execution.py         # Binance REST execution engine
│   └── orchestrator.py      # Ana orkestratör
└── agents/
    ├── momentum.py          # MomentumSniper
    ├── orderflow.py         # OrderFlowAgent
    ├── funding.py           # FundingAgent
    └── liquidation.py       # LiquidationHunter
```

---

## Önemli notlar

- Bot **Spot Testnet** üzerinde çalışır — gerçek para kullanılmaz
- `API_KEY` boşsa **dry-run** moduna geçer (log'lar yazar, emir atmaz)
- Futures funding rate testnet'te çalışmayabilir; `FundingAgent` bunu yakalar ve nötr skor (50) döner
- Short pozisyonlar Spot'ta mümkün değildir; short sinyalleri yalnızca mevcut long'ları kapatmak için kullanılabilir (ileride Futures mimarisine geçilebilir)
