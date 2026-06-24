"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { verifyShareLink } from "@/lib/api/collaboration";

export default function SharedPage() {
  const { token } = useParams<{ token: string }>();
  const [data, setData] = useState<{ project_id: string; permission: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    verifyShareLink(token)
      .then(setData)
      .catch((e) => setError(e.message || "유효하지 않은 공유 링크입니다"));
  }, [token]);

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p className="text-error">{error}</p>
      </div>
    );
  }

  if (!data) {
    return <div className="min-h-screen flex items-center justify-center"><p className="text-gray-500">확인 중...</p></div>;
  }

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="text-center">
        <p className="text-lg font-semibold mb-2">공유 프로젝트</p>
        <p className="text-sm text-gray-500">권한: {data.permission === "edit" ? "편집" : "읽기 전용"}</p>
      </div>
    </div>
  );
}
