'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import Header from '@/components/common/Header';
import ChatPanel from '@/components/chat/ChatPanel';
import { builder as builderApi, agents as agentsApi } from '@/lib/api-client';
import type { ChatMessage, AgentConfig } from '@/lib/types';

const BUILDER_STEPS = [
  { key: 'INIT', label: 'Start' },
  { key: 'INTENT_GATHERING', label: 'Intent' },
  { key: 'BOUNDARY_DEFINITION', label: 'Boundary' },
  { key: 'TOOL_MATCHING', label: 'Tools' },
  { key: 'DELEGATION_CHECK', label: 'Delegation' },
  { key: 'CONFIG_GENERATION', label: 'Config' },
  { key: 'DONE', label: 'Complete' },
];

const MODEL_OPTIONS = [
  { label: 'Claude Sonnet 4.6', value: 'global.anthropic.claude-sonnet-4-6' },
  { label: 'Claude Opus 4.6', value: 'global.anthropic.claude-opus-4-6-v1' },
  { label: 'Claude Haiku 4.5', value: 'global.anthropic.claude-haiku-4-5-20251001-v1:0' },
];

const EXAMPLE_PROMPTS = [
  'EKS 클러스터의 Pod 상태를 모니터링하는 Agent를 만들어줘',
  'CloudWatch 알람 기반으로 인시던트를 자동 생성하는 Agent',
  '보안 감사 리포트를 매주 생성하는 Agent를 설계해줘',
];

