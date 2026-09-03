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
  getRCA,
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
  selectedEpisodeId: null,
  rca: { loading: false, data: null, error: null },
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

  // Overview Incident Elements
  incidentsContainer: document.getElementById('incidents-list-container'),
  incidentsErrorBanner: document.getElementById('incidents-error-banner'),
  incidentsErrorMessage: document.getElementById('incidents-error-message'),
  incidentsEmptyState: document.getElementById('incidents-empty-state'),

  // Phase 7B Incident Workspace Elements
  incidentsCountBadge: document.getElementById('incidents-count-badge'),
  masterEpisodesList: document.getElementById('master-episodes-list'),
  masterEpisodesError: document.getElementById('master-episodes-error'),
  masterEpisodesErrorMsg: document.getElementById('master-episodes-error-msg'),
  masterEpisodesEmpty: document.getElementById('master-episodes-empty'),

  rcaNoSelection: document.getElementById('rca-no-selection'),
  rcaLoadingState: document.getElementById('rca-loading-state'),
  rcaErrorBanner: document.getElementById('rca-error-banner'),
  rcaErrorMsg: document.getElementById('rca-error-msg'),
  rcaDetailContent: document.getElementById('rca-detail-content'),

  // Detail View Header & Impact Fields
  detSeverityPill: document.getElementById('det-severity-pill'),
  detStatusPill: document.getElementById('det-status-pill'),
  detSegmentTitle: document.getElementById('det-segment-title'),
  detEpisodeId: document.getElementById('det-episode-id'),
  detStartedTime: document.getElementById('det-started-time'),
  detEndedTime: document.getElementById('det-ended-time'),
  detPeakDrop: document.getElementById('det-peak-drop'),

  detImpactDrop: document.getElementById('det-impact-drop'),
  detImpactWindows: document.getElementById('det-impact-windows'),
  detImpactPredictions: document.getElementById('det-impact-predictions'),
  detImpactProtected: document.getElementById('det-impact-protected'),

  detRcaClassification: document.getElementById('det-rca-classification'),
  detRcaCandidatesList: document.getElementById('det-rca-candidates-list'),

  detPredPeakProb: document.getElementById('det-pred-peak-prob'),
  detPredSeverityBonus: document.getElementById('det-pred-severity-bonus'),
  detPredRcaBonus: document.getElementById('det-pred-rca-bonus'),
  detPredStatus: document.getElementById('det-pred-status'),

  detInterVerdicts: document.getElementById('det-inter-verdicts'),
  detInterRoute: document.getElementById('det-inter-route'),

  causalStep1Val: document.getElementById('causal-step-1-val'),
  causalStep2Val: document.getElementById('causal-step-2-val'),
  causalStep3Val: document.getElementById('causal-step-3-val'),
  causalStep4Val: document.getElementById('causal-step-4-val'),
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
 * Switches the active dashboard view/tab programmatically.
 */
export function switchToTab(tabName) {
  elements.navTabs.forEach((t) => {
    if (t.getAttribute('data-view') === tabName) {
      t.classList.add('active');
    } else {
      t.classList.remove('active');
    }
  });

  elements.viewContainers.forEach((view) => {
    if (view.id === `view-${tabName}`) {
      view.classList.add('active');
    } else {
      view.classList.remove('active');
    }
  });

  state.activeTab = tabName;
}

/**
 * Renders recent degradation incidents or clean empty states.
 * Updates both the Overview monitor and the Phase 7B Master Feed panel.
 */
