import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Luister op alle interfaces zodat zowel localhost (IPv6) als 127.0.0.1 werken.
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        timeout: 120_000,
        proxyTimeout: 120_000,
      },
      // Suggesties rechtstreeks (niet via FastAPI) — blijft snel als bike-network de backend belast.
      "/nominatim": {
        target: "https://nominatim.openstreetmap.org",
        changeOrigin: true,
        secure: true,
        rewrite: (path) => path.replace(/^\/nominatim/, ""),
        headers: {
          "User-Agent": "Veloverhaal/0.1 (local; contact=dev)",
          "Accept-Language": "nl,en",
        },
        timeout: 8_000,
        proxyTimeout: 8_000,
      },
      "/photon-api": {
        target: "https://photon.komoot.io",
        changeOrigin: true,
        secure: true,
        rewrite: (path) => path.replace(/^\/photon-api/, ""),
        timeout: 8_000,
        proxyTimeout: 8_000,
      },
      // Direct OSRM for magenta legs — skips FastAPI hop (~sneller).
      "/osrm-bike": {
        target: "https://routing.openstreetmap.de",
        changeOrigin: true,
        secure: true,
        rewrite: (path) => path.replace(/^\/osrm-bike/, "/routed-bike"),
        timeout: 20_000,
        proxyTimeout: 20_000,
      },
    },
  },
});
