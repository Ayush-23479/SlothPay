import type { Metadata } from "next";
import "./globals.css";
import { SiteNav } from "@/components/SiteNav";

export const metadata: Metadata = {
  title: "SlothPay – payments that never double-charge",
  description:
    "SlothPay is a payment-safety layer for the PayPal sandbox: no duplicate charges, honest status, and a rescue plan when something goes wrong.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full" data-scroll-behavior="smooth" >
      <body className="min-h-full flex flex-col">
        <a
          href="#main"
          className="no-print sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-full focus:bg-white focus:px-4 focus:py-2 focus:font-semibold"
        >
          Skip to content
        </a>
        <SiteNav />
        <main id="main" className="flex-1">
          {children}
        </main>
        <footer className="no-print border-t border-line bg-white">
          <p className="mx-auto max-w-6xl px-4 py-6 text-sm text-muted sm:px-6">
            SlothPay is a hackathon project. It uses the PayPal sandbox only: no real money moves.
          </p>
        </footer>
      </body>
    </html>
  );
}