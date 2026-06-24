import { test, expect } from "@playwright/test";

const UNIQUE = Date.now();

test.describe("Auth Flow", () => {
  test("register new user and redirect to dashboard", async ({ page }) => {
    await page.goto("/register");
    await page.getByTestId("register-name").fill("E2E User");
    await page.getByTestId("register-email").fill(`e2e-${UNIQUE}@test.com`);
    await page.getByTestId("register-password").fill("SecurePass123");
    await page.getByTestId("register-submit").click();

    await page.waitForURL(/\/dashboard/, { timeout: 10000 });
    await expect(page).toHaveURL(/\/dashboard/);
  });

  test("login with existing user", async ({ page }) => {
    await page.goto("/login");
    await page.getByTestId("login-email").fill("e2etest@example.com");
    await page.getByTestId("login-password").fill("TestPass123");
    await page.getByTestId("login-submit").click();

    await page.waitForURL(/\/dashboard/, { timeout: 10000 });
    await expect(page).toHaveURL(/\/dashboard/);
  });

  test("login with wrong password shows error", async ({ page }) => {
    await page.goto("/login");
    await page.getByTestId("login-email").fill("e2etest@example.com");
    await page.getByTestId("login-password").fill("WrongPassword");
    await page.getByTestId("login-submit").click();

    // Should stay on login page and show a toast/error
    await page.waitForTimeout(2000);
    await expect(page).toHaveURL(/\/login/);
  });

  test("authenticated user sees dashboard with projects", async ({ page }) => {
    // Login first
    await page.goto("/login");
    await page.getByTestId("login-email").fill("e2etest@example.com");
    await page.getByTestId("login-password").fill("TestPass123");
    await page.getByTestId("login-submit").click();
    await page.waitForURL(/\/dashboard/, { timeout: 10000 });

    // Dashboard should show create button
    await expect(page.getByTestId("create-project-btn")).toBeVisible();
  });
});
