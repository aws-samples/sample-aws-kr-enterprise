/**
 * Main entry point — bootstraps polling and UI interactions.
 */

import { fetchLeaderboard } from "./api";
import { initControls } from "./controls";
import { initDashboard } from "./dashboard";
import { renderLeaderboard } from "./leaderboard";

const gameSelect = document.getElementById("game-select") as HTMLSelectElement;
const pollStatus = document.getElementById("poll-status") as HTMLSpanElement;

let pollInterval: ReturnType<typeof setInterval> | null = null;

function getSelectedGame(): string {
  return gameSelect.value;
}

async function poll(): Promise<void> {
  try {
    pollStatus.classList.add("active");
    const data = await fetchLeaderboard(getSelectedGame(), 100);
    renderLeaderboard(data.top);
    pollStatus.classList.remove("error");
  } catch {
    pollStatus.classList.add("error");
  } finally {
    setTimeout(() => pollStatus.classList.remove("active"), 200);
  }
}

function startPolling(): void {
  if (pollInterval) clearInterval(pollInterval);
  poll(); // immediate first call
  pollInterval = setInterval(poll, 1000);
}

function init(): void {
  initControls();
  initDashboard();

  // Start polling for the default game
  startPolling();

  // Restart polling when game selection changes
  gameSelect.addEventListener("change", () => {
    startPolling();
  });
}

// Boot
document.addEventListener("DOMContentLoaded", init);
