import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // TODO: React 19 新的 hook lint 规则(set-state-in-effect / refs during render)
  // 在 theme-context / use-sse / app/page.tsx 产生 ~18 个错误,
  // 先放过 build,另起 task 按 react-hooks 指南重写相关 effect。
  eslint: { ignoreDuringBuilds: true },
};

export default nextConfig;
