import { test as setup, expect } from '@playwright/test';
import path from 'path';
import fs from 'fs';

const AUTH_FILE = path.join(process.cwd(), 'e2e/.auth.json');

// This test sets up authentication for arena.ai
// Run with: npx playwright test e2e/login-setup.spec.js
setup('authenticate with arena.ai', async ({ page }) => {
  // Navigate to arena.ai
  await page.goto('https://arena.ai');
  await page.waitForLoadState('networkidle');
  
  // Check if we're already logged in
  const isLoggedIn = await page.locator('text=Sign out').count() > 0;
  if (isLoggedIn) {
    // Save the storage state
    await page.context().storageState({ path: AUTH_FILE });
    return;
  }
  
  // Click the sign in button
  const signInBtn = page.locator('text=Sign in, text=Login, button:has-text("Sign in")').first();
  await signInBtn.click();
  
  // Wait for the login flow
  await page.waitForTimeout(2000);
  
  // Handle the login flow (this depends on the provider)
  // For Google login, you'd need to fill in credentials
  // For email/password, fill in the form
  
  // Check if login was successful
  await page.waitForTimeout(3000);
  
  // Save the storage state
  await page.context().storageState({ path: AUTH_FILE });
});
