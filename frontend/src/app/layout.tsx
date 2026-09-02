import type { Metadata, Viewport } from "next";
import { IBM_Plex_Sans, IBM_Plex_Mono } from "next/font/google";
import { StreamProvider } from "@/components/StreamProvider";
import "./globals.css";

/*
 * Self-hosted at build time (not a runtime CDN pull), so `docker compose up`
 * works with no network - demo beat 1:00. Weights are pruned to what the UI
 * actually sets: 400/500/600/700 sans, 400/500/600 mono.
 */
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-plex-sans",
  display: "swap",
});

const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "PayRecover · Autonomous Payment Recovery",
  description:
    "An autonomous agent that recovers failed payments and produces an auditable, compliance-enforced record of every decision.",
};

export const viewport: Viewport = {
  themeColor: "#0e1116",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${plexSans.variable} ${plexMono.variable}`}>
      <body className="min-h-screen antialiased">
        <StreamProvider>{children}</StreamProvider>
      </body>
    </html>
  );
}
