"use client";

import { useCallback, useEffect, useState } from "react";
import { deleteFile as deleteFileApi, listFiles, uploadFile } from "@/lib/api/files";

export interface UploadedFile {
  id: string;
  name: string;
  size: number;
}

interface UploadState {
  isUploading: boolean;
  progress: number;
  files: UploadedFile[];
  error: string | null;
}

export function useFileUpload(projectId: string) {
  const [state, setState] = useState<UploadState>({ isUploading: false, progress: 0, files: [], error: null });

  useEffect(() => {
    if (!projectId) return;
    listFiles(projectId).then((serverFiles) => {
      const loaded: UploadedFile[] = serverFiles
        .filter((f) => f.upload_status === "completed")
        .map((f) => ({ id: f.file_id, name: f.filename, size: f.size }));
      if (loaded.length > 0) {
        setState((s) => ({ ...s, files: loaded }));
      }
    }).catch(() => {});
  }, [projectId]);

  const upload = useCallback(async (files: File[]) => {
    setState((s) => ({ ...s, isUploading: true, progress: 0, error: null }));

    const uploaded: UploadedFile[] = [];
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const fileId = await uploadFile(projectId, file, (pct) => {
          const overall = Math.round(((i + pct / 100) / files.length) * 100);
          setState((s) => ({ ...s, progress: overall }));
        });
        uploaded.push({ id: fileId, name: file.name, size: file.size });
      }
      setState((s) => ({ ...s, isUploading: false, progress: 100, files: [...s.files, ...uploaded] }));
    } catch (e: any) {
      setState((s) => ({ ...s, isUploading: false, error: e.message || "Upload failed" }));
    }

    return uploaded.map((f) => f.id);
  }, [projectId]);

  const removeFile = useCallback((fileId: string) => {
    setState((s) => ({ ...s, files: s.files.filter((f) => f.id !== fileId) }));
    deleteFileApi(projectId, fileId).catch(() => {});
  }, [projectId]);

  const reset = useCallback(() => {
    setState({ isUploading: false, progress: 0, files: [], error: null });
  }, []);

  const fileIds = state.files.map((f) => f.id);

  return { ...state, fileIds, upload, removeFile, reset };
}
