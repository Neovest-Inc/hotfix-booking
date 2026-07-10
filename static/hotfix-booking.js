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
  // Currently-selected release line. `null` = server default (current release).
  // Values are integers representing major/minor of the release the user picked.
  let selectedMajor = null;
  let selectedMinor = null;
  let bookMinorOptionsLoaded = false; // populate the release <select> only once
  let selectedComponents = [];
  let selectedClients = [];
  let availableComponents = [];
  let availableClients = [];
  let jiraBaseUrl = '';
  // Last-rendered list of hotfixes for the current release line — used by
  // the Cancel flow to preview affected downstream bookings client-side.
  let renderedHotfixes = [];

  // The signed-in user (resolved via Jira email lookup, cached in localStorage).
  let userEmail = '';
  let userName = '';
  const USER_EMAIL_KEY = 'hotfixBooking.userEmail';
  const USER_NAME_KEY = 'hotfixBooking.userName';
  // Which of the three pill views was last active. Restored on load so a full
  // page refresh keeps the user where they were (Book / Matrix / History).
  const ACTIVE_VIEW_KEY = 'hotfixBooking.activeView';

  // Show at most this many rows in the My Hotfixes feed. The release-line
  // dropdown separately shows the top 8 minor lines (see derive_minor_versions
  // on the server).
  const MY_HOTFIXES_COUNT = 20;

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
        setActiveView(btn.dataset.view);
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

    // Expand / collapse the +N more toggles in the My Hotfixes list.
    // Also handles the Cancel button and the Rebased-chip popover toggle.
    if (bookingsListEl) {
      bookingsListEl.addEventListener('click', (e) => {
        const rebaseBtn = e.target.closest('.hb-rebase-toggle');
        if (rebaseBtn) {
          e.stopPropagation();
          toggleRebasePopover(rebaseBtn);
          return;
        }
        const cancelBtn = e.target.closest('[data-action="cancel-booking"]');
        if (cancelBtn) {
          e.stopPropagation();
          startCancelFlow(cancelBtn.dataset.bookingId, cancelBtn.dataset.bookingVersion);
          return;
        }
        const btn = e.target.closest('.hb-tags-toggle');
        if (!btn) return;
        const container = btn.closest('.hb-collapsible-tags');
        if (!container) return;
        const collapse = btn.dataset.action === 'collapse';
        container.querySelector('.hb-tags-collapsed').style.display = collapse ? '' : 'none';
        container.querySelector('.hb-tags-expanded').style.display = collapse ? 'none' : '';
      });
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

    // Book Hotfix: release-line selector (lets users book against previous minors
    // and previous majors, e.g. 9.99.x while 10.0.x is active).
    if (bookMinorSelect) {
      bookMinorSelect.addEventListener('change', async () => {
        const parsed = parseReleaseValue(bookMinorSelect.value);
        selectedMajor = parsed.major;
        selectedMinor = parsed.minor;
        showLoading(true);
        try {
          await Promise.all([loadNextVersion(), loadBookings()]);
        } finally {
          showLoading(false);
        }
      });
    }

    // "Booking as:" user email + resolved-name display.
    initUserBox();

    // Restore the last-active pill view from localStorage. Do this AFTER
    // initUserBox so a first-visit modal (which needs the app initialized)
    // still opens on top. Book is the default fallback — matches the HTML's
    // `pill-btn active` marker on the Book button.
    const storedView = (() => {
      try { return localStorage.getItem(ACTIVE_VIEW_KEY); } catch (_) { return null; }
    })();
    if (storedView && storedView !== 'book' && ['matrix', 'history'].includes(storedView)) {
      setActiveView(storedView);
    }

    initialized = true;
  }

  /**
   * Switch to one of the three pill views. Handles button state, view
   * visibility, auto-refresh lifecycle, view-specific data loading, and
   * persistence to localStorage so a refresh keeps the user in place.
   */
  function setActiveView(view) {
    if (!['book', 'matrix', 'history'].includes(view)) view = 'book';

    pillBtns.forEach(b => b.classList.toggle('active', b.dataset.view === view));
    if (bookView) bookView.style.display = view === 'book' ? 'block' : 'none';
    if (matrixView) matrixView.style.display = view === 'matrix' ? 'block' : 'none';
    if (historyView) historyView.style.display = view === 'history' ? 'block' : 'none';

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

    try { localStorage.setItem(ACTIVE_VIEW_KEY, view); } catch (_) { /* ignore quota errors */ }
  }

  /**
   * Wire up the header "Booking as:" area.
   * - Restores previously-verified email + name from localStorage on load.
   * - Resolves the email via Jira on blur / Enter and displays the real name.
   * - Book Hotfix button is disabled until a name is resolved.
   * - On a first visit with no stored user, pops a blocking modal so the app
   *   can't be used anonymously.
   */
  function initUserBox() {
    const emailEl = document.getElementById('hbUserEmail');
    const statusEl = document.getElementById('hbUserStatus');
    const clearBtn = document.getElementById('hbUserClear');
    if (!emailEl || !statusEl) return;

    const storedEmail = localStorage.getItem(USER_EMAIL_KEY) || '';
    const storedName = localStorage.getItem(USER_NAME_KEY) || '';
    if (storedEmail && storedName) {
      userEmail = storedEmail;
      userName = storedName;
      showResolvedUser(statusEl, emailEl, clearBtn);
    } else {
      updateBookButtonState();
      openUserModal();
    }

    async function tryResolve() {
      const email = emailEl.value.trim();
      if (!email) return;
      if (email === userEmail && userName) return;   // already resolved
      statusEl.textContent = 'Looking up…';
      statusEl.className = 'hb-user-hint';
      const result = await resolveEmail(email);
      if (result.ok) {
        userEmail = result.email;
        userName = result.displayName;
        localStorage.setItem(USER_EMAIL_KEY, userEmail);
        localStorage.setItem(USER_NAME_KEY, userName);
        showResolvedUser(statusEl, emailEl, clearBtn);
        // The header inline input is only reachable AFTER the first-visit
        // modal has closed, so any success here is a mid-session identity
        // swap — pull fresh data so the visible view reflects the new user
        // (Cancel buttons re-evaluate against the new email).
        reloadCurrentViewData();
      } else {
        statusEl.textContent = result.error || 'User not found in Jira';
        statusEl.className = 'hb-user-error';
        userEmail = '';
        userName = '';
        updateBookButtonState();
      }
    }

    emailEl.addEventListener('blur', tryResolve);
    emailEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        emailEl.blur();
      }
    });

    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        userEmail = '';
        userName = '';
        localStorage.removeItem(USER_EMAIL_KEY);
        localStorage.removeItem(USER_NAME_KEY);
        emailEl.value = '';
        emailEl.style.display = '';
        emailEl.focus();
        clearBtn.style.display = 'none';
        statusEl.textContent = 'Booking as:';
        statusEl.className = 'hb-user-hint';
        updateBookButtonState();
      });
    }
  }

  /**
   * Shared email → { ok, email, displayName } | { ok:false, error } resolver.
   */
  async function resolveEmail(email) {
    try {
      const r = await fetch(`/api/hotfix-booking/resolve-user?email=${encodeURIComponent(email)}`);
      const data = await r.json();
      if (r.status === 200 && data.displayName) {
        return { ok: true, email: data.email, displayName: data.displayName };
      }
      return { ok: false, error: data.error || 'User not found in Jira' };
    } catch (e) {
      console.error('User resolve failed:', e);
      return { ok: false, error: 'Failed to reach Jira' };
    }
  }

  /**
   * Blocking first-visit modal. Only way past is a successful email resolve.
   */
  function openUserModal() {
    const overlay = document.getElementById('hbUserModal');
    const emailEl = document.getElementById('hbModalEmail');
    const submitBtn = document.getElementById('hbModalSubmit');
    const errorEl = document.getElementById('hbModalError');
    const headerEmailEl = document.getElementById('hbUserEmail');
    const headerStatusEl = document.getElementById('hbUserStatus');
    const headerClearBtn = document.getElementById('hbUserClear');
    if (!overlay || !emailEl || !submitBtn) return;

    overlay.style.display = 'flex';
    setTimeout(() => emailEl.focus(), 0);
    errorEl.textContent = '';

    async function submit() {
      const email = emailEl.value.trim();
      if (!email) {
        errorEl.textContent = 'Please enter your email.';
        return;
      }
      submitBtn.disabled = true;
      submitBtn.textContent = 'Looking up…';
      errorEl.textContent = '';
      const result = await resolveEmail(email);
      if (result.ok) {
        // Was this a mid-session swap (someone already loaded data as user A
        // and just re-identified as user B)? If so, refresh; otherwise it's
        // the first-visit resolve and `onTabShow` is already loading data.
        const wasFirstVisit = !fieldOptionsLoaded;
        userEmail = result.email;
        userName = result.displayName;
        localStorage.setItem(USER_EMAIL_KEY, userEmail);
        localStorage.setItem(USER_NAME_KEY, userName);
        overlay.style.display = 'none';
        showResolvedUser(headerStatusEl, headerEmailEl, headerClearBtn);
        if (!wasFirstVisit) reloadCurrentViewData();
      } else {
        errorEl.textContent = result.error;
        submitBtn.disabled = false;
        submitBtn.textContent = 'Continue';
      }
    }

    submitBtn.onclick = submit;
    emailEl.onkeydown = (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submit();
      }
    };
  }

  function showResolvedUser(statusEl, emailEl, clearBtn) {
    statusEl.innerHTML = `Booking as: <span class="hb-user-name">${Utils.escapeHtml(userName)}</span>`;
    statusEl.className = 'hb-user-hint';
    emailEl.value = userEmail;
    emailEl.style.display = 'none';
    if (clearBtn) clearBtn.style.display = '';
    updateBookButtonState();
  }

  function updateBookButtonState() {
    if (!bookBtn) return;
    if (!userName) {
      bookBtn.disabled = true;
      bookBtn.title = 'Enter your Neovest email above before booking';
    } else {
      bookBtn.disabled = false;
      bookBtn.title = '';
    }
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
    // Rebased-chip popover toggle
    const rebaseBtn = e.target.closest('.hb-rebase-toggle');
    if (rebaseBtn) {
      e.stopPropagation();
      toggleRebasePopover(rebaseBtn);
      return;
    }

    // Cancel button
    const cancelBtn = e.target.closest('[data-action="cancel-booking"]');
    if (cancelBtn) {
      e.stopPropagation();
      startCancelFlow(cancelBtn.dataset.bookingId, cancelBtn.dataset.bookingVersion);
      return;
    }

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
   * Toggle the Rebased-chip's popover panel for the row it belongs to.
   * The popover sits inside the same `.hb-booking-item` (My Hotfixes) or
   * `<tr>` (Hotfix History) so we search from the button's closest common
   * ancestor.
   */
  function toggleRebasePopover(btn) {
    const popoverId = btn.dataset.popoverId;
    if (!popoverId) return;
    const scope = btn.closest('.hb-booking-item') || btn.closest('tr') || document;
    const popover = scope.querySelector(`.hb-rebase-popover[data-popover-for="${CSS.escape(popoverId)}"]`);
    if (!popover) return;
    // Close every other popover first so only one is open at a time.
    document.querySelectorAll('.hb-rebase-popover').forEach(p => {
      if (p !== popover) p.style.display = 'none';
    });
    popover.style.display = popover.style.display === 'none' ? '' : 'none';
  }

  // Click-away: close any open Rebased popover when clicking outside it.
  document.addEventListener('click', (e) => {
    if (e.target.closest('.hb-rebase-popover') || e.target.closest('.hb-rebase-toggle')) return;
    document.querySelectorAll('.hb-rebase-popover').forEach(p => { p.style.display = 'none'; });
  });

  /**
   * Called when tab is shown
   */
  async function onTabShow() {
    if (!fieldOptionsLoaded) {
      showLoading(true);
      try {
        // Load all three in parallel — spinner stays up until they all finish
        // so the user sees a clear "working" state during the initial wait.
        await Promise.all([
          loadFieldOptions(),
          loadNextVersion(),
          loadBookings(),
        ]);
      } finally {
        showLoading(false);
      }
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
    // "All Environments" pseudo-item. Materialising semantics: clicking this
    // selects every real client env below, so the booking stores them
    // individually and existing overlap / rebase math applies unchanged.
    // Checked iff every real client is currently selected — so partial edits
    // downgrade the indicator honestly.
    const allClientsSelected = availableClients.length > 0
      && availableClients.every(c => selectedClients.includes(c.value));
    html += `
      <div class="hb-dropdown-item hb-dropdown-item--all ${allClientsSelected ? 'selected' : ''}"
           data-action="select-all-clients"
           title="Select every client environment for this hotfix">
        <span class="hb-check-icon material-icons">${allClientsSelected ? 'check_box' : 'check_box_outline_blank'}</span>
        <span class="hb-item-label">All Environments</span>
      </div>
    `;
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
        if (item.dataset.action === 'select-all-clients') {
          toggleSelectAllClients();
          return;
        }
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
   * "All Environments" toggle handler. Flips between all-selected and
   * all-cleared based on the current state, then re-renders the dropdown
   * so every checkbox visual matches reality.
   */
  function toggleSelectAllClients() {
    const allSelected = availableClients.length > 0
      && availableClients.every(c => selectedClients.includes(c.value));
    selectedClients = allSelected ? [] : availableClients.map(c => c.value);
    renderClientDropdown();
    updateToggleText('client');
  }

  /**
   * Keep the "All Environments" row's checkbox in sync when the user toggles
   * a single client (via `toggleSelection`). Avoids a full re-render — the
   * search input's focus and value stay intact.
   */
  function updateSelectAllClientsIndicator() {
    if (!clientDropdown) return;
    const row = clientDropdown.querySelector('[data-action="select-all-clients"]');
    if (!row) return;
    const allSelected = availableClients.length > 0
      && availableClients.every(c => selectedClients.includes(c.value));
    const icon = row.querySelector('.hb-check-icon');
    if (allSelected) {
      row.classList.add('selected');
      if (icon) icon.textContent = 'check_box';
    } else {
      row.classList.remove('selected');
      if (icon) icon.textContent = 'check_box_outline_blank';
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

    // Keep the "All Environments" pseudo-item in sync when a single client
    // toggle just made the selection whole (or broke it).
    if (type === 'client') {
      updateSelectAllClientsIndicator();
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
    } else if (type === 'client'
               && availableClients.length > 0
               && selected.length === availableClients.length) {
      // Friendlier label when every environment is selected — mirrors the
      // "All Environments" pseudo-item the user probably clicked to get here.
      textSpan.textContent = 'All Environments';
      textSpan.className = 'hb-select-text';
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
      const url = releaseQueryString('/api/hotfix-booking/next-version');
      const response = await fetch(url);
      const data = await response.json();

      // Populate the release-selector once we have the minorVersions list.
      if (!bookMinorOptionsLoaded && bookMinorSelect && Array.isArray(data.minorVersions)) {
        bookMinorSelect.innerHTML = '';
        data.minorVersions.forEach(v => {
          const opt = document.createElement('option');
          opt.value = `${v.major}.${v.minor}`;
          opt.textContent = v.label;
          if (v.major === data.major && v.minor === data.minor) opt.selected = true;
          bookMinorSelect.appendChild(opt);
        });
        bookMinorOptionsLoaded = true;
        // Now that we know the effective release, lock it in so the bookings
        // list stays in sync with the badge.
        if (selectedMinor === null
            && typeof data.major === 'number' && typeof data.minor === 'number') {
          selectedMajor = data.major;
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
   * Load hotfixes for the selected release line — a merged view of
   * already-deployed CMs (from Jira) and pending bookings from the local
   * store. `renderMyHotfixes` then filters this down to just the current
   * user's. Only makes the call once we know which release to filter by.
   */
  async function loadBookings() {
    if (selectedMinor === null || selectedMajor === null) return;
    try {
      const url = releaseQueryString('/api/hotfix-booking/history');
      const response = await fetch(url);
      const data = await response.json();
      renderedHotfixes = data.hotfixes || [];
      // Cache the Jira base URL for CM links in My Hotfixes rows. The
      // History view populates the same variable, but that view may never
      // be visited during a session that only ever books — populate here
      // so the CM link in My Hotfixes works even in that case.
      if (data.jiraBaseUrl) jiraBaseUrl = data.jiraBaseUrl;
      renderMyHotfixes(renderedHotfixes);
    } catch (error) {
      console.error('Failed to load hotfixes:', error);
    }
  }

  /**
   * Parse a release-select `<option>` value like "9.97" into { major, minor }.
   * Empty / unparseable values return nulls (server default).
   */
  function parseReleaseValue(value) {
    if (!value) return { major: null, minor: null };
    const parts = String(value).split('.');
    if (parts.length !== 2) return { major: null, minor: null };
    const major = parseInt(parts[0], 10);
    const minor = parseInt(parts[1], 10);
    if (Number.isNaN(major) || Number.isNaN(minor)) return { major: null, minor: null };
    return { major, minor };
  }

  /**
   * Append `?major=X&minor=Y` (or nothing) to a URL based on current selection.
   */
  function releaseQueryString(url) {
    if (selectedMajor === null || selectedMinor === null) return url;
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}major=${selectedMajor}&minor=${selectedMinor}`;
  }

  /**
   * Return true iff this hotfix belongs to the current user — either they
   * booked it in the app, or they're the Jira reporter of the associated
   * CM. Same ownership definition the Cancel button uses (case-insensitive,
   * trimmed on both sides).
   */
  function hotfixIsMine(hf) {
    const me = (userEmail || '').toLowerCase();
    const meName = (userName || '').trim().toLowerCase();
    const owner = (hf.bookedByEmail || '').toLowerCase();
    if (owner && me && owner === me) return true;
    const reporter = (hf.reporter || '').trim().toLowerCase();
    if (reporter && meName && reporter === meName) return true;
    return false;
  }

  /**
   * Compact list of the latest N hotfixes attributed to the current user
   * (either as booker or as Jira CM reporter) on the currently-selected
   * release line. Filters the full merged list `hotfixes` before slicing,
   * so the count reflects "mine" not "most-recent globally".
   */
  function renderMyHotfixes(hotfixes) {
    if (!bookingsListEl) return;

    const releaseLabel = selectedMinor !== null && selectedMajor !== null
      ? `${selectedMajor}.${selectedMinor}.x` : '';
    const releaseSuffix = releaseLabel ? ` on ${releaseLabel}` : '';

    // Without a resolved user we can't compute "mine" — the first-visit
    // modal blocks this state, but auto-refresh could fire during a
    // change-user swap. Show a neutral hint rather than empty state.
    if (!userEmail && !userName) {
      bookingsListEl.innerHTML =
        `<p class="hb-no-bookings">Sign in above to see your hotfixes${releaseSuffix}.</p>`;
      return;
    }

    if (hotfixes.length === 0) {
      bookingsListEl.innerHTML =
        `<p class="hb-no-bookings">No hotfixes${releaseSuffix} yet.</p>`;
      return;
    }

    const mine = hotfixes.filter(hotfixIsMine);
    if (mine.length === 0) {
      bookingsListEl.innerHTML =
        `<p class="hb-no-bookings">None of your hotfixes${releaseSuffix} yet. See Hotfix History for the full list.</p>`;
      return;
    }

    const versionById = buildVersionById(hotfixes);
    const top = mine.slice(0, MY_HOTFIXES_COUNT);
    bookingsListEl.innerHTML = top.map((hf, idx) => {
      const date = hf.deployedAt || hf.bookedAt || '';
      const by = hf.reporter || hf.bookedBy || '';
      const isCancelledBooking = hf.type === 'booked' && hf.bookingStatus === 'cancelled';
      const statusLabel = isCancelledBooking
        ? 'Cancelled'
        : (hf.type === 'deployed' ? (hf.status || 'Deployed') : 'Booked');
      const statusClass = getStatusClass(statusLabel);
      const clients = hf.clientEnvironments || [];
      const components = hf.components || [];
      const versionSpan = isCancelledBooking
        ? `<span class="hb-booking-version hb-booking-version--cancelled">${Utils.escapeHtml(hf.version)}</span>`
        : `<span class="hb-booking-version">${Utils.escapeHtml(hf.version)}</span>`;
      // CM link — shown only when the row has an associated Jira CM. Placed
      // right after the version so scanning "9.98.42 CM-11702" reads as one
      // unit. `jiraBaseUrl` is populated by loadBookings from the /history
      // response; if it's ever empty we fall back to a bare span so nothing
      // breaks.
      const cmLink = hf.cmKey
        ? (jiraBaseUrl
            ? `<a href="${jiraBaseUrl}/browse/${Utils.escapeHtml(hf.cmKey)}" target="_blank" rel="noopener noreferrer" class="item-key hb-booking-cm-link" title="${Utils.escapeHtml(hf.summary || 'Open in Jira')}">${Utils.escapeHtml(hf.cmKey)}</a>`
            : `<span class="hb-booking-cm-link hb-booking-cm-link--plain">${Utils.escapeHtml(hf.cmKey)}</span>`)
        : '';
      const cancelBtn = renderCancelButton(hf, `mine-${idx}`);
      const activeCmChip = renderActiveCmChip(hf);
      const rebasedChip = renderRebasedChip(hf, versionById, `mine-${idx}`);
      return `
        <div class="hb-booking-item ${isCancelledBooking ? 'hb-booking-item--cancelled' : ''}"
             data-booking-id="${Utils.escapeHtml(hf.id || '')}">
          <div class="hb-booking-header">
            ${versionSpan}
            ${cmLink}
            <span class="cm-status ${statusClass}">${Utils.escapeHtml(statusLabel)}</span>
            ${rebasedChip}
            ${activeCmChip}
            <span class="hb-booking-when">
              ${date ? Utils.escapeHtml(formatDate(date)) : ''}${by ? ' &middot; ' + Utils.escapeHtml(by) : ''}
            </span>
            ${cancelBtn}
          </div>
          ${renderExpandableTags(components, 'component')}
          ${renderExpandableTags(clients, 'client')}
          ${renderBasisLine(hf, versionById)}
          ${renderCancelledMeta(hf)}
          ${renderRebasePopover(hf, versionById, `mine-${idx}`)}
        </div>
      `;
    }).join('');
  }

  /**
   * Render a horizontally-wrapping list of tags with an inline "+N more" toggle
   * that expands to show every item, and a "Show less" toggle that collapses back.
   */
  function renderExpandableTags(items, type) {
    if (!items || items.length === 0) return '';
    const tagClass = type === 'component' ? 'hb-component-tag' : 'hb-client-tag';
    const renderTag = v =>
      `<span class="hb-tag ${tagClass}">${Utils.escapeHtml(v)}</span>`;
    const collapsedTags = items.slice(0, 3).map(renderTag).join('');
    const allTags = items.map(renderTag).join('');
    const extra = items.length - 3;
    if (extra <= 0) {
      return `<div class="hb-booking-tags">${collapsedTags}</div>`;
    }
    return `
      <div class="hb-booking-tags hb-collapsible-tags">
        <div class="hb-tags-collapsed">
          ${collapsedTags}
          <button type="button" class="hb-tag hb-more-tag hb-tags-toggle" data-action="expand">+${extra} more</button>
        </div>
        <div class="hb-tags-expanded" style="display: none;">
          ${allTags}
          <button type="button" class="hb-tag hb-more-tag hb-tags-toggle" data-action="collapse">Show less</button>
        </div>
      </div>
    `;
  }

  /**
   * Cancel-and-rebase support helpers
   * ----------------------------------
   * A booking's `parents` list holds booking IDs (not versions). To display
   * "Based on 9.97.1" we need to translate IDs → versions. The list we render
   * (My Hotfixes / Hotfix History) contains every booking on the release
   * line, so a local map is sufficient.
   *
   * The parents list may also contain `jira:<CM-KEY>` pseudo-IDs when the
   * booking was created on top of a Jira CM originating from the legacy
   * Teams-chat workflow. Those resolve via the deployed CM rows in the same
   * render list.
   */
  function buildVersionById(hotfixes) {
    const map = {};
    (hotfixes || []).forEach(hf => {
      if (!hf) return;
      if (hf.id && hf.version) map[hf.id] = hf.version;
      // Emit a jira:<cmKey> entry for every row with a CM key, so parents
      // of the form "jira:CM-1234" resolve to the CM's version regardless
      // of whether the CM has a matching local booking (unified row) or not.
      if (hf.cmKey && hf.version) map[`jira:${hf.cmKey}`] = hf.version;
    });
    return map;
  }

  function baselineVersionOf(version) {
    if (!version) return null;
    const parts = String(version).split('.');
    if (parts.length !== 3) return null;
    return `${parts[0]}.${parts[1]}.0`;
  }

  function parentsAsVersions(parentIds, versionById) {
    if (!parentIds || parentIds.length === 0) return [];
    return parentIds.map(id => versionById[id]).filter(Boolean);
  }

  /**
   * Basis line rendered under the tags: "Based on 9.97.1" / "Based on baseline
   * 9.97.0" / "Based on 9.97.1, 9.97.4" for multi-parent. Only shown for
   * booked entries — deployed rows are historical and don't have a basis.
   */
  function renderBasisLine(hf, versionById) {
    if (hf.type !== 'booked') return '';
    const versions = parentsAsVersions(hf.parents || [], versionById);
    let text;
    if (versions.length === 0) {
      const baseline = baselineVersionOf(hf.version);
      text = baseline ? `Based on baseline ${baseline}` : 'Based on baseline';
    } else {
      text = `Based on ${versions.join(', ')}`;
    }
    return `<div class="hb-basis-line">${Utils.escapeHtml(text)}</div>`;
  }

  /**
   * "cancelled by X on Y" line rendered under the basis for cancelled bookings.
   */
  function renderCancelledMeta(hf) {
    if (hf.type !== 'booked' || hf.bookingStatus !== 'cancelled') return '';
    const by = hf.cancelledBy || 'Unknown';
    const at = hf.cancelledAt ? formatDate(hf.cancelledAt) : '';
    const suffix = at ? ` on ${at}` : '';
    return `<div class="hb-cancelled-meta">Cancelled by ${Utils.escapeHtml(by)}${Utils.escapeHtml(suffix)}</div>`;
  }

  /**
   * Amber chip warning that a cancelled booking has an active Jira CM. Two
   * activation paths:
   *  1. Booked-cancelled row where the server merged `deployedInJira: true`.
   *  2. Deployed row where the server marked `cancelledLocally: true`.
   */
  function renderActiveCmChip(hf) {
    const isBookedCancelledWithCm =
      hf.type === 'booked' && hf.bookingStatus === 'cancelled' && hf.deployedInJira;
    const isDeployedWithLocalCancel =
      hf.type === 'deployed' && hf.cancelledLocally;
    if (!isBookedCancelledWithCm && !isDeployedWithLocalCancel) return '';
    return `<span class="hb-chip hb-chip--warning"
              title="This booking is cancelled locally but a CM exists in Jira.">
              <span class="material-icons">warning</span>Active CM
            </span>`;
  }

  /**
   * "Rebased" chip that toggles a popover listing each rebase event.
   */
  function renderRebasedChip(hf, versionById, popoverId) {
    if (hf.type !== 'booked') return '';
    const history = hf.rebaseHistory || [];
    if (history.length === 0) return '';
    return `<button type="button" class="hb-chip hb-chip--rebased hb-rebase-toggle"
                    data-popover-id="${Utils.escapeHtml(popoverId)}"
                    title="This booking's basis changed ${history.length} time(s). Click for details.">
              <span class="material-icons">history</span>Rebased
            </button>`;
  }

  function renderRebasePopover(hf, versionById, popoverId) {
    if (hf.type !== 'booked') return '';
    const history = hf.rebaseHistory || [];
    if (history.length === 0) return '';
    // Most recent first
    const events = [...history].reverse().map(ev => {
      const prevVs = (ev.previousParentVersions && ev.previousParentVersions.length)
        ? ev.previousParentVersions.join(', ')
        : (baselineVersionOf(hf.version) || 'baseline');
      const newVs = (ev.newParentVersions && ev.newParentVersions.length)
        ? ev.newParentVersions.join(', ')
        : (baselineVersionOf(hf.version) || 'baseline');
      const when = ev.at ? formatDate(ev.at) : '';
      const by = ev.cancelledBy || 'Unknown';
      const cancelledV = ev.cancelledVersion || '';
      return `
        <li>
          <strong>${Utils.escapeHtml(cancelledV)} was cancelled by ${Utils.escapeHtml(by)}</strong>
          ${when ? `<span class="hb-rebase-when">${Utils.escapeHtml(when)}</span>` : ''}
          <div>Was based on <em>${Utils.escapeHtml(prevVs)}</em> → now based on <em>${Utils.escapeHtml(newVs)}</em></div>
        </li>
      `;
    }).join('');
    return `
      <div class="hb-rebase-popover" data-popover-for="${Utils.escapeHtml(popoverId)}" style="display: none;">
        <ol class="hb-rebase-events">${events}</ol>
      </div>
    `;
  }

  function currentUserCanCancel(hf) {
    // The app only cancels LOCAL bookings. Deployed-only rows (Jira CMs
    // without a local booking) are read-only here — cancelling those
    // belongs in Jira, not in this app.
    if (hf.type !== 'booked' || hf.bookingStatus === 'cancelled') return false;
    const owner = (hf.bookedByEmail || '').toLowerCase();
    const me = (userEmail || '').toLowerCase();
    // Path 1: the booker themselves.
    if (owner && owner === me) return true;
    // Path 2: the Jira reporter of the CM at this version. Mirrors the
    // server-side `_is_cm_reporter` check (case-insensitive, whitespace-
    // trimmed exact match on displayName). `hf.reporter` is set to the CM's
    // reporter for unified rows (booking + CM), and falls back to the
    // booker's name for booking-only rows (in which case path 1 already
    // decided). Admin allow-list is intentionally NOT exposed client-side.
    const reporter = (hf.reporter || '').trim().toLowerCase();
    const meName = (userName || '').trim().toLowerCase();
    if (reporter && meName && reporter === meName) return true;
    return false;
  }

  function renderCancelButton(hf, keyForDom) {
    if (!currentUserCanCancel(hf)) return '';
    // Vary the tooltip so it's obvious WHY the button appeared — helps a
    // reporter who didn't make the booking understand they can still cancel.
    const isOwner = (hf.bookedByEmail || '').toLowerCase() === (userEmail || '').toLowerCase();
    const title = isOwner
      ? 'Cancel this booking'
      : 'Cancel this booking (you are the Jira CM reporter for this version)';
    return `<button type="button" class="hb-cancel-btn"
                    data-action="cancel-booking"
                    data-booking-id="${Utils.escapeHtml(hf.id || '')}"
                    data-booking-version="${Utils.escapeHtml(hf.version)}"
                    title="${Utils.escapeHtml(title)}">
              <span class="material-icons">close</span>
              <span class="hb-cancel-btn-text">Cancel</span>
            </button>`;
  }

  /**
   * Compute the parents a new booking would inherit if submitted right now,
   * mirroring the server's DAG rule: for each (client, component) cell the
   * new booking covers, take the most-recent non-cancelled prior on the same
   * release line that also covers that cell; dedupe across cells.
   *
   * Priors include BOTH local bookings AND deployed Jira CMs — CMs created
   * outside the app (via the legacy Teams-chat flow) count as valid parents,
   * matching the server-side computation in `dependencies.compute_parents`.
   *
   * `renderedHotfixes` is already scoped to the current release line by the
   * `/history` fetch. Client-side compute is best-effort: a race with someone
   * else booking on the same line could yield a slightly different real
   * parents list, but the confirmation is about the user understanding the
   * general dependency, not exact accuracy. The server's canonical
   * computation still runs.
   */
  function computeProposedParents(components, clients) {
    const priors = (renderedHotfixes || [])
      .filter(h => {
        if (!h) return false;
        // Local booked rows (any bookingStatus except cancelled).
        if (h.type === 'booked' && h.bookingStatus !== 'cancelled' && h.id) return true;
        // Deployed rows representing external Jira CMs — eligible parents.
        if (h.type === 'deployed' && h.cmKey) return true;
        return false;
      })
      .slice()
      .sort((a, b) => {
        // Local bookings sort by bookedAt; deployed rows have no bookedAt so
        // fall back to deployedAt (Jira's target deployment date). If neither
        // is present, the row falls to the back of the priors list.
        const at = x => x.bookedAt || x.deployedAt || '';
        return at(b).localeCompare(at(a));
      });

    // Most-recent overlapping prior for each cell, one entry per cell.
    const cellToParent = new Map();
    for (const client of clients) {
      for (const comp of components) {
        const key = `${client}|${comp}`;
        const parent = priors.find(p =>
          (p.clientEnvironments || []).includes(client)
          && (p.components || []).includes(comp)
        );
        if (parent) cellToParent.set(key, parent);
      }
    }

    // Dedupe by identity (booking id for local rows, jira:<cmKey> for CMs).
    const seenIds = new Set();
    const uniqueParents = [];
    for (const parent of cellToParent.values()) {
      const identity = parent.id || (parent.cmKey ? `jira:${parent.cmKey}` : null);
      if (identity && !seenIds.has(identity)) {
        seenIds.add(identity);
        uniqueParents.push(parent);
      }
    }
    return uniqueParents;
  }

  /**
   * Show the "your booking will be based on these existing hotfixes" modal
   * and resolve to `true` if the user confirms, `false` on cancel / dismiss.
   * Uses `onclick` (not addEventListener) so re-opening replaces the previous
   * handler cleanly.
   */
  function confirmBookingWithParents(parents, version) {
    return new Promise(resolve => {
      const overlay = document.getElementById('hbBookConfirmModal');
      if (!overlay) { resolve(true); return; }  // fail-open if HTML missing
      const body = overlay.querySelector('#hbBookConfirmBody');

      const listHtml = parents.map(p => `
        <li>
          <strong>${Utils.escapeHtml(p.version)}</strong>
          &mdash; ${Utils.escapeHtml(p.bookedBy || 'Unknown')}
          <span class="hb-cancel-email">&lt;${Utils.escapeHtml(p.bookedByEmail || 'no email')}&gt;</span>
          <br><span class="hb-cancel-hint">
            overlaps on ${Utils.escapeHtml((p.components || []).join(', ') || '\u2014')}
            \u00b7 ${Utils.escapeHtml((p.clientEnvironments || []).join(', ') || '\u2014')}
          </span>
        </li>
      `).join('');

      const thisThese = parents.length === 1 ? 'this version' : 'these versions';

      body.innerHTML = `
        <ul class="hb-cancel-affected">${listHtml}</ul>
        <p><strong>Before you deploy, make sure your branch includes the changes from ${thisThese}.</strong></p>
        <div class="hb-cancel-buttons">
          <button type="button" id="hbBookConfirmCancel" class="hb-cancel-abort">Cancel</button>
          <button type="button" id="hbBookConfirmOk" class="hb-confirm-book">
            <span class="material-icons">check</span>
            I understand &mdash; Book ${Utils.escapeHtml(version)}
          </button>
        </div>
      `;

      overlay.style.display = 'flex';
      const cancelBtn = overlay.querySelector('#hbBookConfirmCancel');
      const okBtn = overlay.querySelector('#hbBookConfirmOk');

      const close = (result) => {
        overlay.style.display = 'none';
        overlay.onclick = null;
        resolve(result);
      };
      cancelBtn.onclick = () => close(false);
      okBtn.onclick = () => close(true);
      // Overlay-click dismiss: treat clicking the dim backdrop as cancel.
      overlay.onclick = (e) => { if (e.target === overlay) close(false); };
    });
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

    if (!userEmail || !userName) {
      Utils.showToast('Enter your Neovest email at the top of the page first.', 'warning');
      return;
    }

    // If this booking would inherit parents (i.e. it overlaps with existing
    // non-cancelled bookings on the same release line), make the user confirm.
    // The whole point of parents is that the booker is aware they need to
    // pull those changes in — surfacing it before the API call is cheap
    // insurance against blind bookings. Mirrors the server's DAG math so
    // what the modal shows matches what the server would compute.
    const proposedParents = computeProposedParents(selectedComponents, selectedClients);
    if (proposedParents.length > 0) {
      const confirmed = await confirmBookingWithParents(proposedParents, nextVersion);
      if (!confirmed) return;
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
          bookedByEmail: userEmail
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
      bookBtn.innerHTML = '<span class="material-icons">book_online</span> Book Hotfix Version';
      updateBookButtonState();
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

      // Cache the Jira base URL so in-flight chip popovers can link CM
      // keys to their tickets even if the user hit Matrix first (before
      // History or My Hotfixes populated it).
      if (data.jiraBaseUrl) jiraBaseUrl = data.jiraBaseUrl;
      renderVersionMatrix(data);
    } catch (error) {
      console.error('Failed to load version matrix:', error);
      matrixTableEl.innerHTML = '<p class="hb-error">Failed to load version matrix.</p>';
    } finally {
      showMatrixLoading(false);
    }
  }

  /**
   * Render version matrix table.
   *
   * The matrix only surfaces CMs currently in-flight (`In Progress`,
   * `QA Approved`, etc.) — already-shipped work is intentionally hidden
   * (see the header disclaimer). Each cell either shows `-` (nothing in
   * flight) or a stack of in-flight items (version · status · CM link).
   */
  function renderVersionMatrix(data) {
    if (!matrixTableEl) return;

    const { matrix, components, clients } = data;

    if (clients.length === 0 || components.length === 0) {
      matrixTableEl.innerHTML = '<p class="hb-no-data">No CMs currently in progress.</p>';
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
        const inflight = Array.isArray(cellData?.inflight) ? cellData.inflight : [];
        if (inflight.length === 0) {
          html += `<td class="hb-version-cell hb-empty-cell">-</td>`;
          return;
        }
        const items = inflight.map(renderInflightItem).join('');
        html += `<td class="hb-version-cell hb-inflight-cell">${items}</td>`;
      });

      html += '</tr>';
    });

    html += '</tbody></table>';
    matrixTableEl.innerHTML = html;
  }

  /**
   * Compact display of one in-flight CM inside a matrix cell:
   * version on top, then a meta row with a colored status dot + label
   * on the left and the CM link on the right. Multiple items stack
   * vertically as small cards.
   */
  function renderInflightItem(cm) {
    const version = Utils.escapeHtml(cm.version || '?');
    const status = Utils.escapeHtml(cm.status || 'Unknown');
    const category = statusCategory(cm.status);
    const key = cm.cmKey ? Utils.escapeHtml(cm.cmKey) : '';
    const keyEl = key
      ? (jiraBaseUrl
          ? `<a class="hb-matrix-inflight-key" href="${jiraBaseUrl}/browse/${key}" target="_blank" rel="noopener noreferrer" title="Open ${key} in Jira">${key}</a>`
          : `<span class="hb-matrix-inflight-key">${key}</span>`)
      : '';
    return `
      <div class="hb-matrix-inflight-item hb-status-${category}">
        <div class="hb-matrix-inflight-version">${version}</div>
        <div class="hb-matrix-inflight-meta">
          <span class="hb-matrix-inflight-status">${status}</span>
          ${keyEl}
        </div>
      </div>
    `;
  }

  /**
   * Rough bucket a Jira status name into a color category so the matrix
   * cells can render a status dot without a hardcoded status list.
   * Falls back to `default` (neutral gray) for anything unfamiliar.
   */
  function statusCategory(status) {
    const s = (status || '').toLowerCase();
    if (s.includes('ready') || s.includes('approv')) return 'ready';
    if (s.includes('progress') || s.includes('review') || s.includes('test') || s.includes('deploy')) return 'progress';
    if (s.includes('block') || s.includes('hold') || s.includes('wait')) return 'blocked';
    if (s.includes('open') || s.includes('to do') || s.includes('backlog') || s.includes('new')) return 'open';
    return 'default';
  }

  /**
   * Load hotfix history for a given release line.
   * @param {string|null} release - "major.minor" e.g. "9.97", or null for server default.
   */
  async function loadHotfixHistory(release = null) {
    showHistoryLoading(true);

    try {
      let url = '/api/hotfix-booking/history';
      if (release) {
        const { major, minor } = parseReleaseValue(release);
        if (major !== null && minor !== null) {
          url += `?major=${major}&minor=${minor}`;
        }
      }

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
          option.value = `${v.major}.${v.minor}`;
          option.textContent = v.label;
          if (v.major === data.currentMajor && v.minor === data.currentMinor) {
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

    const versionById = buildVersionById(hotfixes);

    let html = `
      <table class="hb-history-table">
        <thead>
          <tr>
            <th>Version</th>
            <th>Status</th>
            <th>Basis</th>
            <th>Components</th>
            <th>Clients</th>
            <th>Reporter</th>
            <th>Date</th>
            <th>CM</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
    `;

    hotfixes.forEach((hf, index) => {
      const rowId = `hf-${index}`;
      const isCancelledBooking = hf.type === 'booked' && hf.bookingStatus === 'cancelled';
      const statusLabel = isCancelledBooking
        ? 'Cancelled'
        : (hf.type === 'deployed' ? hf.status : 'Booked');
      const statusClass = getStatusClass(statusLabel);
      const date = hf.deployedAt || hf.bookedAt || '-';
      const displayDate = formatDateOnly(date);
      const versionCell = isCancelledBooking
        ? `<span class="hb-version-value hb-booking-version--cancelled">${Utils.escapeHtml(hf.version)}</span>`
        : `<span class="hb-version-value">${Utils.escapeHtml(hf.version)}</span>`;
      const cancelBtn = renderCancelButton(hf, `hist-${index}`);
      const rebasedChip = renderRebasedChip(hf, versionById, `hist-${index}`);
      const activeCmChip = renderActiveCmChip(hf);
      const basisText = renderBasisText(hf, versionById);
      const cancelledMeta = renderCancelledMeta(hf);

      html += `
        <tr data-booking-id="${Utils.escapeHtml(hf.id || '')}">
          <td class="hb-version-cell">
            ${versionCell}
          </td>
          <td>
            <span class="cm-status ${statusClass}">${Utils.escapeHtml(statusLabel)}</span>
            ${rebasedChip}
            ${activeCmChip}
            ${cancelledMeta}
            ${renderRebasePopover(hf, versionById, `hist-${index}`)}
          </td>
          <td class="hb-basis-cell">${basisText}</td>
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
          <td>${cancelBtn}</td>
        </tr>
      `;
    });

    html += '</tbody></table>';
    historyTableEl.innerHTML = html;
  }

  /**
   * Plain-text (no wrapper div) version of the basis line — used inside the
   * Hotfix History table cell where we already have a `<td>`.
   */
  function renderBasisText(hf, versionById) {
    if (hf.type !== 'booked') return '<span class="hb-basis-dash">—</span>';
    const versions = parentsAsVersions(hf.parents || [], versionById);
    if (versions.length === 0) {
      const baseline = baselineVersionOf(hf.version);
      return `<span class="hb-basis-baseline">${Utils.escapeHtml(baseline ? `baseline ${baseline}` : 'baseline')}</span>`;
    }
    return Utils.escapeHtml(versions.join(', '));
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
   * Silently refetch the data behind whichever view is currently visible.
   * Used after a mid-session user change so ownership-based UI (e.g. Cancel
   * buttons) re-renders against the new identity. Unlike `refresh()`, no
   * toast is shown and the spinner is left alone — this is background work.
   */
  function reloadCurrentViewData() {
    // Book-view data is cheap and shared context; always refetch it.
    loadNextVersion();
    loadBookings();
    if (matrixView && matrixView.style.display !== 'none') {
      loadVersionMatrix();
    }
    if (historyView && historyView.style.display !== 'none') {
      loadHotfixHistory(minorVersionSelect ? minorVersionSelect.value : null);
    }
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

  /**
   * Cancel-flow entry point — invoked from the Cancel button on any booked row.
   *
   * The confirmation modal previews affected downstream bookings client-side
   * (we already have the full release-line loaded in `renderedHotfixes`), so
   * the user sees the impact before we hit the API. On success we replace the
   * modal body with a "Notify these people" panel listing name+email of each
   * affected booker plus a "Copy list" button.
   */
  function startCancelFlow(bookingId, version) {
    if (!bookingId) return;
    if (!userEmail) {
      Utils.showToast('Set your email at the top of the page first.', 'warning');
      return;
    }
    const affected = previewAffected(bookingId);
    openCancelConfirm({ bookingId, version, affected });
  }

  /**
   * Compute direct children whose CURRENT parents include the given id.
   * Uses `renderedHotfixes` (the last-rendered list for this release line),
   * which the API already scopes to the same release line — so nothing off
   * the current view can be a child.
   */
  function previewAffected(bookingId) {
    const list = renderedHotfixes || [];
    return list.filter(h =>
      h && h.type === 'booked'
      && h.bookingStatus !== 'cancelled'
      && Array.isArray(h.parents)
      && h.parents.indexOf(bookingId) !== -1
    );
  }

  function openCancelConfirm({ bookingId, version, affected }) {
    const overlay = document.getElementById('hbCancelModal');
    if (!overlay) return;
    const body = overlay.querySelector('#hbCancelBody');
    const title = overlay.querySelector('#hbCancelTitle');
    title.textContent = `Cancel ${version}?`;

    let affectedHtml = '';
    if (affected.length === 0) {
      affectedHtml = '<p class="hb-cancel-affected-empty">No other bookings depend on this one — no downstream users to notify.</p>';
    } else {
      affectedHtml = `
        <p><strong>${affected.length}</strong> downstream booking${affected.length === 1 ? '' : 's'} will be rebased. Please notify:</p>
        <ul class="hb-cancel-affected">
          ${affected.map(a => {
            const currentBasis = parentsAsVersions(a.parents || [], buildVersionById(renderedHotfixes || [])).join(', ') || `baseline ${baselineVersionOf(a.version) || ''}`;
            return `<li>
              <strong>${Utils.escapeHtml(a.version)}</strong>
              — ${Utils.escapeHtml(a.bookedBy || 'Unknown')} <span class="hb-cancel-email">&lt;${Utils.escapeHtml(a.bookedByEmail || 'no email')}&gt;</span>
              <br><span class="hb-cancel-hint">was based on ${Utils.escapeHtml(currentBasis)}</span>
            </li>`;
          }).join('')}
        </ul>
      `;
    }

    body.innerHTML = `
      ${affectedHtml}
      <div class="hb-cancel-buttons">
        <button type="button" id="hbCancelKeep" class="hb-cancel-keep">Keep it</button>
        <button type="button" id="hbCancelConfirm" class="hb-cancel-confirm">Cancel ${Utils.escapeHtml(version)}</button>
      </div>
    `;

    overlay.style.display = 'flex';
    const keepBtn = overlay.querySelector('#hbCancelKeep');
    const confirmBtn = overlay.querySelector('#hbCancelConfirm');
    keepBtn.addEventListener('click', () => { overlay.style.display = 'none'; });
    confirmBtn.addEventListener('click', () => performCancel(bookingId, version, overlay));
  }

  async function performCancel(bookingId, version, overlay) {
    const confirmBtn = overlay.querySelector('#hbCancelConfirm');
    if (confirmBtn) {
      confirmBtn.disabled = true;
      confirmBtn.textContent = 'Cancelling…';
    }
    try {
      const resp = await fetch('/api/hotfix-booking/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bookingId, cancelledByEmail: userEmail })
      });
      const data = await resp.json();
      if (!resp.ok) {
        Utils.showToast(data.error || 'Failed to cancel booking', 'error');
        if (confirmBtn) {
          confirmBtn.disabled = false;
          confirmBtn.textContent = `Cancel ${version}`;
        }
        return;
      }
      showCancelResult(overlay, version, data);
      // Repaint the list so all affected rows update with their new basis.
      await Promise.all([loadBookings(), loadNextVersion()]);
      // If the History view is currently visible, refresh it too.
      if (historyView && historyView.style.display !== 'none') {
        loadHotfixHistory();
      }
    } catch (err) {
      console.error('Cancel failed:', err);
      Utils.showToast('Failed to cancel booking', 'error');
      if (confirmBtn) {
        confirmBtn.disabled = false;
        confirmBtn.textContent = `Cancel ${version}`;
      }
    }
  }

  function showCancelResult(overlay, version, data) {
    const body = overlay.querySelector('#hbCancelBody');
    const title = overlay.querySelector('#hbCancelTitle');
    title.textContent = `${version} cancelled`;
    const affected = data.affected || [];
    const warning = data.activeCmWarning;

    let warningHtml = '';
    if (warning) {
      warningHtml = `
        <div class="hb-cancel-warning">
          <span class="material-icons">warning</span>
          <div>
            <strong>Active CM in Jira.</strong> The version you cancelled has a
            live CM (${Utils.escapeHtml(warning.cmKey)}) whose status is
            <em>${Utils.escapeHtml(warning.status)}</em>. Please reconcile the
            CM in Jira — cancelling here does not stop the change process there.
          </div>
        </div>
      `;
    }

    let notifyHtml;
    let copyPayload = '';
    if (affected.length === 0) {
      notifyHtml = '<p>Nobody else depended on this booking.</p>';
    } else {
      copyPayload = affected.map(a =>
        `${a.bookedBy || 'Unknown'} <${a.bookedByEmail || 'no email'}> — ${a.version} (was ${(a.previousParentVersions || []).join(', ') || 'baseline'} → now ${(a.newParentVersions || []).join(', ') || 'baseline'})`
      ).join('\n');
      notifyHtml = `
        <p><strong>Please notify these ${affected.length} downstream booker${affected.length === 1 ? '' : 's'}:</strong></p>
        <ul class="hb-cancel-affected">
          ${affected.map(a => {
            const oldB = (a.previousParentVersions && a.previousParentVersions.length) ? a.previousParentVersions.join(', ') : 'baseline';
            const newB = (a.newParentVersions && a.newParentVersions.length) ? a.newParentVersions.join(', ') : 'baseline';
            return `<li>
              <strong>${Utils.escapeHtml(a.version)}</strong>
              — ${Utils.escapeHtml(a.bookedBy || 'Unknown')} <span class="hb-cancel-email">&lt;${Utils.escapeHtml(a.bookedByEmail || 'no email')}&gt;</span>
              <br><span class="hb-cancel-hint">was based on ${Utils.escapeHtml(oldB)} → now based on ${Utils.escapeHtml(newB)}</span>
            </li>`;
          }).join('')}
        </ul>
        <div class="hb-cancel-buttons">
          <button type="button" id="hbCancelCopy" class="hb-cancel-copy">
            <span class="material-icons">content_copy</span> Copy list
          </button>
        </div>
      `;
    }

    body.innerHTML = `
      ${warningHtml}
      ${notifyHtml}
      <div class="hb-cancel-buttons">
        <button type="button" id="hbCancelDone" class="hb-cancel-keep">Close</button>
      </div>
    `;

    const doneBtn = overlay.querySelector('#hbCancelDone');
    doneBtn.addEventListener('click', () => { overlay.style.display = 'none'; });

    const copyBtn = overlay.querySelector('#hbCancelCopy');
    if (copyBtn) {
      copyBtn.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(copyPayload);
          copyBtn.innerHTML = '<span class="material-icons">check</span> Copied';
        } catch (err) {
          Utils.showToast('Copy failed — please select and copy manually.', 'error');
        }
      });
    }
  }

  // Export module
  window.HotfixBookingModule = {
    init,
    onTabShow,
    refresh
  };
})();
