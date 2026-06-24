export interface HandoffArtifact {
  projectId: string;
  versionId: string;
  artifactType: "compose_project" | "design_tokens" | "preview_png" | "readme";
  s3Key: string;
  fileName: string;
  sizeBytes: number;
  createdAt: string;
}
