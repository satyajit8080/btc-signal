import type { Metadata, Viewport } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "BTC Signal",
  description: "Bitcoin market condition: bullish or bearish, trade or no trade. Market intelligence, not financial advice.",
};

export const viewport: Viewport = { themeColor: "#09090b", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-zinc-950 text-zinc-100">
        <header className="sticky top-0 z-10 border-b border-zinc-800/80 bg-zinc-950/90 backdrop-blur">
          <nav className="mx-auto flex max-w-3xl items-center justify-between px-4 py-3 text-sm">
            <Link href="/" className="font-bold tracking-wide">
              <span className="text-amber-400">₿</span> BTC SIGNAL
            </Link>
            <div className="flex gap-4 text-zinc-400">
              <Link href="/" className="hover:text-zinc-100">Signal</Link>
              <Link href="/history" className="hover:text-zinc-100">History</Link>
              <Link href="/admin" className="hover:text-zinc-100">Admin</Link>
            </div>
          </nav>
        </header>
        <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-6">{children}</main>
        <footer className="mx-auto max-w-3xl px-4 pb-8 text-center text-xs text-zinc-500">
          Market intelligence, not financial advice. No outcome is guaranteed; historical performance does not
          predict future results.
        </footer>
      </body>
    </html>
  );
}
