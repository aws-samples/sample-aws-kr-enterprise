"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useToast } from "@/components/common/Toast";
import * as adminApi from "@/lib/api/admin";
import type { PromptSlot } from "@/lib/api/admin";

interface SlotInfo {
  label: string;
  description: string;
  placeholders: string[];
}

const SLOT_ORDER = [
  "CHATBOT_SYSTEM",
  "REQUIREMENTS_SYNTHESIS",
  "WIREFRAME_SYSTEM",
  "WIREFRAME_CHAT",
  "DESIGNER_SYSTEM",
  "DESIGN_CHAT",
  "MODIFY_SYSTEM",
  "SCREEN_CODEGEN",
];

const SLOT_INFO: Record<string, SlotInfo> = {
  CHATBOT_SYSTEM: {
    label: "① 요구사항 수집 챗봇",
    description: "Stage 1 — 사용자와 대화하며 앱 요구사항을 수집하는 챗봇",
    placeholders: [
      "플레이스홀더: 없음",
      "입력: 사용자 채팅 메시지 + 첨부 파일 텍스트(PDF/DOCX 파싱 결과)",
      "출력: 대화 응답 텍스트 (채팅 세션에 누적)",
    ],
  },
  REQUIREMENTS_SYNTHESIS: {
    label: "② 요구사항 문서 합성",
    description: "Stage 1 완료 시 — 채팅 히스토리를 구조화된 요구사항 JSON으로 변환",
    placeholders: [
      "플레이스홀더: 없음",
      "입력: User+AI 전체 채팅 히스토리 텍스트",
      "출력: 구조화된 요구사항 JSON (app_name, screens, navigation, visual_requirements)",
    ],
  },
  WIREFRAME_SYSTEM: {
    label: "③ 와이어프레임 생성",
    description: "Stage 2 — 요구사항 문서를 기반으로 화면 구조(wireframe JSON)를 생성",
    placeholders: [
      "플레이스홀더: 없음",
      "입력: {command, stage: 'wireframe', context: {requirements: ②의 요구사항 JSON}}",
      "출력: wireframe JSON (screens 배열 + components 트리)",
    ],
  },
  WIREFRAME_CHAT: {
    label: "③-1 와이어프레임 수정 채팅",
    description: "Stage 2 채팅 — 생성된 와이어프레임에 대해 수정사항을 논의",
    placeholders: [
      "플레이스홀더: {design_context} → 현재 와이어프레임 구조 요약으로 자동 치환",
      "입력: 사용자 수정 요청 메시지",
      "출력: 대화 응답 (수정은 MODIFY_SYSTEM이 처리)",
    ],
  },
  DESIGNER_SYSTEM: {
    label: "④ 모바일 디자인 생성",
    description: "Stage 3 — 요구사항 + 와이어프레임을 기반으로 풀 디자인 생성",
    placeholders: [
      "플레이스홀더: 없음",
      "입력: {command, stage: 'design', context: {requirements: ②의 요구사항 JSON, previous_stage_result: ③의 wireframe JSON}}",
      "출력: 디자인 JSON (screens + tokens: colors/typography/spacing)",
    ],
  },
  DESIGN_CHAT: {
    label: "④-1 디자인 수정 채팅",
    description: "Stage 3 채팅 — 생성된 디자인에 대해 색상/스타일 수정을 논의",
    placeholders: [
      "플레이스홀더: {design_context} → 현재 디자인 구조 요약으로 자동 치환",
      "입력: 사용자 수정 요청 메시지",
      "출력: 대화 응답 (수정은 MODIFY_SYSTEM이 처리)",
    ],
  },
  MODIFY_SYSTEM: {
    label: "③④ 디자인 수정 (패치)",
    description: "Stage 2/3 공통 — 기존 와이어프레임/디자인에 부분 수정(JSON patch)을 생성",
    placeholders: [
      "플레이스홀더: 없음",
      "입력: {command: 수정 요청 텍스트, current_design: 현재 stage의 전체 디자인 JSON}",
      "출력: JSON patches 배열 (update/add/remove/add_screen/remove_screen)",
    ],
  },
  SCREEN_CODEGEN: {
    label: "⑤ 핸드오프 코드 생성",
    description: "Stage 4 — 디자인 JSON을 화면별 Kotlin Compose .kt 파일로 변환",
    placeholders: [
      "플레이스홀더: {screen_name} → 화면 이름(PascalCase)으로 자동 치환",
      "입력 (system): 이 프롬프트 전체 (아이콘 매핑, 구조 규칙 포함)",
      "입력 (user): Design Tokens JSON + 개별 Screen JSON",
      "출력: 단일 .kt 파일 (빌드 가능한 Compose 코드)",
    ],
  },
};

export default function AdminPromptsPage() {
  const { showToast } = useToast();
  const [slots, setSlots] = useState<PromptSlot[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    loadSlots();
  }, []);

  async function loadSlots() {
    setIsLoading(true);
    try {
      const data = await adminApi.listPromptSlots();
      setSlots(data);
    } catch {
      showToast("프롬프트 목록을 불러오지 못했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <div>
      <h2 className="text-lg font-semibold mb-6">프롬프트 관리</h2>

      {isLoading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => <div key={i} className="h-16 bg-gray-100 rounded-mdesigner animate-pulse" />)}
        </div>
      ) : slots.length === 0 ? (
        <p className="text-gray-500 text-sm">프롬프트 슬롯이 없습니다.</p>
      ) : (
        <div className="space-y-3">
          {[...slots].sort((a, b) => {
            const ai = SLOT_ORDER.indexOf(a.slot);
            const bi = SLOT_ORDER.indexOf(b.slot);
            return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
          }).map((slot) => {
            const info = SLOT_INFO[slot.slot];
            return (
              <Link
                key={slot.slot}
                href={`/admin/prompts/${slot.slot}`}
                className="block p-4 border rounded-mdesigner hover:bg-gray-50 transition-colors"
                data-testid={`prompt-slot-${slot.slot}`}
              >
                <div className="flex justify-between items-start">
                  <div className="flex-1 min-w-0">
                    <h3 className="font-medium text-sm">{info?.label || slot.slot}</h3>
                    <p className="text-xs text-gray-500 mt-1">{info?.description || ""}</p>
                    {info?.placeholders && info.placeholders.length > 0 && (
                      <div className="mt-2 space-y-0.5">
                        {info.placeholders.map((ph, i) => (
                          <p key={i} className="text-xs text-blue-600 font-mono">{ph}</p>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="text-right shrink-0 ml-4">
                    {slot.active_version !== null ? (
                      <span className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded font-medium">
                        활성 ({slot.total_versions}개 버전)
                      </span>
                    ) : (
                      <span className="text-xs text-gray-400">활성 버전 없음</span>
                    )}
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}
