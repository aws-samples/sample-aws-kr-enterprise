'use client';

import './globals.css';
import { useState } from 'react';
import Sidebar from '@/components/common/Sidebar';
import AuthGate from '@/components/auth/AuthGate';

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <html lang="ko">
      <head>
        <title>AIOps Multi Agent Platform</title>
        <meta name="description" content="Bedrock AgentCore 기반 AIOps Multi Agent Platform v2" />
      </head>
      <body>
        <AuthGate>
          <div className="flex">
            <Sidebar collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)} />
            <main className="flex-1 min-h-screen">{children}</main>
          </div>
        </AuthGate>
      </body>
    </html>
  );
}
