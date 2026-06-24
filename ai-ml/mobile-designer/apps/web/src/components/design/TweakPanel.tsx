"use client";

import { useState } from "react";
import type { useDesignTokens } from "@/lib/hooks/useDesignTokens";
import type { useSystemSettings } from "@/lib/hooks/useSystemSettings";
import { Button } from "@/components/common/Button";

interface TweakPanelProps {
  tokens: ReturnType<typeof useDesignTokens>;
  systemSettings: ReturnType<typeof useSystemSettings>;
  onSaveTokens?: () => Promise<void>;
}

export function TweakPanel({ tokens, systemSettings, onSaveTokens }: TweakPanelProps) {
  const { settings } = systemSettings;
  const [saving, setSaving] = useState(false);

  return (
    <div className="space-y-6 p-4 border rounded-mdesigner bg-white" data-testid="tweak-panel">
      <h3 className="font-semibold text-sm">실시간 트윅</h3>

      <section className="space-y-2">
        <h4 className="text-xs font-medium text-gray-500">시스템 설정</h4>
        <label className="flex items-center justify-between text-sm">
          <span>다크 모드</span>
          <input type="checkbox" checked={settings.darkMode} onChange={systemSettings.toggleDarkMode} className="toggle" />
        </label>
        <label className="flex items-center justify-between text-sm">
          <span>글꼴 크기</span>
          <select value={settings.fontScale} onChange={(e) => systemSettings.setFontScale(Number(e.target.value))} className="text-xs border rounded px-2 py-1">
            <option value={0.8}>작게</option>
            <option value={1.0}>보통</option>
            <option value={1.2}>크게</option>
            <option value={1.4}>매우 크게</option>
          </select>
        </label>
        <label className="flex items-center justify-between text-sm">
          <span>화면 방향</span>
          <select value={settings.rotation} onChange={(e) => systemSettings.setRotation(e.target.value as "portrait" | "landscape")} className="text-xs border rounded px-2 py-1">
            <option value="portrait">세로</option>
            <option value="landscape">가로</option>
          </select>
        </label>
      </section>

      <section className="space-y-2">
        <h4 className="text-xs font-medium text-gray-500">디자인 토큰</h4>
        {Object.entries(tokens.appliedTokens.colors).slice(0, 4).map(([key, value]) => (
          <label key={key} className="flex items-center justify-between text-sm">
            <span>{key}</span>
            <input
              type="color"
              value={value}
              onChange={(e) => tokens.updateToken("colors", key, e.target.value)}
              className="w-8 h-6 border rounded cursor-pointer"
            />
          </label>
        ))}
      </section>

      {tokens.isDirty && (
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => tokens.resetTokens()} className="flex-1">초기화</Button>
          <Button size="sm" className="flex-1" disabled={saving} onClick={async () => {
            if (!onSaveTokens) return;
            setSaving(true);
            try { await onSaveTokens(); } finally { setSaving(false); }
          }}>
            {saving ? "저장 중..." : "저장"}
          </Button>
        </div>
      )}
    </div>
  );
}
