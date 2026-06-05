import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// Dev server: proxy /api → the backend container (compose service name `backend`),
// so the browser sees same-origin /api and there's no CORS in dev (SPEC §12.5).
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    host: "0.0.0.0",
    port: 3000,
    // Dev tool on a trusted/local network (also reached via LAN IP or the homeserver
    // hostname), so relax Vite's DNS-rebinding host check. Prod serves the built
    // bundle behind nginx (UI Step 6), where this setting doesn't apply.
    allowedHosts: true,
    proxy: {
      "/api": { target: "http://backend:8000", changeOrigin: true },
    },
  },
});
