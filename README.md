# OKX API 交易機器人

這是一個可執行的 OKX 現貨交易機器人 starter 專案。它會讀取 K 線資料，用 EMA + RSI 產生訊號，套用單筆與每日交易額風控，然後依照設定執行 dry-run、OKX 模擬盤或真實交易。

> 重要：這不是投資建議，也不保證獲利。預設是 `DRY_RUN=true`，不會送出訂單。請先用模擬盤驗證策略，再考慮小資金測試。

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

接著打開 `.env`，填入你的 OKX API 資訊。不要把 `.env` 上傳到 GitHub。

## 建議先用模擬盤

在 OKX 建立 Demo Trading API key，並保持：

```env
OKX_SIMULATED_TRADING=true
DRY_RUN=true
ENABLE_LIVE_TRADING=false
```

跑一次訊號檢查：

```powershell
python -m okx_bot once
```

連續執行：

```powershell
python -m okx_bot loop
```

查詢餘額：

```powershell
python -m okx_bot balance
```

## 切換到模擬盤下單

確認策略與風控都符合預期後，可以讓程式送出 OKX 模擬盤訂單：

```env
OKX_SIMULATED_TRADING=true
DRY_RUN=false
ENABLE_LIVE_TRADING=false
```

此模式需要 Demo Trading API key。Demo key 和正式帳戶 key 不能混用。

## 切換到真實交易

真實交易前請先確認你已理解風險，並把 `ORDER_QUOTE_AMOUNT` 設很小。

```env
OKX_SIMULATED_TRADING=false
DRY_RUN=false
ENABLE_LIVE_TRADING=true
ORDER_QUOTE_AMOUNT=10
MAX_QUOTE_PER_ORDER=10
MAX_DAILY_NOTIONAL=50
```

如果 `ENABLE_LIVE_TRADING` 不是 `true`，程式會拒絕送出真實訂單。

## 策略邏輯

- 買入：快速 EMA 上穿慢速 EMA，且 RSI 沒有過熱。
- 賣出：快速 EMA 下穿慢速 EMA，或 RSI 過熱。
- 風控賣出：達到停損或停利條件。

這只是範例策略，目標是建立安全可運行的交易框架，而不是提供保證獲利的策略。

## 參考

- OKX API FAQ：https://www.okx.com/en-us/help/api-faq
- OKX API Docs：https://www.okx.com/docs-v5/en/
- CCXT OKX Docs：https://docs.ccxt.com/docs/exchanges/okx
- CCXT Sandbox Manual：https://github.com/ccxt/ccxt/wiki/manual#testnets-and-sandbox-environments

