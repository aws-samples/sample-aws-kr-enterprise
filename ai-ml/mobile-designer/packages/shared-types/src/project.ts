export type ProjectStage = "requirements" | "wireframe" | "design" | "handoff";

export type ProjectStatus = "active" | "archived" | "deleted";

export interface Project {
  id: string;
  userId: string;
  name: string;
  description: string;
  currentStage: ProjectStage;
  status: ProjectStatus;
  createdAt: string;
  updatedAt: string;
}
