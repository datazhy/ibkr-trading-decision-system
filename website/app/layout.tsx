import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host") ?? "localhost:3000";
  const protocol = requestHeaders.get("x-forwarded-proto") ?? (host.startsWith("localhost") ? "http" : "https");
  const base = new URL(`${protocol}://${host}`);
  const image = new URL("/og.png", base).toString();

  return {
    metadataBase: base,
    title: {
      default: "Personal Trading Decision System (IBKR)",
      template: "%s · Personal Trading Decision System",
    },
    description: "A self-hosted, read-only IBKR trading decision, risk-control and review system.",
    openGraph: {
      title: "Decisions before orders.",
      description: "Explore the open-source system through a privacy-safe synthetic portfolio demo.",
      type: "website",
      images: [{ url: image, width: 1734, height: 907, alt: "Decisions before orders — synthetic public demo" }],
    },
    twitter: {
      card: "summary_large_image",
      title: "Decisions before orders.",
      description: "A privacy-safe synthetic demo of the Personal Trading Decision System (IBKR).",
      images: [image],
    },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
