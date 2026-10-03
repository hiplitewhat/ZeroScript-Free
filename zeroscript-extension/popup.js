// popup.js - ZeroScript Free popup UI
document.addEventListener('DOMContentLoaded', () => {
  const statusText = document.getElementById('statusText');
  const providerInfo = document.getElementById('providerInfo');
  const providerSelect = document.getElementById('providerSelect');
  const refreshBtn = document.getElementById('refreshBtn');
  const reloadBtn = document.getElementById('reloadBtn');

  // Get the current tab and provider info
  async function getCurrentTab() {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    return tabs[0];
  }

  async function getProviderStatus() {
    const tab = await getCurrentTab();
    if (!tab) return;
    
    // Send message to content script to get provider info
    try {
      const response = await chrome.tabs.sendMessage(tab.id, { type: 'getProviderInfo' });
      if (response) {
        updateUI(response);
      }
    } catch (e) {
      // Content script not available
      providerInfo.textContent = '⚠️ Extension not active on this page';
      statusText.textContent = '● Inactive';
      statusText.className = 'value offline';
    }
  }

  function updateUI(info) {
    if (info.provider) {
      providerInfo.textContent = `Provider: ${info.provider.displayName || info.provider.id}`;
      statusText.textContent = '● Connected';
      statusText.className = 'value';
    } else {
      providerInfo.textContent = 'No provider detected';
      statusText.textContent = '● Waiting';
      statusText.className = 'value offline';
    }
  }

  // Handle provider selection
  providerSelect.addEventListener('change', async () => {
    const selected = providerSelect.value;
    const tab = await getCurrentTab();
    if (!tab) return;
    
    try {
      await chrome.tabs.sendMessage(tab.id, {
        type: 'setProvider',
        provider: selected
      });
    } catch (e) {
      console.error('Failed to set provider:', e);
    }
  });

  // Refresh button
  refreshBtn.addEventListener('click', () => {
    getProviderStatus();
    // Also try to detect providers
    getProviders();
  });

  // Reload extension button
  reloadBtn.addEventListener('click', () => {
    chrome.runtime.reload();
    // Close popup after reload
    window.close();
  });

  // Get list of available providers
  async function getProviders() {
    // This would need to be implemented in the content script
    // For now, just show the current status
    await getProviderStatus();
  }

  // Initial load
  getProviders();

  // Listen for messages from content script
  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === 'providerStatus') {
      updateUI(message.info);
    }
  });
});
