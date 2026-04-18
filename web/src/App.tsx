import { useEffect, useMemo, useRef, useState } from "react";

type ViewKey = "overview" | "decisions" | "orders" | "portfolio" | "events" | "settings" | "daemon";

type StatusPayload = {
  trading_mode: string;
  market_family: string;
  live_trading_enabled: boolean;
  open_positions: number;
  available_usd: number;
  paper_available_usd: number;
  funded_balance_usd: number | null;
  available_usd_source: string;
  daily_realized_pnl: number;
  rejected_orders: number;
};

type AuthPayload = {
  readonly_ready: boolean;
  balance: number | null;
  wallet_address: string;
  open_orders_count: number;
  diagnostics_collected: boolean;
};

type LiveActivityPayload = {
  market_id: string;
  last_poll: {
    polled_at: string;
    time_remaining_seconds: number;
    time_remaining_minutes: number;
    trade_counts: {
      yes: number;
      no: number;
      other: number;
      total: number;
    };
  };
  preflight: {
    blockers: string[];
    market: {
      question: string;
      implied_probability: number;
      liquidity_usd: number;
      seconds_to_expiry: number;
    };
    assessment: {
      fair_probability: number;
      confidence: number;
      edge: number;
      suggested_side: string;
    };
  };
  tracked_orders: {
    count: number;
    active_count: number;
    terminal_count: number;
  };
  recent_trades: {
    count: number;
  };
};

type PortfolioSummaryPayload = {
  open_positions: number;
  closed_positions: number;
  total_realized_pnl: number;
  daily_realized_pnl: number;
  open_position_notional: number;
};

type ClosedPosition = {
  market_id: string;
  side: string;
  size_usd: number;
  entry_price: number;
  exit_price: number;
  close_reason: string;
  realized_pnl: number;
  cumulative_pnl: number;
  closed_at: string | null;
};

type ClosedPositionsPayload = {
  count: number;
  positions: ClosedPosition[];
};

type EquityPoint = {
  sequence: number;
  market_id: string;
  closed_at: string | null;
  realized_pnl: number;
  equity: number;
};

type EquityCurvePayload = {
  count: number;
  points: EquityPoint[];
};

type ReportPayload = {
  session_id: string;
  generated_at: string;
  summary: string;
  items: string[];
};

type RecentEvent = {
  event_type: string;
  logged_at: string;
  payload: Record<string, unknown>;
};

type RecentEventsPayload = {
  count: number;
  events: RecentEvent[];
};

type DecisionItem = {
  event_type: string;
  logged_at: string;
  payload: Record<string, unknown>;
};

type DecisionsPayload = {
  count: number;
  decisions: DecisionItem[];
};

type LiveOrder = {
  order_id: string;
  market_id?: string;
  side?: string;
  status: string;
  price?: number;
  size?: number;
  size_matched?: number;
  created_at?: string;
  asset_id?: string;
};

type LiveOrdersPayload = {
  count: number;
  orders: LiveOrder[];
};

type LiveTrade = {
  trade_id: string;
  order_id?: string;
  market_id?: string;
  status?: string;
  side?: string;
  amount?: number;
  asset_id?: string;
  price?: number;
  size?: number;
  created_at?: string;
};

type LiveTradesPayload = {
  count: number;
  trades: LiveTrade[];
};

type DaemonHeartbeatPayload = {
  age_seconds: number | null;
  heartbeat: {
    written_at: string;
    metrics: {
      started_at: string;
      active_market_count: number;
      polymarket_events: number;
      btc_ticks: number;
      decision_ticks: number;
      last_decision_latency_ms: number;
      safety_stop_reason: string | null;
      maintenance_runs: number;
    };
    btc_last_price: number | null;
    btc_seconds_since_last_update: number | null;
    safety_stop_reason: string | null;
    market_family: string;
    active_market_ids: string[];
  } | null;
};

type DaemonTickPayload = {
  market_id: string;
  question: string;
  seconds_to_expiry: number;
  bid_yes: number;
  ask_yes: number;
  fair_probability: number;
  fair_probability_no: number;
  edge_yes: number;
  edge_no: number;
  suggested_side: string;
  confidence: number;
  btc_price: number | null;
  btc_realized_vol_30m: number | null;
  expiry_risk: string;
};

type SettingsFieldMeta = {
  label: string;
  type: "text" | "number" | "boolean" | "select";
  group: "runtime" | "live" | "thresholds" | "paper";
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
};

type SettingsPayload = {
  values: Record<string, string | number | boolean>;
  overrides: Record<string, string | number | boolean>;
  fields: Record<string, SettingsFieldMeta>;
};

type DashboardState = {
  status: StatusPayload | null;
  auth: AuthPayload | null;
  settings: SettingsPayload | null;
  liveActivity: LiveActivityPayload | null;
  portfolioSummary: PortfolioSummaryPayload | null;
  closedPositions: ClosedPositionsPayload | null;
  equityCurve: EquityCurvePayload | null;
  report: ReportPayload | null;
  recentEvents: RecentEvent[];
  recentDecisions: DecisionItem[];
  liveOrders: LiveOrder[];
  liveTrades: LiveTrade[];
  daemonHeartbeat: DaemonHeartbeatPayload | null;
  daemonTicks: DaemonTickPayload[];
};

