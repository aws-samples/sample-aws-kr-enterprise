/**
 * Load generator control buttons and status.
 */

import { startLoad } from "./api";

const statusText = document.getElementById("load-status-text") as HTMLSpanElement;
const buttons = document.querySelectorAll<HTMLButtonElement>(".load-btn");

let isRunning = false;

export function initControls(): void {
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => handleClick(btn));
  });
}

async function handleClick(btn: HTMLButtonElement): Promise<void> {
  if (isRunning) return;

  const pattern = btn.dataset.pattern;
  if (!pattern) return;

  setRunning(true, `Starting ${pattern}...`);

  try {
    const result = await startLoad({ pattern });
    setRunning(
      true,
      `Running: ${result.pattern} (${result.config.tps} TPS x ${result.config.duration_sec}s)`
    );

    // Auto-reset status after duration + buffer
    const duration = result.config.duration_sec * 1000 + 5000;
    setTimeout(() => {
      setRunning(false, "Idle");
    }, duration);
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    setRunning(false, `Error: ${message}`);
  }
}

function setRunning(running: boolean, message: string): void {
  isRunning = running;
  statusText.textContent = message;
  statusText.className = running ? "status-running" : "";

  buttons.forEach((btn) => {
    btn.disabled = running;
  });
}
