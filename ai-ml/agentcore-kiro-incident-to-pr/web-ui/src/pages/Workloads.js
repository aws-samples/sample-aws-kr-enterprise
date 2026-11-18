import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Typography, Button, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Paper, Dialog, DialogTitle, DialogContent, DialogActions, TextField, IconButton, Chip, Stack } from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";

const EMPTY = { workload_id: "", name: "", description: "", alarm_prefixes: "", slack_webhook: "" };

export default function Workloads() {
  const [workloads, setWorkloads] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(EMPTY);

  const load = () => api.listWorkloads().then(setWorkloads).catch(console.error);
  useEffect(() => { load(); }, []);

  const submit = async () => {
    try {
      await api.createWorkload({
        workload_id: form.workload_id, name: form.name, description: form.description,
        alarm_prefixes: form.alarm_prefixes.split(",").map(s => s.trim()).filter(Boolean),
        notification: form.slack_webhook.trim() ? { slack_webhook: form.slack_webhook.trim() } : {},
      });
      setOpen(false); setForm(EMPTY); load();
    } catch (e) { alert(e.message); }
  };

  const del = async (id) => { if (window.confirm(`Delete ${id}?`)) { await api.deleteWorkload(id); load(); } };
  const f = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  return (
    <>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="h4" fontWeight={600}>워크로드</Typography>
        <Button variant="contained" startIcon={<AddIcon />} onClick={() => setOpen(true)} data-testid="add-workload">워크로드 등록</Button>
      </Stack>

      <Dialog open={open} onClose={() => setOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>워크로드 등록</DialogTitle>
        <DialogContent sx={{ display: "flex", flexDirection: "column", gap: 2, pt: "16px !important" }}>
          <TextField label="Workload ID" value={form.workload_id} onChange={f("workload_id")} required size="small" inputProps={{ "data-testid": "input-wid" }} />
          <TextField label="Name" value={form.name} onChange={f("name")} required size="small" inputProps={{ "data-testid": "input-name" }} />
          <TextField label="Alarm Prefix (쉼표 구분)" value={form.alarm_prefixes} onChange={f("alarm_prefixes")} size="small" inputProps={{ "data-testid": "input-alarms" }} />
          <TextField label="Slack Webhook URL" value={form.slack_webhook} onChange={f("slack_webhook")} size="small" inputProps={{ "data-testid": "input-slack" }} />
          <TextField label="설명" value={form.description} onChange={f("description")} multiline rows={3} inputProps={{ "data-testid": "input-desc" }} />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>취소</Button>
          <Button variant="contained" onClick={submit} data-testid="submit-workload">등록</Button>
        </DialogActions>
      </Dialog>

      {workloads.length === 0 ? <Typography color="text.secondary">등록된 워크로드가 없습니다.</Typography> : (
        <TableContainer component={Paper} elevation={1} data-testid="workloads-table">
          <Table size="small">
            <TableHead><TableRow>
              <TableCell>ID</TableCell><TableCell>이름</TableCell><TableCell>Alarm Prefix</TableCell><TableCell>알림</TableCell><TableCell align="right">관리</TableCell>
            </TableRow></TableHead>
            <TableBody>
              {workloads.map(w => (
                <TableRow key={w.workload_id} hover>
                  <TableCell><Button component={Link} to={`/workloads/${w.workload_id}`} size="small" sx={{ textTransform: "none" }}>{w.workload_id}</Button></TableCell>
                  <TableCell>{w.name}</TableCell>
                  <TableCell>{(w.alarm_prefixes || []).map((p, i) => <Chip key={i} label={p} size="small" variant="outlined" sx={{ mr: 0.5 }} />)}</TableCell>
                  <TableCell>
                    {w.notification?.slack_webhook_configured && <Typography variant="caption" sx={{ bgcolor: "#4A154B", color: "#fff", px: 0.5, borderRadius: 0.5, fontSize: 11 }}>Slack</Typography>}
                  </TableCell>
                  <TableCell align="right">
                    <IconButton size="small" color="error" onClick={() => del(w.workload_id)}><DeleteIcon fontSize="small" /></IconButton>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </>
  );
}
