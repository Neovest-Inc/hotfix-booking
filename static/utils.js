/**
 * Shared Utility Functions
 * 
 * Common utilities used across multiple tabs/features.
 */

/**
 * Escape HTML characters to prevent XSS
 * @param {string} text - Text to escape
 * @returns {string} Escaped text
 */
function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Get CSS class for status badges
 * @param {string} status - Jira status name
 * @returns {string} CSS class name
 */
function getStatusClass(status) {
  if (!status) return '';
  const statusLower = status.toLowerCase();
  if (statusLower === 'done' || statusLower === 'ready' || statusLower === 'partial release') {
    return 'status-done';
  }
  if (statusLower === 'in progress' || statusLower === 'in review') {
    return 'status-in-progress';
  }
  return 'status-default';
}

/**
 * Build heatmap data from stories
 * @param {Array} stories - Array of story objects with securityTypes and clientEnvironments
 * @returns {Object} { matrix, clients, securityTypes }
 */
function buildHeatmapData(stories) {
  const matrix = {}; // { securityType: { client: count } }
  const allClients = new Set();
  const allSecurityTypes = new Set();

  for (const story of stories) {
    const secTypes = story.securityTypes || [];
    const clients = story.clientEnvironments || [];

    // Only include stories that have BOTH security types AND clients
    if (secTypes.length === 0 || clients.length === 0) continue;

    for (const secType of secTypes) {
      if (!matrix[secType]) {
        matrix[secType] = {};
      }
      allSecurityTypes.add(secType);

      for (const client of clients) {
        allClients.add(client);
        matrix[secType][client] = (matrix[secType][client] || 0) + 1;
      }
    }
  }

  return {
    matrix,
    clients: Array.from(allClients).sort(),
    securityTypes: Array.from(allSecurityTypes).sort()
  };
}

/**
 * Render a heatmap from data
 * @param {Object} data - Heatmap data from buildHeatmapData
 * @param {Array} selectedCells - Array of selected {securityType, client} pairs
 * @returns {string} HTML string for the heatmap
 */
