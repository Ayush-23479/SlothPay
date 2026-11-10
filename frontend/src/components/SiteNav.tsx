import Link from "next/link";

export function SiteNav() {
  return (
    <header className="no-print sticky top-0 z-40 border-b border-line bg-white/95 backdrop-blur">
      <nav
        aria-label="Main"
        className="mx-auto flex h-16 max-w-6xl items-center gap-4 px-4 sm:px-6"
      >
        <Link href="/" className="flex items-center gap-2 text-xl font-extrabold text-brand">
          <svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true">
            <circle cx="14" cy="14" r="14" fill="#0b3d91" />
            <path
              d="M8 16c1.5 3 4 4.2 6 4.2S18.500 19 20 16M9.500 11.500h2m5 0h2"
              stroke="#fff"
              strokeWidth="2"
              strokeLinecap="round"
              fill="none"
            />
          </svg>
          SlothPay
        </Link>
        <ul className="ml-4 hidden items-center gap-6 text-sm font-semibold text-ink md:flex">
          <li>
            <a href="#pay" className="hover:underline">
              Pay
            </a>
          </li>
          <li>
            <a href="#payments" className="hover:underline">
              Payments
            </a>
          </li>
          <li>
            <a href="#safety" className="hover:underline">
              Safety
            </a>
          </li>
          <li>
            <Link href="/compare" className="hover:underline">
              Compare
            </Link>
          </li>
        </ul>
        <div className="ml-auto flex items-center gap-3">
          <span className="rounded-full bg-soft px-3 py-1 text-xs font-bold uppercase tracking-wide text-ink">
            Sandbox
          </span>
          <a href="#pay" className="btn btn-primary btn-sm">
            New payment
          </a>
        </div>
      </nav>
    </header>
  );
}