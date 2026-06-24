import type { Metadata } from "next";
import { Providers } from "@/lib/contexts/Providers";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mobile Designer",
  description: "자연어로 모바일 앱 UI를 디자인하고 Android 프로젝트를 핸드오프하세요",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <head>
        <link href="https://fonts.googleapis.com/icon?family=Material+Icons+Outlined" rel="stylesheet" />
      </head>
      <body>
        <ErrorBoundary>
          <Providers>{children}</Providers>
        </ErrorBoundary>
      </body>
    </html>
  );
}
