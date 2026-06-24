"use client";

import dynamic from "next/dynamic";
import { useParams } from "next/navigation";
import { Skeleton } from "@/components/common/Skeleton";

const Stage1Page = dynamic(() => import("@/components/design/stages/Stage1Requirements"), { loading: () => <StageSkeleton /> });
const Stage2Page = dynamic(() => import("@/components/design/stages/Stage2Wireframe"), { loading: () => <StageSkeleton /> });
const Stage3Page = dynamic(() => import("@/components/design/stages/Stage3Design"), { loading: () => <StageSkeleton /> });
const Stage4Page = dynamic(() => import("@/components/design/stages/Stage4Handoff"), { loading: () => <StageSkeleton /> });

function StageSkeleton() {
  return <div className="space-y-4"><Skeleton className="h-64 w-full" /><Skeleton className="h-12 w-1/3" /></div>;
}

export default function StagePage() {
  const { stageId } = useParams<{ stageId: string }>();

  switch (stageId) {
    case "1": return <Stage1Page />;
    case "2": return <Stage2Page />;
    case "3": return <Stage3Page />;
    case "4": return <Stage4Page />;
    default: return <p>Invalid stage</p>;
  }
}
