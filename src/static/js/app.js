/**
 * Application Controller — Phase 7A Executive Overview
 * 
 * Manages independent section loading, error handling, manual refresh,
 * navigation tab switching, and dynamic rendering of live API metrics.
 */

import {
  getHealth,
  getSummary,
  getEpisodes,
  formatINR,
  formatPercentage,
  formatCount,
  formatDateTime
} from './api.js';

// Application State
const state = {
  loading: false,
  health: { connected: false, error: null },
  summary: { data: null, error: null },
  episodes: { data: [], error: null },
  activeTab: 'overview',
  lastUpdated: null,
};

// DOM Element References
const elements = {
  healthBadge: document.getElementById('db-health-badge'),
  healthText: document.getElementById('db-health-text'),
  lastUpdatedText: document.getElementById('last-updated-time'),
  refreshButton: document.getElementById('btn-refresh'),
  navTabs: document.querySelectorAll('.nav-tab'),
  viewContainers: document.querySelectorAll('.view-container'),

  // Summary KPI Values
  kpiProtectedRevenue: document.getElementById('kpi-protected-revenue'),
  kpiProcessedVolume: document.getElementById('kpi-processed-volume'),
  kpiSuccessRate: document.getElementById('kpi-success-rate'),
  kpiActiveIncidents: document.getElementById('kpi-active-incidents'),
  kpiInterventions: document.getElementById('kpi-interventions'),
  kpiAuthCaptureVolume: document.getElementById('kpi-auth-capture-volume'),
  summaryErrorBanner: document.getElementById('summary-error-banner'),
  summaryErrorMessage: document.getElementById('summary-error-message'),

  // Incident Elements
  incidentsContainer: document.getElementById('incidents-list-container'),
  incidentsErrorBanner: document.getElementById('incidents-error-banner'),
  incidentsErrorMessage: document.getElementById('incidents-error-message'),
  incidentsEmptyState: document.getElementById('incidents-empty-state'),
};

/**
 * Initialize application listeners and trigger initial data load.
 */
export function init() {
  bindNavigation();
  bindControls();
  loadAllData();
}

/**
 * Binds tab switching.
 */
function bindNavigation() {
  elements.navTabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const targetView = tab.getAttribute('data-view');
      if (!targetView) return;

      elements.navTabs.forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');

      elements.viewContainers.forEach((view) => {
        if (view.id === `view-${targetView}`) {
          view.classList.add('active');
        } else {
          view.classList.remove('active');
        }
      });

      state.activeTab = targetView;
    });
  });
}

/**
 * Binds header controls (manual refresh).
 */
function bindControls() {
  if (elements.refreshButton) {
    elements.refreshButton.addEventListener('click', () => {
      loadAllData();
    });
  }
}

/**
 * Loads all data independently using Promise.allSettled.
 * Prevents a single endpoint failure from failing the entire dashboard.
 */
export async function loadAllData() {
  if (state.loading) return;
  state.loading = true;
  updateLoadingState(true);

  const [healthResult, summaryResult, episodesResult] = await Promise.allSettled([
    getHealth(),
    getSummary(),
    getEpisodes(10)
  ]);

  // 1. Handle Health Check Result
  if (healthResult.status === 'fulfilled') {
    state.health = { connected: true, error: null };
  } else {
    state.health = { connected: false, error: healthResult.reason.message };
  }
  renderHealth();

  // 2. Handle Summary Metrics Result
  if (summaryResult.status === 'fulfilled') {
    state.summary = { data: summaryResult.value, error: null };
  } else {
    state.summary = { data: null, error: summaryResult.reason.message };
  }
  renderSummary();

  // 3. Handle Episodes Result
  if (episodesResult.status === 'fulfilled') {
    state.episodes = { data: episodesResult.value, error: null };
  } else {
    state.episodes = { data: [], error: episodesResult.reason.message };
  }
  renderEpisodes();

  // Update Last-Updated Timestamp
  state.lastUpdated = new Date();
  if (elements.lastUpdatedText) {
    elements.lastUpdatedText.textContent = `Updated ${formatDateTime(state.lastUpdated)}`;
  }

  state.loading = false;
  updateLoadingState(false);
}

/**
 * Updates UI loading indicators without replacing the entire layout.
 */
function updateLoadingState(isLoading) {
  if (elements.refreshButton) {
    elements.refreshButton.disabled = isLoading;
    elements.refreshButton.textContent = isLoading ? 'Refreshing...' : 'Refresh';
  }

  const kpiValues = document.querySelectorAll('.kpi-value');
  if (isLoading) {
    kpiValues.forEach((el) => {
      if (!el.textContent || el.textContent === '—') {
        el.classList.add('skeleton');
      }
    });
  } else {
    kpiValues.forEach((el) => el.classList.remove('skeleton'));
  }
}

/**
 * Renders database health badge.
 */
function renderHealth() {
  if (!elements.healthBadge || !elements.healthText) return;

  if (state.health.connected) {
    elements.healthBadge.className = 'status-badge connected';
    elements.healthText.textContent = 'DB: CONNECTED';
  } else {
    elements.healthBadge.className = 'status-badge disconnected';
    elements.healthText.textContent = 'DB: DISCONNECTED';
  }
}

/**
 * Renders Executive Overview KPI cards or an error banner if /summary failed.
 */
