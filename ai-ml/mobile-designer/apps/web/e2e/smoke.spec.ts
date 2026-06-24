import { test, expect } from "@playwright/test";

test("/ redirects to /login", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);
});

test("login page renders form", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByTestId("login-form")).toBeVisible();
  await expect(page.getByTestId("login-email")).toBeVisible();
  await expect(page.getByTestId("login-password")).toBeVisible();
  await expect(page.getByTestId("login-submit")).toBeVisible();
});

test("register page renders form", async ({ page }) => {
  await page.goto("/register");
  await expect(page.getByTestId("register-form")).toBeVisible();
  await expect(page.getByTestId("register-name")).toBeVisible();
  await expect(page.getByTestId("register-email")).toBeVisible();
  await expect(page.getByTestId("register-password")).toBeVisible();
});

test("dashboard redirects to login when not authenticated", async ({ page }) => {
  await page.goto("/dashboard");
  await page.waitForURL(/\/login/, { timeout: 5000 });
  await expect(page).toHaveURL(/\/login/);
});
