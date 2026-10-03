import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

// Simple test that verifies the provider file exists and has the required content
test('Provider file exists and has required content', () => {
  const filePath = path.join(process.cwd(), 'zeroscript-extension/providers/arena-agent.js');
  expect(fs.existsSync(filePath)).toBe(true);
  const content = fs.readFileSync(filePath, 'utf-8');
  
  // Check for key provider identifiers
  expect(content).toContain('ZSProvider');
  expect(content).toContain('arena-agent');
  expect(content).toContain('typeAndSend');
  expect(content).toContain('isGenerating');
  expect(content).toContain('id: "arena-agent"');
  expect(content).toContain('displayName: "Arena Agent"');
});

test('Provider has required methods', () => {
  const filePath = path.join(process.cwd(), 'zeroscript-extension/providers/arena-agent.js');
  const content = fs.readFileSync(filePath, 'utf-8');
  
  const requiredMethods = [
    'allItems', 'isUserItem', 'isAssistantItem', 'itemText',
    'assistantCount', 'userCount', 'lastAssistant', 'readAssistant',
    'getEditor', 'editorText', 'chatIsEmpty', 'typeAndSend', 'stopGeneration',
    'isGenerating', 'isBusyNow', 'scanError', 'snapshot', 'conversationKey'
  ];
  
  for (const method of requiredMethods) {
    expect(content).toContain(method);
  }
});

test('Provider has correct TURN_INNER selector', () => {
  const filePath = path.join(process.cwd(), 'zeroscript-extension/providers/arena-agent.js');
  const content = fs.readFileSync(filePath, 'utf-8');
  expect(content).toContain('div.flex.flex-col.gap-2');
});
