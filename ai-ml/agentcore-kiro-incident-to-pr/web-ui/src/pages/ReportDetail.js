import React, { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../api";
import ReactMarkdown from "react-markdown";
import { markdownComponents } from "../markdown";
import remarkGfm from "remark-gfm";
import { Typography, Paper, Chip, Button, List, ListItem, ListItemText, Card, CardContent, Stack, CircularProgress, Alert } from "@mui/material";

const statusColors = { ANALYZING: "default", MERGED: "info", REPORTED: "default", FAILED: "error", PENDING: "warning", RESOLVED: "success" };
const statusLabels = { ANALYZING: "분석 중", PENDING: "PR 리뷰 대기", MERGED: "병합됨, 배포 중", RESOLVED: "배포 완료", REPORTED: "분석 완료, PR 없음", FAILED: "실패" };
const prStates = {
  OPEN: ["열림: 리뷰와 병합을 기다립니다", "warning"], MERGED: ["병합됨: 파이프라인이 배포 중입니다", "info"],
  DEPLOYED: ["배포 완료", "success"], CLOSED: ["병합하지 않고 닫힘", "default"], UNKNOWN: ["상태를 확인할 수 없음", "error"],
  FAILED: ["PR을 만들지 못함", "error"],
};
const riskColors = { LOW: "success", MEDIUM: "warning", HIGH: "error" };

export default function ReportDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => { api.getReport(id).then(setReport).catch(e => setError(e.message)); }, [id]);

  if (error) return <Alert severity="error">{error}</Alert>;
  if (!report) return <CircularProgress sx={{ mt: 4 }} />;

  const data = report.report_data || {};

  return (
    <>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="h4" fontWeight={600}>RCA 리포트</Typography>
        <Button variant="outlined" color="error" size="small" onClick={async () => {
          if (window.confirm("이 리포트를 삭제하시겠습니까?")) { await api.deleteReport(id); navigate("/"); }
        }}>삭제</Button>
      </Stack>
      <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 3 }} flexWrap="wrap">
        <Typography variant="body2" color="text.secondary">ID: {report.report_id}</Typography>
        <Chip label={statusLabels[report.status] || report.status} size="small" color={statusColors[report.status] || "default"} />
        <Typography variant="body2">Alarm: <strong>{report.alarm_name}</strong></Typography>
        <Typography variant="body2" color="text.secondary">{report.created_at}</Typography>
      </Stack>

      {report.status === "ANALYZING" && (
        <Paper elevation={1} sx={{ p: 3, mb: 2, textAlign: "center" }}>
          <CircularProgress sx={{ mb: 1 }} />
          <Typography color="text.secondary">RCA 분석 중... 잠시 후 새로고침 해주세요.</Typography>
        </Paper>
      )}
      {data.review && <Alert severity="warning" sx={{ mb: 2 }}>{data.review}</Alert>}
      {data.summary && report.status !== "ANALYZING" && (
        <Paper elevation={1} sx={{ p: 2, mb: 2 }}>
          <Typography variant="subtitle2" color="text.secondary">요약</Typography>
          <Typography variant="body1" sx={{ mt: 1 }}>{data.summary}</Typography>
        </Paper>
      )}

      {data.timeline?.length > 0 && (
        <Paper elevation={1} sx={{ p: 2, mb: 2 }}>
          <Typography variant="subtitle2" color="text.secondary" gutterBottom>타임라인</Typography>
          <List dense disablePadding>
            {data.timeline.map((t, i) => (
              <ListItem key={i} disableGutters>
                <ListItemText primary={t.event} secondary={t.time} />
              </ListItem>
            ))}
          </List>
        </Paper>
      )}

      {data.root_cause && (
        <Paper elevation={1} sx={{ p: 2, mb: 2 }}>
          <Typography variant="subtitle2" color="text.secondary" gutterBottom>근본 원인</Typography>
          <Typography>{data.root_cause.description}</Typography>
          <Chip label={`신뢰도: ${data.root_cause.confidence}`} size="small" sx={{ mt: 1 }} />
          {data.root_cause.evidence?.length > 0 && (
            <List dense sx={{ mt: 1 }}>{data.root_cause.evidence.map((e, i) => <ListItem key={i} disableGutters><ListItemText primary={e} primaryTypographyProps={{ variant: "body2" }} /></ListItem>)}</List>
          )}
        </Paper>
      )}

      {data.impact && (
        <Paper elevation={1} sx={{ p: 2, mb: 2 }}>
          <Typography variant="subtitle2" color="text.secondary" gutterBottom>영향 범위</Typography>
          <Typography>{data.impact.service_impact}</Typography>
          {data.impact.affected_resources?.length > 0 && (
            <List dense>{data.impact.affected_resources.map((r, i) => <ListItem key={i} disableGutters><ListItemText primary={r} primaryTypographyProps={{ variant: "body2", fontFamily: "monospace" }} /></ListItem>)}</List>
          )}
        </Paper>
      )}

      {data.pull_request && <PullRequestCard pullRequest={data.pull_request} />}

      {data.recommended_actions?.length > 0 && (
        <>
          <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 1 }}>권장 조치</Typography>
          {data.recommended_actions.map(action => <ActionCard key={action.action_id} action={action} />)}
        </>
      )}
    </>
  );
}

function PullRequestCard({ pullRequest }) {
  const [label, color] = prStates[pullRequest.state || pullRequest.status] || [pullRequest.state || pullRequest.status, "default"];
  return (
    <Paper elevation={1} sx={{ p: 2, mb: 2, borderLeft: 4, borderColor: "primary.main" }} data-testid="pull-request">
      <Stack direction="row" justifyContent="space-between" alignItems="center" flexWrap="wrap">
        <Typography variant="subtitle2" color="text.secondary">수정 PR (워크로드 저장소)</Typography>
        <Chip label={label} size="small" color={color} />
      </Stack>
      {pullRequest.pull_request_id ? (
        <>
          <Typography variant="body1" sx={{ mt: 1 }}>#{pullRequest.pull_request_id} {pullRequest.title}</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ fontFamily: "monospace" }}>
            {pullRequest.branch} {"->"} main: {(pullRequest.files || []).join(", ")}
          </Typography>
          <Typography variant="body2" sx={{ mt: 1 }}>
            PR을 리뷰하고 병합하면 파이프라인이 배포합니다. 배포가 끝나면 이 리포트는 배포 완료로 바뀝니다.
          </Typography>
          {pullRequest.url && (
            <Button variant="contained" size="small" sx={{ mt: 1 }} component="a" href={pullRequest.url}
              target="_blank" rel="noopener noreferrer">CodeCommit에서 PR 열기</Button>
          )}
        </>
      ) : (
        <Alert severity="error" sx={{ mt: 1 }}>수정 PR을 만들지 못했습니다. {pullRequest.error}</Alert>
      )}
    </Paper>
  );
}

function ActionCard({ action }) {
  return (
    <Card elevation={1} sx={{ mb: 1 }} data-testid={`action-${action.action_id}`}>
      <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="body2" fontWeight={600}>#{action.priority}</Typography>
          <Typography variant="body2">{action.description}</Typography>
          <Chip label={action.risk_level} size="small" color={riskColors[action.risk_level] || "default"} />
        </Stack>
        {action.change && (
          <Paper variant="outlined" sx={{ mt: 1, p: 1, borderRadius: 1, "& p": { m: 0 } }}>
            <Typography variant="caption" color="text.secondary">코드 변경</Typography>
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{action.change}</ReactMarkdown>
          </Paper>
        )}
      </CardContent>
    </Card>
  );
}
