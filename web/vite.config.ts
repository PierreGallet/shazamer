import { defineConfig } from "vite";
import solid from "vite-plugin-solid";

export default defineConfig({
  plugins: [solid()],
  build: { outDir: "dist", target: "es2022", sourcemap: false },
  server: {
    port: 5173,
    // Dev server proxies the API so the frontend runs against the real backend
    // without CORS gymnastics.
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true, ws: false },
    },
  },
});
