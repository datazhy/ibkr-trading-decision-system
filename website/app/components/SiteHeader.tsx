import Link from "next/link";

export function SiteHeader({ demo = false }: { demo?: boolean }) {
  return (
    <header className="site-header">
      <Link className="brand" href="/" aria-label="Personal Trading Decision System home">
        <span className="brand-mark" aria-hidden="true">D/O</span>
        <span><strong>Decision System</strong><small>IBKR · READ ONLY</small></span>
      </Link>
      <nav aria-label="Primary navigation">
        {!demo && <a href="#system">System</a>}
        {!demo && <a href="#safety">Safety</a>}
        <Link href="/demo">Demo</Link>
        <a href="https://github.com/datazhy/ibkr-trading-decision-system">GitHub</a>
      </nav>
      <a className="language-link" href="https://github.com/datazhy/ibkr-trading-decision-system/blob/main/README.zh-CN.md">
        简体中文
      </a>
    </header>
  );
}
