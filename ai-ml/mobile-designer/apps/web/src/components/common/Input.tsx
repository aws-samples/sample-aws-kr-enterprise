"use client";

import { clsx } from "clsx";
import type { InputHTMLAttributes } from "react";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  helper?: string;
  "data-testid"?: string;
}

export function Input({ label, error, helper, className, id, "data-testid": dataTestId, ...props }: InputProps) {
  const inputId = id || label?.toLowerCase().replace(/\s/g, "-");
  const testId = dataTestId || `input-${inputId}`;
  return (
    <div className="space-y-1">
      {label && <label htmlFor={inputId} className="block text-sm font-medium text-gray-700">{label}</label>}
      <input
        id={inputId}
        className={clsx(
          "w-full px-3 py-2 border rounded-mdesigner text-sm transition-colors",
          "focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary",
          error ? "border-error" : "border-gray-300",
          className,
        )}
        data-testid={testId}
        {...props}
      />
      {error && <p className="text-xs text-error">{error}</p>}
      {helper && !error && <p className="text-xs text-gray-500">{helper}</p>}
    </div>
  );
}
