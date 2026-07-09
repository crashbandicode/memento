"use client";

import { useTheme } from "@/lib/theme-context";

/**
 * Aurora skin background. Desktop gets animated ambient blobs; mobile,
 * hidden tabs, and reduced-motion users get a cheaper static treatment.
 * Null unless skin="aurora".
 */
export function AuroraBackdrop() {
  const { skin, theme } = useTheme();
  if (skin !== "aurora") return null;

  const dark = theme === "dark";

  return (
    <>
      <div
        aria-hidden
        style={{
          position: "fixed", inset: 0, pointerEvents: "none",
          zIndex: -2, background: "var(--aurora-bg)",
        }}
      />
      <div
        aria-hidden
        style={{
          position: "fixed", inset: 0, pointerEvents: "none",
          zIndex: -1, overflow: "hidden",
          background: [
            "radial-gradient(circle at 12% 4%, var(--aurora-glow1), transparent 36%)",
            "radial-gradient(circle at 92% 12%, var(--aurora-glow2), transparent 34%)",
            "radial-gradient(circle at 52% 110%, var(--aurora-glow3), transparent 38%)",
          ].join(", "),
          opacity: dark ? 0.28 : 0.36,
        }}
      >
        <div
          style={{
            position: "absolute", inset: 0,
            opacity: dark ? 0.22 : 0.32,
            mixBlendMode: "overlay",
            backgroundImage:
              "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.35 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>\")",
          }}
        />
      </div>
    </>
  );
}
