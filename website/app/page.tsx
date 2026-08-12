import Link from "next/link";
import { SiteFooter } from "./components/SiteFooter";
import { SiteHeader } from "./components/SiteHeader";

const riskRules = [
  ["R1", "Single-name concentration"],
  ["R2", "Sector concentration"],
  ["R3", "Margin usage"],
  ["R4", "Repeated risk increases"],
  ["R5", "Monthly activity"],
  ["R6", "Decision-level loss"],
  ["R7", "Suggested stop reached"],
] as const;

const modules = [
  {
    number: "01",
    title: "Account truth",
    copy: "Read balances, positions, executions, PnL and margin from IBKR without exposing an order route.",
  },
  {
    number: "02",
    title: "Decision context",
    copy: "Combine portfolio risk, technical state, market regime and existing plans into explainable actions.",
  },
  {
    number: "03",
    title: "Review loop",
    copy: "Trace every recommendation, threshold, execution and outcome without rewriting the historical record.",
  },
] as const;

export default function Home() {
  return (
    <div className="site-shell">
      <SiteHeader />

      <main>
        <section className="hero section-frame">
          <div className="hero-copy">
            <div className="eyebrow"><span /> Read-only by design · Open source</div>
            <h1>Decisions<br />before <em>orders.</em></h1>
            <p className="hero-lede">
              A self-hosted IBKR decision-support system that turns account facts,
              portfolio risk and market context into auditable actions—without
              placing a trade.
            </p>
            <div className="hero-actions">
              <Link className="button button-primary" href="/demo">
                Open synthetic demo <span aria-hidden="true">↗</span>
              </Link>
              <a className="button button-ghost" href="https://github.com/datazhy/ibkr-trading-decision-system">
                View on GitHub <span aria-hidden="true">→</span>
              </a>
            </div>
            <p className="hero-note">No broker connection. No real account data. No persistence.</p>
          </div>

          <div className="decision-proof" aria-label="Illustrative decision card">
            <div className="proof-topline">
              <span>DECISION / SIM-A</span>
              <span className="proof-status">REVIEW</span>
            </div>
            <div className="proof-action">
              <span className="proof-kicker">Suggested action</span>
              <strong>Reduce 3%</strong>
              <p>Concentration exceeds the personal risk budget while the position remains above its stop.</p>
            </div>
            <div className="proof-grid">
              <div><span>Risk score</span><strong>72</strong><small>/ 100</small></div>
              <div><span>Position</span><strong>27.4%</strong><small>of equity</small></div>
              <div><span>Evidence</span><strong>4</strong><small>sources</small></div>
            </div>
            <div className="proof-bar" aria-label="Risk budget used: 72 percent"><span /></div>
            <div className="proof-footer">
              <span>R1 concentration</span><span>Technical intact</span><span>Manual execution</span>
            </div>
          </div>
        </section>

        <section className="signal-strip" aria-label="Safety principles">
          <span>IBKR ACCOUNT TRUTH</span><i>•</i><span>RULES R1–R7</span><i>•</i>
          <span>MANUAL EXECUTION</span><i>•</i><span>POINT-IN-TIME REVIEW</span>
        </section>

        <section className="section-frame value-section" id="system">
          <div className="section-heading">
            <span className="section-index">01 / SYSTEM</span>
            <h2>One evidence chain.<br />Three deliberate stages.</h2>
            <p>Built for the moment between seeing a number and acting on it.</p>
          </div>
          <div className="module-grid">
            {modules.map((module) => (
              <article className="module-card" key={module.number}>
                <span className="module-number">{module.number}</span>
                <div className="module-marker" aria-hidden="true"><span /></div>
                <h3>{module.title}</h3>
                <p>{module.copy}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="safety-section" id="safety">
          <div className="section-frame safety-grid">
            <div className="safety-copy">
              <span className="section-index section-index-light">02 / SAFETY BOUNDARY</span>
              <h2>The broker is a source.<br />Never an actuator.</h2>
              <p>
                Read-only mode is enforced at the API client and Gateway. The application
                contains no order, cancellation, exercise or transfer route. Adopting a
                recommendation creates a local draft only.
              </p>
              <a href="https://github.com/datazhy/ibkr-trading-decision-system/blob/main/docs/architecture.md">
                Read the safety model <span aria-hidden="true">→</span>
              </a>
            </div>
            <div className="boundary-stack" aria-label="Read-only safety layers">
              <div><span>01</span><strong>Dedicated broker user</strong><small>Least-privilege account access</small></div>
              <div><span>02</span><strong>Gateway read-only mode</strong><small>IB_READONLY=yes</small></div>
              <div><span>03</span><strong>API read-only mode</strong><small>readonly=True</small></div>
              <div><span>04</span><strong>No execution routes</strong><small>Decisions remain drafts</small></div>
            </div>
          </div>
        </section>

        <section className="section-frame rules-section" id="rules">
          <div className="section-heading rules-heading">
            <span className="section-index">03 / RISK ENGINE</span>
            <h2>Rules that leave<br />an audit trail.</h2>
            <p>Each evaluation stores status, evidence, source batch and notification state.</p>
          </div>
          <div className="rules-list">
            {riskRules.map(([code, label], index) => (
              <div className="rule-row" key={code}>
                <span className="rule-code">{code}</span>
                <strong>{label}</strong>
                <span className="rule-dots" aria-hidden="true" />
                <small>{index === 2 ? "ZERO MARGIN" : index === 6 ? "STOP QUALITY" : "PERSONAL LIMIT"}</small>
              </div>
            ))}
          </div>
        </section>

        <section className="demo-callout section-frame">
          <div>
            <span className="section-index">04 / PUBLIC DEMO</span>
            <h2>Change the scenario.<br />Watch the decision change.</h2>
          </div>
          <div className="demo-callout-copy">
            <p>
              Explore three synthetic portfolio states and see how concentration,
              drawdown and liquidity alter the risk score and recommended action.
            </p>
            <Link className="button button-dark" href="/demo">Launch demo <span aria-hidden="true">↗</span></Link>
          </div>
        </section>

        <section className="open-source-section">
          <div className="section-frame open-source-grid">
            <div>
              <span className="section-index section-index-light">05 / OPEN SOURCE</span>
              <h2>Inspect the logic.<br />Own the data.</h2>
            </div>
            <div>
              <p>
                Python, FastAPI, SQLite and Docker Compose. MIT licensed, self-hosted,
                documented in English and Chinese, and designed around synthetic tests.
              </p>
              <div className="source-links">
                <a href="https://github.com/datazhy/ibkr-trading-decision-system">GitHub repository <span>→</span></a>
                <a href="https://github.com/datazhy/ibkr-trading-decision-system/blob/main/README.zh-CN.md">中文 README <span>→</span></a>
                <a href="https://github.com/datazhy/ibkr-trading-decision-system/blob/main/LICENSE">MIT License <span>→</span></a>
              </div>
            </div>
          </div>
        </section>
      </main>

      <SiteFooter />
    </div>
  );
}