function renderHeatmap(data, selectedCells = []) {
  const { matrix, clients, securityTypes } = data;

  if (clients.length === 0 || securityTypes.length === 0) {
    return `
      <div class="heatmap-container">
        <div class="heatmap-header">
          <h3><span class="material-icons">grid_on</span> Heatmap: Security Types vs Clients</h3>
        </div>
        <div class="heatmap-empty">
          <span class="material-icons">info</span>
          <p>No stories with both Security Types and Clients to display.</p>
        </div>
      </div>
    `;
  }

  // Find max value for color scaling
  let maxCount = 0;
  for (const secType of securityTypes) {
    for (const client of clients) {
      const count = matrix[secType]?.[client] || 0;
      if (count > maxCount) maxCount = count;
    }
  }

  const getHeatColor = (count) => {
    if (count === 0) return '#f8f9fa';
    const intensity = count / maxCount;
    // Gradient from light yellow to orange to red
    if (intensity <= 0.33) {
      return `rgba(255, 235, 59, ${0.3 + intensity * 0.7})`; // Yellow
    } else if (intensity <= 0.66) {
      return `rgba(255, 152, 0, ${0.5 + (intensity - 0.33) * 0.5})`; // Orange
    } else {
      return `rgba(244, 67, 54, ${0.6 + (intensity - 0.66) * 0.4})`; // Red
    }
  };

  // Helper to check if a cell is selected
  const isCellSelected = (secType, client) => {
    return selectedCells.some(c => c.securityType === secType && c.client === client);
  };

  const headerCells = clients.map(client => 
    `<th class="heatmap-client-header">${escapeHtml(client)}</th>`
  ).join('');

  const rows = securityTypes.map(secType => {
    // Calculate row total
    let rowTotal = 0;
    for (const client of clients) {
      rowTotal += matrix[secType]?.[client] || 0;
    }

    const cells = clients.map(client => {
      const count = matrix[secType]?.[client] || 0;
      const bgColor = getHeatColor(count);
      const textColor = count > 0 ? '#202124' : '#9aa0a6';
      const isSelected = isCellSelected(secType, client);
      const cellClass = `heatmap-cell${count > 0 ? ' clickable' : ''}${isSelected ? ' selected' : ''}`;
      return `<td class="${cellClass}" data-sectype="${escapeHtml(secType)}" data-client="${escapeHtml(client)}" data-count="${count}" style="background-color: ${bgColor}; color: ${textColor};">${count}</td>`;
    }).join('');

    // Total cell - uses __ALL__ as special client marker
    const totalBgColor = getHeatColor(rowTotal);
    const totalTextColor = rowTotal > 0 ? '#202124' : '#9aa0a6';
    const isTotalSelected = isCellSelected(secType, '__ALL__');
    const totalCellClass = `heatmap-cell heatmap-total-cell${rowTotal > 0 ? ' clickable' : ''}${isTotalSelected ? ' selected' : ''}`;
    const totalCell = `<td class="${totalCellClass}" data-sectype="${escapeHtml(secType)}" data-client="__ALL__" data-count="${rowTotal}" style="background-color: ${totalBgColor}; color: ${totalTextColor}; font-weight: 600;">${rowTotal}</td>`;

    return `
      <tr>
        <th class="heatmap-sectype-header">${escapeHtml(secType)}</th>
        ${cells}
        ${totalCell}
      </tr>
    `;
  }).join('');

  return `
    <div class="heatmap-container">
      <div class="heatmap-header">
        <h3><span class="material-icons">grid_on</span> Heatmap: Security Types vs Clients</h3>
      </div>
      <div class="heatmap-wrapper">
        <table class="heatmap-table">
          <thead>
            <tr>
              <th class="heatmap-corner"></th>
              ${headerCells}
              <th class="heatmap-total-header">Total</th>
            </tr>
          </thead>
          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
      <div class="heatmap-legend">
        <span class="legend-label">Less</span>
        <div class="legend-gradient"></div>
        <span class="legend-label">More</span>
      </div>
    </div>
  `;
}

/**
 * Toast Notification System
 * Global toast container reference
 */
let toastContainer = null;

/**
 * Show a toast notification
 * @param {string} message - Message to display
 * @param {string} type - Type: 'success', 'error', 'warning', 'info'
 */
function showToast(message, type = 'info') {
  // Create container if it doesn't exist
  if (!toastContainer) {
    toastContainer = document.createElement('div');
    toastContainer.className = 'hb-toast-container';
    document.body.appendChild(toastContainer);
  }

  // Create toast element
  const toast = document.createElement('div');
  toast.className = `hb-toast hb-toast-${type}`;
  
  const icon = type === 'success' ? 'check_circle' 
             : type === 'error' ? 'error' 
             : type === 'warning' ? 'warning' 
             : 'info';
  
  toast.innerHTML = `
    <span class="material-icons hb-toast-icon">${icon}</span>
    <span class="hb-toast-message">${escapeHtml(message)}</span>
    <button class="hb-toast-close">
      <span class="material-icons">close</span>
    </button>
  `;

  // Add close button handler
  toast.querySelector('.hb-toast-close').addEventListener('click', () => {
    dismissToast(toast);
  });

  // Add to container
  toastContainer.appendChild(toast);

  // Trigger animation
  requestAnimationFrame(() => {
    toast.classList.add('show');
  });

  // Auto-dismiss after 4 seconds
  setTimeout(() => {
    dismissToast(toast);
  }, 4000);
}

/**
 * Dismiss a toast notification
 * @param {HTMLElement} toast - Toast element to dismiss
 */
function dismissToast(toast) {
  if (!toast || !toast.parentNode) return;
  toast.classList.remove('show');
  toast.classList.add('hide');
  setTimeout(() => {
    if (toast.parentNode) {
      toast.parentNode.removeChild(toast);
    }
  }, 300);
}

// Export functions for use in other modules
window.Utils = {
  escapeHtml,
  getStatusClass,
  buildHeatmapData,
  renderHeatmap,
  showToast,
  dismissToast
};