type DashboardSnapshotPayload = {
  status: StatusPayload;
  auth: AuthPayload;
  settings: SettingsPayload;
  live_activity: LiveActivityPayload;
  portfolio_summary: PortfolioSummaryPayload;
  closed_positions: ClosedPositionsPayload;
  equity_curve: EquityCurvePayload;
  report: ReportPayload;
  recent_events: RecentEventsPayload;
  recent_decisions: DecisionsPayload;
  live_orders: LiveOrdersPayload;
  live_trades: LiveTradesPayload;
  daemon_heartbeat: DaemonHeartbeatPayload;
  daemon_ticks: { ticks: DaemonTickPayload[] };
};

const VIEWS: Array<{ key: ViewKey; label: string }> = [
  { key: "overview", label: "Overview" },
  { key: "decisions", label: "Signal History" },
  { key: "orders", label: "Orders & Trades" },
  { key: "portfolio", label: "Portfolio" },
  { key: "events", label: "Event Log" },
  { key: "settings", label: "Settings" },
  { key: "daemon", label: "Daemon" },
];

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(`Request failed for ${path}: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function sendJson<T>(path: string, method: "POST" | "PUT", body: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const payload = await response.json();
      detail = payload.detail || JSON.stringify(payload);
    } catch {
      detail = await response.text();
    }
    throw new Error(`Request failed for ${path}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  return `$${value.toFixed(2)}`;
}

function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  return `${(value * 100).toFixed(1)}%`;
}

function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined) return "n/a";
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainingSeconds = seconds % 60;
  const parts: string[] = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  if (!parts.length || remainingSeconds) parts.push(`${remainingSeconds}s`);
  return parts.join(" ");
}


function getInitialView(): ViewKey {
  const hash = window.location.hash.replace("#", "") as ViewKey;
  return VIEWS.some((item) => item.key === hash) ? hash : "overview";
}

function mapSnapshotToState(snapshot: DashboardSnapshotPayload): DashboardState {
  return {
    status: snapshot.status,
    auth: snapshot.auth,
    settings: snapshot.settings,
    liveActivity: snapshot.live_activity,
    portfolioSummary: snapshot.portfolio_summary,
    closedPositions: snapshot.closed_positions,
    equityCurve: snapshot.equity_curve,
    report: snapshot.report,
    recentEvents: snapshot.recent_events.events,
    recentDecisions: snapshot.recent_decisions.decisions,
    liveOrders: snapshot.live_orders.orders,
    liveTrades: snapshot.live_trades.trades,
    daemonHeartbeat: snapshot.daemon_heartbeat ?? null,
    daemonTicks: snapshot.daemon_ticks?.ticks ?? [],
  };
}

function applyDashboardDelta(current: DashboardState, eventName: string, payload: unknown): DashboardState {
  switch (eventName) {
    case "status":
      return { ...current, status: payload as StatusPayload };
    case "auth":
      return { ...current, auth: payload as AuthPayload };
    case "settings":
      return { ...current, settings: payload as SettingsPayload };
    case "live_activity":
      return { ...current, liveActivity: payload as LiveActivityPayload };
    case "portfolio_summary":
      return { ...current, portfolioSummary: payload as PortfolioSummaryPayload };
    case "closed_positions":
      return { ...current, closedPositions: payload as ClosedPositionsPayload };
    case "equity_curve":
      return { ...current, equityCurve: payload as EquityCurvePayload };
    case "report":
      return { ...current, report: payload as ReportPayload };
    case "recent_events":
      return { ...current, recentEvents: (payload as RecentEventsPayload).events };
    case "recent_decisions":
      return { ...current, recentDecisions: (payload as DecisionsPayload).decisions };
    case "live_orders":
      return { ...current, liveOrders: (payload as LiveOrdersPayload).orders };
    case "live_trades":
      return { ...current, liveTrades: (payload as LiveTradesPayload).trades };
    case "daemon_heartbeat":
      return { ...current, daemonHeartbeat: payload as DaemonHeartbeatPayload };
    case "daemon_ticks":
      return { ...current, daemonTicks: (payload as { ticks: DaemonTickPayload[] }).ticks };
    default:
      return current;
  }
}

function PnlChart({ points }: { points: EquityPoint[] }) {
  const polylinePoints = useMemo(() => {
    if (!points.length) return "";
    const max = Math.max(...points.map((item) => item.equity), 0.01);
    const min = Math.min(...points.map((item) => item.equity), 0);
    const range = Math.max(max - min, 0.01);
    return points
      .map((item, index) => {
        const x = points.length === 1 ? 0 : (index / (points.length - 1)) * 100;
        const y = 100 - ((item.equity - min) / range) * 100;
        return `${x},${y}`;
      })
      .join(" ");
  }, [points]);

  if (!points.length) return <div className="empty-state">No closed positions yet.</div>;

  return (
    <svg className="chart" viewBox="0 0 100 100" preserveAspectRatio="none">
      <polyline fill="none" stroke="currentColor" strokeWidth="2" points={polylinePoints} />
    </svg>
  );
}

