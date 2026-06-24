"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useProject } from "@/lib/contexts/ProjectContext";

const STAGE_TO_ID: Record<string, string> = {
  requirements: "1",
  wireframe: "2",
  design: "3",
  handoff: "4",
};

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  const { project } = useProject();
  const router = useRouter();

  useEffect(() => {
    if (project) {
      const stageId = STAGE_TO_ID[project.current_stage] || "1";
      router.replace(`/project/${id}/stage/${stageId}`);
    }
  }, [project, id, router]);

  return null;
}
