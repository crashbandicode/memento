import type { Metadata, Viewport } from "next";
import "./globals.css";
import ClientLayout from "./client-layout";

// Without this, Next.js 16 omits <meta name="viewport">, so iOS Safari and
// Android Chrome render at ~980px desktop width and then scale to fit —
// every page looks oversized and text tiny on phones.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

// Keep the server-rendered language aligned with the app's default locale.
// A Chinese `lang` attribute around an English UI makes mobile browsers offer
// to translate the page from Chinese even after the client corrects the locale.
export const metadata: Metadata = {
  title: "Memento — Your AI coding memory",
  description: "A shared brain for your AI coding tools — self-hosted, cross-device, searchable.",
  icons: {
    icon: [
      { url: "/favicon.svg", type: "image/svg+xml" },
      { url: "/favicon.png", type: "image/png" },
    ],
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full">
        <ClientLayout>{children}</ClientLayout>
      </body>
    </html>
  );
}
