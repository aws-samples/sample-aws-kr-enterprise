"use client";

import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "@/components/common/Button";

interface Message {
  role: "user" | "assistant";
  content: string;
}

interface ChatInterfaceProps {
  messages: Message[];
  onSend: (command: string) => void;
  isStreaming: boolean;
  placeholder?: string;
}

export function ChatInterface({ messages, onSend, isStreaming, placeholder }: ChatInterfaceProps) {
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isStreaming) return;
    onSend(input.trim());
    setInput("");
  };

  return (
    <div className="flex flex-col h-[500px] border rounded-mdesigner bg-white" data-testid="chat-interface">
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 && (
          <p className="text-center text-gray-400 text-sm mt-12">메시지를 입력하여 시작하세요</p>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[80%] px-3 py-2 rounded-mdesigner text-sm ${
              msg.role === "user" ? "bg-primary text-on-primary" : "bg-gray-100 text-gray-800"
            }`}>
              {msg.role === "assistant" ? (
                <div className="prose prose-sm max-w-none prose-p:my-2 prose-ul:my-2 prose-ol:my-2 prose-li:my-1 prose-headings:mt-4 prose-headings:mb-2 prose-hr:my-3">
                  <Markdown remarkPlugins={[remarkGfm]}>{msg.content.replace(/\*\*([^*]+)\*\*/g, ' **$1** ')}</Markdown>
                </div>
              ) : (
                msg.content
              )}
            </div>
          </div>
        ))}
        {isStreaming && (
          <div className="flex justify-start">
            <div className="bg-gray-100 px-3 py-2 rounded-mdesigner">
              <span className="animate-pulse text-sm text-gray-500">생성 중...</span>
            </div>
          </div>
        )}
      </div>
      <form onSubmit={handleSubmit} className="flex gap-2 p-3 border-t">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={placeholder || "메시지를 입력하세요..."}
          className="flex-1 px-3 py-2 border rounded-mdesigner text-sm focus:outline-none focus:border-primary"
          disabled={isStreaming}
          data-testid="chat-input"
        />
        <Button type="submit" size="sm" disabled={!input.trim() || isStreaming} data-testid="chat-send-btn">
          전송
        </Button>
      </form>
    </div>
  );
}
