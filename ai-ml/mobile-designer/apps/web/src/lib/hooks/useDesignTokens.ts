"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

export interface DesignTokens {
  colors: Record<string, string>;
  typography: Record<string, { fontSize: number; fontWeight: string }>;
  spacing: Record<string, number>;
}

const DEFAULT_TOKENS: DesignTokens = {
  colors: {
    primary: "#1a73e8",
    onPrimary: "#ffffff",
    surface: "#ffffff",
    onSurface: "#1f1f1f",
    background: "#f8f9fa",
    error: "#d93025",
  },
  typography: {
    displayLarge: { fontSize: 34, fontWeight: "Bold" },
    headlineMedium: { fontSize: 24, fontWeight: "SemiBold" },
    bodyMedium: { fontSize: 14, fontWeight: "Regular" },
    labelSmall: { fontSize: 12, fontWeight: "Medium" },
  },
  spacing: {
    screenMargin: 24,
    componentGap: 16,
    cardPadding: 16,
  },
};

export function useDesignTokens(initialTokens?: Partial<DesignTokens>) {
  const [tokens, setTokens] = useState<DesignTokens>({ ...DEFAULT_TOKENS, ...initialTokens });
  const [dirtyTokens, setDirtyTokens] = useState<Partial<DesignTokens>>({});

  const appliedTokens = useMemo<DesignTokens>(() => ({
    colors: { ...tokens.colors, ...dirtyTokens.colors },
    typography: { ...tokens.typography, ...dirtyTokens.typography },
    spacing: { ...tokens.spacing, ...dirtyTokens.spacing },
  }), [tokens, dirtyTokens]);

  const isDirty = Object.keys(dirtyTokens).length > 0;

  useEffect(() => {
    const root = document.documentElement;
    Object.entries(appliedTokens.colors).forEach(([key, value]) => {
      const cssVar = `--md-color-${key.replace(/([A-Z])/g, "-$1").toLowerCase()}`;
      root.style.setProperty(cssVar, value);
    });
    Object.entries(appliedTokens.spacing).forEach(([key, value]) => {
      const cssVar = `--md-spacing-${key.replace(/([A-Z])/g, "-$1").toLowerCase()}`;
      root.style.setProperty(cssVar, `${value}px`);
    });
  }, [appliedTokens]);

  const updateToken = useCallback((category: keyof DesignTokens, key: string, value: unknown) => {
    setDirtyTokens((prev) => ({
      ...prev,
      [category]: { ...(prev[category] as Record<string, unknown>), [key]: value },
    }));
  }, []);

  const resetTokens = useCallback((category?: keyof DesignTokens) => {
    if (category) {
      setDirtyTokens((prev) => {
        const next = { ...prev };
        delete next[category];
        return next;
      });
    } else {
      setDirtyTokens({});
    }
  }, []);

  const commitTokens = useCallback((newTokens: DesignTokens) => {
    setTokens(newTokens);
    setDirtyTokens({});
  }, []);

  return { tokens, dirtyTokens, appliedTokens, isDirty, updateToken, resetTokens, commitTokens };
}
