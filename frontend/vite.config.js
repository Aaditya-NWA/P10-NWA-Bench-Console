import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    open: true,
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  test: {
    environment: "jsdom",              // Hooks under test touch window/localStorage/WebSocket
    globals: true,
    include: ["src/**/*.test.{js,jsx}"],
    restoreMocks: true,                // A stubbed WebSocket must not leak into the next file
  },
});
