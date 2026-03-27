import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Apex Dashboard — The Ledger",
  description: "Agentic Event Store & Enterprise Audit Infrastructure",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-[#0a0a0a] text-gray-300 antialiased" suppressHydrationWarning>
        {children}
      </body>
    </html>
  );
}
