"use client";

import { useCallback } from "react";
import { ProgressBar } from "@/components/common/ProgressBar";
import type { UploadedFile } from "@/lib/hooks/useFileUpload";

interface FileUploadProps {
  projectId: string;
  fileUpload: {
    isUploading: boolean;
    progress: number;
    files: UploadedFile[];
    fileIds: string[];
    error: string | null;
    upload: (files: File[]) => Promise<string[]>;
    removeFile: (fileId: string) => void;
  };
}

const ACCEPTED_TYPES = ".pdf,.docx,.md,.txt";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

export function FileUpload({ projectId, fileUpload }: FileUploadProps) {
  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) fileUpload.upload(files);
  }, [fileUpload]);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (files.length > 0) fileUpload.upload(files);
  };

  return (
    <div className="space-y-3" data-testid="file-upload">
      <h3 className="text-sm font-medium">참고 파일</h3>
      <div
        onDrop={handleDrop}
        onDragOver={(e) => e.preventDefault()}
        className="border-2 border-dashed border-gray-300 rounded-mdesigner p-6 text-center hover:border-primary/50 transition-colors"
      >
        <p className="text-sm text-gray-500 mb-2">파일을 드래그하거나 클릭하여 업로드</p>
        <p className="text-xs text-gray-400">PDF, DOCX, MD, TXT (최대 20MB)</p>
        <input type="file" accept={ACCEPTED_TYPES} multiple onChange={handleChange} className="hidden" id="file-input" />
        <label htmlFor="file-input" className="mt-3 inline-block text-sm text-primary cursor-pointer hover:underline">파일 선택</label>
      </div>

      {fileUpload.isUploading && <ProgressBar percent={fileUpload.progress} label="업로드 중..." />}
      {fileUpload.error && <p className="text-xs text-error">{fileUpload.error}</p>}

      {fileUpload.files.length > 0 && (
        <ul className="space-y-1">
          {fileUpload.files.map((file) => (
            <li key={file.id} className="flex items-center justify-between px-3 py-2 bg-gray-50 rounded-mdesigner">
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-sm truncate">{file.name}</span>
                <span className="text-xs text-gray-400 shrink-0">{formatSize(file.size)}</span>
              </div>
              <button
                onClick={() => fileUpload.removeFile(file.id)}
                className="text-gray-400 hover:text-error text-lg leading-none shrink-0 ml-2"
                aria-label={`${file.name} 제거`}
              >
                &times;
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
