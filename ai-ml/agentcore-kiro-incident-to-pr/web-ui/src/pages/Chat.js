import React, { useState, useRef, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import { markdownComponents } from "../markdown";
import remarkGfm from "remark-gfm";
import { Box, Paper, TextField, IconButton, Typography, Avatar, CircularProgress } from "@mui/material";
import SendIcon from "@mui/icons-material/Send";
import SmartToyIcon from "@mui/icons-material/SmartToy";
import { api } from "../api";
import { getToken, logout } from "../auth";

const WS_URL = process.env.REACT_APP_WS_URL || "";

export default function Chat() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState("");
  const [sessionId] = useState(() => crypto.randomUUID());
  const wsRef = useRef(null);
  const bottomRef = useRef(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, status]);

  const connect = useCallback(() => {
    return new Promise((resolve, reject) => {
      api.getStatus().then(d => {
        const wsUrl = d.ws_url || WS_URL;
        if (!wsUrl) { reject(new Error("WebSocket URL not configured")); return; }
        const token = getToken();
        if (!token) { logout(); reject(new Error("로그인이 필요합니다.")); return; }
        // Browsers cannot set headers on a WebSocket, so the token goes in the query string for $connect
        // and in every message, which the server checks again.
        const ws = new WebSocket(`${wsUrl}?token=${encodeURIComponent(token)}`);
        ws.onopen = () => { wsRef.current = ws; resolve(ws); };
        ws.onerror = (e) => reject(e);
        ws.onmessage = (e) => {
          const msg = JSON.parse(e.data);
          if (msg.type === "status") setStatus(msg.data);
          if (msg.type === "response") { setMessages(prev => [...prev, { role: "assistant", text: msg.data }]); setStatus(""); setLoading(false); }
          if (msg.type === "error") { setMessages(prev => [...prev, { role: "assistant", text: `${msg.data}` }]); setStatus(""); setLoading(false); }
          if (msg.type === "done") { setStatus(""); setLoading(false); }
          if (msg.type === "unauthorized") { setStatus(""); setLoading(false); logout(); }
        };
        ws.onclose = () => { wsRef.current = null; };
      }).catch(reject);
    });
  }, []);

  const send = async () => {
    if (!input.trim() || loading) return;
    const userMsg = input.trim();
    setInput(""); setMessages(prev => [...prev, { role: "user", text: userMsg }]); setLoading(true); setStatus("연결 중...");
    try {
      let ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) ws = await connect();
      ws.send(JSON.stringify({ action: "sendMessage", message: userMsg, session_id: sessionId, token: getToken() }));
    } catch (e) {
      setStatus("WebSocket 연결 실패, REST API 사용...");
      try {
        const data = await api.chat(userMsg, sessionId);
        setMessages(prev => [...prev, { role: "assistant", text: data.response || data.error || "응답 없음" }]);
      } catch (e2) { setMessages(prev => [...prev, { role: "assistant", text: e2.message || "요청 시간이 초과되었습니다." }]); }
      setStatus(""); setLoading(false);
    }
  };

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "calc(100vh - 140px)" }}>
      <Typography variant="h4" fontWeight={600} gutterBottom>장애 분석 에이전트 채팅</Typography>
      <Paper elevation={1} sx={{ flex: 1, overflow: "auto", p: 2, mb: 2 }} data-testid="message-list">
        {messages.map((m, i) => (
          <Box key={i} sx={{ display: "flex", justifyContent: m.role === "user" ? "flex-end" : "flex-start", mb: 2 }}>
            {m.role === "assistant" && <Avatar sx={{ bgcolor: "primary.main", width: 32, height: 32, mr: 1, mt: 0.5 }}><SmartToyIcon fontSize="small" /></Avatar>}
            <Paper elevation={0} sx={{
              p: 1.5, maxWidth: "70%", borderRadius: 2,
              bgcolor: m.role === "user" ? "primary.main" : "grey.100",
              color: m.role === "user" ? "primary.contrastText" : "text.primary",
              "& p": { m: 0 }, "& ul, & ol": { my: 0.5, pl: 2 }, "& h2, & h3": { mt: 1, mb: 0.5, fontSize: "1rem" },
              "& code": { bgcolor: "rgba(0,0,0,0.06)", px: 0.5, borderRadius: 0.5, fontSize: "0.85em" },
              "& pre": { bgcolor: "rgba(0,0,0,0.06)", p: 1, borderRadius: 1, overflow: "auto" },
              "& table": { borderCollapse: "collapse", width: "100%", my: 1, fontSize: "0.85em" },
              "& th, & td": { border: "1px solid", borderColor: "divider", px: 1, py: 0.5 },
              "& th": { bgcolor: "rgba(0,0,0,0.04)", fontWeight: 600 },
            }}>
              {m.role === "user" ? <Typography variant="body2">{m.text}</Typography> : <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{m.text}</ReactMarkdown>}
            </Paper>
          </Box>
        ))}
        {status && (
          <Box sx={{ display: "flex", alignItems: "center", gap: 1, color: "text.secondary", py: 1 }}>
            <CircularProgress size={16} /> <Typography variant="body2">{status}</Typography>
          </Box>
        )}
        {loading && !status && (
          <Box sx={{ display: "flex", alignItems: "center", gap: 1, color: "text.secondary", py: 1 }}>
            <CircularProgress size={16} /> <Typography variant="body2">분석 중...</Typography>
          </Box>
        )}
        <div ref={bottomRef} />
      </Paper>
      <Paper elevation={2} sx={{ display: "flex", alignItems: "center", p: 1, gap: 1 }}>
        <TextField
          data-testid="chat-input"
          value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
          placeholder={loading ? "응답 대기 중..." : "시스템에 대해 질문하세요..."}
          disabled={loading} fullWidth size="small" variant="outlined"
          sx={{ "& .MuiOutlinedInput-root": { borderRadius: 2 } }}
        />
        <IconButton color="primary" onClick={send} disabled={loading || !input.trim()} data-testid="chat-send">
          <SendIcon />
        </IconButton>
      </Paper>
    </Box>
  );
}
