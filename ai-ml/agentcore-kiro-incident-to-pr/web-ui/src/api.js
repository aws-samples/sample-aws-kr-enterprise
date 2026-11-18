import { getToken, logout } from "./auth";

const API_BASE = process.env.REACT_APP_API_URL || "/api";

async function request(path, options = {}) {
  const token = getToken();
  const headers = { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) };
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (res.status === 401 && path !== "/login") { logout(); throw new Error("로그인이 필요합니다."); }
  if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.error || e.message || res.statusText || `HTTP ${res.status}`); }
  return res.json();
}

export const api = {
  // Workloads
  listWorkloads: () => request("/workloads"),
  getWorkload: (id) => request(`/workloads/${id}`),
  createWorkload: (data) => request("/workloads", { method: "POST", body: JSON.stringify(data) }),
  updateWorkload: (id, data) => request(`/workloads/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteWorkload: (id) => request(`/workloads/${id}`, { method: "DELETE" }),
  // Presigned POST: S3 enforces the key and the size limit from the signed policy.
  uploadDocument: async (id, file) => {
    const { upload } = await request(`/workloads/${id}/upload-url`, { method: "POST", body: JSON.stringify({ filename: file.name }) });
    const form = new FormData();
    Object.entries(upload.fields).forEach(([k, v]) => form.append(k, v));
    form.append("file", file);
    const res = await fetch(upload.url, { method: "POST", body: form });
    if (!res.ok) throw new Error(res.status === 400 ? "파일이 크기 제한(1MB)을 넘었거나 업로드 조건에 맞지 않습니다." : `업로드 실패 (HTTP ${res.status})`);
  },
  listDocuments: (id) => request(`/workloads/${id}/documents`),
  deleteDocument: (id, key) => request(`/workloads/${id}/documents`, { method: "DELETE", body: JSON.stringify({ key }) }),
  syncKb: (id) => request(`/workloads/${id}/sync`, { method: "POST" }),

  // Reports
  listReports: () => request("/reports"),
  getReport: (id) => request(`/reports/${id}`),
  deleteReport: (id) => request(`/reports/${id}`, { method: "DELETE" }),

  // Login, Status & Chat
  login: (password) => request("/login", { method: "POST", body: JSON.stringify({ password }) }),
  getStatus: () => request("/status"),
  chat: (message, sessionId) => request("/chat", { method: "POST", body: JSON.stringify({ message, session_id: sessionId }) }),
};
