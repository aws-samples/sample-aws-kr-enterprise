"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/common/Button";
import * as handoffApi from "@/lib/api/handoff";
import type { HandoffType } from "@/lib/api/handoff";
import { useToast } from "@/components/common/Toast";

interface HandoffOption {
  type: HandoffType;
  label: string;
  description: string;
}

const HANDOFF_OPTIONS: HandoffOption[] = [
  { type: "design_tokens", label: "디자인 토큰 (JSON)", description: "색상, 타이포, 간격 토큰" },
  { type: "figma_tokens", label: "Figma 토큰", description: "Figma Token Studio 형식" },
  { type: "compose_theme", label: "Compose Theme", description: "Color.kt, Type.kt, Theme.kt" },
  { type: "design_spec", label: "디자인 스펙 문서", description: "개발자 전달용 마크다운 스펙" },
];

export default function Stage4Handoff() {
  const { id } = useParams<{ id: string }>();
  const { showToast } = useToast();
  const [loadingType, setLoadingType] = useState<HandoffType | null>(null);
  const [results, setResults] = useState<Record<string, { versionId: string; artifactKey?: string }>>({});

  const handleGenerate = async (type: HandoffType) => {
    setLoadingType(type);
    try {
      const result = await handoffApi.generateHandoff(id, type);
      const artifactKey = result.artifacts?.[0]?.key;
      setResults((prev) => ({ ...prev, [type]: { versionId: result.version_id, artifactKey } }));
      showToast("산출물이 생성되었습니다", "success");

      const url = await handoffApi.getDownloadUrl(id, result.version_id, artifactKey);
      window.open(url, "_blank");
    } catch {
      showToast("생성에 실패했습니다", "error");
    } finally {
      setLoadingType(null);
    }
  };

  const handleDownloadProject = async () => {
    try {
      const url = await handoffApi.downloadProject(id);
      window.open(url, "_blank");
    } catch {
      showToast("다운로드에 실패했습니다", "error");
    }
  };

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h2 className="text-xl font-bold">핸드오프</h2>
        <p className="text-gray-500 text-sm mt-1">디자인 결과물을 원하는 형태로 내보냅니다.</p>
      </div>

      <div className="p-4 border rounded-mdesigner bg-green-50 border-green-200">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-green-800">Android 프로젝트 (AI 생성)</p>
            <p className="text-xs text-green-600 mt-1">LLM 기반 Compose 프로젝트 다운로드</p>
          </div>
          <Button size="sm" onClick={handleDownloadProject}>다운로드</Button>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {HANDOFF_OPTIONS.map((option) => (
          <button
            key={option.type}
            onClick={() => handleGenerate(option.type)}
            disabled={loadingType !== null}
            className="flex flex-col items-start p-4 border rounded-mdesigner hover:border-primary hover:bg-primary/5 transition-colors text-left disabled:opacity-50"
          >
            <span className="text-sm font-medium">{option.label}</span>
            <span className="text-xs text-gray-500 mt-1">{option.description}</span>
            {loadingType === option.type && (
              <span className="text-xs text-primary mt-2 animate-pulse">생성 중...</span>
            )}
            {results[option.type] && loadingType !== option.type && (
              <span className="text-xs text-green-600 mt-2">생성 완료</span>
            )}
          </button>
        ))}
      </div>
    </div>
  );
}
