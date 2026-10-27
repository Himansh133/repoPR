/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const api = "http://127.0.0.1:8000";

export default defineConfig({
  base: "/ui/",
  plugins: [react(), tailwindcss()],
  server: { host: "127.0.0.1", proxy: { "/runs": api, "/health": api } },
  test: { environment: "jsdom", setupFiles: ["tests/setup.ts"] },
});
