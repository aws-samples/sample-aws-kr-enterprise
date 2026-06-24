import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center min-h-screen">
      <h1 className="text-4xl font-bold mb-2">404</h1>
      <p className="text-gray-500 mb-6">페이지를 찾을 수 없습니다</p>
      <Link href="/dashboard" className="text-primary hover:underline">
        대시보드로 돌아가기
      </Link>
    </div>
  );
}
