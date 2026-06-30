# OKX USDT 合約交易機器人

這是一個 OKX USDT 永續合約交易機器人。策略會結合 order flow、liquidity sweep、anchored VWAP、volume profile、SMC，並使用多週期判斷：

- `30m`：只負責找進場訊號與計算 confidence。
- `1h` / `4h`：只做方向確認，不再跟 30m 一起平均分數。
- 多單需要 1h、4h 偏多或至少不偏空；空單需要 1h、4h 偏空或至少不偏多。

## 安全提醒

- 不要把 `.env` commit 到 GitHub。
- API key 不要開啟提幣權限。
- 真實交易需要同時設定 `DRY_RUN=false`、`OKX_SIMULATED_TRADING=false`、`ENABLE_LIVE_TRADING=true`。
- 回測指令只使用 OKX 公開 K 線，不會下單，也不需要 API key。

## 安裝

```powershell
git clone https://github.com/11256011-max/okxapi.git
cd okxapi
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
```

接著打開 `.env` 填入自己的 OKX API 資訊。

## 常用指令

跑一次策略：

```powershell
python -m okx_bot once
```

持續自動運行：

```powershell
python -m okx_bot loop
```

查詢合約帳戶餘額：

```powershell
python -m okx_bot balance
```

正式回測最近 365 天，最多列出最近 100 筆已完成交易：

```powershell
python -m okx_bot backtest --days 365 --trades 100
```

也可以輸出 CSV：

```powershell
python -m okx_bot backtest --days 365 --trades 100 --csv backtest_results.csv
```

## 核心設定

```env
ENTRY_TIMEFRAME=30m
CONFIRMATION_TIMEFRAMES=1h,4h
STRATEGY=combined
SIGNAL_CONFIDENCE_THRESHOLD=0.68
SYMBOL_CONFIDENCE_THRESHOLDS=BTC:0.72,ETH:0.68,SOL:0.68
COMBINED_MIN_SCORE=0.68
COMBINED_MIN_EDGE=0.12
```

- `SIGNAL_CONFIDENCE_THRESHOLD`：送出交易前的最低 confidence。
- `SYMBOL_CONFIDENCE_THRESHOLDS`：分幣種交易門檻；BTC 目前較弱，所以預設提高到 72%，ETH/SOL 維持 68%。
- `COMBINED_MIN_SCORE`：30m 進場方向分數最低要求。
- `COMBINED_MIN_EDGE`：多空分數差距最低要求，避免多空太接近時進場。

## 合約風控

```env
MARKET_TYPE=swap
MARGIN_MODE=isolated
POSITION_MODE=auto
RISK_PER_TRADE_PCT=0.01
DAILY_MAX_LOSS_PCT=0.06
ORDER_QUOTE_AMOUNT=10
MAX_QUOTE_PER_ORDER=10
STOP_LOSS_PCT=0.02
TAKE_PROFIT_PCT=0.04
SYMBOL_STOP_LOSS_PCTS=ETH:0.015
SYMBOL_TAKE_PROFIT_PCTS=ETH:0.06
ATTACH_TP_SL=true
```

- 單筆最大風險由 `RISK_PER_TRADE_PCT`、`STOP_LOSS_PCT`、可用資產推算。
- 每日已實現虧損達 `DAILY_MAX_LOSS_PCT` 後會停止開新倉。
- 程式沒有 `MAX_LEVERAGE` 設定；槓桿會依風險模型計算，再由 OKX 該商品允許的最大槓桿自動封頂。
- `ATTACH_TP_SL=true` 且實際送單時，會在 OKX 訂單參數內附上止盈與止損。
- `SYMBOL_STOP_LOSS_PCTS` / `SYMBOL_TAKE_PROFIT_PCTS`：分幣種出場設定；ETH 目前使用 1.5% 止損與 6% 止盈，其他幣沿用全域 2% / 4%。

## 動態出場

```env
DYNAMIC_EXIT_SYMBOLS=ETH
DYNAMIC_EXIT_ATR_PERIOD=14
DYNAMIC_EXIT_STRUCTURE_LOOKBACK=20
DYNAMIC_EXIT_MIN_STOP_PCT=0.012
DYNAMIC_EXIT_MAX_STOP_PCT=0.030
DYNAMIC_EXIT_ATR_MULTIPLIER=1.2
DYNAMIC_EXIT_BASE_RR=2.0
DYNAMIC_EXIT_STRONG_RR=4.0
DYNAMIC_EXIT_STRONG_CONFIDENCE=0.70
DYNAMIC_EXIT_TREND_MA_PERIOD=20
```

- 停損會根據 30m ATR 與近期結構高低點推算，並限制在最小/最大範圍內。
- 停利不再只用固定百分比；普通訊號用 `DYNAMIC_EXIT_BASE_RR`，強趨勢訊號用 `DYNAMIC_EXIT_STRONG_RR`。
- 強趨勢需要 confidence 達標、高週期方向一致，且 30m 均線方向配合。
- `DYNAMIC_EXIT_SYMBOLS=ETH` 代表目前只讓 ETH 使用動態出場；空白則代表所有幣種都使用。
- 實盤送單與 backtest 共用同一套 exit plan。

## 外部資料

```env
EXTERNAL_CONTEXT_ENABLED=true
NEWSAPI_ENABLED=true
GDELT_ENABLED=true
FEAR_GREED_ENABLED=true
EXTERNAL_CONTEXT_CACHE_SECONDS=300
EXTERNAL_CONTEXT_TIMEOUT_SECONDS=15
```

實際交易 loop 會把 NewsAPI、GDELT、恐慌貪婪指數、手動基本面偏向納入訊號修正。回測目前只回放價格策略，不回放歷史新聞資料。

## 回測規則

`backtest` 使用 OKX 公開 OHLCV：

- 以 30m 收盤後產生訊號。
- 下一根 30m K 線開盤進場。
- 使用 `.env` 內 `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` 模擬出場。
- 同一根 K 同時碰到止盈與止損時，保守地先算止損。
- 持倉中若出現反向訊號，以下一根 30m 開盤價平倉。

回測成本假設：

```env
BACKTEST_FEE_PCT=0.0005
BACKTEST_SLIPPAGE_PCT=0.0005
BACKTEST_FUNDING_RATE_8H=0.0001
```

- `BACKTEST_FEE_PCT`：單邊手續費估算。
- `BACKTEST_SLIPPAGE_PCT`：單邊滑點估算。
- `BACKTEST_FUNDING_RATE_8H`：每 8 小時資金費率成本估算。
- 回測報表會同時顯示 gross PnL、成本與扣成本後的 net PnL。

回測結果是策略研究用途，不代表未來績效。
