import { test, expect } from "@playwright/test";

test.describe("Project Flow", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/login");
    await page.getByTestId("login-email").fill("e2etest@example.com");
    await page.getByTestId("login-password").fill("TestPass123");
    await page.getByTestId("login-submit").click();
    await page.waitForURL(/\/dashboard/, { timeout: 10000 });
  });

  test("create a new project", async ({ page }) => {
    await page.getByTestId("create-project-btn").click();
    await expect(page.getByTestId("modal-overlay")).toBeVisible();
    await page.getByTestId("create-project-name").fill("E2E 테스트 앱");
    await page.getByTestId("create-project-submit").click();

    // Modal should close and project card should appear
    await page.waitForTimeout(2000);
    await expect(page.getByText("E2E 테스트 앱").first()).toBeVisible();
  });

  test("navigate to project stage view", async ({ page }) => {
    // Click on the first project card
    const firstCard = page.locator("[data-testid^='project-card-']").first();
    if (await firstCard.isVisible()) {
      await firstCard.click();
      await page.waitForURL(/\/project\/.*\/stage/, { timeout: 10000 });
      await expect(page.url()).toContain("/stage/");
    }
  });
});