export default function BuilderPage() {
  const router = useRouter();
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      content:
        '안녕하세요! 어떤 작업을 수행하는 Agent를 만들고 싶으신가요?\n\n예시: "인시던트 원인 분석 전문 Agent", "EKS 클러스터 모니터링 Agent" 등',
      timestamp: new Date().toISOString(),
    },
  ]);
  const [isLoading, setIsLoading] = useState(false);
  const [sessionId, setSessionId] = useState('');
  const [state, setState] = useState('INIT');
  const [generatedConfig, setGeneratedConfig] = useState<AgentConfig | null>(null);
  const [selectedModel, setSelectedModel] = useState(MODEL_OPTIONS[0].value);
  const [savedAgentId, setSavedAgentId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deploying, setDeploying] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);

  const handleSend = async (message: string) => {
    const userMessage: ChatMessage = {
      role: 'user',
      content: message,
      timestamp: new Date().toISOString(),
    };
    const updatedMessages = [...messages, userMessage];
    setMessages(updatedMessages);
    setIsLoading(true);
    setLastError(null);

    try {
      const response = await builderApi.chatStream(updatedMessages, sessionId, state);

      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(error.detail || response.statusText);
      }
      if (!response.body) throw new Error('No response body');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let streamingContent = '';
      let streamingMsgAdded = false;
      let currentEventType = 'text';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split('\n');

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEventType = line.replace('event: ', '').trim();
          } else if (line.startsWith('data: ')) {
            const dataStr = line.replace('data: ', '').trim();
            try {
              const data = JSON.parse(dataStr);

              if (currentEventType === 'text' && data.content) {
                streamingContent += data.content;
                if (!streamingMsgAdded) {
                  streamingMsgAdded = true;
                  setMessages((prev) => [
                    ...prev,
                    { role: 'assistant', content: streamingContent, timestamp: new Date().toISOString() },
                  ]);
                } else {
                  setMessages((prev) => {
                    const updated = [...prev];
                    updated[updated.length - 1] = {
                      ...updated[updated.length - 1],
                      content: streamingContent,
                    };
                    return updated;
                  });
                }
              } else if (currentEventType === 'done') {
                setSessionId(data.sessionId || sessionId);
                setState(data.state || state);
                if (data.agentConfig) {
                  setGeneratedConfig(data.agentConfig);
                }
                if (!streamingMsgAdded && data.fullMessage) {
                  setMessages((prev) => [
                    ...prev,
                    { role: 'assistant', content: data.fullMessage, timestamp: new Date().toISOString() },
                  ]);
                }
              } else if (currentEventType === 'error') {
                throw new Error(data.error || 'Builder stream error');
              }
            } catch (parseErr) {
              if (parseErr instanceof SyntaxError) continue;
              throw parseErr;
            }
            currentEventType = 'text';
          }
        }
      }
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      setLastError(errorMessage);
      const userFriendlyMsg = errorMessage.includes('요청이 너무 많습니다')
        ? '요청이 너무 많습니다. 잠시 후 다시 시도해주세요.'
        : errorMessage.includes('입력이 너무 깁니다')
          ? '대화가 너무 길어졌습니다. "새로 시작" 버튼을 눌러주세요.'
          : `오류가 발생했습니다: ${errorMessage}`;
      setMessages((prev) => [
        ...prev,
        { role: 'system', content: userFriendlyMsg, timestamp: new Date().toISOString() },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleSave = async () => {
    if (!generatedConfig) return;
    setSaving(true);
    try {
      const configWithModel = { ...generatedConfig, model: selectedModel };
      const result = await agentsApi.create(configWithModel);
      setSavedAgentId(result.agentId);
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : 'Unknown error';
      alert(`Agent 저장 실패: ${msg}`);
    } finally {
      setSaving(false);
    }
  };

  const handleDeploy = async () => {
    if (!savedAgentId) return;
    setDeploying(true);
    try {
      await agentsApi.deploy(savedAgentId);
      router.push(`/agents/${savedAgentId}/design`);
    } catch (error: unknown) {
      const msg = error instanceof Error ? error.message : 'Unknown error';
      if (msg.includes('AccessDeniedException') || msg.includes('not found')) {
        alert('Agent가 저장되었습니다. Runtime 배포는 Agent Registry에서 진행해주세요.');
        router.push('/agents');
      } else {
        alert(`Runtime 배포 실패: ${msg}`);
      }
    } finally {
      setDeploying(false);
    }
  };

  return (
    <div className="flex flex-col h-screen">
      <Header title="Agent Builder" />
      <div className="flex flex-1 overflow-hidden">
        <div className="flex-1 flex flex-col">
          <ChatPanel
            messages={messages}
            onSend={handleSend}
            isLoading={isLoading}
            placeholder="Agent에 대한 요구사항을 설명하세요..."
            examplePrompts={EXAMPLE_PROMPTS}
          />
        </div>
        {generatedConfig && (
          <div className="w-[480px] border-l border-[var(--border)] p-4 overflow-y-auto">
            <h3 className="text-sm font-semibold text-white mb-3">
              Generated Agent Config
            </h3>

            {/* Config 유효성 표시 */}
            <div className="mb-3 space-y-1">
              {(!generatedConfig.systemPrompt || generatedConfig.systemPrompt.length < 100) && (
                <div className="text-xs text-[var(--warning)] bg-[var(--warning)]/10 px-2 py-1 rounded">
                  systemPrompt가 비어있거나 너무 짧습니다
                </div>
              )}
              {(!generatedConfig.gateways?.length && !((generatedConfig as unknown) as Record<string, unknown>).capabilities) && (
                <div className="text-xs text-[var(--warning)] bg-[var(--warning)]/10 px-2 py-1 rounded">
                  Gateway가 설정되지 않았습니다
                </div>
              )}
            </div>

            <div className="mb-3">
              <label className="text-xs text-[var(--text-dim)] block mb-1">Model</label>
              <select
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
                className="w-full bg-[var(--bg)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-[var(--purple)]"
              >
                {MODEL_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            <pre className="bg-[var(--bg)] rounded-lg p-3 text-xs text-[var(--text-dim)] overflow-x-auto mb-4 border border-[var(--border)] max-h-96">
              {JSON.stringify(generatedConfig, null, 2)}
            </pre>

            <div className="flex gap-2">
              {!savedAgentId ? (
                <button
                  onClick={handleSave}
                  disabled={saving}
                  className="flex-1 px-4 py-2 bg-[var(--purple)] text-white rounded-lg text-sm font-medium hover:bg-[var(--purple)]/80 transition-colors disabled:opacity-50"
                >
                  {saving ? 'Saving...' : 'Save Agent'}
                </button>
              ) : (
                <>
                  <div className="flex-1 px-4 py-2 bg-[var(--success)]/15 text-[var(--success)] rounded-lg text-sm font-medium text-center">
                    Saved ({savedAgentId})
                  </div>
                  <button
                    onClick={handleDeploy}
                    disabled={deploying}
                    className="flex-1 px-4 py-2 bg-[var(--success)] text-white rounded-lg text-sm font-medium hover:bg-[#059669] transition-colors disabled:opacity-50"
                  >
                    {deploying ? 'Deploying...' : 'Deploy Runtime'}
                  </button>
                </>
              )}
            </div>
          </div>
        )}
      </div>
      {lastError && (
        <div className="px-4 py-2 bg-[var(--red)]/10 border-t border-[var(--red)]/20 flex items-center justify-between">
          <span className="text-xs text-[var(--red)]">마지막 요청이 실패했습니다</span>
          <button
            onClick={() => {
              setLastError(null);
              const lastUserMsg = [...messages].reverse().find(m => m.role === 'user');
              if (lastUserMsg) handleSend(lastUserMsg.content);
            }}
            className="text-xs px-3 py-1 bg-[var(--red)]/20 text-[var(--red)] rounded hover:bg-[var(--red)]/30 transition-colors"
          >
            다시 시도
          </button>
        </div>
      )}
      <div className="px-4 py-3 bg-[var(--surface)] border-t border-[var(--border)] flex items-center gap-1">
        <button
          onClick={() => {
            setMessages([{ role: 'assistant', content: '안녕하세요! 어떤 작업을 수행하는 Agent를 만들고 싶으신가요?\n\n예시: "인시던트 원인 분석 전문 Agent", "EKS 클러스터 모니터링 Agent" 등', timestamp: new Date().toISOString() }]);
            setSessionId('');
            setState('INIT');
            setGeneratedConfig(null);
            setSavedAgentId(null);
          }}
          className="text-xs text-[var(--text-dim)] hover:text-white px-2 py-1 rounded hover:bg-[var(--surface-hover)] transition-colors mr-2"
        >
          새로 시작
        </button>
        <span className="text-[var(--border)] mr-1">|</span>
        {BUILDER_STEPS.map((step, idx) => {
          const effectiveState = generatedConfig ? 'DONE' : state;
          const currentIdx = BUILDER_STEPS.findIndex((s) => s.key === effectiveState);
          const isCompleted = idx < currentIdx;
          const isCurrent = idx === currentIdx;
          return (
            <div key={step.key} className="flex items-center gap-1">
              <div className={`flex items-center gap-1.5 px-2 py-1 rounded text-xs ${
                isCurrent
                  ? 'bg-[var(--purple)]/20 text-[var(--purple)] font-medium'
                  : isCompleted
                    ? 'text-[var(--success)]'
                    : 'text-[var(--text-muted)]'
              }`}>
                <span className={`w-1.5 h-1.5 rounded-full ${
                  isCurrent && isLoading
                    ? 'bg-[var(--purple)] animate-pulse'
                    : isCurrent
                      ? 'bg-[var(--purple)]'
                      : isCompleted
                        ? 'bg-[var(--success)]'
                        : 'bg-[var(--text-muted)]'
                }`} />
                {step.label}
              </div>
              {idx < BUILDER_STEPS.length - 1 && (
                <span className="text-[var(--text-muted)] text-xs">&rsaquo;</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
