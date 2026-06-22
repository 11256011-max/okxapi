# OKX API 交易機器人

這是一個可執行的 OKX USDT 永續合約交易機器人 starter 專案。它可以讀取 K 線資料、產生交易訊號、套用風控，並依照設定執行 dry-run、OKX 模擬盤或真實交易。

> 重要：這不是投資建議，也不保證獲利。預設是 `DRY_RUN=true`，不會送出訂單。請先用 dry-run 和模擬盤驗證，再考慮小資金測試。

## 安全設計

- 預設 dry-run，不會下單。
- 預設 OKX 模擬交易模式。
- 僅支援 USDT 永續合約；可做多、做空，並依風險計算槓桿。
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

目前策略已統一為五模組整合版：

```env
STRATEGY=combined
```

舊的 `STRATEGY=ema_rsi` 或 `STRATEGY=smc` 會自動映射到 `combined`。

## 訊號符合度門檻

策略會計算 `confidence`，代表 order flow、liquidity sweep、anchored VWAP、volume profile、SMC 五個模組統整後的符合度。只有達到門檻的 `buy` 或 `sell` 訊號才會進入下單流程；低於門檻會自動改成 `hold`。

```env
SIGNAL_CONFIDENCE_THRESHOLD=0.80
```

你也可以寫成百分比：

```env
SIGNAL_CONFIDENCE_THRESHOLD=80
```

預設 80% 是比較積極的設定。log 會顯示類似：

```text
Signal=hold confidence=74.00% reason=BUY signal blocked because confidence 74.00% is below threshold 80.00%.
```

停損和停利是風控退出，不會被這個門檻擋住。

## 整合策略

目前只有一個策略：`combined`。舊的 `STRATEGY=ema_rsi` 或 `STRATEGY=smc` 會自動映射到 `combined`，不會再跑舊策略。

它會把五個模組合併評分：

- `order flow`：用 K 線方向、收盤位置與成交量估算買賣力道。
- `liquidity sweep`：觀察是否掃過近期高低點後收回。
- `anchored VWAP`：從近期高點或低點錨定 VWAP，判斷價格是否站回或跌破成本中樞。
- `volume profile`：估算 POC、value area high、value area low，判斷價格是否突破或守住主要成交區。
- `SMC`：保留 swing、BOS、FVG、order block 等市場結構概念。

`confidence` 是以上五個模組加權後的整合分數，不再是單一 SMC 分數。它是訊號強度估算，不是保證勝率；若要知道真實勝率，需要另外做歷史回測。

設定：

```env
STRATEGY=combined
SIGNAL_CONFIDENCE_THRESHOLD=0.80
COMBINED_MIN_SCORE=0.80
COMBINED_MIN_EDGE=0.10
COMBINED_SWING_LOOKBACK=3
COMBINED_STRUCTURE_LOOKBACK=40
COMBINED_ORDER_FLOW_LOOKBACK=20
COMBINED_AVWAP_LOOKBACK=80
COMBINED_VOLUME_PROFILE_LOOKBACK=80
COMBINED_VOLUME_PROFILE_BINS=24
COMBINED_VALUE_AREA_PCT=0.70
COMBINED_SWEEP_TOLERANCE_PCT=0.001
COMBINED_MIN_DISPLACEMENT_PCT=0.002
```

參數意思：

- `COMBINED_MIN_SCORE`：多方或空方整合分數至少要到多少才可能交易，`0.80` 代表 80%。
- `COMBINED_MIN_EDGE`：多空分數差距至少要多少，避免多空分數太接近還下單。
- `COMBINED_SWING_LOOKBACK`：左右各看幾根 K 線確認 swing high / low。
- `COMBINED_STRUCTURE_LOOKBACK`：市場結構、流動性掃點、FVG、order block 的回看 K 線數。
- `COMBINED_ORDER_FLOW_LOOKBACK`：比較成交量均值的回看 K 線數。
- `COMBINED_AVWAP_LOOKBACK`：尋找 anchored VWAP 錨點的回看 K 線數。
- `COMBINED_VOLUME_PROFILE_LOOKBACK`：計算 volume profile 的回看 K 線數。
- `COMBINED_VOLUME_PROFILE_BINS`：volume profile 分幾個價格區間。
- `COMBINED_VALUE_AREA_PCT`：value area 覆蓋多少成交量。
- `COMBINED_SWEEP_TOLERANCE_PCT`：判斷掃流動性的容許百分比。
- `COMBINED_MIN_DISPLACEMENT_PCT`：SMC 突破位移至少要多少才算有效。

