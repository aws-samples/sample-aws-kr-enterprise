"use client";

import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { Modal } from "@/components/common/Modal";
import * as projectsApi from "@/lib/api/projects";
import type { Project } from "@/lib/api/projects";

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onCreated: (project: Project) => void;
}

export function CreateProjectModal({ isOpen, onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setIsLoading(true);
    try {
      const project = await projectsApi.createProject(name.trim());
      onCreated(project);
      setName("");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="새 프로젝트">
      <form onSubmit={handleSubmit} className="space-y-4">
        <Input label="프로젝트 이름" value={name} onChange={(e) => setName(e.target.value)} placeholder="예: 쇼핑 앱 디자인" required data-testid="create-project-name" />
        <div className="flex gap-2 justify-end">
          <Button variant="ghost" type="button" onClick={onClose}>취소</Button>
          <Button type="submit" isLoading={isLoading} data-testid="create-project-submit">만들기</Button>
        </div>
      </form>
    </Modal>
  );
}
