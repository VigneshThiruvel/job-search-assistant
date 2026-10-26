import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev only: the UI runs on :5173 and forwards /api to FastAPI on :8000.
// changeOrigin rewrites Host to 127.0.0.1:8000 (passes TrustedHost); the browser's
// Origin (localhost:5173) stays, and the API allows it.
export default defineConfig({
  plugins: [react()],
  build: { chunkSizeWarningLimit: 1000 }, // served from localhost: size barely matters
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
});
