import { defineConfig } from "vite";

export default defineConfig({
  define: {
    __API_URL__: JSON.stringify(
      process.env.VITE_API_URL || "https://pijtf5xn90.execute-api.us-east-1.amazonaws.com"
    ),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
