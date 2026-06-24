"use client";

import { useCallback, useState } from "react";

export interface SystemSettings {
  darkMode: boolean;
  fontScale: number;
  dynamicColorSeed: string;
  displayZoom: number;
  oneHandMode: boolean;
  rotation: "portrait" | "landscape";
}

const DEFAULT_SETTINGS: SystemSettings = {
  darkMode: false,
  fontScale: 1.0,
  dynamicColorSeed: "#1a73e8",
  displayZoom: 1.0,
  oneHandMode: false,
  rotation: "portrait",
};

export function useSystemSettings() {
  const [settings, setSettings] = useState<SystemSettings>(DEFAULT_SETTINGS);

  const toggleDarkMode = useCallback(() => {
    setSettings((s) => ({ ...s, darkMode: !s.darkMode }));
  }, []);

  const setFontScale = useCallback((scale: number) => {
    setSettings((s) => ({ ...s, fontScale: scale }));
  }, []);

  const setDynamicColor = useCallback((seed: string) => {
    setSettings((s) => ({ ...s, dynamicColorSeed: seed }));
  }, []);

  const setDisplayZoom = useCallback((zoom: number) => {
    setSettings((s) => ({ ...s, displayZoom: zoom }));
  }, []);

  const toggleOneHandMode = useCallback(() => {
    setSettings((s) => ({ ...s, oneHandMode: !s.oneHandMode }));
  }, []);

  const setRotation = useCallback((rotation: "portrait" | "landscape") => {
    setSettings((s) => ({ ...s, rotation }));
  }, []);

  const reset = useCallback(() => setSettings(DEFAULT_SETTINGS), []);

  return { settings, toggleDarkMode, setFontScale, setDynamicColor, setDisplayZoom, toggleOneHandMode, setRotation, reset };
}
