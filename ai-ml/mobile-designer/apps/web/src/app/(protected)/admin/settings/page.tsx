"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { useToast } from "@/components/common/Toast";
import * as adminApi from "@/lib/api/admin";
import type { SystemConfig } from "@/lib/api/admin";

const AVAILABLE_MODELS = [
  { id: "global.anthropic.claude-opus-4-8", label: "Claude Opus 4.8 (최신, 최고 품질)" },
  { id: "global.anthropic.claude-opus-4-7", label: "Claude Opus 4.7 (고품질, 느림)" },
  { id: "global.anthropic.claude-opus-4-6-v1", label: "Claude Opus 4.6 (고품질, 느림)" },
  { id: "global.anthropic.claude-opus-4-5-20251101-v1:0", label: "Claude Opus 4.5" },
  { id: "global.anthropic.claude-sonnet-4-6", label: "Claude Sonnet 4.6 (빠름, 균형)" },
  { id: "global.anthropic.claude-sonnet-4-5-20250929-v1:0", label: "Claude Sonnet 4.5" },
  { id: "global.anthropic.claude-sonnet-4-20250514-v1:0", label: "Claude Sonnet 4.0" },
  { id: "global.anthropic.claude-haiku-4-5-20251001-v1:0", label: "Claude Haiku 4.5 (경량, 빠름)" },
];

const MODEL_SLOTS = [
  { key: "chat", label: "채팅 (요구사항 수집)", description: "Stage 1 대화, Stage 2/3 수정 채팅" },
  { key: "wireframe", label: "와이어프레임 생성", description: "화면 구조 JSON 생성" },
  { key: "designer", label: "모바일 디자인 생성", description: "풀 디자인 JSON 생성" },
  { key: "modify", label: "디자인 수정 (패치)", description: "기존 디자인 부분 수정" },
  { key: "codegen", label: "핸드오프 코드 생성", description: "Kotlin Compose .kt 파일 생성" },
];

export default function AdminSettingsPage() {
  const { showToast } = useToast();
  const [config, setConfig] = useState<SystemConfig | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    loadSettings();
  }, []);

  async function loadSettings() {
    setIsLoading(true);
    try {
      const data = await adminApi.getSettings();
      if (!data.models) {
        data.models = {
          chat: "global.anthropic.claude-sonnet-4-6",
          wireframe: "global.anthropic.claude-sonnet-4-6",
          designer: "global.anthropic.claude-sonnet-4-6",
          modify: "global.anthropic.claude-sonnet-4-6",
          codegen: "global.anthropic.claude-opus-4-6-v1",
        };
      }
      setConfig(data);
    } catch {
      showToast("설정을 불러오지 못했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleSave() {
    if (!config) return;
    setSaving(true);
    try {
      const updated = await adminApi.updateSettings(config);
      setConfig(updated);
      showToast("설정이 저장되었습니다", "success");
    } catch (err: any) {
      showToast(err.message || "설정 저장에 실패했습니다", "error");
    } finally {
      setSaving(false);
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        {[1, 2, 3].map((i) => <div key={i} className="h-12 bg-gray-100 rounded-mdesigner animate-pulse" />)}
      </div>
    );
  }

  if (!config) {
    return <p className="text-gray-500 text-sm">설정을 불러올 수 없습니다.</p>;
  }

  return (
    <div>
      <h2 className="text-lg font-semibold mb-6">시스템 설정</h2>

      <div className="space-y-6">
        <div className="flex items-center justify-between p-4 border rounded-mdesigner">
          <div>
            <h3 className="text-sm font-medium">가입 허용</h3>
            <p className="text-xs text-gray-500 mt-0.5">새 사용자가 회원가입할 수 있도록 허용합니다.</p>
          </div>
          <label className="relative inline-flex items-center cursor-pointer">
            <input
              type="checkbox"
              checked={config.registrationOpen}
              onChange={(e) => setConfig({ ...config, registrationOpen: e.target.checked })}
              className="sr-only peer"
              data-testid="toggle-registration"
            />
            <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-primary/30 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-primary" />
          </label>
        </div>

        <div className="p-4 border rounded-mdesigner">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-medium">최대 사용자 수</h3>
              <p className="text-xs text-gray-500 mt-0.5">0으로 설정하면 무제한입니다.</p>
            </div>
            <input
              type="number"
              min={0}
              value={config.maxUsers}
              onChange={(e) => setConfig({ ...config, maxUsers: parseInt(e.target.value) || 0 })}
              className="w-24 px-3 py-1.5 border border-gray-300 rounded-mdesigner text-sm text-right focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary"
              data-testid="input-max-users"
            />
          </div>
        </div>

        <div className="flex items-center justify-between p-4 border rounded-mdesigner">
          <div>
            <h3 className="text-sm font-medium">유지보수 모드</h3>
            <p className="text-xs text-gray-500 mt-0.5">활성화하면 관리자 외 접근이 제한됩니다.</p>
          </div>
          <label className="relative inline-flex items-center cursor-pointer">
            <input
              type="checkbox"
              checked={config.maintenanceMode}
              onChange={(e) => setConfig({ ...config, maintenanceMode: e.target.checked })}
              className="sr-only peer"
              data-testid="toggle-maintenance"
            />
            <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-primary/30 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-primary" />
          </label>
        </div>

        <div className="border-t pt-6 mt-6">
          <h3 className="text-sm font-semibold mb-4">AI 모델 설정</h3>
          <p className="text-xs text-gray-500 mb-4">각 기능에서 사용할 Bedrock 모델을 선택합니다.</p>
          <div className="space-y-3">
            {MODEL_SLOTS.map(({ key, label, description }) => (
              <div key={key} className="flex items-center justify-between p-3 border rounded-mdesigner">
                <div>
                  <span className="text-sm font-medium">{label}</span>
                  <p className="text-xs text-gray-500">{description}</p>
                </div>
                <select
                  value={config.models?.[key] || "global.anthropic.claude-sonnet-4-6"}
                  onChange={(e) => setConfig({ ...config, models: { ...config.models, [key]: e.target.value } })}
                  className="text-xs border rounded-mdesigner px-2 py-1.5 max-w-[260px] focus:outline-none focus:ring-2 focus:ring-primary/30"
                >
                  {AVAILABLE_MODELS.map((m) => (
                    <option key={m.id} value={m.id}>{m.label}</option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        </div>

        <Button onClick={handleSave} isLoading={saving} data-testid="save-settings-btn">
          설정 저장
        </Button>
      </div>
    </div>
  );
}
