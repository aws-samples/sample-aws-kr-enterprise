import type { LucideIcon } from 'lucide-react';

interface StatsCardProps {
  label: string;
  value: string | number;
  icon: LucideIcon;
  color?: string;
  href?: string;
}

export default function StatsCard({
  label,
  value,
  icon: Icon,
  color = 'var(--purple)',
  href,
}: StatsCardProps) {
  const handleClick = () => {
    if (href) {
      document.querySelector(href)?.scrollIntoView({ behavior: 'smooth' });
    }
  };

  return (
    <div
      onClick={handleClick}
      className={`bg-[var(--surface)] rounded-xl p-5 border border-[var(--border)] shadow-[0_2px_12px_rgba(0,0,0,0.3)] hover:shadow-[0_8px_32px_rgba(139,92,246,0.15)] hover:border-[var(--purple)] transition-all duration-200 ${href ? 'cursor-pointer' : ''}`}
    >
      <div className="flex items-center justify-between">
        <div>
          <p className="text-[var(--text-dim)] text-sm">{label}</p>
          <p className="text-2xl font-bold text-white mt-1">{value}</p>
        </div>
        <Icon size={28} style={{ color }} />
      </div>
    </div>
  );
}
