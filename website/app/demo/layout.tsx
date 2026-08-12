import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Synthetic Public Demo",
  description:
    "Explore fabricated portfolio scenarios without a broker connection, persistence or real account data.",
};

export default function DemoLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
