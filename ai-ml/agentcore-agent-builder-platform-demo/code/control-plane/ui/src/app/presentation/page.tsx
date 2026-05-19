'use client';

import Header from '@/components/common/Header';

export default function PresentationPage() {
  return (
    <div className="flex flex-col h-full">
      <Header title="Platform Presentation" />
      <iframe
        src="/platform-presentation.html"
        className="flex-1 w-full border-0"
        title="AIOps Platform Presentation"
      />
    </div>
  );
}
