from __future__ import annotations

import json
import logging
from dataclasses import asdict
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .backtest import BacktestResult, BacktestRunner, BacktestTrade
from .bot import TradingBot
from .config import BotConfig
from .models import Candle, Signal


def run_ui(config: BotConfig, host: str = "127.0.0.1", port: int = 8787) -> None:
    handler = dashboard_handler(config)
    server = ThreadingHTTPServer((host, port), handler)
    logging.info("OKX bot UI running at http://%s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def dashboard_handler(config: BotConfig) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self.send_html(INDEX_HTML)
                return
            if parsed.path == "/api/snapshot":
                self.send_json_response(lambda: build_snapshot(config))
                return
            if parsed.path == "/api/backtest":
                query = parse_qs(parsed.query)
                days = int(first_query_value(query, "days", "30"))
                trades = int(first_query_value(query, "trades", "100"))
                self.send_json_response(lambda: build_backtest_snapshot(config, days, trades))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format_string: str, *args: Any) -> None:
            logging.info("UI %s - %s", self.address_string(), format_string % args)

        def send_json_response(self, builder: Any) -> None:
            try:
                payload = builder()
                self.send_json(payload)
            except Exception as exc:
                logging.exception("UI request failed")
                self.send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return DashboardHandler


def first_query_value(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    if not values:
        return default
    return values[0] or default


def build_snapshot(config: BotConfig) -> dict[str, Any]:
    bot = TradingBot(config)
    symbol = config.symbols[0]
    candles_by_timeframe = bot.fetch_analysis_candles(symbol)
    strategy_signal = bot.strategy.generate_multi(candles_by_timeframe)
    context_signal = bot.apply_external_context_filter(symbol, strategy_signal)
    final_signal = bot.apply_signal_confidence_gate(symbol, context_signal)
    final_signal = bot.apply_position_risk(symbol, final_signal)
    threshold = config.confidence_threshold_for_symbol_and_action(symbol, context_signal.action)

    return {
        "symbol": symbol,
        "mode": {
            "dry_run": config.dry_run,
            "simulated": config.okx_simulated_trading,
            "live_enabled": config.enable_live_trading,
        },
        "config": config_snapshot(config),
        "signal": signal_snapshot(final_signal, threshold),
        "strategy_signal": signal_snapshot(strategy_signal, threshold),
        "context_signal": signal_snapshot(context_signal, threshold),
        "timeframes": timeframe_snapshots(config, candles_by_timeframe, final_signal),
        "external_context": external_context_snapshot(final_signal),
        "risk": risk_snapshot(config, bot),
        "state": state_snapshot(bot),
    }


def config_snapshot(config: BotConfig) -> dict[str, Any]:
    return {
        "symbols": config.symbols,
        "entry_timeframe": config.entry_timeframe,
        "confirmation_timeframes": config.confirmation_timeframes,
        "risk_per_trade_pct": decimal_string(config.risk_per_trade_pct),
        "daily_max_loss_pct": decimal_string(config.daily_max_loss_pct),
        "max_consecutive_daily_losses": config.max_consecutive_daily_losses,
        "order_quote_amount": decimal_string(config.order_quote_amount),
        "max_quote_per_order": decimal_string(config.max_quote_per_order),
        "combined_min_score": decimal_string(config.combined_min_score),
        "combined_min_edge": decimal_string(config.combined_min_edge),
        "exit_breakeven_r": decimal_string(config.exit_breakeven_r),
        "exit_partial_take_profit_r": decimal_string(config.exit_partial_take_profit_r),
        "exit_partial_fraction": decimal_string(config.exit_partial_fraction),
        "exit_trailing_atr_multiplier": decimal_string(config.exit_trailing_atr_multiplier),
    }


def signal_snapshot(signal: Signal, threshold: Decimal) -> dict[str, Any]:
    confidence_gap = signal.confidence - threshold
    return {
        "action": signal.action,
        "action_label": action_label(signal.action),
        "reason": signal.reason,
        "price": decimal_string(signal.price),
        "confidence": decimal_string(signal.confidence),
        "confidence_pct": percent_string(signal.confidence),
        "threshold": decimal_string(threshold),
        "threshold_pct": percent_string(threshold),
        "confidence_gap": decimal_string(confidence_gap),
        "confidence_gap_pct": percent_string(confidence_gap),
        "indicators": sanitize_mapping(signal.indicators),
    }


def timeframe_snapshots(
    config: BotConfig,
    candles_by_timeframe: dict[str, list[Candle]],
    signal: Signal,
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for timeframe in config.analysis_timeframes:
        candles = candles_by_timeframe.get(timeframe, [])
        latest = candles[-1]
        previous = candles[-2] if len(candles) > 1 else latest
        prefix = timeframe.replace("/", "_").replace(":", "_")
        snapshots.append({
            "timeframe": timeframe,
            "latest": candle_snapshot(latest, previous),
            "scores": {
                "bullish": indicator_value(signal, prefix, "bullish_score"),
                "bearish": indicator_value(signal, prefix, "bearish_score"),
                "edge": indicator_value(signal, prefix, "strategy_edge"),
                "smc_bullish": indicator_value(signal, prefix, "smc_bullish_score"),
                "smc_bearish": indicator_value(signal, prefix, "smc_bearish_score"),
                "order_flow_bullish": indicator_value(signal, prefix, "order_flow_bullish_score"),
                "order_flow_bearish": indicator_value(signal, prefix, "order_flow_bearish_score"),
                "liquidity_bullish": indicator_value(signal, prefix, "liquidity_sweep_bullish_score"),
                "liquidity_bearish": indicator_value(signal, prefix, "liquidity_sweep_bearish_score"),
                "vwap_bullish": indicator_value(signal, prefix, "anchored_vwap_bullish_score"),
                "vwap_bearish": indicator_value(signal, prefix, "anchored_vwap_bearish_score"),
                "profile_bullish": indicator_value(signal, prefix, "volume_profile_bullish_score"),
                "profile_bearish": indicator_value(signal, prefix, "volume_profile_bearish_score"),
            },
            "levels": {
                "anchored_vwap_from_low": indicator_value(signal, prefix, "anchored_vwap_from_low"),
                "anchored_vwap_from_high": indicator_value(signal, prefix, "anchored_vwap_from_high"),
                "volume_profile_poc": indicator_value(signal, prefix, "volume_profile_poc"),
                "value_area_low": indicator_value(signal, prefix, "volume_profile_value_area_low"),
                "value_area_high": indicator_value(signal, prefix, "volume_profile_value_area_high"),
            },
        })
    return snapshots


def candle_snapshot(latest: Candle, previous: Candle) -> dict[str, Any]:
    change_pct = Decimal("0")
    if previous.close > 0:
        change_pct = (latest.close - previous.close) / previous.close
    return {
        "timestamp": latest.timestamp,
        "open": decimal_string(latest.open),
        "high": decimal_string(latest.high),
        "low": decimal_string(latest.low),
        "close": decimal_string(latest.close),
        "volume": decimal_string(latest.volume),
        "change_pct": decimal_string(change_pct),
        "change_pct_label": percent_string(change_pct),
    }


def external_context_snapshot(signal: Signal) -> dict[str, Any]:
    indicators = signal.indicators
    keys = [
        "external_context_score",
        "external_context_sources",
        "external_context_support",
        "newsapi_score",
        "gdelt_score",
        "fear_greed_score",
        "fundamental_score",
        "risk_multiplier",
    ]
    return {key: sanitize_value(indicators.get(key)) for key in keys if key in indicators}


def risk_snapshot(config: BotConfig, bot: TradingBot) -> dict[str, Any]:
    return {
        "risk_per_trade_pct": percent_string(config.risk_per_trade_pct),
        "daily_max_loss_pct": percent_string(config.daily_max_loss_pct),
        "max_consecutive_daily_losses": config.max_consecutive_daily_losses,
        "order_quote_amount": decimal_string(config.order_quote_amount),
        "max_quote_per_order": decimal_string(config.max_quote_per_order),
        "daily_realized_pnl": decimal_string(bot.state.daily_realized_pnl),
        "daily_loss_streak": bot.state.daily_loss_streak,
        "dynamic_exit": {
            "breakeven_r": decimal_string(config.exit_breakeven_r),
            "partial_take_profit_r": decimal_string(config.exit_partial_take_profit_r),
            "partial_fraction": percent_string(config.exit_partial_fraction),
            "trailing_atr_multiplier": decimal_string(config.exit_trailing_atr_multiplier),
        },
    }


def state_snapshot(bot: TradingBot) -> dict[str, Any]:
    state = bot.state
    positions = {
        symbol: sanitize_mapping(position.to_json())
        for symbol, position in state.positions.items()
    }
    trades = [
        sanitize_mapping(trade)
        for trade in state.trades[-10:]
        if isinstance(trade, dict)
    ]
    return {
        "day": state.day,
        "daily_notional": decimal_string(state.daily_notional),
        "daily_realized_pnl": decimal_string(state.daily_realized_pnl),
        "daily_loss_streak": state.daily_loss_streak,
        "positions": positions,
        "recent_trades": trades,
    }


def build_backtest_snapshot(config: BotConfig, days: int, trades: int) -> dict[str, Any]:
    result = BacktestRunner(config).run(days=days, max_trades=trades)
    return backtest_result_snapshot(result)


def backtest_result_snapshot(result: BacktestResult) -> dict[str, Any]:
    return {
        "summary": {
            "total_completed_trades": result.total_completed_trades,
            "reported_trades": len(result.trades),
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": percent_string(result.win_rate),
            "average_gross_pnl": percent_string(result.average_gross_pnl_pct),
            "average_cost": percent_string(result.average_cost_pct),
            "average_net_pnl": percent_string(result.average_pnl_pct),
            "average_notional": decimal_string(result.average_notional),
            "total_net_pnl": decimal_string(result.total_pnl_quote),
            "account_return": percent_string(result.total_account_return_pct),
            "starting_equity": decimal_string(result.starting_equity),
            "ending_equity": decimal_string(result.starting_equity + result.total_pnl_quote),
        },
        "symbols": symbol_summaries(result),
        "trades": [trade_snapshot(trade) for trade in result.trades[-30:]],
    }


def symbol_summaries(result: BacktestResult) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for symbol in sorted({trade.symbol for trade in result.trades}):
        trades = [trade for trade in result.trades if trade.symbol == symbol]
        wins = sum(1 for trade in trades if trade.won)
        win_rate = Decimal(wins) / Decimal(len(trades)) if trades else Decimal("0")
        net_pnl = sum((trade.pnl_quote for trade in trades), Decimal("0"))
        summaries.append({
            "symbol": symbol,
            "trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "win_rate": percent_string(win_rate),
            "net_pnl": decimal_string(net_pnl),
            "account_return": percent_string(net_pnl / result.starting_equity if result.starting_equity > 0 else Decimal("0")),
        })
    return summaries


def trade_snapshot(trade: BacktestTrade) -> dict[str, Any]:
    raw = asdict(trade)
    return sanitize_mapping(raw)


def indicator_value(signal: Signal, prefix: str, key: str) -> Any:
    return sanitize_value(signal.indicators.get(f"{prefix}_{key}"))


def sanitize_mapping(values: dict[str, Any]) -> dict[str, Any]:
    return {key: sanitize_value(value) for key, value in values.items()}


def sanitize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_string(value)
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, dict):
        return sanitize_mapping(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    return value


def decimal_string(value: Decimal | Any) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    normalized = value.normalize()
    return format(normalized, "f")


def percent_string(value: Decimal | Any) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"


def action_label(action: str) -> str:
    labels = {
        "buy": "做多",
        "sell": "做空",
        "hold": "觀望",
    }
    return labels.get(action, action)


INDEX_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OKX AI 情報站</title>
  <style>
    :root {
      --bg: #f5f3ee;
      --surface: #ffffff;
      --surface-2: #f9faf7;
      --text: #22241f;
      --muted: #6d7168;
      --line: #ddd8ce;
      --accent: #176c5f;
      --accent-2: #284f8f;
      --danger: #b43b35;
      --warn: #a96b10;
      --ok: #167348;
      --shadow: 0 8px 22px rgba(31, 28, 22, 0.08);
      font-family: "Segoe UI", "Microsoft JhengHei", Arial, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    button, input, select { font: inherit; letter-spacing: 0; }
    .shell {
      max-width: 1440px;
      margin: 0 auto;
      padding: 24px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 18px;
    }
    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
    }
    .sub {
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .btn {
      min-height: 38px;
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--text);
      border-radius: 6px;
      padding: 8px 12px;
      cursor: pointer;
      box-shadow: none;
    }
    .btn.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    .btn:disabled { opacity: 0.58; cursor: wait; }
    .grid {
      display: grid;
      grid-template-columns: minmax(320px, 1.1fr) minmax(320px, 0.9fr);
      gap: 16px;
      align-items: start;
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 16px;
    }
    .panel h2 {
      margin: 0 0 12px;
      font-size: 16px;
      line-height: 1.25;
    }
    .signal {
      display: grid;
      grid-template-columns: 132px 1fr;
      gap: 16px;
      align-items: center;
    }
    .badge {
      display: grid;
      place-items: center;
      min-height: 104px;
      border-radius: 8px;
      color: #fff;
      font-size: 28px;
      font-weight: 800;
      background: var(--muted);
    }
    .badge.buy { background: var(--ok); }
    .badge.sell { background: var(--danger); }
    .badge.hold { background: var(--warn); }
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .metric {
      min-height: 74px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface-2);
      padding: 10px;
    }
    .metric .label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .metric .value {
      font-size: 20px;
      font-weight: 750;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }
    .reason {
      margin-top: 12px;
      color: var(--muted);
      line-height: 1.55;
      font-size: 14px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }
    th, td {
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      overflow-wrap: anywhere;
    }
    th {
      color: var(--muted);
      font-weight: 650;
      background: #fbfaf7;
    }
    .stack {
      display: grid;
      gap: 16px;
    }
    .two {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .status-line {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 12px;
      min-height: 22px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      background: #eef3ec;
      color: var(--accent);
      border: 1px solid #d8e5dc;
      font-weight: 650;
    }
    .pill.danger { color: var(--danger); background: #fff0ed; border-color: #f1d0c8; }
    .pill.warn { color: var(--warn); background: #fff8e7; border-color: #ead7a5; }
    .backtest-controls {
      display: grid;
      grid-template-columns: 96px 96px auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 12px;
    }
    label {
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
    }
    input {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 7px 9px;
      color: var(--text);
    }
    .small {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.45;
    }
    .error {
      border-color: #efc2b8;
      color: var(--danger);
      background: #fff7f5;
    }
    @media (max-width: 980px) {
      .shell { padding: 14px; }
      header { align-items: flex-start; flex-direction: column; }
      .grid, .signal, .metric-grid, .two { grid-template-columns: 1fr; }
      .badge { min-height: 82px; }
      .backtest-controls { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <h1>OKX AI 情報站</h1>
        <div class="sub" id="modeLine">載入中</div>
      </div>
      <div class="toolbar">
        <button class="btn" id="refreshBtn">刷新</button>
        <button class="btn primary" id="backtestBtnTop">跑回測</button>
      </div>
    </header>

    <div class="status-line">
      <span id="statusText">等待資料</span>
      <span class="pill" id="symbolPill">ETH</span>
    </div>

    <section class="grid">
      <div class="stack">
        <section class="panel">
          <h2>即時判定</h2>
          <div class="signal">
            <div class="badge hold" id="actionBadge">觀望</div>
            <div>
              <div class="metric-grid">
                <div class="metric"><div class="label">價格</div><div class="value" id="price">-</div></div>
                <div class="metric"><div class="label">信心</div><div class="value" id="confidence">-</div></div>
                <div class="metric"><div class="label">門檻</div><div class="value" id="threshold">-</div></div>
                <div class="metric"><div class="label">差距</div><div class="value" id="gap">-</div></div>
              </div>
              <div class="reason" id="reason">-</div>
            </div>
          </div>
        </section>

        <section class="panel">
          <h2>多週期策略</h2>
          <table>
            <thead>
              <tr>
                <th>週期</th>
                <th>收盤</th>
                <th>多方</th>
                <th>空方</th>
                <th>SMC</th>
                <th>VWAP / VP</th>
              </tr>
            </thead>
            <tbody id="timeframeRows"></tbody>
          </table>
        </section>

        <section class="panel">
          <h2>回測</h2>
          <div class="backtest-controls">
            <label>天數<input id="daysInput" type="number" min="1" max="1000" value="365" /></label>
            <label>筆數<input id="tradesInput" type="number" min="1" max="2000" value="1000" /></label>
            <button class="btn primary" id="backtestBtn">開始</button>
          </div>
          <div id="backtestResult" class="small">尚未執行</div>
        </section>
      </div>

      <div class="stack">
        <section class="panel">
          <h2>風控</h2>
          <div class="two" id="riskGrid"></div>
        </section>

        <section class="panel">
          <h2>外部情緒</h2>
          <div class="two" id="contextGrid"></div>
        </section>

        <section class="panel">
          <h2>倉位狀態</h2>
          <div id="positionBox" class="small">-</div>
        </section>

        <section class="panel">
          <h2>近期紀錄</h2>
          <table>
            <thead><tr><th>時間</th><th>方向</th><th>數量</th><th>價格</th><th>PnL</th></tr></thead>
            <tbody id="tradeRows"></tbody>
          </table>
        </section>
      </div>
    </section>
  </main>

  <script>
    const el = (id) => document.getElementById(id);
    let busy = false;

    function pct(value) {
      if (value === null || value === undefined || value === "") return "-";
      const n = Number(value);
      if (Number.isNaN(n)) return value;
      return `${(n * 100).toFixed(2)}%`;
    }
    function num(value, digits = 2) {
      if (value === null || value === undefined || value === "") return "-";
      const n = Number(value);
      if (Number.isNaN(n)) return value;
      return n.toLocaleString("zh-TW", { maximumFractionDigits: digits });
    }
    function setStatus(text, isError = false) {
      el("statusText").textContent = text;
      el("statusText").className = isError ? "pill danger" : "";
    }
    function metric(label, value) {
      return `<div class="metric"><div class="label">${label}</div><div class="value">${value}</div></div>`;
    }
    function scorePair(scores, bullKey, bearKey) {
      return `${pct(scores[bullKey])} / ${pct(scores[bearKey])}`;
    }

    async function loadSnapshot() {
      if (busy) return;
      busy = true;
      el("refreshBtn").disabled = true;
      setStatus("更新中");
      try {
        const response = await fetch("/api/snapshot");
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "snapshot failed");
        renderSnapshot(data);
        setStatus(`最後更新 ${new Date().toLocaleTimeString("zh-TW")}`);
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        busy = false;
        el("refreshBtn").disabled = false;
      }
    }

    function renderSnapshot(data) {
      el("symbolPill").textContent = data.symbol;
      const mode = data.mode;
      el("modeLine").textContent = `${mode.dry_run ? "DRY RUN" : mode.simulated ? "OKX 模擬盤" : "LIVE"} · ${data.config.entry_timeframe} / ${data.config.confirmation_timeframes.join(", ")}`;

      const signal = data.signal;
      const badge = el("actionBadge");
      badge.textContent = signal.action_label;
      badge.className = `badge ${signal.action}`;
      el("price").textContent = num(signal.price, 4);
      el("confidence").textContent = signal.confidence_pct;
      el("threshold").textContent = signal.threshold_pct;
      el("gap").textContent = signal.confidence_gap_pct;
      el("reason").textContent = signal.reason;

      el("timeframeRows").innerHTML = data.timeframes.map((tf) => {
        const s = tf.scores;
        const levels = tf.levels;
        return `<tr>
          <td>${tf.timeframe}</td>
          <td>${num(tf.latest.close, 4)}<br><span class="small">${tf.latest.change_pct_label}</span></td>
          <td>${pct(s.bullish)}<br><span class="small">OF ${pct(s.order_flow_bullish)}</span></td>
          <td>${pct(s.bearish)}<br><span class="small">OF ${pct(s.order_flow_bearish)}</span></td>
          <td>${scorePair(s, "smc_bullish", "smc_bearish")}</td>
          <td>VWAP ${num(levels.anchored_vwap_from_low, 2)}<br><span class="small">POC ${num(levels.volume_profile_poc, 2)}</span></td>
        </tr>`;
      }).join("");

      el("riskGrid").innerHTML = [
        metric("單筆風險", data.risk.risk_per_trade_pct),
        metric("日虧損停手", data.risk.daily_max_loss_pct),
        metric("連虧停手", data.risk.max_consecutive_daily_losses),
        metric("今日連虧", data.risk.daily_loss_streak),
        metric("單筆保證金", `${num(data.risk.order_quote_amount, 2)}U`),
        metric("最大保證金", `${num(data.risk.max_quote_per_order, 2)}U`),
        metric("今日已實現", `${num(data.risk.daily_realized_pnl, 4)}U`),
        metric("動態出場", `${data.risk.dynamic_exit.breakeven_r}R / ${data.risk.dynamic_exit.partial_take_profit_r}R`)
      ].join("");

      const ctx = data.external_context;
      el("contextGrid").innerHTML = [
        metric("總情緒", pct(ctx.external_context_score)),
        metric("恐慌貪婪", pct(ctx.fear_greed_score)),
        metric("新聞", pct(ctx.newsapi_score)),
        metric("GDELT", pct(ctx.gdelt_score)),
        metric("方向支持", pct(ctx.external_context_support)),
        metric("風險乘數", ctx.risk_multiplier ?? "-")
      ].join("");

      const positions = data.state.positions || {};
      const rows = Object.entries(positions).map(([symbol, p]) => {
        const side = p.side || "flat";
        return `<div class="metric">
          <div class="label">${symbol}</div>
          <div class="value">${side}</div>
          <div class="small">數量 ${p.position_base} · 均價 ${p.entry_price} · SL ${p.stop_loss_price}</div>
        </div>`;
      });
      el("positionBox").innerHTML = rows.length ? rows.join("") : "無本地倉位紀錄";

      const trades = data.state.recent_trades || [];
      el("tradeRows").innerHTML = trades.length ? trades.map((trade) => `<tr>
        <td>${(trade.time || "").slice(0, 19)}</td>
        <td>${trade.side || "-"}</td>
        <td>${trade.amount_base || "-"}</td>
        <td>${trade.price || "-"}</td>
        <td>${trade.realized_pnl || "-"}</td>
      </tr>`).join("") : `<tr><td colspan="5">無紀錄</td></tr>`;
    }

    async function runBacktest() {
      const btn = el("backtestBtn");
      btn.disabled = true;
      el("backtestResult").textContent = "回測中";
      try {
        const days = encodeURIComponent(el("daysInput").value || "365");
        const trades = encodeURIComponent(el("tradesInput").value || "1000");
        const response = await fetch(`/api/backtest?days=${days}&trades=${trades}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "backtest failed");
        const s = data.summary;
        el("backtestResult").innerHTML = `
          <div class="metric-grid">
            ${metric("交易數", s.reported_trades)}
            ${metric("勝率", s.win_rate)}
            ${metric("淨利", `${num(s.total_net_pnl, 2)}U`)}
            ${metric("帳戶報酬", s.account_return)}
          </div>
          <div class="small" style="margin-top:10px">本金 ${num(s.starting_equity, 2)}U，期末 ${num(s.ending_equity, 2)}U，平均倉位 ${num(s.average_notional, 2)}U</div>
        `;
      } catch (error) {
        el("backtestResult").innerHTML = `<div class="panel error">${error.message}</div>`;
      } finally {
        btn.disabled = false;
      }
    }

    el("refreshBtn").addEventListener("click", loadSnapshot);
    el("backtestBtn").addEventListener("click", runBacktest);
    el("backtestBtnTop").addEventListener("click", runBacktest);
    loadSnapshot();
    setInterval(loadSnapshot, 60000);
  </script>
</body>
</html>"""
