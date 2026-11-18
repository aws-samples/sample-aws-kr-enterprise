import React, { useState } from "react";
import { Box, Paper, Typography, TextField, Button, Alert, Avatar } from "@mui/material";
import { api } from "../api";
import { setToken } from "../auth";

export default function Login({ onLogin }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setLoading(true); setError("");
    try {
      const { token } = await api.login(password);
      setToken(token); setPassword(""); onLogin();
    } catch (err) { setError(err.message); }
    setLoading(false);
  };

  return (
    <Box sx={{ display: "flex", justifyContent: "center", mt: 12 }}>
      <Paper elevation={2} component="form" onSubmit={submit} sx={{ p: 4, width: 360, display: "flex", flexDirection: "column", gap: 2 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1.5 }}>
          <Avatar src="/logo.svg" variant="square" sx={{ width: 32, height: 32 }} />
          <Typography variant="h6" fontWeight={700}>Incident to PR</Typography>
        </Box>
        <Typography variant="body2" color="text.secondary">배포할 때 Secrets Manager에 저장한 웹 패스워드를 입력하세요.</Typography>
        <TextField label="패스워드" type="password" value={password} onChange={e => setPassword(e.target.value)}
          autoFocus required size="small" inputProps={{ "data-testid": "input-password" }} />
        {error && <Alert severity="error">{error}</Alert>}
        <Button type="submit" variant="contained" disabled={loading || !password} data-testid="login">로그인</Button>
      </Paper>
    </Box>
  );
}