function renderSummary() {
  const { data, error } = state.summary;

  if (error) {
    if (elements.summaryErrorBanner) elements.summaryErrorBanner.style.display = 'flex';
    if (elements.summaryErrorMessage) {
      elements.summaryErrorMessage.textContent = `Failed to load executive summary: ${error}. Verify that FastAPI and PostgreSQL are reachable.`;
    }
    // Set fallback indicators (not fake numbers)
    if (elements.kpiProtectedRevenue) elements.kpiProtectedRevenue.textContent = '—';
    if (elements.kpiProcessedVolume) elements.kpiProcessedVolume.textContent = '—';
    if (elements.kpiSuccessRate) elements.kpiSuccessRate.textContent = '—';
    if (elements.kpiActiveIncidents) elements.kpiActiveIncidents.textContent = '—';
    if (elements.kpiInterventions) elements.kpiInterventions.textContent = '—';
    if (elements.kpiAuthCaptureVolume) elements.kpiAuthCaptureVolume.textContent = '—';
    return;
  }

  if (elements.summaryErrorBanner) elements.summaryErrorBanner.style.display = 'none';
  if (!data) return;

  // 1. Protected Revenue (Stage 8 Attributed)
  if (elements.kpiProtectedRevenue) {
    elements.kpiProtectedRevenue.textContent = formatINR(data.total_protected_gmv_minor_units);
  }

  // 2. Processed Volume (Stage 1 Auth GMV)
  if (elements.kpiProcessedVolume) {
    elements.kpiProcessedVolume.textContent = formatINR(data.total_gmv_minor_units);
  }

  // 3. Authorization Success Rate
  if (elements.kpiSuccessRate) {
    elements.kpiSuccessRate.textContent = formatPercentage(data.overall_success_rate, 1);
  }

  // 4. Active Degradation Incidents
  if (elements.kpiActiveIncidents) {
    elements.kpiActiveIncidents.textContent = formatCount(data.active_degradation_episodes);
  }

  // 5. Interventions Dispatched
  if (elements.kpiInterventions) {
    elements.kpiInterventions.textContent = formatCount(data.total_successful_interventions);
  }

  // 6. Authorization vs Capture Counts
  if (elements.kpiAuthCaptureVolume) {
    elements.kpiAuthCaptureVolume.textContent = `${formatCount(data.total_auths)} / ${formatCount(data.total_captures)}`;
  }
}

/**
 * Renders recent degradation incidents or a clean empty state if none exist.
 */
function renderEpisodes() {
  const { data, error } = state.episodes;

  if (error) {
    if (elements.incidentsErrorBanner) elements.incidentsErrorBanner.style.display = 'flex';
    if (elements.incidentsErrorMessage) {
      elements.incidentsErrorMessage.textContent = `Failed to load incidents: ${error}`;
    }
    if (elements.incidentsEmptyState) elements.incidentsEmptyState.style.display = 'none';
    if (elements.incidentsContainer) elements.incidentsContainer.innerHTML = '';
    return;
  }

  if (elements.incidentsErrorBanner) elements.incidentsErrorBanner.style.display = 'none';

  if (!data || data.length === 0) {
    if (elements.incidentsEmptyState) elements.incidentsEmptyState.style.display = 'flex';
    if (elements.incidentsContainer) elements.incidentsContainer.innerHTML = '';
    return;
  }

  if (elements.incidentsEmptyState) elements.incidentsEmptyState.style.display = 'none';
  if (!elements.incidentsContainer) return;

  // Render list of recent/active incidents dynamically
  elements.incidentsContainer.innerHTML = data.map((ep) => {
    const severityLower = (ep.severity || 'moderate').toLowerCase();
    const isCritical = severityLower === 'critical';
    const isHigh = severityLower === 'high';
    const cardClass = isCritical ? 'critical' : (isHigh ? 'high' : 'moderate');
    const peakDropStr = ep.peak_absolute_drop ? `-${(ep.peak_absolute_drop * 100).toFixed(1)}%` : '—';
    const startStr = formatDateTime(ep.started_at_window);
    const endStr = ep.ended_at_window ? formatDateTime(ep.ended_at_window) : 'Ongoing (Active)';

    return `
      <div class="incident-card ${cardClass}">
        <div class="incident-top-row">
          <div class="incident-badges">
            <span class="severity-pill ${severityLower}">${escapeHTML(ep.severity)}</span>
            <span class="status-pill">${escapeHTML(ep.status)}</span>
          </div>
          <div class="incident-segment">
            ${escapeHTML(ep.segment_dimension)} : ${escapeHTML(ep.segment_value)}
          </div>
        </div>
        <div class="incident-meta-grid">
          <div class="meta-item">
            <span class="meta-label">Incident Started</span>
            <span class="meta-val">${startStr}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">Incident Resolved</span>
            <span class="meta-val">${endStr}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">Peak Drop</span>
            <span class="meta-val drop">${peakDropStr}</span>
          </div>
          <div class="meta-item">
            <span class="meta-label">Episode Reference</span>
            <span class="meta-val" title="${escapeHTML(ep.episode_id)}">${escapeHTML(ep.episode_id.substring(0, 8))}...</span>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

/**
 * Basic HTML escaping helper to prevent XSS.
 */
function escapeHTML(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Auto-initialize when loaded as an ES module
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
