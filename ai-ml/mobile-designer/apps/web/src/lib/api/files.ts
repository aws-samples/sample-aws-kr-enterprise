import { apiClient } from "./client";

interface PresignResponse {
  file_id: string;
  upload_url: string;
  key: string;
  max_size_bytes: number;
}

export async function requestPresign(projectId: string, filename: string, contentType: string, size: number): Promise<PresignResponse> {
  return apiClient.post("/files/presign", { project_id: projectId, filename, content_type: contentType, size });
}

export async function completeUpload(projectId: string, fileId: string): Promise<void> {
  await apiClient.post("/files/complete", { project_id: projectId, file_id: fileId });
}

export async function listFiles(projectId: string): Promise<Array<{ file_id: string; filename: string; file_type: string; upload_status: string; size: number }>> {
  return apiClient.get(`/files/${projectId}`);
}

export async function deleteFile(projectId: string, fileId: string): Promise<void> {
  await apiClient.delete(`/files/${projectId}/${fileId}`);
}

export async function uploadFile(projectId: string, file: File, onProgress?: (pct: number) => void): Promise<string> {
  const contentType = file.type || "application/octet-stream";
  const presign = await requestPresign(projectId, file.name, contentType, file.size);

  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", presign.upload_url);
    xhr.setRequestHeader("Content-Type", contentType);
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
    }
    xhr.onload = () => (xhr.status < 400 ? resolve() : reject(new Error(`Upload failed: ${xhr.status}`)));
    xhr.onerror = () => reject(new Error("Upload network error"));
    xhr.send(file);
  });

  await completeUpload(projectId, presign.file_id);
  return presign.file_id;
}
