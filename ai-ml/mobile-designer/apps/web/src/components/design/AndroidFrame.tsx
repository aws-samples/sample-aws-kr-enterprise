"use client";

import { clsx } from "clsx";
import type { ReactNode } from "react";

interface AndroidFrameProps {
  children: ReactNode;
  rotation?: "portrait" | "landscape";
  darkMode?: boolean;
}

export function AndroidFrame({ children, rotation = "portrait", darkMode = false }: AndroidFrameProps) {
  const isLandscape = rotation === "landscape";
  // Frame is rendered at 1:1 dp scale (360x780 = typical phone viewport)
  // Then scaled down with CSS transform to fit the web layout
  const scale = isLandscape ? 0.65 : 0.75;

  return (
    <div
      style={{
        width: isLandscape ? `${700 * scale}px` : `${386 * scale}px`,
        height: isLandscape ? `${386 * scale}px` : `${806 * scale}px`,
      }}
    >
      <div
        className={clsx(
          "relative bg-black rounded-[3rem] p-3 shadow-2xl origin-top-left",
          isLandscape ? "w-[700px] h-[360px]" : "w-[360px] h-[780px]",
        )}
        style={{ transform: `scale(${scale})` }}
        data-testid="android-frame"
      >
        <div className={clsx("w-full h-full rounded-[2rem] overflow-hidden", darkMode ? "bg-[#121212]" : "bg-white")}>
          <div className="h-6 bg-gray-900 flex items-center justify-center">
            <div className="w-16 h-1 bg-gray-700 rounded-full" />
          </div>
          <div className="h-[calc(100%-1.5rem)] overflow-y-auto relative">
            {children}
          </div>
        </div>
      </div>
    </div>
  );
}
