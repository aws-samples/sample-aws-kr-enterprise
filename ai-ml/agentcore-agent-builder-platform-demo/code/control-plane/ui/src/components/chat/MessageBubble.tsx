'use client';

import { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ChatMessage } from '@/lib/types';

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';
  const [timeStr, setTimeStr] = useState('');

  useEffect(() => {
    setTimeStr(new Date(message.timestamp).toLocaleTimeString('ko-KR'));
  }, [message.timestamp]);

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-4`}>
      <div
        className={`max-w-[80%] rounded-2xl px-4 py-3 ${
          isUser
            ? 'bg-[var(--purple)] text-white border border-[var(--border)] shadow-[0_4px_16px_rgba(0,0,0,0.3)] rounded-br-sm'
            : 'bg-[var(--surface)] text-[var(--text)] border border-[var(--border)] shadow-[0_4px_16px_rgba(0,0,0,0.3)] rounded-bl-sm'
        }`}
      >
        {isUser ? (
          <div className="text-sm whitespace-pre-wrap">{message.content}</div>
        ) : (
          <div className="text-sm prose prose-invert prose-sm max-w-none msg-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.content}
            </ReactMarkdown>
          </div>
        )}
        {timeStr && (
          <div
            className={`text-xs mt-1 ${isUser ? 'text-white/60' : 'text-[var(--text-dim)]'}`}
          >
            {timeStr}
          </div>
        )}
      </div>
    </div>
  );
}