function OverviewPage({ state }: { state: DashboardState }) {
  const { auth, status, portfolioSummary, liveActivity, equityCurve } = state;
  return (
    <>
      <section className="grid cards">
        <article className="card">
          <h2>Account</h2>
          <p title={auth?.wallet_address}>{auth?.wallet_address || "n/a"}</p>
          <dl>
            <div><dt>Balance</dt><dd>{formatMoney(auth?.balance)}</dd></div>
            <div><dt>Open Orders</dt><dd>{auth?.open_orders_count ?? 0}</dd></div>
            <div><dt>Diagnostics</dt><dd>{auth?.diagnostics_collected ? "Collected" : "Pending"}</dd></div>
          </dl>
        </article>

        <article className="card">
          <h2>Strategy</h2>
          <dl>
            <div><dt>Mode</dt><dd>{status?.trading_mode || "n/a"}</dd></div>
            <div><dt>Market Family</dt><dd>{status?.market_family || "n/a"}</dd></div>
            <div><dt>Available USD</dt><dd>{formatMoney(status?.available_usd)}</dd></div>
            <div><dt>Balance Source</dt><dd>{status?.available_usd_source === "funded_balance" ? "Polymarket" : "Paper"}</dd></div>
            <div><dt>Rejected Orders</dt><dd>{status?.rejected_orders ?? 0}</dd></div>
          </dl>
        </article>

        <article className="card">
          <h2>Portfolio</h2>
          <dl>
            <div><dt>Total PnL</dt><dd>{formatMoney(portfolioSummary?.total_realized_pnl)}</dd></div>
            <div><dt>Daily PnL</dt><dd>{formatMoney(portfolioSummary?.daily_realized_pnl)}</dd></div>
            <div><dt>Closed Trades</dt><dd>{portfolioSummary?.closed_positions ?? 0}</dd></div>
            <div><dt>Open Notional</dt><dd>{formatMoney(portfolioSummary?.open_position_notional)}</dd></div>
          </dl>
        </article>

        <article className="card">
          <h2>Last Poll</h2>
          <p>{liveActivity?.preflight?.market?.question || "n/a"}</p>
          <dl>
            <div><dt>Time Remaining</dt><dd>{formatDuration(liveActivity?.last_poll?.time_remaining_seconds)}</dd></div>
            <div><dt>Yes Trades</dt><dd>{liveActivity?.last_poll?.trade_counts?.yes ?? 0}</dd></div>
            <div><dt>No Trades</dt><dd>{liveActivity?.last_poll?.trade_counts?.no ?? 0}</dd></div>
            <div><dt>Total Trades</dt><dd>{liveActivity?.last_poll?.trade_counts?.total ?? 0}</dd></div>
          </dl>
        </article>
      </section>

      <section className="grid detail-grid">
        <article className="panel">
          <div className="panel-header">
            <h2>Equity Curve</h2>
            <span>{equityCurve?.count ?? 0} realized points</span>
          </div>
          <PnlChart points={equityCurve?.points ?? []} />
        </article>

        <article className="panel">
          <div className="panel-header">
            <h2>Decision</h2>
            <span>{liveActivity?.market_id || "n/a"}</span>
          </div>
          <div className="decision-grid">
            <div>
              <label>Suggested Side</label>
              <strong>{liveActivity?.preflight?.assessment?.suggested_side || "n/a"}</strong>
            </div>
            <div>
              <label>Edge</label>
              <strong>{formatPct(liveActivity?.preflight?.assessment?.edge)}</strong>
            </div>
            <div>
              <label>Time Remaining</label>
              <strong>{formatDuration(liveActivity?.last_poll?.time_remaining_seconds)}</strong>
            </div>
            <div>
              <label>Tracked Orders</label>
              <strong>{liveActivity?.tracked_orders?.count ?? 0}</strong>
            </div>
            <div>
              <label>Recent Trades</label>
              <strong>{liveActivity?.recent_trades?.count ?? 0}</strong>
            </div>
          </div>
        </article>
      </section>
    </>
  );
}

