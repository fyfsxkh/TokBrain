import type { NextConfig } from "next";

// Keep the browser API endpoint in frontend/lib/api.ts (or NEXT_PUBLIC_API_URL)
// so Windows does not silently resolve localhost to IPv6 while the backend is
// deliberately bound to the IPv4 loopback address.
const nextConfig: NextConfig = {
  allowedDevOrigins: ["127.0.0.1"],
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000",
  },
};

export default nextConfig;
