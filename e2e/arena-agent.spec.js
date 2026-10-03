import { test, expect } from '@playwright/test';
import path from 'path';
import fs from 'fs';

const EXTENSION_PATH = path.join(process.cwd(), 'zeroscript-extension');
const AUTH_FILE = path.join(process.cwd(), 'e2e/.auth.json');

// Fixture that loads the extension
test.use({
  contextOptions: {
    // Load the unpacked extension
    args: [
      `--disable-extensions-except=${EXTENSION_PATH}`,
      `--load-extension=${EXTENSION_PATH}`,
    ],
    // Use persistent context with storage state if available
    storageState: fs.existsSync(AUTH_FILE) ? AUTH_FILE : undefined,
  },
});

test.describe('Arena Agent ZeroScript provider', () => {
  test('Extension should load and provider should be available', async ({ page }) => {
    await page.goto('https://arena.ai/agent');
    await page.waitForLoadState('networkidle');
    
    // Check if the extension's root element exists
    const zsRoot = await page.locator('#zs-root').count();
    expect(zsRoot).toBe(1);
    
    // Check if the bar exists
    const zsBar = await page.locator('#zs-bar').count();
    expect(zsBar).toBe(1);
  });

  test('Provider should detect Arena Agent page', async ({ page }) => {
    await page.goto('https://arena.ai/agent');
    await page.waitForLoadState('networkidle');
    
    // Check for the provider-specific indicator
    // The provider adds a debug log, but we can check if it's active
    const providerActive = await page.evaluate(() => {
      // Check if the provider is loaded in the page context
      return typeof window.ZSProvider !== 'undefined';
    });
    expect(providerActive).toBe(true);
  });

  test('Should detect signed-out state and skip gracefully', async ({ page }) => {
    await page.goto('https://arena.ai/agent');
    await page.waitForLoadState('networkidle');
    
    // Check if we're signed out
    const signedOut = await page.locator('text=Sign in').count() > 0;
    if (signedOut) {
      test.skip('arena.ai requires login — skipping test');
    }
  });

  test('Provider should find turns when messages are sent', async ({ page }) => {
    await page.goto('https://arena.ai/agent');
    await page.waitForLoadState('networkidle');
    
    // Check if we're signed in
    const signedOut = await page.locator('text=Sign in').count() > 0;
    if (signedOut) {
      test.skip('arena.ai requires login — skipping test');
    }
    
    // Wait for the page to be ready
    await page.waitForSelector('[contenteditable="true"]', { timeout: 10000 });
    
    // Type a message
    const editor = page.locator('[contenteditable="true"]');
    await editor.click();
    await editor.fill('Say hello');
    
    // Send the message (enter key)
    await editor.press('Enter');
    
    // Wait for a response
    await page.waitForTimeout(5000);
    
    // Check if the provider detected the turn
    const turnCount = await page.evaluate(() => {
      if (typeof ZSProvider !== 'undefined') {
        return ZSProvider.assistantCount();
      }
      return -1;
    });
    
    // Should have at least one assistant turn
    expect(turnCount).toBeGreaterThanOrEqual(1);
  });
});
