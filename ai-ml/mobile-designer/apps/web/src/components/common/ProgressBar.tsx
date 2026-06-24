import { clsx } from "clsx";

interface ProgressBarProps {
  percent: number;
  label?: string;
  className?: string;
}

export function ProgressBar({ percent, label, className }: ProgressBarProps) {
  return (
    <div className={clsx("w-full", className)}>
      {label && <p className="text-xs text-gray-500 mb-1">{label}</p>}
      <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
        <div
          className="h-full bg-primary rounded-full transition-all duration-300"
          style={{ width: `${Math.min(100, Math.max(0, percent))}%` }}
          data-testid="progress-bar-fill"
        />
      </div>
    </div>
  );
}
