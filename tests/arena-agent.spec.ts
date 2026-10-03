import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

test('Provider file exists and has correct content', async () => {
  const filePath = path.join(process.cwd(), 'zeroscript-extension/providers/arena-agent.js');
  expect(fs.existsSync(filePath)).toBe(true);
  const content = fs.readFileSync(filePath, 'utf-8');
  expect(content).toContain('ZSProvider');
  expect(content).toContain('arena-agent');
});

test('Provider has required methods', async () => {
  const filePath = path.join(process.cwd(), 'zeroscript-extension/providers/arena-agent.js');
  const content = fs.readFileSync(filePath, 'utf-8');
  const requiredMethods = ['allItems', 'isUserItem', 'isAssistantItem', 'assistantCount', 'userCount', 'lastAssistant', 'readAssistant'];
  for (const method of requiredMethods) {
    expect(content).toContain(method);
  }
});
test('Provider has correct TURN_INNER selector', async () => {
  const filePath = path.join(process.cwd(), 'zeroscript-extension/providers/arena-agent.js');
  const content = fs.readFileSync(filePath, 'utf-8');
  expect(content).toContain('div.flex.flex-col.gap-2');
});
