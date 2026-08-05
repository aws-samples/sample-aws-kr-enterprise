'use client';

import { useState, useEffect, useRef } from 'react';
import { useParams } from 'next/navigation';
import Header from '@/components/common/Header';
import ChatPanel from '@/components/chat/ChatPanel';
import SSEEventDisplay from '@/components/chat/SSEEventDisplay';
import { agents as agentsApi, feedback as feedbackApi, authHeaders } from '@/lib/api-client';
import type { ChatMessage, SSEEvent } from '@/lib/types';

// --- HITL Modal Component ---
function HITLModal({
  action,
  agentId,
  sessionId,
  onClose,
}: {
  action: Record<string, unknown>;
  agentId: string;
  sessionId: string;
  onClose: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDecision = async (approved: boolean) => {
    setSubmitting(true);
    setError(null);
    try {
      await feedbackApi.send(agentId, sessionId, {
        approved,
        comment: approved
          ? 'Approved via Playground'
          : 'Rejected via Playground',
      });
      // Feedback was recorded successfully — close the dialog.
      onClose();
    } catch (err: unknown) {
      // Surface the failure to the user instead of silently swallowing it;
      // keep the dialog open so they can retry.
      const errorMessage =
        err instanceof Error ? err.message : 'Unknown error';
      setError(`피드백 기록에 실패했습니다: ${errorMessage}`);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
      <div className="bg-[var(--surface)] border border-[var(--border)] rounded-lg p-6 max-w-md w-full mx-4 shadow-xl">
        <h3 className="text-lg font-semibold text-white mb-2">
          Agent Feedback
        </h3>
        <p className="text-[var(--text-dim)] text-sm mb-4">
          {String(
            action.description ||
              action.message ||
              'Agent가 다음 작업에 대한 피드백을 요청했습니다.',
          )}
        </p>
        <p className="text-[var(--text-muted)] text-xs mb-4">
          참고: 피드백은 기록되지만 이미 진행 중인 작업을 중단하거나 되돌리지는 않습니다.
        </p>
        {action.tool ? (
          <div className="bg-[var(--bg)] rounded p-3 mb-4 text-xs text-[var(--text-dim)] font-mono">
            Tool: {String(action.tool)}
            <br />
            {action.args ? (
              <span>Args: {JSON.stringify(action.args, null, 2)}</span>
            ) : null}
          </div>
        ) : null}
        {error && (
          <div className="mb-4 text-xs text-[var(--red)] bg-[var(--red)]/10 border border-[var(--red)]/20 rounded px-3 py-2">
            {error}
          </div>
        )}
        <div className="flex gap-3 justify-end">
          <button
            onClick={() => handleDecision(false)}
            disabled={submitting}
            className="px-4 py-2 bg-[var(--red)] text-white rounded-lg text-sm font-medium hover:bg-[#c93b3b] disabled:opacity-50 transition-colors"
          >
            {submitting ? '기록 중...' : 'Reject'}
          </button>
          <button
            onClick={() => handleDecision(true)}
            disabled={submitting}
            className="px-4 py-2 bg-[var(--success)] text-white rounded-lg text-sm font-medium hover:bg-[#059669] disabled:opacity-50 transition-colors"
          >
            {submitting ? '기록 중...' : 'Approve'}
          </button>
        </div>
      </div>
    </div>
  );
}

// --- Playground Page ---
export default function PlaygroundPage() {
  const params = useParams();
  const agentId = params.id as string;
  const [agentName, setAgentName] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sseEvents, setSSEEvents] = useState<SSEEvent[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [hitlAction, setHitlAction] = useState<Record<string, unknown> | null>(
    null,
  );
  const [currentSessionId, setCurrentSessionId] = useState('');

  // useRef to track events during streaming (avoids useState closure issue)
  const sseEventsRef = useRef<SSEEvent[]>([]);

  useEffect(() => {
    agentsApi
      .get(agentId)
      .then((data) => {
        setAgentName(data.config?.name || agentId);
      })
      .catch(() => {});
  }, [agentId]);

  const handleSend = async (message: string) => {
    const sessionId = currentSessionId || `pg-${crypto.randomUUID()}-${Date.now()}`;
    if (!currentSessionId) setCurrentSessionId(sessionId);
    setMessages((prev) => [
      ...prev,
      { role: 'user', content: message, timestamp: new Date().toISOString() },
    ]);
    sseEventsRef.current = [];
    setSSEEvents([]);
    setIsLoading(true);

    // POST to start chat -- the response is SSE stream
    try {
      const response = await fetch(`/api/agents/${agentId}/chat`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ message, sessionId }),
      });

      if (!response.ok) throw new Error('Failed to start chat');
      if (!response.body) throw new Error('No response body');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let fullContent = '';
      let currentEventType = 'message';
      let streamingMsgAdded = false;
      // SSE lines can be split across network chunks; carry the trailing
      // partial line over to the next read instead of parsing it broken.
      let buffer = '';

      const processLine = (line: string) => {
        if (line.startsWith('event: ')) {
          currentEventType = line.replace('event: ', '').trim();
          return;
        }
        if (!line.startsWith('data: ')) return;
        const dataStr = line.replace('data: ', '').trim();
        try {
          const data = JSON.parse(dataStr);
          const newEvent: SSEEvent = { type: currentEventType, data };
          sseEventsRef.current = [...sseEventsRef.current, newEvent];
          setSSEEvents([...sseEventsRef.current]);

          if (currentEventType === 'text' && data.content) {
            fullContent += data.content;
            if (!streamingMsgAdded) {
              streamingMsgAdded = true;
              setMessages((prev) => [
                ...prev,
                { role: 'assistant', content: fullContent, timestamp: new Date().toISOString() },
              ]);
            } else {
              setMessages((prev) => {
                const updated = [...prev];
                updated[updated.length - 1] = {
                  ...updated[updated.length - 1],
                  content: fullContent,
                };
                return updated;
              });
            }
          } else if (currentEventType === 'done' && data.content && !streamingMsgAdded) {
            fullContent = data.content;
            setMessages((prev) => [
              ...prev,
              { role: 'assistant', content: fullContent, timestamp: new Date().toISOString(), events: sseEventsRef.current },
            ]);
            streamingMsgAdded = true;
          }

          if (currentEventType === 'hitl') {
            setHitlAction(data);
          }

          if (currentEventType === 'error') {
            // Surface an explicit, actionable error to the user instead of
            // silently ending the stream. Backend sends { error, code, hint? }.
            const errMsg = data.error || 'Agent error';
            const hint = data.hint ? `\n\n💡 ${data.hint}` : '';
            setMessages((prev) => [
              ...prev,
              {
                role: 'system',
                content: `⚠️ ${errMsg}${hint}`,
                timestamp: new Date().toISOString(),
                events: sseEventsRef.current,
              },
            ]);
            streamingMsgAdded = true;
          }
        } catch {
          // ignore unparseable data lines
        }
        currentEventType = 'message';
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          // Flush any complete line left in the buffer (last SSE line may
          // arrive without a trailing newline).
          if (buffer.length > 0) processLine(buffer);
          break;
        }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        // Keep the last (possibly incomplete) segment in the buffer.
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          processLine(line);
        }
      }

      // done 이벤트 후 최종 events 첨부
      if (streamingMsgAdded) {
        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = {
            ...updated[updated.length - 1],
            events: sseEventsRef.current,
          };
          return updated;
        });
      } else if (fullContent) {
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: fullContent, timestamp: new Date().toISOString(), events: sseEventsRef.current },
        ]);
      }
    } catch (error: unknown) {
      const errorMessage =
        error instanceof Error ? error.message : 'Unknown error';
      setMessages((prev) => [
        ...prev,
        {
          role: 'system',
          content: `Error: ${errorMessage}`,
          timestamp: new Date().toISOString(),
        },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="flex flex-col h-screen">
      <Header title={`Playground — ${agentName}`} />
      <div className="flex flex-1 overflow-hidden">
        <div className="flex-1 flex flex-col">
          <ChatPanel
            messages={messages}
            onSend={handleSend}
            isLoading={isLoading}
            placeholder={`${agentName}에게 질문하세요...`}
          />
        </div>
        <div className="w-80 border-l border-[var(--border)] p-4 overflow-y-auto">
          <SSEEventDisplay events={sseEvents} />
          <div className="mt-4">
            <h4 className="text-xs font-semibold text-[var(--text-dim)] mb-2">
              Agent Info
            </h4>
            <div className="text-xs text-[var(--text-dim)] space-y-1">
              <p>ID: {agentId}</p>
              <p>Name: {agentName}</p>
              <p>Events: {sseEvents.length}</p>
            </div>
          </div>
        </div>
      </div>
      {hitlAction && (
        <HITLModal
          action={hitlAction}
          agentId={agentId}
          sessionId={currentSessionId}
          onClose={() => setHitlAction(null)}
        />
      )}
    </div>
  );
}