## 外部資訊濾網

外部資訊濾網會在策略產生 `buy` / `sell` 後執行。它不會自己產生交易訊號，只會調整策略訊號的 `confidence`，或在外部資訊明顯反向時把訊號改成 `hold`。

支援來源：

- NewsAPI：用 `/v2/everything` 搜尋近幾小時的新聞標題與摘要。
- GDELT：用 DOC 2.0 `timelinetone` 取得新聞 tone。
- Crypto Fear & Greed Index：用 Alternative.me `/fng/` 取得市場情緒。
- 基本面偏向：你手動設定每個幣的偏多或偏空分數。

設定：

```env
EXTERNAL_CONTEXT_ENABLED=true
NEWSAPI_ENABLED=true
NEWSAPI_API_KEY=
NEWSAPI_PAGE_SIZE=20
GDELT_ENABLED=true
FEAR_GREED_ENABLED=true
FEAR_GREED_MODE=momentum
FUNDAMENTAL_CONTEXT_ENABLED=true
FUNDAMENTAL_BIAS=BTC:0.2,ETH:0.1,SOL:-0.1
EXTERNAL_CONTEXT_LOOKBACK_HOURS=24
EXTERNAL_CONTEXT_CACHE_SECONDS=300
EXTERNAL_CONTEXT_TIMEOUT_SECONDS=6
EXTERNAL_CONTEXT_MAX_CONFIDENCE_ADJUSTMENT=0.15
EXTERNAL_CONTEXT_MIN_SUPPORT=-0.35
```

- `EXTERNAL_CONTEXT_CACHE_SECONDS=300`：同一幣種每 5 分鐘才重新抓一次外部資料。
- `FEAR_GREED_MODE=momentum`：貪婪偏多、恐慌偏空；若要反向解讀可設 `contrarian`。
- `FUNDAMENTAL_BIAS`：正數偏多，負數偏空，可用 `-1..1` 或 `-100..100`。
- `EXTERNAL_CONTEXT_MAX_CONFIDENCE_ADJUSTMENT`：外部資訊最多調整多少信心。
- `EXTERNAL_CONTEXT_MIN_SUPPORT`：外部資訊對該方向低於這個值時擋單。

啟用後，log 的 `indicators` 會出現 `external_context_score`、`newsapi_score`、`gdelt_score`、`fear_greed_score`、`fundamental_score` 等欄位。

## 加倉規則

當已有同方向倉位時，新的同方向 `buy` / `sell` 訊號不會直接加倉。bot 會先檢查以下條件：

- 原有部位已達設定浮盈。
- 均線方向與價格位置支持目前趨勢。
- 出現放量突破前高 / 前低，或回踩均線、支撐 / 壓力後反彈。
- 尚未超過最大加倉次數。

若條件沒有全部達成，bot 只保留原有倉位，不會加倉。

```env
ADD_POSITION_ENABLED=true
MAX_POSITION_ADDS=2
ADD_POSITION_QUOTE_FRACTION=0.5
ADD_POSITION_REQUIRE_PROFIT=true
ADD_POSITION_MIN_PROFIT_PCT=0.005
ADD_POSITION_BREAKOUT_LOOKBACK=20
ADD_POSITION_PULLBACK_MA_PERIOD=20
ADD_POSITION_SUPPORT_LOOKBACK=20
ADD_POSITION_SUPPORT_TOLERANCE_PCT=0.003
ADD_POSITION_VOLUME_MULTIPLIER=1.2
```

