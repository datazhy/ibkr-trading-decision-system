"use client";

import Link from "next/link";
import { useState } from "react";
import { SiteFooter } from "../components/SiteFooter";
import { SiteHeader } from "../components/SiteHeader";

type ScenarioKey = "balanced" | "concentrated" | "drawdown";

type Position = {
  symbol: string;
  allocation: number;
  pnl: string;
  trend: "Positive" | "Neutral" | "Weak";
  action: string;
};

type Scenario = {
  label: string;
  note: string;
  equity: string;
  dayPnl: string;
  dayPnlTone: "positive" | "negative";
  riskScore: number;
  cash: number;
  recommendation: string;
  recommendationDetail: string;
  status: string;
  bars: number[];
  positions: Position[];
  rules: { code: string; label: string; state: "PASS" | "WATCH" | "TRIGGER"; detail: string }[];
};

const scenarios: Record<ScenarioKey, Scenario> = {
  balanced: {
    label: "Balanced",
    note: "Diversified allocation with healthy cash reserve",
    equity: "$128,400",
    dayPnl: "+$1,142 · +0.90%",
    dayPnlTone: "positive",
    riskScore: 34,
    cash: 17,
    recommendation: "Hold and monitor",
    recommendationDetail: "Risk budgets are intact. Preserve cash and wait for planned entry conditions.",
    status: "NORMAL",
    bars: [42, 47, 45, 53, 58, 55, 61, 64, 59, 67, 70, 74],
    positions: [
      { symbol: "SIM-A", allocation: 22.4, pnl: "+4.8%", trend: "Positive", action: "Hold" },
      { symbol: "SIM-B", allocation: 19.2, pnl: "+1.9%", trend: "Positive", action: "Hold" },
      { symbol: "SIM-C", allocation: 15.8, pnl: "−0.6%", trend: "Neutral", action: "Wait" },
      { symbol: "SIM-D", allocation: 12.6, pnl: "+2.4%", trend: "Positive", action: "Hold" },
      { symbol: "OTHER", allocation: 13.0, pnl: "+0.3%", trend: "Neutral", action: "Review" },
    ],
    rules: [
      { code: "R1", label: "Single-name concentration", state: "PASS", detail: "22.4% < 30%" },
      { code: "R2", label: "Sector concentration", state: "PASS", detail: "35.2% < 40%" },
      { code: "R3", label: "Margin usage", state: "PASS", detail: "No margin" },
      { code: "R7", label: "Suggested stop", state: "PASS", detail: "All stops intact" },
    ],
  },
  concentrated: {
    label: "Concentrated",
    note: "One position exceeds the personal allocation limit",
    equity: "$131,760",
    dayPnl: "+$684 · +0.52%",
    dayPnlTone: "positive",
    riskScore: 72,
    cash: 9,
    recommendation: "Reduce SIM-A by 3%",
    recommendationDetail: "Single-name and sector exposure exceed the personal budget. Trend remains intact, so reduce methodically rather than exit reactively.",
    status: "REVIEW",
    bars: [38, 46, 51, 57, 55, 64, 69, 73, 71, 78, 82, 86],
    positions: [
      { symbol: "SIM-A", allocation: 33.1, pnl: "+8.7%", trend: "Positive", action: "Reduce 3%" },
      { symbol: "SIM-B", allocation: 21.7, pnl: "+2.1%", trend: "Positive", action: "Hold" },
      { symbol: "SIM-C", allocation: 14.2, pnl: "−0.8%", trend: "Neutral", action: "Wait" },
      { symbol: "SIM-D", allocation: 11.0, pnl: "+1.7%", trend: "Positive", action: "Hold" },
      { symbol: "OTHER", allocation: 11.0, pnl: "+0.2%", trend: "Neutral", action: "Review" },
    ],
    rules: [
      { code: "R1", label: "Single-name concentration", state: "TRIGGER", detail: "33.1% > 30%" },
      { code: "R2", label: "Sector concentration", state: "WATCH", detail: "39.7% near limit" },
      { code: "R3", label: "Margin usage", state: "PASS", detail: "No margin" },
      { code: "R7", label: "Suggested stop", state: "PASS", detail: "Price above stop" },
    ],
  },
  drawdown: {
    label: "Drawdown",
    note: "Loss limit reached while quote quality remains valid",
    equity: "$119,280",
    dayPnl: "−$3,486 · −2.84%",
    dayPnlTone: "negative",
    riskScore: 88,
    cash: 12,
    recommendation: "Stop new risk for 48 hours",
    recommendationDetail: "Decision-level loss has exceeded the cooldown threshold. Review fills and stop quality before adding exposure.",
    status: "ACTION",
    bars: [78, 74, 70, 68, 61, 64, 57, 52, 48, 43, 39, 34],
    positions: [
      { symbol: "SIM-A", allocation: 26.8, pnl: "−5.4%", trend: "Weak", action: "Protect stop" },
      { symbol: "SIM-B", allocation: 20.5, pnl: "−3.1%", trend: "Weak", action: "No add" },
      { symbol: "SIM-C", allocation: 16.7, pnl: "−2.3%", trend: "Neutral", action: "Hold" },
      { symbol: "SIM-D", allocation: 13.0, pnl: "+0.4%", trend: "Neutral", action: "Hold" },
      { symbol: "OTHER", allocation: 11.0, pnl: "−0.7%", trend: "Neutral", action: "Review" },
    ],
    rules: [
      { code: "R1", label: "Single-name concentration", state: "PASS", detail: "26.8% < 30%" },
      { code: "R3", label: "Margin usage", state: "PASS", detail: "No margin" },
      { code: "R6", label: "Decision-level loss", state: "TRIGGER", detail: "Loss > 2%" },
      { code: "R7", label: "Suggested stop", state: "WATCH", detail: "SIM-A near stop" },
    ],
  },
};

