import { clsx } from "clsx";

interface SkeletonProps {
  className?: string;
  variant?: "text" | "rect" | "circle";
  width?: string;
  height?: string;
}

export function Skeleton({ className, variant = "rect", width, height }: SkeletonProps) {
  return (
    <div
      className={clsx(
        "animate-pulse bg-gray-200",
        {
          "rounded": variant === "text",
          "rounded-mdesigner": variant === "rect",
          "rounded-full": variant === "circle",
        },
        className,
      )}
      style={{ width, height: height || (variant === "text" ? "1em" : undefined) }}
    />
  );
}
