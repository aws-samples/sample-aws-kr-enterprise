"use client";

import { useCallback, useRef, useState } from "react";

export type SSEStatus = "idle" | "connecting" | "connected" | "error" | "closed";

interface SSEMessage {
  event: string;
  data: Record<string, unknown>;
}

interface UseSSEStreamOptions {
  onMessage?: (msg: SSEMessage) => void;
  onComplete?: (data: Record<string, unknown>) => void;
  onError?: (error: string) => void;
  maxRetries?: number;
}

function parseSSELine(line: string): { event?: string; data?: string } | null {
  if (line.startsWith("event:")) return { event: line.slice(6).trim() };
  if (line.startsWith("data:")) return { data: line.slice(5).trim() };
  return null;
}

export function useSSEStream(options: UseSSEStreamOptions = {}) {
  const { onMessage, onComplete, onError, maxRetries = 3 } = options;
  const [status, setStatus] = useState<SSEStatus>("idle");
  const [messages, setMessages] = useState<SSEMessage[]>([]);
  const [progress, setProgress] = useState<{ step?: string; percent?: number } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const retriesRef = useRef(0);

  const connect = useCallback(async (url: string, body?: Record<string, unknown>) => {
    if (abortRef.current) abortRef.current.abort();

    const controller = new AbortController();
    abortRef.current = controller;
    setStatus("connecting");

    const token = typeof window !== "undefined" ? localStorage.getItem("access_token") : null;
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = `Bearer ${token}`;

    try {
      const res = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(body ?? {}),
        signal: controller.signal,
      });

      if (!res.ok) {
        const errBody = await res.text().catch(() => "");
        onError?.(`Request failed: ${res.status} ${errBody}`);
        setStatus("error");
        return;
      }

      setStatus("connected");
      retriesRef.current = 0;

      const reader = res.body?.getReader();
      if (!reader) {
        onError?.("No response body");
        setStatus("error");
        return;
      }

      const decoder = new TextDecoder();
      let buffer = "";
      let currentEvent = "message";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line === "") {
            currentEvent = "message";
            continue;
          }

          const parsed = parseSSELine(line);
          if (!parsed) continue;

          if (parsed.event) {
            currentEvent = parsed.event;
          } else if (parsed.data !== undefined) {
            let data: Record<string, unknown>;
            try {
              data = JSON.parse(parsed.data);
            } catch {
              data = { raw: parsed.data };
            }

            const msg: SSEMessage = { event: currentEvent, data };

            if (currentEvent === "start") {
              setMessages((prev) => [...prev, msg]);
              onMessage?.(msg);
            } else if (currentEvent === "progress") {
              setProgress({ step: data.step as string, percent: data.progress as number });
              onMessage?.(msg);
            } else if (currentEvent === "design_complete") {
              setMessages((prev) => [...prev, msg]);
              onComplete?.(data);
            } else if (currentEvent === "error") {
              onError?.((data.message as string) || "Unknown error");
              setStatus("error");
              return;
            } else if (currentEvent === "done") {
              setStatus("closed");
              return;
            } else {
              setMessages((prev) => [...prev, msg]);
              onMessage?.(msg);
            }

            currentEvent = "message";
          }
        }
      }

      setStatus("closed");
    } catch (err: unknown) {
      if ((err as Error).name === "AbortError") return;
      if (retriesRef.current < maxRetries) {
        retriesRef.current++;
        setTimeout(() => connect(url, body), 3000 * retriesRef.current);
      } else {
        setStatus("error");
        onError?.("Connection failed after retries");
      }
    }
  }, [onMessage, onComplete, onError, maxRetries]);

  const disconnect = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStatus("closed");
  }, []);

  const reset = useCallback(() => {
    disconnect();
    setMessages([]);
    setProgress(null);
    setStatus("idle");
  }, [disconnect]);

  return { status, messages, progress, connect, disconnect, reset };
}
