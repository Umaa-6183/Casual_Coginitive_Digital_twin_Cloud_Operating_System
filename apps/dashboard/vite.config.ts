import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 3000,
    proxy: {
      "/api": {
        // FIX: was 'http://localhost:8000' — inside Docker, localhost is the
        // dashboard container itself, not the host. Must use the Docker
        // service name so the request routes correctly within the Docker network.
        target: "http://api-gateway:8000",
        changeOrigin: true,

        // FIX: required for SSE streaming (text/event-stream responses).
        // Without these, Vite buffers the response and the stream never
        // reaches the browser, causing the frontend to see a 500/timeout.
        ws: false,
        configure: (proxy) => {
          // Disable response buffering for SSE
          proxy.on("proxyRes", (proxyRes) => {
            proxyRes.headers["x-accel-buffering"] = "no";
          });
        },
      },
      "/ws": {
        target: "ws://api-gateway:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
