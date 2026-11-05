import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Job Agent", description: "Review-before-submit job tracker" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
