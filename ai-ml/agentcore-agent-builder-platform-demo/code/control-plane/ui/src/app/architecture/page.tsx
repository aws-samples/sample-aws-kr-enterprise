'use client';

import Header from '@/components/common/Header';

export default function ArchitecturePage() {
  return (
    <div className="flex flex-col h-full">
      <Header title="Architecture" />
      <iframe
        src="/architecture-diagram.html"
        className="flex-1 w-full border-0"
        title="AIOps Platform Architecture"
      />
    </div>
  );
}
