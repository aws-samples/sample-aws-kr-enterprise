import React, { useEffect, useState, useRef, useCallback } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { Typography, Paper, Button, List, ListItem, ListItemText, Alert, CircularProgress, Stack, TextField, IconButton, Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Chip, FormControlLabel, Checkbox } from "@mui/material";
import CloudUploadIcon from "@mui/icons-material/CloudUpload";
import EditIcon from "@mui/icons-material/Edit";
import SaveIcon from "@mui/icons-material/Save";
import CancelIcon from "@mui/icons-material/Cancel";
import DeleteIcon from "@mui/icons-material/Delete";
import SyncIcon from "@mui/icons-material/Sync";

export default function WorkloadDetail() {
  const { id } = useParams();
  const [w, setW] = useState(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({});
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState("");
  const [docs, setDocs] = useState([]);
  const [syncing, setSyncing] = useState(false);
  const [syncStatus, setSyncStatus] = useState(null);
  const fileRef = useRef();

  const loadDocs = useCallback(() => { api.listDocuments(id).then(d => setDocs(d.documents || [])).catch(() => {}); }, [id]);
  const load = useCallback(() => { api.getWorkload(id).then(d => { setW(d); setForm({ name: d.name || "", description: d.description || "", alarm_prefixes: (d.alarm_prefixes || []).join(", "), slack_webhook: "", remove_webhook: false }); }); loadDocs(); }, [id, loadDocs]);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setSaving(true);
    try {
      // The stored webhook is never sent to the browser: leave the field empty to keep it.
      const update = {
        name: form.name, description: form.description,
        alarm_prefixes: form.alarm_prefixes.split(",").map(s => s.trim()).filter(Boolean),
      };
      if (form.remove_webhook) update.notification = { slack_webhook: null };
      else if (form.slack_webhook.trim()) update.notification = { slack_webhook: form.slack_webhook.trim() };
      await api.updateWorkload(id, update);
      setEditing(false); load();
    } catch (e) { alert(e.message); }
    setSaving(false);
  };

  const upload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setUploading(true); setMsg("");
    try {
      await api.uploadDocument(id, file);
      setMsg(`업로드 완료: ${file.name}`); fileRef.current.value = "";
      loadDocs();
    } catch (e) { setMsg(`Error: ${e.message}`); }
    setUploading(false);
  };

  const deleteDoc = async (key, name) => {
    if (!window.confirm(`"${name}" 파일을 삭제하시겠습니까?`)) return;
    try { await api.deleteDocument(id, key); loadDocs(); } catch (e) { alert(e.message); }
  };

  const syncKb = async () => {
    setSyncing(true); setSyncStatus(null);
    try {
      const res = await api.syncKb(id);
      setSyncStatus(res);
    } catch (e) { setSyncStatus({ status: "FAILED", error: e.message }); }
    setSyncing(false);
  };

  if (!w) return <CircularProgress sx={{ mt: 4 }} />;
  const f = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const fmtSize = (bytes) => bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
  const syncColor = { IN_PROGRESS: "info", COMPLETE: "success", FAILED: "error", STARTING: "info" };

  return (
    <>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <div>
          <Typography variant="h4" fontWeight={600}>{w.name}</Typography>
          <Typography variant="body2" color="text.secondary">{w.workload_id}</Typography>
        </div>
        {!editing && <IconButton onClick={() => setEditing(true)}><EditIcon /></IconButton>}
      </Stack>

      {editing ? (
        <Paper elevation={1} sx={{ p: 2, mb: 2, mt: 2, display: "flex", flexDirection: "column", gap: 2 }}>
          <TextField label="이름" value={form.name} onChange={f("name")} size="small" />
          <TextField label="설명" value={form.description} onChange={f("description")} multiline rows={3} />
          <TextField label="Alarm Prefix (쉼표 구분)" value={form.alarm_prefixes} onChange={f("alarm_prefixes")} size="small" />
          <TextField label={w.notification?.slack_webhook_configured ? "새 Slack Webhook URL (비워 두면 기존 값 유지)" : "Slack Webhook URL"}
            value={form.slack_webhook} onChange={f("slack_webhook")} size="small" disabled={form.remove_webhook} />
          {w.notification?.slack_webhook_configured && (
            <FormControlLabel label="Slack Webhook 삭제"
              control={<Checkbox checked={form.remove_webhook} onChange={e => setForm({ ...form, remove_webhook: e.target.checked })} />} />
          )}
          <Stack direction="row" spacing={1}>
            <Button variant="contained" startIcon={saving ? <CircularProgress size={16} /> : <SaveIcon />} onClick={save} disabled={saving}>저장</Button>
            <Button variant="outlined" startIcon={<CancelIcon />} onClick={() => setEditing(false)}>취소</Button>
          </Stack>
        </Paper>
      ) : (
        <>
          <Paper elevation={1} sx={{ p: 2, mb: 2, mt: 2 }}>
            <Typography variant="subtitle2" color="text.secondary">설명</Typography>
            <Typography variant="body1" sx={{ whiteSpace: "pre-wrap", mt: 1 }}>{w.description || "—"}</Typography>
          </Paper>
          <Paper elevation={1} sx={{ p: 2, mb: 2 }}>
            <Typography variant="subtitle2" color="text.secondary" gutterBottom>Alarm Prefix</Typography>
            {(w.alarm_prefixes || []).length === 0 ? <Typography>None</Typography> :
              <List dense disablePadding>{w.alarm_prefixes.map((p, i) => <ListItem key={i} disableGutters><ListItemText primary={p} primaryTypographyProps={{ fontFamily: "monospace" }} /></ListItem>)}</List>}
          </Paper>
          <Paper elevation={1} sx={{ p: 2, mb: 2 }}>
            <Typography variant="subtitle2" color="text.secondary" gutterBottom>알림 설정</Typography>
            <Typography variant="body2">Slack: {w.notification?.slack_webhook_configured ? "설정됨" : "—"}</Typography>
          </Paper>
        </>
      )}

      <Paper elevation={1} sx={{ p: 2, mb: 2 }}>
        <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
          <div>
            <Typography variant="subtitle2" color="text.secondary">Knowledge Base 문서</Typography>
            <Typography variant="caption" color="text.secondary">S3에 업로드하여 에이전트가 RAG 검색에 활용합니다. 텍스트 파일(.md, .txt, .json, .yaml, .yml, .csv, .log), 1MB 이하, 워크로드당 20개까지</Typography>
          </div>
          <Stack direction="row" spacing={1} alignItems="center">
            <Button variant="outlined" size="small" startIcon={syncing ? <CircularProgress size={16} /> : <SyncIcon />}
              onClick={syncKb} disabled={syncing}>
              {syncing ? "동기화 중..." : "지식 동기화"}
            </Button>
          </Stack>
        </Stack>

        {syncStatus && (
          <Alert severity={syncStatus.status === "FAILED" ? "error" : syncStatus.status === "COMPLETE" ? "success" : "info"} sx={{ mb: 1 }}>
            {syncStatus.error ? syncStatus.error
              : syncStatus.status === "IN_PROGRESS" ? `동기화 진행 중 (Job: ${syncStatus.job_id?.slice(0, 8)}...)`
              : `동기화 시작됨 (Job: ${syncStatus.job_id?.slice(0, 8)}...)`}
            {syncStatus.statistics && Object.keys(syncStatus.statistics).length > 0 && (
              <Typography variant="caption" display="block">
                처리: {syncStatus.statistics.numberOfDocumentsScanned || 0}건,
                성공: {syncStatus.statistics.numberOfNewDocumentsIndexed || 0}건,
                수정: {syncStatus.statistics.numberOfModifiedDocumentsIndexed || 0}건,
                실패: {syncStatus.statistics.numberOfDocumentsFailed || 0}건
              </Typography>
            )}
          </Alert>
        )}

        {docs.length > 0 && (
          <TableContainer sx={{ mb: 1 }}>
            <Table size="small">
              <TableHead><TableRow>
                <TableCell>파일명</TableCell><TableCell>크기</TableCell><TableCell>업로드 시간</TableCell><TableCell align="right" />
              </TableRow></TableHead>
              <TableBody>
                {docs.map(d => (
                  <TableRow key={d.key} hover>
                    <TableCell sx={{ fontFamily: "monospace", fontSize: 13 }}>{d.name}</TableCell>
                    <TableCell>{fmtSize(d.size)}</TableCell>
                    <TableCell sx={{ fontSize: 13 }}>{d.last_modified?.substring(0, 19)}</TableCell>
                    <TableCell align="right">
                      <IconButton size="small" color="error" onClick={() => deleteDoc(d.key, d.name)}><DeleteIcon fontSize="small" /></IconButton>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
        {docs.length === 0 && <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>업로드된 문서가 없습니다.</Typography>}

        <Stack direction="row" spacing={1} alignItems="center">
          <input ref={fileRef} type="file" data-testid="file-input" />
          <Button variant="contained" size="small" startIcon={uploading ? <CircularProgress size={16} /> : <CloudUploadIcon />}
            onClick={upload} disabled={uploading} data-testid="upload-btn">
            {uploading ? "업로드 중..." : "업로드"}
          </Button>
        </Stack>
        {msg && <Alert severity={msg.startsWith("Error") ? "error" : "success"} sx={{ mt: 1 }}>{msg}</Alert>}
      </Paper>
    </>
  );
}
