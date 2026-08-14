import type { Metadata } from "next";
import { DM_Sans, IBM_Plex_Mono, Newsreader } from "next/font/google";
import { TopBanner } from "@/components/TopBanner";
import "./globals.css";

const newsreader = Newsreader({
  subsets: ["latin"],
  style: ["normal", "italic"],
  variable: "--font-newsreader",
});

const dmSans = DM_Sans({
  subsets: ["latin"],
  variable: "--font-dm-sans",
});

const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-mono",
});

export const metadata: Metadata = {
  title: "CareLoop — synthetic clinical AI prototype",
  description:
    "Closed-loop clinical execution demo on fully synthetic data. Not for patient care.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${newsreader.variable} ${dmSans.variable} ${plexMono.variable}`}
    >
      <body>
        {/* Persistent on every screen (contract cross-cutting rule). */}
        <TopBanner />
        {children}
      </body>
    </html>
  );
}
