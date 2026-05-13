/**
 * Leaderboard table rendering.
 */

import type { LeaderboardEntry } from "./api";

const tbody = document.getElementById("leaderboard-body") as HTMLTableSectionElement;

export function renderLeaderboard(entries: LeaderboardEntry[]): void {
  if (entries.length === 0) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty-state">No data yet. Start a load test!</td></tr>`;
    return;
  }

  const fragment = document.createDocumentFragment();

  for (const entry of entries) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td class="col-rank">${entry.rank}</td>
      <td class="col-user">${escapeHtml(entry.userId)}</td>
      <td class="col-score">${formatScore(entry.score)}</td>
    `;
    fragment.appendChild(row);
  }

  tbody.innerHTML = "";
  tbody.appendChild(fragment);
}

function escapeHtml(str: string): string {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function formatScore(score: number): string {
  return Math.floor(score).toLocaleString();
}