function DecisionsPage({ decisions }: { decisions: DecisionItem[] }) {
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Signal History</h2>
        <span>{decisions.length} daemon ticks</span>
      </div>
      {decisions.length === 0 ? (
        <div className="empty-state">No signal history yet — start the daemon to populate this view.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Market</th>
                <th>Side</th>
                <th>Fair</th>
                <th>Edge YES</th>
                <th>Edge NO</th>
                <th>Confidence</th>
                <th>TTE</th>
              </tr>
            </thead>
            <tbody>
              {[...decisions].reverse().map((item, index) => {
                const p = item.payload;
                const side = String(p.suggested_side ?? "");
                const sideClass = side === "YES" ? "side-yes" : side === "NO" ? "side-no" : "side-abstain";
                const edgeYes = typeof p.edge_yes === "number" ? p.edge_yes : null;
                const edgeNo = typeof p.edge_no === "number" ? p.edge_no : null;
                const fair = typeof p.fair_probability === "number" ? p.fair_probability : null;
                const conf = typeof p.confidence === "number" ? p.confidence : null;
                const tte = typeof p.seconds_to_expiry === "number" ? p.seconds_to_expiry : null;
                const question = typeof p.question === "string" ? p.question : String(p.market_id ?? "");
                return (
                  <tr key={`${item.logged_at}-${index}`}>
                    <td style={{ whiteSpace: "nowrap", color: "var(--muted)", fontSize: "12px" }}>{item.logged_at.slice(11, 19)}</td>
                    <td title={question}>{question.length > 42 ? `${question.slice(0, 42)}…` : question}</td>
                    <td><span className={sideClass}>{side || "n/a"}</span></td>
                    <td>{fair !== null ? `${(fair * 100).toFixed(1)}%` : "n/a"}</td>
                    <td className={edgeYes !== null && edgeYes > 0 ? "positive" : edgeYes !== null ? "negative" : ""}>
                      {edgeYes !== null ? `${edgeYes >= 0 ? "+" : ""}${(edgeYes * 100).toFixed(2)}%` : "n/a"}
                    </td>
                    <td className={edgeNo !== null && edgeNo > 0 ? "positive" : edgeNo !== null ? "negative" : ""}>
                      {edgeNo !== null ? `${edgeNo >= 0 ? "+" : ""}${(edgeNo * 100).toFixed(2)}%` : "n/a"}
                    </td>
                    <td>{conf !== null ? `${(conf * 100).toFixed(0)}%` : "n/a"}</td>
                    <td style={{ whiteSpace: "nowrap" }}>{formatDuration(tte)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function EventEntry({
  title,
  timestamp,
  content,
  defaultExpanded = false,
}: {
  title: string;
  timestamp: string;
  content: string;
  defaultExpanded?: boolean;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const preview = content.length > 180 ? `${content.slice(0, 180)}...` : content;
  return (
    <li className="event-entry">
      <div className="event-entry-header">
        <div>
          <strong>{title}</strong>
          <div className="event-time">{timestamp}</div>
        </div>
        <button type="button" className="toggle-button" onClick={() => setExpanded((value) => !value)}>
          {expanded ? "Collapse" : "Expand"}
        </button>
      </div>
      <pre className="event-preview">{expanded ? content : preview}</pre>
    </li>
  );
}

function OrdersPage({ liveOrders, liveTrades, liveActivity }: { liveOrders: LiveOrder[]; liveTrades: LiveTrade[]; liveActivity: LiveActivityPayload | null }) {
  const [selectedOrderId, setSelectedOrderId] = useState<string>("");
  const [selectedTradeId, setSelectedTradeId] = useState<string>("");
  const selectedOrder = liveOrders.find((order) => order.order_id === selectedOrderId) ?? liveOrders[0];
  const selectedTrade = liveTrades.find((trade) => trade.trade_id === selectedTradeId) ?? liveTrades[0];
  return (
    <section className="grid detail-grid">
      <article className="panel">
        <div className="panel-header">
          <h2>Open Orders</h2>
          <span>{liveOrders.length} open</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Order ID</th>
                <th>Market</th>
                <th>Side</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {liveOrders.map((order) => (
                <tr key={order.order_id} className={selectedOrder?.order_id === order.order_id ? "selected-row" : ""} onClick={() => setSelectedOrderId(order.order_id)}>
                  <td>{order.order_id}</td>
                  <td>{order.market_id || "n/a"}</td>
                  <td>{order.side || "n/a"}</td>
                  <td>{order.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!liveOrders.length && <div className="empty-state">No open live orders.</div>}
        </div>
      </article>

      <article className="panel">
        <div className="panel-header">
          <h2>Recent Trades</h2>
          <span>{liveTrades.length} recent</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Trade ID</th>
                <th>Order</th>
                <th>Market</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {liveTrades.map((trade) => (
                <tr key={trade.trade_id} className={selectedTrade?.trade_id === trade.trade_id ? "selected-row" : ""} onClick={() => setSelectedTradeId(trade.trade_id)}>
                  <td>{trade.trade_id}</td>
                  <td>{trade.order_id || "n/a"}</td>
                  <td>{trade.market_id || "n/a"}</td>
                  <td>{trade.status || "n/a"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!liveTrades.length && <div className="empty-state">No recent live trades.</div>}
        </div>
      </article>

      <article className="panel full-span">
        <div className="panel-header">
          <h2>Detail Drawer</h2>
          <span>{liveActivity?.market_id || "n/a"}</span>
        </div>
        <div className="detail-drawer-grid">
          <div>
            <label>Selected Order</label>
            <strong>{selectedOrder?.order_id || "n/a"}</strong>
            <p className="detail-copy">
              status={selectedOrder?.status || "n/a"} | side={selectedOrder?.side || "n/a"} | market={selectedOrder?.market_id || "n/a"}
            </p>
            <p className="detail-copy">
              price={selectedOrder?.price ?? "n/a"} | size={selectedOrder?.size ?? "n/a"} | matched={selectedOrder?.size_matched ?? "n/a"}
            </p>
            <p className="detail-copy">created={selectedOrder?.created_at || "n/a"} | asset={selectedOrder?.asset_id || "n/a"}</p>
          </div>
          <div>
            <label>Selected Trade</label>
            <strong>{selectedTrade?.trade_id || "n/a"}</strong>
            <p className="detail-copy">
              status={selectedTrade?.status || "n/a"} | side={selectedTrade?.side || "n/a"} | market={selectedTrade?.market_id || "n/a"}
            </p>
            <p className="detail-copy">
              price={selectedTrade?.price ?? "n/a"} | size={selectedTrade?.size ?? "n/a"} | amount={selectedTrade?.amount ?? "n/a"}
            </p>
            <p className="detail-copy">created={selectedTrade?.created_at || "n/a"} | asset={selectedTrade?.asset_id || "n/a"}</p>
          </div>
          <div>
            <label>Live Preflight</label>
            <strong>{liveActivity?.preflight?.market?.question || "n/a"}</strong>
            <p className="detail-copy">blockers={liveActivity?.preflight?.blockers?.join(", ") || "none"}</p>
            <p className="detail-copy">
              implied={formatPct(liveActivity?.preflight?.market?.implied_probability)} | fair={formatPct(liveActivity?.preflight?.assessment?.fair_probability)}
            </p>
            <p className="detail-copy">
              confidence={formatPct(liveActivity?.preflight?.assessment?.confidence)} | liquidity={formatMoney(liveActivity?.preflight?.market?.liquidity_usd)}
            </p>
          </div>
        </div>
      </article>
    </section>
  );
}

function PortfolioPage({ summary, positions, equityCurve }: { summary: PortfolioSummaryPayload | null; positions: ClosedPosition[]; equityCurve: EquityCurvePayload | null }) {
  return (
    <section className="grid detail-grid">
      <article className="panel">
        <div className="panel-header">
          <h2>Equity Curve</h2>
          <span>{equityCurve?.count ?? 0} closed points</span>
        </div>
        <PnlChart points={equityCurve?.points ?? []} />
        <div className="axis-labels">
          <span>{equityCurve?.points[0]?.closed_at?.slice(0, 10) || "start"}</span>
          <span>{equityCurve?.points[equityCurve.points.length - 1]?.closed_at?.slice(0, 10) || "latest"}</span>
        </div>
      </article>

      <article className="panel">
        <div className="panel-header">
          <h2>Portfolio Metrics</h2>
          <span>Realized performance</span>
        </div>
        <dl>
          <div><dt>Total Realized PnL</dt><dd>{formatMoney(summary?.total_realized_pnl)}</dd></div>
          <div><dt>Daily Realized PnL</dt><dd>{formatMoney(summary?.daily_realized_pnl)}</dd></div>
          <div><dt>Closed Positions</dt><dd>{summary?.closed_positions ?? 0}</dd></div>
          <div><dt>Open Position Notional</dt><dd>{formatMoney(summary?.open_position_notional)}</dd></div>
        </dl>
      </article>

      <article className="panel full-span">
        <div className="panel-header">
          <h2>Closed Positions</h2>
          <span>Latest realized trades</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Market</th>
                <th>Side</th>
                <th>Size</th>
                <th>PnL</th>
                <th>Cumulative</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((position) => (
                <tr key={`${position.market_id}-${position.closed_at}`}>
                  <td>{position.market_id}</td>
                  <td>{position.side}</td>
                  <td>{formatMoney(position.size_usd)}</td>
                  <td className={position.realized_pnl >= 0 ? "positive" : "negative"}>{formatMoney(position.realized_pnl)}</td>
                  <td>{formatMoney(position.cumulative_pnl)}</td>
                  <td>{position.close_reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!positions.length && <div className="empty-state">No closed positions yet.</div>}
        </div>
      </article>
    </section>
  );
}

function EventsPage({ events, report }: { events: RecentEvent[]; report: ReportPayload | null }) {
  const visibleEvents = events.length ? events : [];
  return (
    <section className="grid detail-grid">
      <article className="panel">
        <div className="panel-header">
          <h2>Streamed Events</h2>
          <span>{visibleEvents.length} items</span>
        </div>
        <ul className="event-list">
          {visibleEvents.map((item, index) => (
            <EventEntry
              key={`${item.logged_at}-${index}`}
              title={item.event_type}
              timestamp={item.logged_at}
              content={JSON.stringify(item.payload, null, 2)}
            />
          ))}
        </ul>
      </article>

      <article className="panel">
        <div className="panel-header">
          <h2>Operator Report</h2>
          <span>{report?.summary || "n/a"}</span>
        </div>
        <ul className="event-list">
          {(report?.items ?? []).slice(0, 12).map((item, index) => (
            <EventEntry
              key={`${report?.session_id ?? "report"}-${index}`}
              title="report_item"
              timestamp={report?.generated_at || "n/a"}
              content={item}
            />
          ))}
        </ul>
      </article>
    </section>
  );
}

function SettingsPage({
  settings,
  onSettingsUpdated,
  onRefresh,
}: {
  settings: SettingsPayload | null;
  onSettingsUpdated: (settings: SettingsPayload) => void;
  onRefresh: () => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string | number | boolean>>({});
  const [watchIterations, setWatchIterations] = useState(3);
  const [watchInterval, setWatchInterval] = useState(2);
  const [saveMessage, setSaveMessage] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionResult, setActionResult] = useState<string>("");
  const [busyAction, setBusyAction] = useState("");

  useEffect(() => {
    setValues(settings?.values ?? {});
  }, [settings]);

  if (!settings) {
    return <section className="panel"><div className="empty-state">Settings are loading.</div></section>;
  }

  const groupedKeys = Object.entries(settings.fields).reduce<Record<string, string[]>>((acc, [key, meta]) => {
    acc[meta.group] = [...(acc[meta.group] ?? []), key];
    return acc;
  }, {});

  const updateValue = (key: string, value: string | number | boolean) => {
    setValues((current) => ({ ...current, [key]: value }));
  };

  const runAction = async (path: string, body: Record<string, unknown>, label: string) => {
    setBusyAction(label);
    setActionError("");
    setActionMessage("");
    try {
      const result = await sendJson<Record<string, unknown>>(path, "POST", body);
      setActionResult(JSON.stringify(result, null, 2));
      setActionMessage(`${label} completed.`);
      await onRefresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : `${label} failed.`);
    } finally {
      setBusyAction("");
    }
  };

  const saveSettings = async () => {
    setSaveMessage("");
    try {
      const updated = await sendJson<SettingsPayload>("/api/settings", "PUT", { values });
      onSettingsUpdated(updated);
      setSaveMessage("Runtime settings saved.");
      await onRefresh();
    } catch (error) {
      setSaveMessage(error instanceof Error ? error.message : "Failed to save settings.");
    }
  };

  const renderField = (key: string) => {
    const meta = settings.fields[key];
    const value = values[key];
    const isOverridden = Object.prototype.hasOwnProperty.call(settings.overrides, key);
    if (meta.type === "boolean") {
      return (
        <label key={key} className="settings-field checkbox-field">
          <span>{meta.label}</span>
          <input
            type="checkbox"
            checked={Boolean(value)}
            onChange={(event) => updateValue(key, event.target.checked)}
          />
          <small>{isOverridden ? "runtime override" : "env/default"}</small>
        </label>
      );
    }
    if (meta.type === "select") {
      return (
        <label key={key} className="settings-field">
          <span>{meta.label}</span>
          <select value={String(value ?? "")} onChange={(event) => updateValue(key, event.target.value)}>
            {(meta.options ?? []).map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
          <small>{isOverridden ? "runtime override" : "env/default"}</small>
        </label>
      );
    }
    return (
      <label key={key} className="settings-field">
        <span>{meta.label}</span>
        <input
          type={meta.type === "number" ? "number" : "text"}
          min={meta.min}
          max={meta.max}
          step={meta.step}
          value={String(value ?? "")}
          onChange={(event) =>
            updateValue(key, meta.type === "number" ? Number(event.target.value) : event.target.value)
          }
        />
        <small>{isOverridden ? "runtime override" : "env/default"}</small>
      </label>
    );
  };

  return (
    <section className="grid detail-grid">
      <article className="panel">
        <div className="panel-header">
          <h2>Runtime Settings</h2>
          <span>{Object.keys(settings.fields).length} editable fields</span>
        </div>
        <div className="settings-groups">
          {Object.keys(groupedKeys).map((group) => (
            <section key={group} className="settings-group">
              <h3>{group}</h3>
              <div className="settings-grid">
                {(groupedKeys[group] ?? []).map(renderField)}
              </div>
            </section>
          ))}
        </div>
        <div className="settings-actions">
          <button type="button" className="refresh-button" onClick={() => void saveSettings()}>
            Save Settings
          </button>
          {saveMessage && <span className="inline-message">{saveMessage}</span>}
        </div>

        <div style={{ marginTop: "24px" }}>
          <div className="panel-header">
            <h2>Per-Family Risk Profiles (read-only)</h2>
            <span>From config defaults</span>
          </div>
          <div className="table-wrap">
            <table className="risk-table">
              <thead>
                <tr>
                  <th>Family</th>
                  <th>Stale Data (s)</th>
                  <th>Exit Buffer % × Window</th>
                  <th>Max Concurrent</th>
                  <th>Exit Buffer Floor</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { family: "btc_1h",  stale_data_seconds: 5, exit_buffer_pct_of_tte: 0.05, max_concurrent_positions: 2, floor_seconds: 30, window: "3600s" },
                  { family: "btc_15m", stale_data_seconds: 3, exit_buffer_pct_of_tte: 0.07, max_concurrent_positions: 2, floor_seconds: 15, window: "900s" },
                  { family: "btc_5m",  stale_data_seconds: 2, exit_buffer_pct_of_tte: 0.10, max_concurrent_positions: 1, floor_seconds: 10, window: "300s" },
                ].map((row) => (
                  <tr key={row.family}>
                    <td><code>{row.family}</code></td>
                    <td>{row.stale_data_seconds}</td>
                    <td>{(row.exit_buffer_pct_of_tte * 100).toFixed(0)}% × {row.window} = {(row.exit_buffer_pct_of_tte * parseInt(row.window)).toFixed(0)}s</td>
                    <td>{row.max_concurrent_positions}</td>
                    <td>{row.floor_seconds}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </article>

      <article className="panel">
        <div className="panel-header">
          <h2>GUI Actions</h2>
          <span>Operator-safe API controls</span>
        </div>
        <div className="action-grid">
          <button
            type="button"
            className="refresh-button"
            disabled={busyAction === "simulate-active"}
            onClick={() => void runAction("/api/actions/simulate-active", { active: true }, "simulate-active")}
          >
            Simulate Active
          </button>
          <button
            type="button"
            className="refresh-button"
            disabled={busyAction === "live-preflight"}
            onClick={() => void runAction("/api/actions/live-preflight", { active: true }, "live-preflight")}
          >
            Live Preflight
          </button>
          <button
            type="button"
            className="refresh-button"
            disabled={busyAction === "live-reconcile"}
            onClick={() => void runAction("/api/actions/live-reconcile", { active: true }, "live-reconcile")}
          >
            Live Reconcile
          </button>
        </div>
        <div className="watch-controls">
          <label className="settings-field">
            <span>Watch Iterations</span>
            <input type="number" min={1} max={100} value={watchIterations} onChange={(event) => setWatchIterations(Number(event.target.value))} />
          </label>
          <label className="settings-field">
            <span>Watch Interval Seconds</span>
            <input type="number" min={0} max={60} value={watchInterval} onChange={(event) => setWatchInterval(Number(event.target.value))} />
          </label>
          <button
            type="button"
            className="refresh-button"
            disabled={busyAction === "live-watch"}
            onClick={() =>
              void runAction(
                "/api/actions/live-watch",
                { active: true, iterations: watchIterations, interval_seconds: watchInterval },
                "live-watch",
              )
            }
          >
            Live Watch
          </button>
        </div>
        {actionMessage && <div className="banner">{actionMessage}</div>}
        {actionError && <div className="banner error">{actionError}</div>}
        <div className="panel-header">
          <h2>Last Action Result</h2>
          <span>{busyAction ? `Running ${busyAction}` : "idle"}</span>
        </div>
        <pre className="event-preview action-result">{actionResult || "Run an action to inspect the response."}</pre>
      </article>
    </section>
  );
}

function heartbeatAgeClass(age: number | null): string {
  if (age === null) return "pill blocked";
  if (age < 15) return "pill ready";
  if (age < 60) return "pill stream-connecting";
  return "pill blocked";
}

function DaemonView({ heartbeat, ticks }: { heartbeat: DaemonHeartbeatPayload | null; ticks: DaemonTickPayload[] }) {
  const hb = heartbeat?.heartbeat ?? null;
  const age = heartbeat?.age_seconds ?? null;
  const metrics = hb?.metrics ?? null;
  const daemonRunning = age !== null && age < 60;

  return (
    <>
      {!daemonRunning && (
        <div className="banner error">Daemon not running — heartbeat is absent or stale.</div>
      )}

      <div className="daemon-header">
        <span className="pill">
          BTC: {formatMoney(hb?.btc_last_price)}
        </span>
        <span className={heartbeatAgeClass(age)}>
          Heartbeat: {age !== null ? `${age.toFixed(1)}s ago` : "absent"}
        </span>
        <span className={hb?.safety_stop_reason ? "pill blocked" : "pill ready"}>
          Safety stop: {hb?.safety_stop_reason ?? "None"}
        </span>
        <span className="pill">
          Active markets: {metrics?.active_market_count ?? 0}
        </span>
        <span className="pill">
          Family: {hb?.market_family ?? "n/a"}
        </span>
      </div>

      <div className="daemon-stat-grid">
        <article className="card">
          <h2>Polymarket Events</h2>
          <strong className="card-stat">{metrics?.polymarket_events?.toLocaleString() ?? "0"}</strong>
        </article>
        <article className="card">
          <h2>BTC Ticks</h2>
          <strong className="card-stat">{metrics?.btc_ticks?.toLocaleString() ?? "0"}</strong>
        </article>
        <article className="card">
          <h2>Decision Ticks</h2>
          <strong className="card-stat">{metrics?.decision_ticks?.toLocaleString() ?? "0"}</strong>
        </article>
        <article className="card">
          <h2>Last Latency</h2>
          <strong className={`card-stat${metrics?.last_decision_latency_ms != null && metrics.last_decision_latency_ms > 100 ? " danger" : ""}`}>
            {metrics?.last_decision_latency_ms != null ? `${metrics.last_decision_latency_ms.toFixed(2)} ms` : "n/a"}
          </strong>
        </article>
      </div>

      {ticks.length === 0 ? (
        <div className="empty-state">No daemon tick data — daemon may not be running or no markets are active.</div>
      ) : (
        <div className="daemon-market-grid">
          {ticks.map((tick) => {
            const fairYes = tick.fair_probability ?? 0;
            const fairNo = tick.fair_probability_no ?? (1 - fairYes);
            const sideClass =
              tick.suggested_side === "YES" ? "side-yes" :
              tick.suggested_side === "NO" ? "side-no" : "side-abstain";
            const expiryClass =
              tick.expiry_risk === "HIGH" ? "side-no" :
              tick.expiry_risk === "MEDIUM" ? "side-abstain" : "side-yes";
            return (
              <div key={tick.market_id} className="daemon-market-card">
                <div className="market-card-title">
                  {tick.question ? (tick.question.length > 55 ? `${tick.question.slice(0, 55)}...` : tick.question) : tick.market_id}
                </div>
                <div className="market-card-meta">
                  <span>TTE: {formatDuration(tick.seconds_to_expiry)}</span>
                  <span>Bid: {tick.bid_yes?.toFixed(3) ?? "n/a"}</span>
                  <span>Ask: {tick.ask_yes?.toFixed(3) ?? "n/a"}</span>
                </div>

                <div className="market-card-prob">
                  <div className="market-card-prob-labels">
                    <span>YES {(fairYes * 100).toFixed(1)}%</span>
                    <span>NO {(fairNo * 100).toFixed(1)}%</span>
                  </div>
                  <div className="prob-bar">
                    <div style={{ width: `${fairYes * 100}%` }} />
                  </div>
                </div>

                <div className="market-card-edges">
                  <div>
                    Edge YES: <span className={tick.edge_yes > 0 ? "edge-positive" : "edge-negative"}>
                      {tick.edge_yes != null ? (tick.edge_yes * 100).toFixed(2) : "n/a"}%
                    </span>
                  </div>
                  <div>
                    Edge NO: <span className={tick.edge_no > 0 ? "edge-positive" : "edge-negative"}>
                      {tick.edge_no != null ? (tick.edge_no * 100).toFixed(2) : "n/a"}%
                    </span>
                  </div>
                </div>

                <div className="market-card-footer">
                  <span className={sideClass}>{tick.suggested_side}</span>
                  <span className="market-card-confidence">
                    Confidence: {tick.confidence != null ? `${(tick.confidence * 100).toFixed(0)}%` : "n/a"}
                  </span>
                  {tick.expiry_risk && (
                    <span className={expiryClass}>
                      {tick.expiry_risk} expiry risk
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

export default function App() {
  const [activeView, setActiveView] = useState<ViewKey>(getInitialView);
  const [streamStatus, setStreamStatus] = useState<"connecting" | "connected" | "reconnecting" | "disconnected">("connecting");
  const [state, setState] = useState<DashboardState>({
    status: null,
    auth: null,
    settings: null,
    liveActivity: null,
    portfolioSummary: null,
    closedPositions: null,
    equityCurve: null,
    report: null,
    recentEvents: [],
    recentDecisions: [],
    liveOrders: [],
    liveTrades: [],
    daemonHeartbeat: null,
    daemonTicks: [],
  });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const reconnectTimerRef = useRef<number | null>(null);

  useEffect(() => {
    const onHashChange = () => setActiveView(getInitialView());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    void refreshDashboard();
  }, []);

  async function refreshDashboard() {
    setLoading(true);
    setError("");
    try {
      const snapshot = await fetchJson<DashboardSnapshotPayload>("/api/dashboard");
      setState(mapSnapshotToState(snapshot));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown dashboard error");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let source: EventSource | null = null;
    let disposed = false;
    const eventNames = [
      "status",
      "auth",
      "settings",
      "live_activity",
      "portfolio_summary",
      "closed_positions",
      "equity_curve",
      "report",
      "recent_events",
      "recent_decisions",
      "live_orders",
      "live_trades",
      "daemon_heartbeat",
      "daemon_ticks",
    ];

    const connect = () => {
      if (disposed) return;
      setStreamStatus((current) => (current === "connected" ? current : "connecting"));
      source = new EventSource("/api/dashboard/stream?interval_seconds=5");
      source.onopen = () => {
        setStreamStatus("connected");
        setError("");
      };
      const handlers = eventNames.map((eventName) => {
        const handler = (event: Event) => {
          const payload = JSON.parse((event as MessageEvent).data);
          setState((current) => applyDashboardDelta(current, eventName, payload));
          setLoading(false);
          setError("");
        };
        source?.addEventListener(eventName, handler);
        return { eventName, handler };
      });
      source.onerror = () => {
        handlers.forEach(({ eventName, handler }) => source?.removeEventListener(eventName, handler));
        source?.close();
        source = null;
        if (disposed) return;
        setStreamStatus("reconnecting");
        if (reconnectTimerRef.current) {
          window.clearTimeout(reconnectTimerRef.current);
        }
        reconnectTimerRef.current = window.setTimeout(() => {
          connect();
        }, 3000);
      };
    };

    connect();
    return () => {
      disposed = true;
      setStreamStatus("disconnected");
      if (reconnectTimerRef.current) {
        window.clearTimeout(reconnectTimerRef.current);
      }
      source?.close();
    };
  }, []);

  const currentView = useMemo(() => {
    switch (activeView) {
      case "decisions":
        return <DecisionsPage decisions={state.recentDecisions} />;
      case "orders":
        return <OrdersPage liveOrders={state.liveOrders} liveTrades={state.liveTrades} liveActivity={state.liveActivity} />;
      case "portfolio":
        return <PortfolioPage summary={state.portfolioSummary} positions={state.closedPositions?.positions ?? []} equityCurve={state.equityCurve} />;
      case "events":
        return <EventsPage events={state.recentEvents} report={state.report} />;
      case "settings":
        return (
          <SettingsPage
            settings={state.settings}
            onSettingsUpdated={(settings) => setState((current) => ({ ...current, settings }))}
            onRefresh={refreshDashboard}
          />
        );
      case "daemon":
        return <DaemonView heartbeat={state.daemonHeartbeat} ticks={state.daemonTicks} />;
      case "overview":
      default:
        return <OverviewPage state={state} />;
    }
  }, [activeView, state]);

  return (
    <div className="app-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">Polymarket AI Agent</p>
          <h1>Operator Dashboard</h1>
          <p className="subtitle">Live monitoring for signals, trades, orders, and portfolio state.</p>
        </div>
        <div className="hero-meta">
          <span className={`pill stream-${streamStatus}`}>
            Stream: {streamStatus}
          </span>
          <span className={`pill ${state.auth?.readonly_ready ? "ready" : "blocked"}`}>
            {state.auth?.readonly_ready ? "Readonly Ready" : "Auth Blocked"}
          </span>
          <span className={`pill ${state.status?.live_trading_enabled ? "ready" : "blocked"}`}>
            {state.status?.live_trading_enabled ? "Live Enabled" : "Live Disabled"}
          </span>
          <button type="button" className="refresh-button" onClick={() => void refreshDashboard()}>
            Refresh
          </button>
        </div>
      </header>

      <nav className="nav-strip">
        {VIEWS.map((view) => (
          <button
            key={view.key}
            type="button"
            className={`nav-pill ${activeView === view.key ? "active" : ""}`}
            onClick={() => {
              window.location.hash = view.key;
              setActiveView(view.key);
            }}
          >
            {view.label}
          </button>
        ))}
      </nav>

      {loading && <div className="banner">Loading dashboard...</div>}
      {error && <div className="banner error">{error}</div>}

      {currentView}
    </div>
  );
}