function renderEpisodes() {
  const { data, error } = state.episodes;

  // 1. Handle error state
  if (error) {
    if (elements.incidentsErrorBanner) elements.incidentsErrorBanner.style.display = 'flex';
    if (elements.incidentsErrorMessage) {
      elements.incidentsErrorMessage.textContent = `Failed to load incidents: ${error}`;
    }
    if (elements.incidentsEmptyState) elements.incidentsEmptyState.style.display = 'none';
    if (elements.incidentsContainer) elements.incidentsContainer.innerHTML = '';

    if (elements.masterEpisodesError) elements.masterEpisodesError.style.display = 'flex';
    if (elements.masterEpisodesErrorMsg) {
      elements.masterEpisodesErrorMsg.textContent = `Failed to load incident feed: ${error}`;
    }
    if (elements.masterEpisodesEmpty) elements.masterEpisodesEmpty.style.display = 'none';
    if (elements.masterEpisodesList) elements.masterEpisodesList.innerHTML = '';
    if (elements.incidentsCountBadge) elements.incidentsCountBadge.textContent = '0 Incidents';
    return;
  }

  if (elements.incidentsErrorBanner) elements.incidentsErrorBanner.style.display = 'none';
  if (elements.masterEpisodesError) elements.masterEpisodesError.style.display = 'none';

  // 2. Handle empty state
  if (!data || data.length === 0) {
    if (elements.incidentsEmptyState) elements.incidentsEmptyState.style.display = 'flex';
    if (elements.incidentsContainer) elements.incidentsContainer.innerHTML = '';

    if (elements.masterEpisodesEmpty) elements.masterEpisodesEmpty.style.display = 'flex';
    if (elements.masterEpisodesList) elements.masterEpisodesList.innerHTML = '';
    if (elements.incidentsCountBadge) elements.incidentsCountBadge.textContent = '0 Incidents';

    if (elements.rcaNoSelection) elements.rcaNoSelection.style.display = 'flex';
    if (elements.rcaDetailContent) elements.rcaDetailContent.style.display = 'none';
    return;
  }

  if (elements.incidentsEmptyState) elements.incidentsEmptyState.style.display = 'none';
  if (elements.masterEpisodesEmpty) elements.masterEpisodesEmpty.style.display = 'none';
  if (elements.incidentsCountBadge) {
    elements.incidentsCountBadge.textContent = `${data.length} Incident${data.length === 1 ? '' : 's'}`;
  }

  // 3. Render Overview Incidents List
  if (elements.incidentsContainer) {
    elements.incidentsContainer.innerHTML = data.map((ep) => {
      const severityLower = (ep.severity || 'moderate').toLowerCase();
      const isCritical = severityLower === 'critical';
      const isHigh = severityLower === 'high';
      const cardClass = isCritical ? 'critical' : (isHigh ? 'high' : 'moderate');
      const peakDropStr = ep.peak_absolute_drop ? `-${(ep.peak_absolute_drop * 100).toFixed(1)}%` : '—';
      const startStr = formatDateTime(ep.started_at_window);
      const endStr = ep.ended_at_window ? formatDateTime(ep.ended_at_window) : 'Ongoing (Active)';

      return `
        <div class="incident-card ${cardClass}" data-episode-id="${escapeHTML(ep.episode_id)}" role="button" tabindex="0" title="Click to investigate this incident">
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

    // Attach click listeners to overview incident cards
    elements.incidentsContainer.querySelectorAll('.incident-card').forEach((card) => {
      card.addEventListener('click', () => {
        const episodeId = card.getAttribute('data-episode-id');
        if (episodeId) {
          switchToTab('incidents');
          selectIncident(episodeId);
        }
      });
    });
  }

  // 4. Render Master Episodes Feed (Phase 7B)
  if (elements.masterEpisodesList) {
    elements.masterEpisodesList.innerHTML = data.map((ep) => {
      const severityLower = (ep.severity || 'moderate').toLowerCase();
      const isCritical = severityLower === 'critical';
      const peakDropStr = ep.peak_absolute_drop ? `-${(ep.peak_absolute_drop * 100).toFixed(1)}%` : '—';
      const isSelected = state.selectedEpisodeId === ep.episode_id;
      const selectedClass = isSelected ? 'selected' : '';
      const criticalClass = isCritical ? 'critical' : '';

      return `
        <div class="master-episode-item ${criticalClass} ${selectedClass}" data-episode-id="${escapeHTML(ep.episode_id)}" role="button" tabindex="0">
          <div class="item-top-row">
            <span class="severity-pill ${severityLower}">${escapeHTML(ep.severity)}</span>
            <span class="item-segment">${escapeHTML(ep.segment_dimension)} : ${escapeHTML(ep.segment_value)}</span>
          </div>
          <div class="item-bottom-row">
            <span>${formatDateTime(ep.started_at_window)}</span>
            <span class="item-drop-pill">${peakDropStr}</span>
          </div>
        </div>
      `;
    }).join('');

    // Attach click listeners to master items
    elements.masterEpisodesList.querySelectorAll('.master-episode-item').forEach((item) => {
      item.addEventListener('click', () => {
        const episodeId = item.getAttribute('data-episode-id');
        if (episodeId) {
          selectIncident(episodeId);
        }
      });
    });
  }

  // 5. Auto-select first episode if none selected or if selected no longer exists
  if (data.length > 0) {
    const stillExists = data.some((ep) => ep.episode_id === state.selectedEpisodeId);
    if (!state.selectedEpisodeId || !stillExists) {
      selectIncident(data[0].episode_id);
    }
  }
}

/**
 * Selects an incident episode and fetches its detailed RCA telemetry.
 */
export async function selectIncident(episodeId) {
  if (!episodeId) return;
  state.selectedEpisodeId = episodeId;

  // Update visual selection in master list
  if (elements.masterEpisodesList) {
    elements.masterEpisodesList.querySelectorAll('.master-episode-item').forEach((item) => {
      if (item.getAttribute('data-episode-id') === episodeId) {
        item.classList.add('selected');
      } else {
        item.classList.remove('selected');
      }
    });
  }

  // Update detail view states
  if (elements.rcaNoSelection) elements.rcaNoSelection.style.display = 'none';
  if (elements.rcaLoadingState) elements.rcaLoadingState.style.display = 'flex';
  if (elements.rcaErrorBanner) elements.rcaErrorBanner.style.display = 'none';
  if (elements.rcaDetailContent) elements.rcaDetailContent.style.display = 'none';

  try {
    const rcaData = await getRCA(episodeId);
    state.rca.data = rcaData;
    state.rca.error = null;
    renderIncidentDetail(rcaData);
  } catch (err) {
    state.rca.error = err.message || String(err);
    state.rca.data = null;
    if (elements.rcaLoadingState) elements.rcaLoadingState.style.display = 'none';
    if (elements.rcaErrorBanner) elements.rcaErrorBanner.style.display = 'flex';
    if (elements.rcaErrorMsg) {
      elements.rcaErrorMsg.textContent = `Failed to load RCA diagnosis: ${state.rca.error}`;
    }
  }
}

/**
 * Renders the full investigation workspace with factual RCA, prediction, and intervention metrics.
 */
function renderIncidentDetail(rca) {
  if (!rca) return;

  if (elements.rcaLoadingState) elements.rcaLoadingState.style.display = 'none';
  if (elements.rcaErrorBanner) elements.rcaErrorBanner.style.display = 'none';
  if (elements.rcaNoSelection) elements.rcaNoSelection.style.display = 'none';
  if (elements.rcaDetailContent) elements.rcaDetailContent.style.display = 'flex';

  const ep = rca.episode;
  const pred = rca.prediction_context || {};
  const inter = rca.intervention_context || {};
  const candidates = rca.candidates || [];

  // A. Incident Overview Header
  if (ep) {
    const sevLower = (ep.severity || 'moderate').toLowerCase();
    if (elements.detSeverityPill) {
      elements.detSeverityPill.textContent = ep.severity;
      elements.detSeverityPill.className = `severity-pill ${sevLower}`;
    }
    if (elements.detStatusPill) {
      elements.detStatusPill.textContent = ep.status;
    }
    if (elements.detSegmentTitle) {
      elements.detSegmentTitle.textContent = `${ep.segment_dimension} : ${ep.segment_value}`;
    }
    if (elements.detEpisodeId) {
      elements.detEpisodeId.textContent = ep.episode_id;
    }
    if (elements.detStartedTime) {
      elements.detStartedTime.textContent = formatDateTime(ep.started_at_window);
    }
    if (elements.detEndedTime) {
      elements.detEndedTime.textContent = ep.ended_at_window ? formatDateTime(ep.ended_at_window) : 'Ongoing (Active)';
    }
    const dropPctStr = ep.peak_absolute_drop != null ? `-${(ep.peak_absolute_drop * 100).toFixed(1)}%` : '—';
    if (elements.detPeakDrop) {
      elements.detPeakDrop.textContent = dropPctStr;
    }

    // B. Incident Impact Telemetry
    if (elements.detImpactDrop) elements.detImpactDrop.textContent = dropPctStr;
    if (elements.detImpactWindows) elements.detImpactWindows.textContent = formatCount(ep.affected_window_count);
  }

  if (elements.detImpactPredictions) {
    elements.detImpactPredictions.textContent = formatCount(pred.window_predictions_evaluated);
  }
  if (elements.detImpactProtected) {
    elements.detImpactProtected.textContent = formatINR(inter.window_protected_gmv_minor_units);
  }

  // C & D. Root Cause Analysis & Ranked Candidates Visualization
  if (elements.detRcaClassification) {
    elements.detRcaClassification.textContent = rca.classification || 'SEGMENT_SPECIFIC';
  }

  if (elements.detRcaCandidatesList) {
    if (candidates.length === 0) {
      elements.detRcaCandidatesList.innerHTML = '<div class="empty-state-box"><p class="empty-state-desc">No candidate causes identified for this episode</p></div>';
    } else {
      elements.detRcaCandidatesList.innerHTML = candidates.map((c) => {
        const strengthLower = (c.evidence_strength || 'weak').toLowerCase();
        const contribPct = c.excess_failure_contribution != null ? (c.excess_failure_contribution * 100) : 0;
        const contribPctStr = `${contribPct.toFixed(1)}%`;

        return `
          <div class="rca-candidate-row">
            <div class="candidate-top-info">
              <div class="candidate-left-group">
                <span class="candidate-rank-badge">#${c.rank}</span>
                <span class="candidate-dimension-title">${escapeHTML(c.dimension)} = ${escapeHTML(c.value)}</span>
                <span class="evidence-strength-pill ${strengthLower}">${escapeHTML(c.evidence_strength)}</span>
              </div>
              <span class="candidate-contrib-text">${contribPctStr} excess failure contribution</span>
            </div>
            <div class="rca-bar-track">
              <div class="rca-bar-fill" style="width: ${Math.min(100, Math.max(0, contribPct))}%;"></div>
            </div>
          </div>
        `;
      }).join('');
    }
  }

  // E. Downstream Prediction Context
  if (elements.detPredPeakProb) {
    elements.detPredPeakProb.textContent = formatPercentage(pred.peak_failure_probability, 1);
  }
  if (elements.detPredSeverityBonus) {
    const sev = pred.severity_bonus_tier;
    const bonus = sev === 'CRITICAL' ? '+0.30' : (sev === 'HIGH' ? '+0.15' : '+0.05');
    elements.detPredSeverityBonus.textContent = sev ? `${sev} (${bonus})` : '—';
  }
  if (elements.detPredRcaBonus) {
    const strength = pred.top_rca_evidence_strength;
    const bonus = strength === 'STRONG' ? '+0.20' : (strength === 'MODERATE' ? '+0.10' : '+0.00');
    elements.detPredRcaBonus.textContent = strength ? `${strength} (${bonus})` : '—';
  }
  if (elements.detPredStatus) {
    elements.detPredStatus.textContent = pred.prediction_status || 'PREDICTED';
  }

  // F. Downstream Intervention Decision Context
  if (elements.detInterVerdicts) {
    const actCount = inter.act_decisions || 0;
    const monCount = inter.monitor_decisions || 0;
    elements.detInterVerdicts.textContent = `${formatCount(actCount)} ACT / ${formatCount(monCount)} MONITOR`;
  }
  if (elements.detInterRoute) {
    elements.detInterRoute.textContent = inter.primary_selected_route || 'FALLBACK_PAYMENT_LINK';
  }

  // G. Operational Causal Chain Sequence
  if (elements.causalStep1Val && ep) {
    elements.causalStep1Val.textContent = `${ep.severity} (-${((ep.peak_absolute_drop || 0) * 100).toFixed(1)}%)`;
  }
  if (elements.causalStep2Val && candidates.length > 0) {
    elements.causalStep2Val.textContent = `${candidates[0].dimension}:${candidates[0].value} (${candidates[0].evidence_strength})`;
  }
  if (elements.causalStep3Val) {
    elements.causalStep3Val.textContent = `Peak P(fail) ${((pred.peak_failure_probability || 0) * 100).toFixed(1)}%`;
  }
  if (elements.causalStep4Val) {
    const actNum = inter.act_decisions || 0;
    elements.causalStep4Val.textContent = `${inter.primary_selected_route || 'FALLBACK_PAYMENT_LINK'} (${actNum} ACT)`;
  }
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
