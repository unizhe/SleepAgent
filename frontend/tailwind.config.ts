import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        background: "#f7f8f5",
        foreground: "#171a1f",
        panel: "#ffffff",
        line: "#dde2dc",
        muted: "#667085",
        brand: {
          50: "#eef8f5",
          100: "#d6eee7",
          500: "#2c8f7b",
          600: "#237462",
          700: "#1d5f53",
        },
        amber: {
          50: "#fff7df",
          500: "#c3831f",
          700: "#855817",
        },
        rose: {
          50: "#fff1f1",
          500: "#d1495b",
          700: "#9d2f3c",
        },
      },
      boxShadow: {
        soft: "0 14px 36px rgba(31, 39, 48, 0.08)",
      },
    },
  },
  plugins: [],
};

export default config;
