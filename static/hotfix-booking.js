/**
 * Hotfix Booking Module
 * 
 * Handles hotfix version booking and version matrix display.
 */
(function() {
  // State
  let initialized = false;
  let fieldOptionsLoaded = false;
  let nextVersion = null;
  let selectedMinor = null;           // null = server default (current release)
  let bookMinorOptionsLoaded = false; // populate the release <select> only once
  let selectedComponents = [];
  let selectedClients = [];
  let availableComponents = [];
  let availableClients = [];
  let jiraBaseUrl = '';

  // Auto-refresh state (Book Hotfix view only, while tab visible)
  const AUTO_REFRESH_MS = 30000;
  let autoRefreshTimer = null;

  // All user-facing timestamps are shown in Eastern Time (ET).
  const DISPLAY_TZ = 'America/New_York';

  // DOM Elements
  let pillBtns;
  let bookView;
  let matrixView;
  let historyView;
  let loadingEl;
  let matrixLoadingEl;
  let historyLoadingEl;
  let nextVersionEl;
  let componentToggle;
  let componentDropdown;
  let clientToggle;
  let clientDropdown;
  let bookBtn;
  let bookingsListEl;
  let bookMinorSelect;
  let matrixTableEl;
  let refreshMatrixBtn;
  let historyTableEl;
  let minorVersionSelect;
  let refreshHistoryBtn;

  // Pre-defined colors for components (consistent with CM table)
  const COMPONENT_COLORS = [
    { bg: '#e8f0fe', text: '#1967d2', border: '#d2e3fc' },  // Blue
    { bg: '#fce8e6', text: '#c5221f', border: '#f5c6cb' },  // Red
    { bg: '#e6f4ea', text: '#1e8e3e', border: '#c6e6cf' },  // Green
    { bg: '#fef7e0', text: '#e37400', border: '#fde69e' },  // Orange
    { bg: '#f3e8fd', text: '#8430ce', border: '#e5cffa' },  // Purple
    { bg: '#e0f7fa', text: '#00838f', border: '#b2ebf2' },  // Cyan
    { bg: '#fce4ec', text: '#c2185b', border: '#f8bbd9' },  // Pink
    { bg: '#e8eaf6', text: '#3f51b5', border: '#c5cae9' },  // Indigo
    { bg: '#fff3e0', text: '#e65100', border: '#ffccbc' },  // Deep Orange
    { bg: '#e0f2f1', text: '#00695c', border: '#b2dfdb' },  // Teal
  ];

  // Cache for component-to-color mapping
  const componentColorCache = {};
  let colorIndex = 0;

  /**
   * Initialize the module
   */
  function init() {
    if (initialized) return;

    // Get DOM elements
    pillBtns = document.querySelectorAll('.hb-pill-toggle .pill-btn');
    bookView = document.getElementById('hbBookView');
    matrixView = document.getElementById('hbMatrixView');
    historyView = document.getElementById('hbHistoryView');
    loadingEl = document.getElementById('hbLoading');
    matrixLoadingEl = document.getElementById('hbMatrixLoading');
    historyLoadingEl = document.getElementById('hbHistoryLoading');
    nextVersionEl = document.getElementById('hbNextVersion');
    componentToggle = document.getElementById('hbComponentToggle');
    componentDropdown = document.getElementById('hbComponentDropdown');
    clientToggle = document.getElementById('hbClientToggle');
    clientDropdown = document.getElementById('hbClientDropdown');
    bookBtn = document.getElementById('hbBookBtn');
    bookingsListEl = document.getElementById('hbBookingsList');
    bookMinorSelect = document.getElementById('hbBookMinorSelect');
    matrixTableEl = document.getElementById('hbMatrixTable');
    refreshMatrixBtn = document.getElementById('hbRefreshMatrix');
    historyTableEl = document.getElementById('hbHistoryTable');
    minorVersionSelect = document.getElementById('hbMinorVersionSelect');
    refreshHistoryBtn = document.getElementById('hbRefreshHistory');

    if (!pillBtns.length) return;

    // Pill toggle event listeners
    pillBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        pillBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        const view = btn.dataset.view;
        bookView.style.display = view === 'book' ? 'block' : 'none';
        matrixView.style.display = view === 'matrix' ? 'block' : 'none';
        historyView.style.display = view === 'history' ? 'block' : 'none';

        if (view === 'book') {
          startBookAutoRefresh();
        } else {
          stopBookAutoRefresh();
        }
        if (view === 'matrix') {
          loadVersionMatrix();
        } else if (view === 'history') {
          loadHotfixHistory();
        }
      });
    });

    // Pause auto-refresh when the browser tab is hidden, resume when visible again
    // (only if we're currently on the Book view).
    document.addEventListener('visibilitychange', () => {
      const bookVisible = bookView && bookView.style.display !== 'none';
      if (document.hidden) {
        stopBookAutoRefresh();
      } else if (bookVisible) {
        startBookAutoRefresh();
      }
    });

    // Multi-select dropdowns
    if (componentToggle) {
      componentToggle.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleDropdown('component');
      });
    }

    if (clientToggle) {
      clientToggle.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleDropdown('client');
      });
    }

    // Close dropdowns on outside click
    document.addEventListener('click', (e) => {
      closeAllDropdowns();
      // Handle history table clicks (expand/collapse)
      if (historyTableEl && historyTableEl.contains(e.target)) {
        handleHistoryClicks(e);
      }
    });

    // Book button
    if (bookBtn) {
      bookBtn.addEventListener('click', bookHotfix);
    }

    // Refresh matrix button
    if (refreshMatrixBtn) {
      refreshMatrixBtn.addEventListener('click', loadVersionMatrix);
    }

    // Refresh history button
    if (refreshHistoryBtn) {
      refreshHistoryBtn.addEventListener('click', () => loadHotfixHistory());
    }

    // Minor version select change
    if (minorVersionSelect) {
      minorVersionSelect.addEventListener('change', () => {
        loadHotfixHistory(minorVersionSelect.value);
      });
    }

    // Book Hotfix: release-line selector (lets users book against previous minors)
    if (bookMinorSelect) {
      bookMinorSelect.addEventListener('change', () => {
        selectedMinor = bookMinorSelect.value ? parseInt(bookMinorSelect.value, 10) : null;
        loadNextVersion();
        loadBookings();
      });
    }

    initialized = true;
  }

  /**
   * Get a consistent color for a component
   */
  function getComponentColor(componentName) {
    if (!componentColorCache[componentName]) {
      componentColorCache[componentName] = COMPONENT_COLORS[colorIndex % COMPONENT_COLORS.length];
      colorIndex++;
    }
    return componentColorCache[componentName];
  }

  /**
   * Get CSS class for CM status badges (matching CM table)
   */
  function getStatusClass(status) {
    if (!status) return 'cm-status-default';
    const statusLower = status.toLowerCase();
    if (statusLower === 'done' || statusLower === 'deployment completed') {
      return 'cm-status-done';
    }
    if (statusLower === 'booked') {
      return 'cm-status-booked';
    }
    if (statusLower === 'cancelled' || statusLower === 'canceled') {
      return 'cm-status-cancelled';
    }
    return 'cm-status-default';
  }

  /**
   * Render a collapsible list of tags (matching CM table)
   */
  function renderCollapsibleList(items, type, maxVisible, rowId) {
    if (!items || items.length === 0) {
      return '-';
    }

    const visibleItems = items.slice(0, maxVisible);
    const hiddenItems = items.slice(maxVisible);
    const hasMore = hiddenItems.length > 0;

    let html = `<div class="collapsible-list" data-row="${rowId}" data-type="${type}">`;
    html += '<div class="collapsible-visible">';
    
    if (type === 'component') {
      html += visibleItems.map(comp => {
        const color = getComponentColor(comp);
        return `<span class="component-tag" style="background-color: ${color.bg}; color: ${color.text}; border-color: ${color.border};">${Utils.escapeHtml(comp)}</span>`;
      }).join('');
    } else {
      html += visibleItems.map(ce => `<span class="client-env-tag">${Utils.escapeHtml(ce)}</span>`).join('');
    }
    
    if (hasMore) {
      html += `<span class="expand-tags-btn" data-row="${rowId}" data-type="${type}">+${hiddenItems.length} more</span>`;
    }
    
    html += '</div>';
    
    if (hasMore) {
      html += '<div class="collapsible-hidden" style="display: none;">';
      if (type === 'component') {
        html += hiddenItems.map(comp => {
          const color = getComponentColor(comp);
          return `<span class="component-tag" style="background-color: ${color.bg}; color: ${color.text}; border-color: ${color.border};">${Utils.escapeHtml(comp)}</span>`;
        }).join('');
      } else {
        html += hiddenItems.map(ce => `<span class="client-env-tag">${Utils.escapeHtml(ce)}</span>`).join('');
      }
      html += `<span class="collapse-tags-btn" data-row="${rowId}" data-type="${type}">Show less</span>`;
      html += '</div>';
    }
    
    html += '</div>';
    return html;
  }

  /**
   * Handle clicks in history table for expand/collapse
   */
  function handleHistoryClicks(e) {
    // Handle expand tags click
    const expandBtn = e.target.closest('.expand-tags-btn');
    if (expandBtn) {
      const container = expandBtn.closest('.collapsible-list');
      if (container) {
        container.querySelector('.collapsible-visible').style.display = 'none';
        container.querySelector('.collapsible-hidden').style.display = 'flex';
      }
      return;
    }

    // Handle collapse tags click
    const collapseBtn = e.target.closest('.collapse-tags-btn');
    if (collapseBtn) {
      const container = collapseBtn.closest('.collapsible-list');
      if (container) {
        container.querySelector('.collapsible-visible').style.display = 'flex';
        container.querySelector('.collapsible-hidden').style.display = 'none';
      }
      return;
    }
  }

  /**
   * Called when tab is shown
   */
  function onTabShow() {
    if (!fieldOptionsLoaded) {
      loadFieldOptions();
      loadNextVersion();
      loadBookings();
    }
    // Book view is the default view — kick off auto-refresh.
    if (bookView && bookView.style.display !== 'none') {
      startBookAutoRefresh();
    }
  }

  /**
   * Periodically refresh next-version and recent bookings while the Book view
   * is on screen and the browser tab is visible. Prevents stale UI if someone
   * else books a version while this tab is idle.
   */
  function startBookAutoRefresh() {
    if (autoRefreshTimer) return;
    autoRefreshTimer = setInterval(() => {
      loadNextVersion();
      loadBookings();
    }, AUTO_REFRESH_MS);
  }

  function stopBookAutoRefresh() {
    if (autoRefreshTimer) {
      clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
    }
  }

  /**
   * Toggle dropdown visibility
   */
  function toggleDropdown(type) {
    closeAllDropdowns();
    const dropdown = type === 'component' ? componentDropdown : clientDropdown;
    dropdown.classList.toggle('open');
  }

  /**
   * Close all dropdowns
   */
  function closeAllDropdowns() {
    if (componentDropdown) componentDropdown.classList.remove('open');
    if (clientDropdown) clientDropdown.classList.remove('open');
  }

  /**
   * Load field options (components and clients) from API
   */
  async function loadFieldOptions() {
    showLoading(true);
    try {
      const response = await fetch('/api/hotfix-booking/field-options');
      const data = await response.json();

      if (data.error) {
        console.error('Field options error:', data.error);
        return;
      }

      availableComponents = data.components || [];
      availableClients = data.clients || [];

      renderComponentDropdown();
      renderClientDropdown();
      fieldOptionsLoaded = true;
    } catch (error) {
      console.error('Failed to load field options:', error);
    } finally {
      showLoading(false);
    }
  }

  /**
   * Render component multi-select dropdown
   */
  function renderComponentDropdown() {
    if (!componentDropdown) return;

    // Add search input at the top
    let html = `<div class="hb-dropdown-search">
      <input type="text" class="hb-search-input" placeholder="Search components..." data-target="component">
    </div>`;
    
    html += `<div class="hb-dropdown-items">`;
    html += availableComponents.map(comp => `
      <div class="hb-dropdown-item ${selectedComponents.includes(comp.name) ? 'selected' : ''}" 
           data-value="${Utils.escapeHtml(comp.name)}">
        <span class="hb-check-icon material-icons">${selectedComponents.includes(comp.name) ? 'check_box' : 'check_box_outline_blank'}</span>
        <span class="hb-item-label">${Utils.escapeHtml(comp.name)}</span>
      </div>
    `).join('');
    html += `</div>`;
    
    componentDropdown.innerHTML = html;

    // Add click listeners to items
    componentDropdown.querySelectorAll('.hb-dropdown-item').forEach(item => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        const value = item.dataset.value;
        toggleSelection('component', value);
      });
    });

    // Add search listener
    const searchInput = componentDropdown.querySelector('.hb-search-input');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        filterDropdownItems(componentDropdown, e.target.value);
      });
      searchInput.addEventListener('click', (e) => e.stopPropagation());
    }
  }

  /**
   * Render client multi-select dropdown
   */
  function renderClientDropdown() {
    if (!clientDropdown) return;

    // Add search input at the top
    let html = `<div class="hb-dropdown-search">
      <input type="text" class="hb-search-input" placeholder="Search clients..." data-target="client">
    </div>`;
    
    html += `<div class="hb-dropdown-items">`;
    html += availableClients.map(client => `
      <div class="hb-dropdown-item ${selectedClients.includes(client.value) ? 'selected' : ''}" 
           data-value="${Utils.escapeHtml(client.value)}">
        <span class="hb-check-icon material-icons">${selectedClients.includes(client.value) ? 'check_box' : 'check_box_outline_blank'}</span>
        <span class="hb-item-label">${Utils.escapeHtml(client.value)}</span>
      </div>
    `).join('');
    html += `</div>`;
    
    clientDropdown.innerHTML = html;

    // Add click listeners to items
    clientDropdown.querySelectorAll('.hb-dropdown-item').forEach(item => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        const value = item.dataset.value;
        toggleSelection('client', value);
      });
    });

    // Add search listener
    const searchInput = clientDropdown.querySelector('.hb-search-input');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        filterDropdownItems(clientDropdown, e.target.value);
      });
      searchInput.addEventListener('click', (e) => e.stopPropagation());
    }
  }

  /**
   * Toggle selection of an item
   */
  function toggleSelection(type, value) {
    const selected = type === 'component' ? selectedComponents : selectedClients;
    const dropdown = type === 'component' ? componentDropdown : clientDropdown;
    
    const index = selected.indexOf(value);
    if (index === -1) {
      selected.push(value);
    } else {
      selected.splice(index, 1);
    }

    // Update visual state of the clicked item
    const item = dropdown.querySelector(`.hb-dropdown-item[data-value="${CSS.escape(value)}"]`);
    if (item) {
      const icon = item.querySelector('.hb-check-icon');
      if (selected.includes(value)) {
        item.classList.add('selected');
        icon.textContent = 'check_box';
      } else {
        item.classList.remove('selected');
        icon.textContent = 'check_box_outline_blank';
      }
    }

    updateToggleText(type);
  }

  /**
   * Filter dropdown items based on search query
   */
  function filterDropdownItems(dropdown, query) {
    const items = dropdown.querySelectorAll('.hb-dropdown-item');
    const lowerQuery = query.toLowerCase();
    
    items.forEach(item => {
      const label = item.querySelector('.hb-item-label').textContent.toLowerCase();
      item.style.display = label.includes(lowerQuery) ? 'flex' : 'none';
    });
  }

  /**
   * Update the toggle button text based on selections
   */
  function updateToggleText(type) {
    const selected = type === 'component' ? selectedComponents : selectedClients;
    const toggle = type === 'component' ? componentToggle : clientToggle;
    const placeholder = type === 'component' ? 'Select components...' : 'Select clients...';

    if (!toggle) return;

    // Use first child span (more reliable than class selector)
    const textSpan = toggle.querySelector('span:first-child');
    if (!textSpan) return;

    if (selected.length === 0) {
      textSpan.textContent = placeholder;
      textSpan.className = 'hb-select-text placeholder';
    } else {
      textSpan.textContent = `${selected.length} selected`;
      textSpan.className = 'hb-select-text';
    }
  }

  /**
   * Load the next available version for the currently-selected release line.
   * Also (on the first call) populates the release-selector dropdown from the
   * `minorVersions` list returned by the server.
   */
  async function loadNextVersion() {
    try {
      const url = selectedMinor !== null
        ? `/api/hotfix-booking/next-version?minor=${selectedMinor}`
        : '/api/hotfix-booking/next-version';
      const response = await fetch(url);
      const data = await response.json();

      // Populate the release-selector once we have the minorVersions list.
      if (!bookMinorOptionsLoaded && bookMinorSelect && Array.isArray(data.minorVersions)) {
        bookMinorSelect.innerHTML = '';
        data.minorVersions.forEach(v => {
          const opt = document.createElement('option');
          opt.value = v.minor;
          opt.textContent = v.label;
          if (v.minor === data.minor) opt.selected = true;
          bookMinorSelect.appendChild(opt);
        });
        bookMinorOptionsLoaded = true;
        // Now that we know the effective minor, lock it in so the bookings
        // list stays in sync with the badge.
        if (selectedMinor === null && typeof data.minor === 'number') {
          selectedMinor = data.minor;
          loadBookings();
        }
      }

      if (data.error && !data.nextVersion) {
        nextVersion = null;
        nextVersionEl.textContent = 'N/A';
        nextVersionEl.title = data.error;
        return;
      }

      nextVersion = data.nextVersion;
      nextVersionEl.textContent = nextVersion;
      nextVersionEl.title = `Current highest: ${data.currentHighest}`;
    } catch (error) {
      console.error('Failed to load next version:', error);
      nextVersionEl.textContent = 'Error';
    }
  }

  /**
   * Load the 5 most recent hotfixes for the selected release line —
   * a merged view of already-deployed CMs (from Jira) and pending
   * bookings from the local store. Only makes the call once we know
   * which release to filter by.
   */
  async function loadBookings() {
    if (selectedMinor === null) return;   // wait until loadNextVersion sets it
    try {
      const url = `/api/hotfix-booking/history?minor=${selectedMinor}&major=9`;
      const response = await fetch(url);
      const data = await response.json();
      renderRecentHotfixes(data.hotfixes || []);
    } catch (error) {
      console.error('Failed to load recent hotfixes:', error);
    }
  }

  /**
   * Compact list of the 5 latest hotfixes (deployed + booked, mixed).
   */
  function renderRecentHotfixes(hotfixes) {
    if (!bookingsListEl) return;

    if (hotfixes.length === 0) {
      const label = selectedMinor !== null ? ` for 9.${selectedMinor}.x` : '';
      bookingsListEl.innerHTML =
        `<p class="hb-no-bookings">No recent hotfixes${label} yet.</p>`;
      return;
    }

    const top5 = hotfixes.slice(0, 5);
    bookingsListEl.innerHTML = top5.map(hf => {
      const date = hf.deployedAt || hf.bookedAt || '';
      const by = hf.reporter || hf.bookedBy || '';
      const statusLabel = hf.type === 'deployed' ? (hf.status || 'Deployed') : 'Booked';
      const statusClass = getStatusClass(statusLabel);
      const clients = hf.clientEnvironments || [];
      const components = hf.components || [];
      return `
        <div class="hb-booking-item">
          <div class="hb-booking-version">
            ${Utils.escapeHtml(hf.version)}
            <span class="cm-status ${statusClass}" style="margin-left: 6px; font-size: 0.75em; vertical-align: middle;">${Utils.escapeHtml(statusLabel)}</span>
          </div>
          <div class="hb-booking-details">
            <div class="hb-booking-tags">
              ${components.map(c => `<span class="hb-tag hb-component-tag">${Utils.escapeHtml(c)}</span>`).join('')}
            </div>
            <div class="hb-booking-tags">
              ${clients.slice(0, 3).map(c => `<span class="hb-tag hb-client-tag">${Utils.escapeHtml(c)}</span>`).join('')}
              ${clients.length > 3 ? `<span class="hb-tag hb-more-tag">+${clients.length - 3} more</span>` : ''}
            </div>
            <div class="hb-booking-meta">
              ${date ? `<span>${formatDate(date)}</span>` : ''}
              ${by ? `<span>by ${Utils.escapeHtml(by)}</span>` : ''}
            </div>
          </div>
        </div>
      `;
    }).join('');
  }

  /**
   * Book a hotfix version
   */
  async function bookHotfix() {
    if (!nextVersion) {
      Utils.showToast('No version available to book.', 'warning');
      return;
    }

    if (selectedComponents.length === 0) {
      Utils.showToast('Please select at least one component.', 'warning');
      return;
    }

    if (selectedClients.length === 0) {
      Utils.showToast('Please select at least one client environment.', 'warning');
      return;
    }

    bookBtn.disabled = true;
    bookBtn.innerHTML = '<span class="material-icons">hourglass_empty</span> Booking...';

    try {
      const response = await fetch('/api/hotfix-booking/book', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          version: nextVersion,
          components: selectedComponents,
          clientEnvironments: selectedClients,
          bookedBy: 'Dashboard User' // Could be enhanced with actual user info
        })
      });

      const data = await response.json();

      // Stale-version case: server tells us the current next has moved on.
      // Update the badge, tell the user, and abort — they can click again.
      if (response.status === 409 && data.currentNext) {
        nextVersion = data.currentNext;
        if (nextVersionEl) {
          nextVersionEl.textContent = nextVersion;
        }
        Utils.showToast(
          `Version ${data.currentNext} is now the next available (you had a stale value). Click Book again to reserve it.`,
          'warning'
        );
        loadBookings();
        return;
      }

      if (data.error) {
        Utils.showToast(data.error, 'error');
        // Something else changed on the server — resync the badge just in case.
        loadNextVersion();
        return;
      }

      // Success - reset selections and reload
      selectedComponents = [];
      selectedClients = [];
      renderComponentDropdown();
      renderClientDropdown();
      updateToggleText('component');
      updateToggleText('client');

      // Reload next version and bookings
      await loadNextVersion();
      await loadBookings();

      Utils.showToast(`Successfully booked version ${data.booking.version}!`, 'success');
    } catch (error) {
      console.error('Booking failed:', error);
      Utils.showToast('Failed to book hotfix version. Please try again.', 'error');
    } finally {
      bookBtn.disabled = false;
      bookBtn.innerHTML = '<span class="material-icons">book_online</span> Book Hotfix Version';
    }
  }

  /**
   * Load version matrix
   */
  async function loadVersionMatrix() {
    showMatrixLoading(true);

    try {
      const response = await fetch('/api/hotfix-booking/client-versions');
      const data = await response.json();

      if (data.error) {
        matrixTableEl.innerHTML = `<p class="hb-error">Error: ${Utils.escapeHtml(data.error)}</p>`;
        return;
      }

      renderVersionMatrix(data);
    } catch (error) {
      console.error('Failed to load version matrix:', error);
      matrixTableEl.innerHTML = '<p class="hb-error">Failed to load version matrix.</p>';
    } finally {
      showMatrixLoading(false);
    }
  }

  /**
   * Render version matrix table
   */
  function renderVersionMatrix(data) {
    if (!matrixTableEl) return;

    const { matrix, components, clients } = data;

    if (clients.length === 0 || components.length === 0) {
      matrixTableEl.innerHTML = '<p class="hb-no-data">No deployed versions found.</p>';
      return;
    }

    let html = `
      <table class="hb-matrix-table">
        <thead>
          <tr>
            <th class="hb-client-col">Client</th>
            ${components.map(c => `<th class="hb-comp-col">${Utils.escapeHtml(c)}</th>`).join('')}
          </tr>
        </thead>
        <tbody>
    `;

    clients.forEach(client => {
      html += `<tr>
        <td class="hb-client-cell">${Utils.escapeHtml(client)}</td>`;
      
      components.forEach(comp => {
        const cellData = matrix[client]?.[comp];
        if (cellData) {
          html += `
            <td class="hb-version-cell" title="CM: ${cellData.cmKey}${cellData.deployedAt ? ', Deployed: ' + cellData.deployedAt : ''}">
              <span class="hb-version-value">${Utils.escapeHtml(cellData.version)}</span>
            </td>`;
        } else {
          html += `<td class="hb-version-cell hb-empty-cell">-</td>`;
        }
      });

      html += '</tr>';
    });

    html += '</tbody></table>';
    matrixTableEl.innerHTML = html;
  }

  /**
   * Load hotfix history
   */
  async function loadHotfixHistory(minor = null) {
    showHistoryLoading(true);

    try {
      const url = minor 
        ? `/api/hotfix-booking/history?minor=${minor}`
        : '/api/hotfix-booking/history';
      
      const response = await fetch(url);
      const data = await response.json();

      if (data.error) {
        historyTableEl.innerHTML = `<p class="hb-error">Error: ${Utils.escapeHtml(data.error)}</p>`;
        return;
      }

      // Populate minor version dropdown if not already done
      if (minorVersionSelect && minorVersionSelect.options.length === 0) {
        data.minorVersions.forEach(v => {
          const option = document.createElement('option');
          option.value = v.minor;
          option.textContent = v.label;
          if (v.minor === data.currentMinor) {
            option.selected = true;
          }
          minorVersionSelect.appendChild(option);
        });
      }

      // Store Jira base URL for CM links
      if (data.jiraBaseUrl) {
        jiraBaseUrl = data.jiraBaseUrl;
      }

      renderHotfixHistory(data.hotfixes);
    } catch (error) {
      console.error('Failed to load hotfix history:', error);
      historyTableEl.innerHTML = '<p class="hb-error">Failed to load hotfix history.</p>';
    } finally {
      showHistoryLoading(false);
    }
  }

  /**
   * Render hotfix history table
   */
  function renderHotfixHistory(hotfixes) {
    if (!historyTableEl) return;

    if (!hotfixes || hotfixes.length === 0) {
      historyTableEl.innerHTML = '<p class="hb-no-data">No hotfixes found for this version.</p>';
      return;
    }

    let html = `
      <table class="hb-history-table">
        <thead>
          <tr>
            <th>Version</th>
            <th>Status</th>
            <th>Components</th>
            <th>Clients</th>
            <th>Reporter</th>
            <th>Date</th>
            <th>CM</th>
          </tr>
        </thead>
        <tbody>
    `;

    hotfixes.forEach((hf, index) => {
      const rowId = `hf-${index}`;
      const statusLabel = hf.type === 'deployed' ? hf.status : 'Booked';
      const statusClass = getStatusClass(statusLabel);
      const date = hf.deployedAt || hf.bookedAt || '-';
      const displayDate = formatDateOnly(date);
      
      html += `
        <tr>
          <td class="hb-version-cell">
            <span class="hb-version-value">${Utils.escapeHtml(hf.version)}</span>
          </td>
          <td>
            <span class="cm-status ${statusClass}">${Utils.escapeHtml(statusLabel)}</span>
          </td>
          <td class="hb-components-cell">
            ${renderCollapsibleList(hf.components, 'component', 2, rowId)}
          </td>
          <td class="hb-clients-cell">
            ${renderCollapsibleList(hf.clientEnvironments, 'client', 2, rowId)}
          </td>
          <td>${Utils.escapeHtml(hf.reporter || '-')}</td>
          <td>${displayDate}</td>
          <td>
            ${hf.cmKey 
              ? `<a href="${jiraBaseUrl}/browse/${hf.cmKey}" target="_blank" rel="noopener noreferrer" class="item-key" title="${Utils.escapeHtml(hf.summary || '')}">${hf.cmKey}</a>` 
              : '-'}
          </td>
        </tr>
      `;
    });

    html += '</tbody></table>';
    historyTableEl.innerHTML = html;
  }

  /**
   * Show/hide loading indicator for history view
   */
  function showHistoryLoading(show) {
    if (historyLoadingEl) {
      historyLoadingEl.style.display = show ? 'flex' : 'none';
    }
  }

  /**
   * Show/hide loading indicator for book view
   */
  function showLoading(show) {
    if (loadingEl) {
      loadingEl.style.display = show ? 'flex' : 'none';
    }
  }

  /**
   * Show/hide loading indicator for matrix view
   */
  function showMatrixLoading(show) {
    if (matrixLoadingEl) {
      matrixLoadingEl.style.display = show ? 'flex' : 'none';
    }
  }

  /**
   * Format an ISO timestamp for display in Eastern Time (ET).
   * Used for `bookedAt` in the Recent Bookings feed.
   */
  function formatDate(dateStr) {
    if (!dateStr) return '';
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return dateStr;
    return date.toLocaleString('en-US', {
      timeZone: DISPLAY_TZ,
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    }) + ' ET';
  }

  /**
   * Format a date-only string (from Jira's TargetDeploymentDate, e.g. "2026-05-15")
   * or an ISO timestamp for display in ET.
   */
  function formatDateOnly(dateStr) {
    if (!dateStr) return '-';
    // Jira `TargetDeploymentDate` values like "2026-05-15" are timezone-agnostic —
    // pass through unchanged. Only convert full ISO timestamps.
    if (/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return dateStr;
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return dateStr;
    return date.toLocaleDateString('en-US', {
      timeZone: DISPLAY_TZ,
      year: 'numeric',
      month: 'numeric',
      day: 'numeric'
    });
  }

  /**
   * Refresh all data (called by Refresh Data button)
   */
  async function refresh() {
    showLoading(true);
    
    // Reset cached state to force reload
    fieldOptionsLoaded = false;
    
    try {
      // Reload field options, next version, and bookings in parallel
      await Promise.all([
        loadFieldOptions(),
        loadNextVersion(),
        loadBookings()
      ]);
      
      // If matrix view is visible, reload it too
      if (matrixView && matrixView.style.display !== 'none') {
        await loadVersionMatrix();
      }
      
      // If history view is visible, reload it too
      if (historyView && historyView.style.display !== 'none') {
        await loadHotfixHistory(minorVersionSelect?.value);
      }
      
      Utils.showToast('Data refreshed successfully', 'success');
    } catch (error) {
      console.error('Refresh failed:', error);
      Utils.showToast('Failed to refresh data', 'error');
    } finally {
      showLoading(false);
    }
  }

  // Export module
  window.HotfixBookingModule = {
    init,
    onTabShow,
    refresh
  };
})();
