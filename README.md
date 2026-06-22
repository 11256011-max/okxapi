# OKX API 交易機器人

這是一個可執行的 OKX 現貨交易機器人 starter 專案。它可以讀取 K 線資料、產生交易訊號、套用風控，並依照設定執行 dry-run、OKX 模擬盤或真實交易。

> 重要：這不是投資建議，也不保證獲利。預設是 `DRY_RUN=true`，不會送出訂單。請先用 dry-run 和模擬盤驗證，再考慮小資金測試。

## 安全設計

- 預設 dry-run，不會下單。
- 預設 OKX 模擬交易模式。
- 僅支援現貨交易，槓桿固定為 1。
- 不包含提幣、轉帳或資金劃轉功能。
- 真實交易必須同時設定 `DRY_RUN=false`、`OKX_SIMULATED_TRADING=false`、`ENABLE_LIVE_TRADING=true`。
- API 權限請只開「讀取」與「交易」，不要開「提幣」。
- 建議綁定 IP 白名單。

## 安裝

```powershell
git clone https://github.com/11256011-max/okxapi.git
cd okxapi
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env
```

打開 `.env`，填入你的 OKX API 資訊。不要把 `.env` 上傳到 GitHub。

## 常用指令

跑一次策略檢查：

```powershell
python -m okx_bot once
```

持續監控：

```powershell
python -m okx_bot loop
```

查詢餘額：

```powershell
python -m okx_bot balance
```

## 交易模式

正式帳戶讀取，但不下單：

```env
OKX_SIMULATED_TRADING=false
DRY_RUN=true
ENABLE_LIVE_TRADING=false
```

OKX 模擬盤 dry-run：

```env
OKX_SIMULATED_TRADING=true
DRY_RUN=true
ENABLE_LIVE_TRADING=false
```

OKX 模擬盤真的送出模擬訂單：

```env
OKX_SIMULATED_TRADING=true
DRY_RUN=false
ENABLE_LIVE_TRADING=false
```

真實交易：

```env
OKX_SIMULATED_TRADING=false
DRY_RUN=false
ENABLE_LIVE_TRADING=true
```

真實交易前請先把 `ORDER_QUOTE_AMOUNT` 設很小，並確認 API 沒有提幣權限。

## 策略選擇

目前支援兩種策略：

```env
STRATEGY=ema_rsi
```

或：

```env
STRATEGY=smc
```

## EMA + RSI 策略

買入：

- 快速 EMA 上穿慢速 EMA
- RSI 沒有過熱

賣出：

- 快速 EMA 下穿慢速 EMA
- 或 RSI 達到賣出門檻
- 或觸發停損 / 停利

設定：

```env
FAST_EMA=9
SLOW_EMA=21
RSI_PERIOD=14
RSI_BUY_MAX=65
RSI_SELL_MIN=70
```

## SMC 策略

SMC 是 Smart Money Concepts 的簡化規則版。這個 bot 的 SMC 策略是「現貨多單版」，不做放空。

它會觀察：

- Swing high / swing low
- BOS，也就是 break of structure
- CHOCH 類似的結構轉弱訊號
- Bullish order block
- Bullish fair value gap，可選

買入：

- 出現 bullish BOS 並有足夠位移
- 或多頭結構成立，價格回踩 bullish order block
- 如果 `SMC_REQUIRE_FVG=true`，還需要近期有 bullish FVG

賣出：

- 出現 bearish BOS 並有足夠位移
- 或多頭結構失效
- 或觸發停損 / 停利

設定：

```env
STRATEGY=smc
SMC_SWING_LOOKBACK=3
SMC_ZONE_LOOKBACK=40
SMC_ZONE_TOLERANCE_PCT=0.003
SMC_MIN_DISPLACEMENT_PCT=0.002
SMC_REQUIRE_FVG=false
```

參數意思：

- `SMC_SWING_LOOKBACK`：左右各看幾根 K 線確認 swing high / low。
- `SMC_ZONE_LOOKBACK`：往回找幾根 K 線內的 order block 和 FVG。
- `SMC_ZONE_TOLERANCE_PCT`：價格接近 order block 的容許範圍。
- `SMC_MIN_DISPLACEMENT_PCT`：突破要至少有多少百分比位移才算有效。
- `SMC_REQUIRE_FVG`：是否要求 bullish FVG 才能買入。

## 風控

```env
ORDER_QUOTE_AMOUNT=10
MAX_QUOTE_PER_ORDER=10
MAX_DAILY_NOTIONAL=50
STOP_LOSS_PCT=0.02
TAKE_PROFIT_PCT=0.04
SELL_FRACTION=1
```

- `ORDER_QUOTE_AMOUNT`：每次買入多少 quote currency，通常是 USDT。
- `MAX_QUOTE_PER_ORDER`：單筆最大交易額。
- `MAX_DAILY_NOTIONAL`：每日最大累計交易額。
- `STOP_LOSS_PCT`：停損百分比。
- `TAKE_PROFIT_PCT`：停利百分比。
- `SELL_FRACTION`：賣出比例，`1` 代表全部賣出。

## 參考

- OKX API FAQ：https://www.okx.com/en-us/help/api-faq
- OKX API Docs：https://www.okx.com/docs-v5/en/
- CCXT OKX Docs：https://docs.ccxt.com/docs/exchanges/okx
- CCXT Sandbox Manual：https://github.com/ccxt/ccxt/wiki/manual#testnets-and-sandbox-environments
