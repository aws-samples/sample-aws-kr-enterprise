"use client";

import { useCallback, useState } from "react";
import type { Version } from "@/lib/api/projects";
import * as projectsApi from "@/lib/api/projects";

export function useVersionHistory(projectId: string) {
  const [versions, setVersions] = useState<Version[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  const loadVersions = useCallback(async (stageId?: string) => {
    setIsLoading(true);
    try {
      const result = await projectsApi.listVersions(projectId, stageId);
      setVersions(result.items);
    } finally {
      setIsLoading(false);
    }
  }, [projectId]);

  const revert = useCallback(async (targetVersionId: string) => {
    const newVersion = await projectsApi.revertVersion(projectId, targetVersionId);
    setVersions((prev) => [newVersion, ...prev]);
    return newVersion;
  }, [projectId]);

  return { versions, isLoading, loadVersions, revert };
}