- `MAX_POSITION_ADDS`：同一倉位最多加倉次數。
- `ADD_POSITION_QUOTE_FRACTION`：加倉單使用原本 `ORDER_QUOTE_AMOUNT` 的比例。
- `ADD_POSITION_REQUIRE_PROFIT`：是否要求原倉已有浮盈才可加倉；若要做回調拉低均價可設 `false`，但風險較高。
- `ADD_POSITION_MIN_PROFIT_PCT`：原倉最低浮盈，`0.005` 代表 0.5%。
- `ADD_POSITION_BREAKOUT_LOOKBACK`：判斷突破前高 / 前低的 K 線數。
- `ADD_POSITION_PULLBACK_MA_PERIOD`：回踩均線使用的均線週期。
- `ADD_POSITION_SUPPORT_LOOKBACK`：判斷支撐 / 壓力的 K 線數。
- `ADD_POSITION_SUPPORT_TOLERANCE_PCT`：接近支撐 / 壓力或均線的容許範圍。
- `ADD_POSITION_VOLUME_MULTIPLIER`：突破時成交量至少要高於均量幾倍。

## 風控

```env
ORDER_QUOTE_AMOUNT=10
MAX_QUOTE_PER_ORDER=10
MARKET_TYPE=swap
MARGIN_MODE=isolated
POSITION_MODE=auto
RISK_PER_TRADE_PCT=0.01
DAILY_MAX_LOSS_PCT=0.06
MAX_LEVERAGE=10
STOP_LOSS_PCT=0.02
TAKE_PROFIT_PCT=0.04
ATTACH_TP_SL=true
SELL_FRACTION=1
```

- `ORDER_QUOTE_AMOUNT`：單筆保證金預算上限。
- `MAX_QUOTE_PER_ORDER`：單筆最大保證金。
- `MARKET_TYPE`：固定使用 `swap`，本專案不送現貨單。
- `MARGIN_MODE`：合約保證金模式，建議先用 `isolated`。
- `POSITION_MODE`：建議用 `auto`，程式會送單前查 OKX 帳戶是單向 `net` 還是雙向 `hedge`，避免 `Parameter posSide error`。
- `RISK_PER_TRADE_PCT`：單筆最大風險，`0.01` 代表總權益的 1%。
- `DAILY_MAX_LOSS_PCT`：日內已實現虧損上限，`0.06` 代表總權益的 6%。
- `MAX_LEVERAGE`：程式自動計算槓桿後的最高上限。
- `STOP_LOSS_PCT`：停損百分比。
- `TAKE_PROFIT_PCT`：停利百分比。
- `ATTACH_TP_SL`：是否在 OKX 掛止盈 / 止損保護單。
- `SELL_FRACTION`：賣出比例，`1` 代表全部賣出。

## OKX 原生止盈 / 止損

當 `ATTACH_TP_SL=true` 且 `DRY_RUN=false` 時，bot 送出開倉市價單時會同時帶上 OKX / CCXT 支援的 `takeProfit` 和 `stopLoss` 附加參數，讓 OKX 端保存止盈與止損條件單：

- 多單止盈觸發價：`entry_price * (1 + TAKE_PROFIT_PCT)`
- 多單止損觸發價：`entry_price * (1 - STOP_LOSS_PCT)`
- 空單止盈觸發價：`entry_price * (1 - TAKE_PROFIT_PCT)`
- 空單止損觸發價：`entry_price * (1 + STOP_LOSS_PCT)`
- 止盈和止損都使用市價執行。

例如多單入場價是 `64000`：

```text
STOP_LOSS_PCT=0.02   -> 止損觸發價 62720
TAKE_PROFIT_PCT=0.04 -> 止盈觸發價 66560
```

例如空單入場價是 `64000`：

```text
STOP_LOSS_PCT=0.02   -> 止損觸發價 65280
TAKE_PROFIT_PCT=0.04 -> 止盈觸發價 61440
```

## 參考

- OKX API FAQ：https://www.okx.com/en-us/help/api-faq
- OKX API Docs：https://www.okx.com/docs-v5/en/
- CCXT OKX Docs：https://docs.ccxt.com/docs/exchanges/okx
- CCXT Sandbox Manual：https://github.com/ccxt/ccxt/wiki/manual#testnets-and-sandbox-environments
- NewsAPI Everything：https://newsapi.org/docs/endpoints/everything
- GDELT DOC 2.0：https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- Alternative.me Fear & Greed：https://alternative.me/crypto/fear-and-greed-index/
