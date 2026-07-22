'use client';

import { useState, useRef, useEffect } from 'react';
import { Send } from 'lucide-react';
import MessageBubble from './MessageBubble';
import type { ChatMessage } from '@/lib/types';

interface Props {
  messages: ChatMessage[];
  onSend: (message: string) => void;
  isLoading: boolean;
  placeholder?: string;
  examplePrompts?: string[];
}

export default function ChatPanel({
  messages,
  onSend,
  isLoading,
  placeholder = '메시지를 입력하세요...',
  examplePrompts,
}: Props) {
  const [input, setInput] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;
    onSend(input.trim());
    setInput('');
  };

  const showExamples = examplePrompts && messages.length <= 1 && !isLoading;

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto p-4 space-y-2">
        {messages.map((msg, i) => (
          <MessageBubble key={i} message={msg} />
        ))}
        {isLoading && (
          <div className="flex justify-start mb-4">
            <div className="bg-[var(--surface)] border border-[var(--border)] rounded-lg px-4 py-3">
              <div className="flex gap-1">
                <span className="typing-dot" />
                <span className="typing-dot" />
                <span className="typing-dot" />
              </div>
            </div>
          </div>
        )}
        {showExamples && (
          <div className="flex flex-col gap-2 mt-4">
            <p className="text-xs text-[var(--text-dim)] mb-1">예시 질문:</p>
            {examplePrompts.map((prompt, i) => (
              <button
                key={i}
                onClick={() => { if (isLoading) return; onSend(prompt); }}
                className="text-left px-4 py-3 bg-[var(--surface)] border border-[var(--border)] rounded-lg text-sm text-[var(--text-dim)] hover:border-[var(--purple)] hover:text-white transition-colors"
              >
                {prompt}
              </button>
            ))}
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>
      <form onSubmit={handleSubmit} className="p-4 border-t border-[var(--border)] bg-[var(--surface)]">
        <div className="flex gap-2 bg-[var(--surface-hover)] border border-[var(--border)] rounded-xl px-3 py-2 shadow-[0_2px_12px_rgba(0,0,0,0.2)] focus-within:border-[var(--purple)] focus-within:shadow-[0_0_0_2px_rgba(139,92,246,0.15)] transition-all">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={placeholder}
            disabled={isLoading}
            className="flex-1 bg-transparent border-none px-2 py-1.5 text-sm text-white placeholder-[var(--text-muted)] focus:outline-none disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={isLoading || !input.trim()}
            className="px-4 py-1.5 bg-[var(--purple)] text-white rounded-lg text-sm font-bold hover:bg-[#7c3aed] hover:shadow-[0_4px_12px_rgba(139,92,246,0.4)] disabled:opacity-50 transition-all"
          >
            전송
          </button>
        </div>
      </form>
    </div>
  );
}
