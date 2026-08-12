export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div>
        <strong>Personal Trading Decision System (IBKR)</strong>
        <p>Independent open-source software. Not affiliated with or endorsed by Interactive Brokers LLC.</p>
      </div>
      <div className="footer-links">
        <a href="https://github.com/datazhy/ibkr-trading-decision-system">Source</a>
        <a href="https://github.com/datazhy/ibkr-trading-decision-system/blob/main/SECURITY.md">Security</a>
        <a href="https://github.com/datazhy/ibkr-trading-decision-system/blob/main/LICENSE">MIT</a>
      </div>
      <small>Decision support only. Not investment advice.</small>
    </footer>
  );
}
