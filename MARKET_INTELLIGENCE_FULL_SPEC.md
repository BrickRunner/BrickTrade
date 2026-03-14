# Market Intelligence Full Specification
Version: 1.0  
Purpose: Scalable Market Analysis Engine (Analytical, No Trading)

---

## 1. GOAL

Создать Market Intelligence System (MIS), которая:

1. Собирает данные с нескольких бирж и по множеству пар (spot и futures).  
2. Вычисляет технические индикаторы и деривативные метрики.  
3. Определяет глобальные и локальные режимы рынка с вероятностными оценками.  
4. Оценивает возможности для каждой пары (Opportunity Score).  
5. Сигнализирует состояние рынка через Telegram или Dashboard.  
6. Логирует все данные и вычисления для бэктестинга.  
7. Готова к подключению торговых стратегий в будущем.  

> Важно: бот не торгует, только анализирует.

---

## 2. ARCHITECTURE

Модули:

1. Data Collector – сбор данных spot/futures, orderbook, funding, OI, liquidations, basis.  
2. Feature Engine – вычисление индикаторов: EMA, RSI, MACD, ATR, ADX, Bollinger Bands, Volume spikes.  
3. Normalization Engine – стандартизация всех метрик (Z-score / min-max / скользящие средние).  
4. Regime Model – глобальный (BTC/USDT) и локальный (каждая пара) режим рынка.  
5. Regime Stability Layer – минимальная длительность режима, порог уверенности, скользящее сглаживание.  
6. Opportunity Scorer – оценка каждой пары по шансам на доход и риску.  
7. Risk & Portfolio Analyzer – распределение капитала, динамический риск с учётом корреляций и волатильности.  
8. Output Dispatcher – Telegram / Dashboard, JSON и человеко-читаемый формат.  
9. Logging & Monitoring – запись всех входных данных, индикаторов, режимов и оценок.  
10. Backtesting Module – проверка regime classifier на исторических данных.

---

## 3. DATA SOURCES

### 3.1 Spot & Futures
- OHLCV (1m, 5m, 1h, 4h, 1d)  
- Orderbook top N levels (10–20)  
- Volume  
- Open Interest (OI)  
- Funding Rate  
- Liquidations  
- Spot-Futures basis  
- Cross-exchange price spreads  

### 3.2 Historical Data
- Rolling statistics (среднее, стандартное отклонение)  
- Корреляции между парами (цена, funding, OI)  
- Исторические паттерны тренда и волатильности  

### 3.3 Optional Event Data
- Новости (API)  
- Соцсети (Twitter, Reddit)  
- Whale alerts (on-chain)  
- Макроэкономические события (CPI, Fed announcements)

---

## 4. FEATURE ENGINE

### 4.1 Trend Indicators
- EMA50, EMA200, EMA cross  
- ADX  
- HH/HL, LH/LL  
- Цена относительно EMA200  

### 4.2 Momentum
- RSI  
- MACD  

### 4.3 Volatility
- ATR  
- Bollinger Band width  
- Rolling volatility  
- Volatility skew (если доступны деривативы)  

### 4.4 Volume & Liquidity
- Volume spikes  
- Cumulative Delta Volume (CVD)  
- Orderbook imbalance  
- Spread widening  

### 4.5 Derivatives Metrics
- Funding rate & delta  
- Open Interest & delta  
- Spot-Futures basis & acceleration  
- Long-short ratio  
- Liquidation clusters  

### 4.6 Correlation Metrics
- BTC ↔ каждая пара (цена)  
- Cross-pair funding correlation  
- Cross-exchange spread correlation  

---

## 5. NORMALIZATION ENGINE
- Z-score / min-max scaling  
- Rolling normalization  
- Стандартизация всех индикаторов и деривативов  

---

## 6. REGIME MODEL

### 6.1 Global Regime
- На основе BTC/USDT  
- Типы: Trend Up, Trend Down, Range, Overheated, Panic, High Volatility  
- Probabilistic output (Softmax)

### 6.2 Local Regime
- Для каждой пары  
- Weighted combination с глобальным режимом  

### 6.3 Regime Scoring

- Конфигурируемые веса  
- Возможность adaptive ML weights  

### 6.4 Regime Stability
- Минимальная длительность режима  
- Confidence threshold (например P>0.6)  
- Rolling average smoothing  
- Предотвращение дрожания между режимами  

---

## 7. OPPORTUNITY SCORER
- Комбинирует regime + индикаторы + корреляции  
- Opportunity Score:  

- Ранжирует пары по потенциалу  
- Фильтрует экстремальные события (Crash, Overheat, Funding Extreme)

---

## 8. RISK & PORTFOLIO ANALYZER
- % капитала на пару (default 20%)  
- Exposure per regime (default 50%)  
- Dynamic risk based on volatility & correlation  
- Генерация risk multipliers  

---

## 9. OUTPUT DISPATCHER
- Telegram / Dashboard  
- JSON + человеко-читаемый формат
- Частота: каждые 5 минут или по событию  

---

## 10. UPDATE FREQUENCY

| Module | Frequency |
|--------|-----------|
| Price & Volume | WebSocket, real-time |
| Indicators | 30–60 sec |
| Funding & OI | 1–5 min |
| Regime Model | 5 min |
| Output / Telegram | 5 min or event-based |
| Historical stats / correlations | 15 min |

---

## 11. LOGGING & BACKTESTING
- Запись всех данных, индикаторов, режимов, оценок  
- Backtest regime classifier на истории  
- Precision / recall regime detection  
- Feature importance  

---

## 12. FAILSAFE
- Freeze calculations при API fail  
- Notify Risk Engine / Telegram  
- Enter conservative state  

---

## 13. DESIGN PRINCIPLES
- Modular & scalable  
- Asynchronous processing  
- Configurable weights & thresholds  
- Environment-driven configuration  
- Backtestable, deterministic behavior  
- Optional adaptive ML