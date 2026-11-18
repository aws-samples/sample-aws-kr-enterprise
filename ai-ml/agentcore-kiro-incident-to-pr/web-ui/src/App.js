import React, { useEffect, useState } from "react";
import { Routes, Route, Link, useLocation } from "react-router-dom";
import { ThemeProvider, createTheme, CssBaseline, AppBar, Toolbar, Typography, Box, Container, Tab, Tabs, Avatar, Alert, Button } from "@mui/material";
import Dashboard from "./pages/Dashboard";
import Workloads from "./pages/Workloads";
import WorkloadDetail from "./pages/WorkloadDetail";
import ReportDetail from "./pages/ReportDetail";
import Chat from "./pages/Chat";
import Login from "./pages/Login";
import { getToken, logout } from "./auth";

const theme = createTheme({
  palette: { primary: { main: "#1565c0" }, secondary: { main: "#7c4dff" } },
});

const NAV = [
  { to: "/", label: "대시보드" },
  { to: "/workloads", label: "워크로드" },
  { to: "/chat", label: "채팅" },
];

class ErrorBoundary extends React.Component {
  state = { error: null };
  static getDerivedStateFromError(error) { return { error }; }
  render() {
    if (this.state.error) return (
      <Box sx={{ p: 4 }}>
        <Alert severity="error" sx={{ mb: 2 }}>Something went wrong: {this.state.error.message}</Alert>
        <Button variant="contained" onClick={() => { this.setState({ error: null }); window.location.href = "/"; }}>Go Home</Button>
      </Box>
    );
    return this.props.children;
  }
}

export default function App() {
  const loc = useLocation();
  const tabIdx = NAV.findIndex(n => n.to === loc.pathname);
  const [loggedIn, setLoggedIn] = useState(() => Boolean(getToken()));
  useEffect(() => {
    const onLogout = () => setLoggedIn(false);
    window.addEventListener("incident-logout", onLogout);
    return () => window.removeEventListener("incident-logout", onLogout);
  }, []);

  if (!loggedIn) return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Login onLogin={() => setLoggedIn(true)} />
    </ThemeProvider>
  );

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <AppBar position="static" elevation={1}>
        <Toolbar>
          <Avatar src="/logo.svg" variant="square" sx={{ width: 32, height: 32, mr: 1.5 }} />
          <Typography variant="h6" sx={{ flexGrow: 1, fontWeight: 700 }}>Incident to PR</Typography>
          <Tabs value={tabIdx >= 0 ? tabIdx : false} textColor="inherit" indicatorColor="secondary"
            sx={{ "& .MuiTab-root": { color: "rgba(255,255,255,0.7)", "&.Mui-selected": { color: "#fff" } } }}>
            {NAV.map(n => (
              <Tab key={n.to} label={n.label} component={Link} to={n.to} data-testid={`nav-${n.label.toLowerCase()}`} />
            ))}
          </Tabs>
          <Button color="inherit" size="small" onClick={logout} sx={{ ml: 1 }} data-testid="logout">로그아웃</Button>
        </Toolbar>
      </AppBar>
      <Container maxWidth="lg" sx={{ py: 3 }}>
        <ErrorBoundary>
          <Box>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/workloads" element={<Workloads />} />
              <Route path="/workloads/:id" element={<WorkloadDetail />} />
              <Route path="/reports/:id" element={<ReportDetail />} />
              <Route path="/chat" element={<Chat />} />
            </Routes>
          </Box>
        </ErrorBoundary>
      </Container>
    </ThemeProvider>
  );
}