export default function DemoPage() {
  const [scenarioKey, setScenarioKey] = useState<ScenarioKey>("concentrated");
  const scenario = scenarios[scenarioKey];

  return (
    <div className="site-shell demo-shell">
      <SiteHeader demo />
      <main className="demo-main">
        <section className="demo-intro">
          <div>
            <div className="eyebrow"><span /> Synthetic data only</div>
            <h1>Decision desk<br /><em>public demo.</em></h1>
          </div>
          <div className="demo-intro-copy">
            <p>Switch portfolio states to see how the same rule engine changes its assessment and suggested action.</p>
            <Link href="/">← Back to project website</Link>
          </div>
        </section>

        <section className="privacy-banner" aria-label="Demo privacy statement">
          <strong>SAFE DEMO</strong>
          <span>Every account, symbol, price and event below is fabricated in source code.</span>
          <span>No live data APIs · No app cookies · No storage</span>
        </section>

        <section className="scenario-control" aria-label="Portfolio scenario">
          <div>
            <span className="control-label">Portfolio state</span>
            <div className="scenario-buttons">
              {(Object.keys(scenarios) as ScenarioKey[]).map((key) => (
                <button
                  className={scenarioKey === key ? "active" : ""}
                  key={key}
                  onClick={() => setScenarioKey(key)}
                  aria-pressed={scenarioKey === key}
                >
                  {scenarios[key].label}
                </button>
              ))}
            </div>
          </div>
          <p aria-live="polite">{scenario.note}</p>
        </section>

        <section className="metric-grid" aria-label="Synthetic account summary">
          <article><span>Net liquidation</span><strong>{scenario.equity}</strong><small>SIMULATED USD</small></article>
          <article><span>Daily P&amp;L</span><strong className={scenario.dayPnlTone}>{scenario.dayPnl}</strong><small>MARK-TO-MARKET</small></article>
          <article><span>Risk score</span><strong>{scenario.riskScore}<i>/100</i></strong><div className="mini-risk"><span style={{ width: `${scenario.riskScore}%` }} /></div></article>
          <article><span>Cash reserve</span><strong>{scenario.cash}%</strong><small>NO MARGIN</small></article>
        </section>

        <section className="demo-dashboard">
          <article className="panel recommendation-panel">
            <div className="panel-heading"><span>PRIMARY DECISION</span><b className={`state-${scenario.status.toLowerCase()}`}>{scenario.status}</b></div>
            <div className="recommendation-content" aria-live="polite">
              <span>Suggested action</span>
              <h2>{scenario.recommendation}</h2>
              <p>{scenario.recommendationDetail}</p>
              <div className="decision-boundary"><span /> Manual review required before any broker action</div>
            </div>
          </article>

          <article className="panel trend-panel">
            <div className="panel-heading"><span>12-SESSION RISK PATH</span><b>SYNTHETIC</b></div>
            <div className="bar-chart" aria-label="Synthetic twelve-session risk path">
              {scenario.bars.map((height, index) => (
                <div key={`${scenarioKey}-${index}`}><span style={{ height: `${height}%` }} /></div>
              ))}
            </div>
            <div className="chart-axis"><span>−12</span><span>Today</span></div>
          </article>

          <article className="panel positions-panel">
            <div className="panel-heading"><span>POSITIONS</span><b>{scenario.positions.length} GROUPS</b></div>
            <div className="position-table" role="table" aria-label="Synthetic positions">
              <div className="position-row position-header" role="row">
                <span>Symbol</span><span>Allocation</span><span>Open P&amp;L</span><span>Trend</span><span>Action</span>
              </div>
              {scenario.positions.map((position) => (
                <div className="position-row" role="row" key={position.symbol}>
                  <strong>{position.symbol}</strong>
                  <span><i className="allocation-track"><i style={{ width: `${Math.min(position.allocation * 2.4, 100)}%` }} /></i>{position.allocation}%</span>
                  <span className={position.pnl.startsWith("+") ? "positive" : position.pnl.startsWith("−") ? "negative" : ""}>{position.pnl}</span>
                  <span>{position.trend}</span>
                  <b>{position.action}</b>
                </div>
              ))}
            </div>
          </article>

          <article className="panel rules-panel">
            <div className="panel-heading"><span>RISK EVIDENCE</span><b>POINT IN TIME</b></div>
            <div className="demo-rules">
              {scenario.rules.map((rule) => (
                <div key={rule.code}>
                  <span className={`rule-state rule-state-${rule.state.toLowerCase()}`}>{rule.state}</span>
                  <strong>{rule.code} · {rule.label}</strong>
                  <small>{rule.detail}</small>
                </div>
              ))}
            </div>
          </article>
        </section>

        <section className="demo-explainer">
          <div><span>01</span><strong>Change scenario</strong><p>Choose a fabricated portfolio state.</p></div>
          <div><span>02</span><strong>Inspect evidence</strong><p>Trace score, rule and source context.</p></div>
          <div><span>03</span><strong>Keep control</strong><p>Recommendations never become orders.</p></div>
        </section>
      </main>
      <SiteFooter />
    </div>
  );
}
