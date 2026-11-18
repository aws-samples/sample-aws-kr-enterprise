import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Typography, Card, CardContent, Grid, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Paper, Chip, Button } from "@mui/material";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import ErrorIcon from "@mui/icons-material/Error";
import DevicesIcon from "@mui/icons-material/Devices";

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [reports, setReports] = useState([]);

  const load = () => {
    api.getStatus().then(setStatus).catch(console.error);
    api.listReports().then(setReports).catch(console.error);
  };
  useEffect(() => { load(); const i = setInterval(load, 30000); return () => clearInterval(i); }, []);

  const cards = status ? [
    { label: "정상", value: status.summary?.ok ?? 0, color: "#4caf50", icon: <CheckCircleIcon sx={{ fontSize: 40, color: "#4caf50" }} /> },
    { label: "알람", value: status.summary?.alarm ?? 0, color: "#f44336", icon: <ErrorIcon sx={{ fontSize: 40, color: "#f44336" }} /> },
    { label: "워크로드", value: status.workload_count ?? 0, color: "#1565c0", icon: <DevicesIcon sx={{ fontSize: 40, color: "#1565c0" }} /> },
  ] : [];

  const statusColors = { ANALYZING: "default", PENDING: "warning", MERGED: "info", REPORTED: "default", RESOLVED: "success", FAILED: "error" };
  const statusLabels = { ANALYZING: "분석 중", PENDING: "PR 리뷰 대기", MERGED: "병합됨, 배포 중", RESOLVED: "배포 완료", REPORTED: "분석 완료, PR 없음", FAILED: "실패" };

  return (
    <>
      <Typography variant="h4" gutterBottom fontWeight={600}>대시보드</Typography>
      <Grid container spacing={2} sx={{ mb: 4 }} data-testid="summary">
        {cards.map(c => (
          <Grid size={{ xs: 12, sm: 4 }} key={c.label}>
            <Card elevation={2}>
              <CardContent sx={{ display: "flex", alignItems: "center", gap: 2 }}>
                {c.icon}
                <div>
                  <Typography variant="h3" fontWeight={700} color={c.color}>{c.value}</Typography>
                  <Typography variant="body2" color="text.secondary">{c.label}</Typography>
                </div>
              </CardContent>
            </Card>
          </Grid>
        ))}
      </Grid>

      <Typography variant="h5" gutterBottom fontWeight={600}>최근 RCA 리포트</Typography>
      {reports.length === 0 ? <Typography color="text.secondary">리포트가 없습니다.</Typography> : (
        <TableContainer component={Paper} elevation={1} data-testid="reports-table">
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>시간</TableCell><TableCell>워크로드</TableCell><TableCell>알람</TableCell><TableCell>상태</TableCell><TableCell />
              </TableRow>
            </TableHead>
            <TableBody>
              {reports.slice(0, 20).map(r => (
                <TableRow key={r.report_id} hover>
                  <TableCell sx={{ fontSize: 13 }}>{r.created_at?.substring(0, 19)}</TableCell>
                  <TableCell>{r.report_data?.workload || r.workload_id || "—"}</TableCell>
                  <TableCell>{r.alarm_name}</TableCell>
                  <TableCell><Chip label={statusLabels[r.status] || r.status} size="small" color={statusColors[r.status] || "default"} /></TableCell>
                  <TableCell><Button component={Link} to={`/reports/${r.report_id}`} size="small">보기</Button></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </>
  );
}
