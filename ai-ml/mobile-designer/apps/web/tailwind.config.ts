import type { Config } from "tailwindcss";
import typography from "@tailwindcss/typography";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        primary: "var(--md-color-primary)",
        "on-primary": "var(--md-color-on-primary)",
        surface: "var(--md-color-surface)",
        "on-surface": "var(--md-color-on-surface)",
        background: "var(--md-color-background)",
        error: "var(--md-color-error)",
      },
      spacing: {
        "md-margin": "var(--md-spacing-screen-margin)",
      },
      borderRadius: {
        mdesigner: "var(--md-radius-md)",
      },
      fontSize: {
        "md-display": "var(--md-font-display)",
        "md-headline": "var(--md-font-headline)",
        "md-body": "var(--md-font-body)",
        "md-label": "var(--md-font-label)",
      },
    },
  },
  plugins: [typography],
};

export default config;
