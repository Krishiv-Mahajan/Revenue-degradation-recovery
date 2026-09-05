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
  getRecovery,
  getAttributions,
  getPayments,
  getPaymentTimeline,
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
  
  // Phase 7C Recovery & Attribution State
  recovery: { loading: false, data: null, error: null, filter: '' },
  attributions: { loading: false, data: [], error: null },

  // Phase 7C Payments & Audit State
  payments: { loading: false, data: [], total: 0, error: null, search: '', filterType: '', status: '' },
  selectedPaymentId: null,
  paymentAudit: { loading: false, data: null, error: null },

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

  // Phase 7C: Recovery Elements
  refreshRecoveryBtn: document.getElementById('refresh-recovery-btn'),
  recStatDispatched: document.getElementById('rec-stat-dispatched'),
  recStatAct: document.getElementById('rec-stat-act'),
  recStatMonitor: document.getElementById('rec-stat-monitor'),
  recStatCaptures: document.getElementById('rec-stat-captures'),
  recStatFailed: document.getElementById('rec-stat-failed'),
  recStatGmv: document.getElementById('rec-stat-gmv'),
  recStatAttributions: document.getElementById('rec-stat-attributions'),
  recStatConfidence: document.getElementById('rec-stat-confidence'),

  recLedgerLoading: document.getElementById('rec-ledger-loading'),
  recLedgerError: document.getElementById('rec-ledger-error'),
  recLedgerErrorMsg: document.getElementById('rec-ledger-error-msg'),
  recoveryLedgerBody: document.getElementById('recovery-ledger-body'),
  recoveryLedgerCount: document.getElementById('recovery-ledger-count'),
  filterRecBtns: document.querySelectorAll('.filter-btn[data-filter]'),

  recAttrLoading: document.getElementById('rec-attr-loading'),
  recAttrError: document.getElementById('rec-attr-error'),
  recAttrErrorMsg: document.getElementById('rec-attr-error-msg'),
  attributionLedgerBody: document.getElementById('attribution-ledger-body'),

  // Phase 7C: Payments Elements
  refreshPaymentsBtn: document.getElementById('refresh-payments-btn'),
  paymentSearchInput: document.getElementById('payment-search-input'),
  paymentFilterSelect: document.getElementById('payment-filter-select'),
  paymentStatusSelect: document.getElementById('payment-status-select'),

  paymentsLoading: document.getElementById('payments-loading'),
  paymentsError: document.getElementById('payments-error'),
  paymentsErrorMsg: document.getElementById('payments-error-msg'),
  paymentsTableBody: document.getElementById('payments-table-body'),
  paymentsPaginationInfo: document.getElementById('payments-pagination-info'),
  paymentsSplitLayout: document.querySelector('.payments-split-layout'),

  paymentAuditDrawer: document.getElementById('payment-audit-drawer'),
  closeAuditDrawerBtn: document.getElementById('close-audit-drawer-btn'),
  auditPaymentId: document.getElementById('audit-payment-id'),
  auditDrawerLoading: document.getElementById('audit-drawer-loading'),
  auditDrawerError: document.getElementById('audit-drawer-error'),
  auditDrawerErrorMsg: document.getElementById('audit-drawer-error-msg'),
  auditDrawerDetails: document.getElementById('audit-drawer-details'),

  auditFactAmount: document.getElementById('audit-fact-amount'),
  auditFactMethod: document.getElementById('audit-fact-method'),
  auditFactStatus: document.getElementById('audit-fact-status'),
  auditFactTime: document.getElementById('audit-fact-time'),

  auditRecEpisode: document.getElementById('audit-rec-episode'),
  auditRecProb: document.getElementById('audit-rec-prob'),
  auditRecDecision: document.getElementById('audit-rec-decision'),
  auditRecRoute: document.getElementById('audit-rec-route'),
  auditRecGmv: document.getElementById('audit-rec-gmv'),
  auditRecConfidence: document.getElementById('audit-rec-confidence'),
  auditTimelineStepper: document.getElementById('audit-timeline-stepper'),
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
      switchToTab(targetView);
    });
  });
}

/**
 * Binds header and workspace controls.
 */
function bindControls() {
  if (elements.refreshButton) {
    elements.refreshButton.addEventListener('click', () => {
      loadAllData();
    });
  }

  if (elements.refreshRecoveryBtn) {
    elements.refreshRecoveryBtn.addEventListener('click', () => {
      loadRecoveryData();
    });
  }

  if (elements.refreshPaymentsBtn) {
    elements.refreshPaymentsBtn.addEventListener('click', () => {
      loadPaymentsData();
    });
  }

  if (elements.filterRecBtns) {
    elements.filterRecBtns.forEach((btn) => {
      btn.addEventListener('click', () => {
        elements.filterRecBtns.forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        state.recovery.filter = btn.getAttribute('data-filter') || '';
        loadRecoveryData();
      });
    });
  }

  if (elements.paymentSearchInput) {
    let debounceTimer = null;
    elements.paymentSearchInput.addEventListener('input', (e) => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        state.payments.search = e.target.value.trim();
        loadPaymentsData();
      }, 300);
    });
  }

  if (elements.paymentFilterSelect) {
    elements.paymentFilterSelect.addEventListener('change', (e) => {
      state.payments.filterType = e.target.value;
      loadPaymentsData();
    });
  }

  if (elements.paymentStatusSelect) {
    elements.paymentStatusSelect.addEventListener('change', (e) => {
      state.payments.status = e.target.value;
      loadPaymentsData();
    });
  }

  if (elements.closeAuditDrawerBtn) {
    elements.closeAuditDrawerBtn.addEventListener('click', () => {
      if (elements.paymentAuditDrawer) elements.paymentAuditDrawer.style.display = 'none';
      if (elements.paymentsSplitLayout) elements.paymentsSplitLayout.classList.remove('drawer-open');
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

  const [healthResult, summaryResult, episodesResult, recoveryResult, attributionsResult, paymentsResult] = await Promise.allSettled([
    getHealth(),
    getSummary(),
    getEpisodes(10),
    getRecovery(state.recovery.filter),
    getAttributions(50),
    getPayments({ search: state.payments.search, filterType: state.payments.filterType, status: state.payments.status, limit: 50, offset: 0 })
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

  // 4. Handle Recovery Result
  if (recoveryResult.status === 'fulfilled') {
    state.recovery.data = recoveryResult.value;
    renderRecoverySummary(recoveryResult.value.summary);
    renderRecoveryLedger(recoveryResult.value.ledger, recoveryResult.value.total_count);
  } else if (elements.recLedgerError) {
    elements.recLedgerError.style.display = 'block';
    if (elements.recLedgerErrorMsg) elements.recLedgerErrorMsg.textContent = recoveryResult.reason?.message || 'Failed to load recovery ledger.';
  }

  // 5. Handle Attributions Result
  if (attributionsResult.status === 'fulfilled') {
    state.attributions.data = attributionsResult.value;
    renderAttributionLedger(attributionsResult.value);
  } else if (elements.recAttrError) {
    elements.recAttrError.style.display = 'block';
    if (elements.recAttrErrorMsg) elements.recAttrErrorMsg.textContent = attributionsResult.reason?.message || 'Failed to load attributions.';
  }

  // 6. Handle Payments Result
  if (paymentsResult.status === 'fulfilled') {
    state.payments.data = paymentsResult.value.items || [];
    state.payments.total = paymentsResult.value.total_count || 0;
    renderPayments(paymentsResult.value.items || [], paymentsResult.value.total_count || 0);
  } else if (elements.paymentsError) {
    elements.paymentsError.style.display = 'block';
    if (elements.paymentsErrorMsg) elements.paymentsErrorMsg.textContent = paymentsResult.reason?.message || 'Failed to load payments.';
  }

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
  if (tabName === 'recovery') {
    loadRecoveryData();
  } else if (tabName === 'payments') {
    loadPaymentsData();
  }
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
    elements.causalStep3Val.textContent = pred.peak_failure_probability != null
      ? `Peak P(fail) ${((pred.peak_failure_probability) * 100).toFixed(1)}%`
      : 'Peak P(fail) N/A';
  }
  if (elements.causalStep4Val) {
    const actNum = inter.act_decisions || 0;
    elements.causalStep4Val.textContent = `${inter.primary_selected_route || 'FALLBACK_PAYMENT_LINK'} (${actNum} ACT)`;
  }
}

/**
 * ==========================================================================
 * Phase 7C: Recovery Operations & Attribution Ledger Controller
 * ==========================================================================
 */

/**
 * Loads recovery workspace summary and ledger records.
 */
export async function loadRecoveryData() {
  if (elements.recLedgerLoading) elements.recLedgerLoading.style.display = 'flex';
  if (elements.recAttrLoading) elements.recAttrLoading.style.display = 'flex';
  if (elements.recLedgerError) elements.recLedgerError.style.display = 'none';
  if (elements.recAttrError) elements.recAttrError.style.display = 'none';

  const [recResult, attrResult] = await Promise.allSettled([
    getRecovery(state.recovery.filter),
    getAttributions(50)
  ]);

  if (elements.recLedgerLoading) elements.recLedgerLoading.style.display = 'none';
  if (elements.recAttrLoading) elements.recAttrLoading.style.display = 'none';

  if (recResult.status === 'fulfilled') {
    state.recovery.data = recResult.value;
    renderRecoverySummary(recResult.value.summary);
    renderRecoveryLedger(recResult.value.ledger, recResult.value.total_count);
  } else {
    if (elements.recLedgerError) {
      elements.recLedgerError.style.display = 'block';
      if (elements.recLedgerErrorMsg) elements.recLedgerErrorMsg.textContent = recResult.reason?.message || 'Failed to load recovery ledger.';
    }
  }

  if (attrResult.status === 'fulfilled') {
    state.attributions.data = attrResult.value;
    renderAttributionLedger(attrResult.value);
  } else {
    if (elements.recAttrError) {
      elements.recAttrError.style.display = 'block';
      if (elements.recAttrErrorMsg) elements.recAttrErrorMsg.textContent = attrResult.reason?.message || 'Failed to load attributions.';
    }
  }
}

/**
 * Renders the 8 Recovery KPI summary metric cards.
 */
function renderRecoverySummary(summary) {
  if (!summary) return;
  if (elements.recStatDispatched) elements.recStatDispatched.textContent = formatCount(summary.interventions_dispatched);
  if (elements.recStatAct) elements.recStatAct.textContent = formatCount(summary.act_count);
  if (elements.recStatMonitor) elements.recStatMonitor.textContent = formatCount(summary.monitor_count);
  if (elements.recStatCaptures) elements.recStatCaptures.textContent = formatCount(summary.successful_captures);
  if (elements.recStatFailed) elements.recStatFailed.textContent = formatCount(summary.failed_outcomes);
  if (elements.recStatGmv) elements.recStatGmv.textContent = formatINR(summary.total_protected_gmv_minor_units);
  if (elements.recStatAttributions) elements.recStatAttributions.textContent = formatCount(summary.rescued_count || summary.attribution_count);
  if (elements.recStatConfidence) {
    const conf = summary.rescued_avg_confidence || summary.avg_attribution_confidence || 0.0;
    elements.recStatConfidence.textContent = formatPercentage(conf, 1);
  }
}

/**
 * Renders the operational recovery ledger table.
 */
function renderRecoveryLedger(ledger, totalCount) {
  if (!elements.recoveryLedgerBody) return;
  if (!ledger || ledger.length === 0) {
    elements.recoveryLedgerBody.innerHTML = `
      <tr>
        <td colspan="10" class="empty-cell">No recovery records found for current filter.</td>
      </tr>
    `;
    if (elements.recoveryLedgerCount) elements.recoveryLedgerCount.textContent = '0 records';
    return;
  }

  if (elements.recoveryLedgerCount) {
    elements.recoveryLedgerCount.textContent = `Showing ${ledger.length} of ${formatCount(totalCount)} decisions`;
  }

  elements.recoveryLedgerBody.innerHTML = ledger.map((row) => {
    const verdictBadge = row.decision_type === 'ACT'
      ? '<span class="badge-act">ACT</span>'
      : '<span class="badge-monitor">MONITOR</span>';

    const execClass = row.execution_status === 'SUCCEEDED' ? 'badge-succeeded' :
      (row.execution_status === 'FAILED' ? 'badge-failed' : 'badge-not-dispatched');
    const execBadge = `<span class="${execClass}">${escapeHTML(row.execution_status)}</span>`;

    const outcomeClass = row.observed_outcome === 'CAPTURED' ? 'badge-captured' :
      (row.observed_outcome === 'FAILED' ? 'badge-failed-outcome' : 'badge-not-observed');
    const outcomeBadge = `<span class="${outcomeClass}">${escapeHTML(row.observed_outcome)}</span>`;

    const attrClass = row.attribution_status === 'ATTRIBUTED' ? 'badge-attributed' : 'badge-not-attributed';
    const attrBadge = `<span class="${attrClass}">${escapeHTML(row.attribution_status)}</span>`;

    const decShort = row.decision_id ? row.decision_id.slice(0, 8) : '—';
    const epShort = row.episode_id ? row.episode_id.slice(0, 8) : '—';
    const gmvText = formatINR(row.attributed_protected_gmv_minor_units);

    return `
      <tr>
        <td>
          <div style="font-weight: 600;">${formatDateTime(row.decided_at)}</div>
          <div style="font-size: 0.7rem; color: var(--text-muted); font-family: var(--font-mono);">${decShort}</div>
        </td>
        <td>
          <a href="#" class="payment-link" data-payment-id="${escapeHTML(row.payment_attempt_id)}" style="font-family: var(--font-mono); font-weight: 600; color: var(--blue-text); text-decoration: none;">
            ${escapeHTML(row.payment_attempt_id)}
          </a>
        </td>
        <td>
          ${row.episode_id ? `
            <a href="#" class="episode-link" data-episode-id="${escapeHTML(row.episode_id)}" style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-secondary); text-decoration: none;">
              ${epShort}
            </a>
          ` : '—'}
        </td>
        <td style="font-family: var(--font-mono); font-size: 0.75rem;">${escapeHTML(row.intervention_route || '—')}</td>
        <td>${verdictBadge}</td>
        <td>${execBadge}</td>
        <td>${outcomeBadge}</td>
        <td class="text-right" style="font-weight: 700; font-family: var(--font-mono); color: ${row.attributed_protected_gmv_minor_units > 0 ? 'var(--emerald-text)' : 'inherit'};">
          ${gmvText}
        </td>
        <td>${attrBadge}</td>
        <td class="text-center">
          <button class="btn-inspect-link" data-payment-id="${escapeHTML(row.payment_attempt_id)}" type="button">Audit</button>
        </td>
      </tr>
    `;
  }).join('');

  // Attach click listeners for payment and episode links
  elements.recoveryLedgerBody.querySelectorAll('.payment-link, .btn-inspect-link').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      const pid = el.getAttribute('data-payment-id');
      if (pid) {
        switchToTab('payments');
        selectPayment(pid);
      }
    });
  });

  elements.recoveryLedgerBody.querySelectorAll('.episode-link').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      const eid = el.getAttribute('data-episode-id');
      if (eid) {
        switchToTab('incidents');
        selectIncident(eid);
      }
    });
  });
}

/**
 * Renders the Stage 8 Counterfactual Attribution financial proof ledger.
 */
function renderAttributionLedger(attributions) {
  if (!elements.attributionLedgerBody) return;
  if (!attributions || attributions.length === 0) {
    elements.attributionLedgerBody.innerHTML = `
      <tr>
        <td colspan="11" class="empty-cell">No Stage 8 counterfactual attribution records found.</td>
      </tr>
    `;
    return;
  }

  elements.attributionLedgerBody.innerHTML = attributions.map((a) => {
    const attrShort = a.attribution_id ? a.attribution_id.slice(0, 8) : '—';
    const decShort = a.decision_id ? a.decision_id.slice(0, 8) : '—';
    const p0 = a.counterfactual_failure_probability != null
      ? formatPercentage(a.counterfactual_failure_probability, 1)
      : '—';
    const gmv = formatINR(a.attributed_protected_gmv_minor_units);
    const alpha = formatPercentage(a.attribution_confidence, 1);

    const comp = a.confidence_components || {};
    const cPred = comp.c_prediction != null ? formatPercentage(comp.c_prediction, 0) : '—';
    const cDiag = comp.c_diagnosis != null ? formatPercentage(comp.c_diagnosis, 0) : '—';
    const cTime = comp.c_timing != null ? formatPercentage(comp.c_timing, 0) : '—';

    return `
      <tr>
        <td style="font-family: var(--font-mono); font-size: 0.75rem;">${attrShort}</td>
        <td>
          <a href="#" class="payment-link" data-payment-id="${escapeHTML(a.payment_attempt_id)}" style="font-family: var(--font-mono); font-weight: 600; color: var(--blue-text); text-decoration: none;">
            ${escapeHTML(a.payment_attempt_id)}
          </a>
        </td>
        <td style="font-family: var(--font-mono); font-size: 0.75rem;">${decShort}</td>
        <td><span class="badge-captured">${escapeHTML(a.observed_payment_outcome)}</span></td>
        <td>
          <span style="font-family: var(--font-mono); font-weight: 700; color: var(--rose-text);">${escapeHTML(a.counterfactual_outcome)}</span>
          <span style="font-size: 0.7rem; color: var(--text-muted); font-family: var(--font-mono);">(${p0})</span>
        </td>
        <td class="text-right" style="font-weight: 700; font-family: var(--font-mono); color: var(--emerald-text);">${gmv}</td>
        <td style="font-weight: 700; font-family: var(--font-mono); color: var(--text-primary);">${alpha}</td>
        <td style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted);">${cPred}</td>
        <td style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted);">${cDiag}</td>
        <td style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted);">${cTime}</td>
        <td style="font-size: 0.75rem; color: var(--text-muted);">${formatDateTime(a.attributed_at)}</td>
      </tr>
    `;
  }).join('');

  elements.attributionLedgerBody.querySelectorAll('.payment-link').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      const pid = el.getAttribute('data-payment-id');
      if (pid) {
        switchToTab('payments');
        selectPayment(pid);
      }
    });
  });
}

/**
 * ==========================================================================
 * Phase 7C: Payment Audit Inspector Controller
 * ==========================================================================
 */

/**
 * Loads searchable and filterable payments list.
 */
export async function loadPaymentsData() {
  if (elements.paymentsLoading) elements.paymentsLoading.style.display = 'flex';
  if (elements.paymentsError) elements.paymentsError.style.display = 'none';

  try {
    const res = await getPayments({
      search: state.payments.search,
      filterType: state.payments.filterType,
      status: state.payments.status,
      limit: 50,
      offset: 0,
    });
    state.payments.data = res.items || [];
    state.payments.total = res.total_count || 0;
    renderPayments(res.items || [], res.total_count || 0);
  } catch (error) {
    if (elements.paymentsError) {
      elements.paymentsError.style.display = 'block';
      if (elements.paymentsErrorMsg) elements.paymentsErrorMsg.textContent = error.message || 'Failed to load payments.';
    }
  } finally {
    if (elements.paymentsLoading) elements.paymentsLoading.style.display = 'none';
  }
}

/**
 * Renders the payments list table.
 */
function renderPayments(items, totalCount) {
  if (!elements.paymentsTableBody) return;
  if (!items || items.length === 0) {
    elements.paymentsTableBody.innerHTML = `
      <tr>
        <td colspan="9" class="empty-cell">No payments found matching the current criteria.</td>
      </tr>
    `;
    if (elements.paymentsPaginationInfo) elements.paymentsPaginationInfo.textContent = '0 payments';
    return;
  }

  if (elements.paymentsPaginationInfo) {
    elements.paymentsPaginationInfo.textContent = `Showing ${items.length} of ${formatCount(totalCount)} payments`;
  }

  elements.paymentsTableBody.innerHTML = items.map((p) => {
    const statusClass = p.payment_status === 'captured' ? 'badge-captured' :
      (p.payment_status === 'failed' ? 'badge-failed-outcome' : 'badge-authorized');
    const statusBadge = `<span class="${statusClass}">${escapeHTML(p.payment_status ? p.payment_status.toUpperCase() : 'UNKNOWN')}</span>`;

    const decBadge = p.decision_type === 'ACT'
      ? '<span class="badge-act">ACT</span>'
      : (p.decision_type === 'MONITOR' ? '<span class="badge-monitor">MONITOR</span>' : '<span style="color: var(--text-muted); font-size: 0.75rem;">—</span>');

    const attrBadge = p.is_attributed
      ? '<span class="badge-attributed">ATTRIBUTED</span>'
      : '<span style="color: var(--text-muted); font-size: 0.75rem;">—</span>';

    const gmvText = p.attributed_protected_gmv_minor_units > 0
      ? formatINR(p.attributed_protected_gmv_minor_units)
      : '—';

    const methodStr = p.payment_method ? `${p.payment_method}${p.bank ? ' (' + p.bank + ')' : ''}` : '—';

    return `
      <tr>
        <td>
          <a href="#" class="payment-audit-link" data-payment-id="${escapeHTML(p.payment_id)}" style="font-family: var(--font-mono); font-weight: 600; color: var(--blue-text); text-decoration: none;">
            ${escapeHTML(p.payment_id)}
          </a>
        </td>
        <td style="font-size: 0.75rem; color: var(--text-muted);">${formatDateTime(p.timestamp)}</td>
        <td class="text-right" style="font-weight: 700; font-family: var(--font-mono);">${formatINR(p.amount_minor_units)}</td>
        <td style="font-size: 0.75rem;">${escapeHTML(methodStr)}</td>
        <td>${statusBadge}</td>
        <td>${decBadge}</td>
        <td class="text-right" style="font-weight: 700; font-family: var(--font-mono); color: ${p.attributed_protected_gmv_minor_units > 0 ? 'var(--emerald-text)' : 'inherit'};">${gmvText}</td>
        <td>${attrBadge}</td>
        <td class="text-center">
          <button class="btn-inspect-link btn-audit-payment" data-payment-id="${escapeHTML(p.payment_id)}" type="button">Audit Trace</button>
        </td>
      </tr>
    `;
  }).join('');

  elements.paymentsTableBody.querySelectorAll('.payment-audit-link, .btn-audit-payment').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      const pid = el.getAttribute('data-payment-id');
      if (pid) selectPayment(pid);
    });
  });
}

/**
 * Selects a payment and opens the detailed audit drawer.
 */
export async function selectPayment(paymentId) {
  if (!paymentId) return;
  const cleanPid = String(paymentId).trim();
  state.selectedPaymentId = cleanPid;

  if (elements.paymentAuditDrawer) {
    elements.paymentAuditDrawer.style.display = 'block';
  }
  if (elements.paymentsSplitLayout) {
    elements.paymentsSplitLayout.classList.add('drawer-open');
  }

  if (elements.auditPaymentId) {
    elements.auditPaymentId.textContent = cleanPid;
  }

  if (elements.auditDrawerLoading) elements.auditDrawerLoading.style.display = 'flex';
  if (elements.auditDrawerError) elements.auditDrawerError.style.display = 'none';
  if (elements.auditDrawerDetails) elements.auditDrawerDetails.style.display = 'none';

  try {
    const timeline = await getPaymentTimeline(cleanPid);
    state.paymentAudit.data = timeline;
    renderPaymentAudit(timeline);
    if (elements.auditDrawerDetails) elements.auditDrawerDetails.style.display = 'flex';
  } catch (error) {
    state.paymentAudit.error = error.message;
    if (elements.auditDrawerError) {
      elements.auditDrawerError.style.display = 'block';
      if (elements.auditDrawerErrorMsg) elements.auditDrawerErrorMsg.textContent = error.message || 'Failed to load timeline trace.';
    }
  } finally {
    if (elements.auditDrawerLoading) elements.auditDrawerLoading.style.display = 'none';
  }
}

/**
 * Renders the payment audit drawer facts and chronological stepper.
 */
function renderPaymentAudit(timeline) {
  if (!timeline) return;

  const info = timeline.payment_info || {};
  const pred = timeline.prediction;
  const dec = timeline.decision;
  const cmd = timeline.command;
  const exec = timeline.execution;
  const obs = timeline.observation;
  const attr = timeline.attribution;

  // Section A: Core Payment Facts
  if (elements.auditFactAmount) elements.auditFactAmount.textContent = formatINR(info.amount_minor_units);
  if (elements.auditFactMethod) elements.auditFactMethod.textContent = `${info.payment_method || '—'} / ${info.bank || '—'}`;
  if (elements.auditFactStatus) {
    const st = info.terminal_status || 'UNKNOWN';
    elements.auditFactStatus.textContent = st.toUpperCase();
  }
  if (elements.auditFactTime) {
    elements.auditFactTime.textContent = timeline.events && timeline.events.length > 0
      ? formatDateTime(timeline.events[0].timestamp)
      : '—';
  }

  // Section B: Recovery Governance
  if (elements.auditRecEpisode) {
    if (dec && dec.episode_id) {
      elements.auditRecEpisode.innerHTML = `
        <a href="#" class="audit-episode-jump" data-episode-id="${escapeHTML(dec.episode_id)}" style="color: var(--blue-text); font-family: var(--font-mono); text-decoration: none;">
          ${dec.episode_id.slice(0, 8)} ↗
        </a>
      `;
      elements.auditRecEpisode.querySelector('.audit-episode-jump')?.addEventListener('click', (e) => {
        e.preventDefault();
        switchToTab('incidents');
        selectIncident(dec.episode_id);
      });
    } else {
      elements.auditRecEpisode.textContent = 'None';
    }
  }

  if (elements.auditRecProb) {
    if (!pred) {
      elements.auditRecProb.textContent = 'Not Evaluated';
    } else if (pred.failure_probability != null) {
      elements.auditRecProb.textContent = formatPercentage(pred.failure_probability, 1);
    } else if (pred.status === 'INSUFFICIENT_DATA') {
      elements.auditRecProb.textContent = 'Insufficient Data';
    } else {
      elements.auditRecProb.textContent = 'N/A';
    }
  }
  if (elements.auditRecDecision) {
    elements.auditRecDecision.textContent = dec ? `${dec.type} (${dec.gate_verdict || 'PASSED'})` : 'No Decision';
  }
  if (elements.auditRecRoute) {
    elements.auditRecRoute.textContent = dec?.route || '—';
  }
  if (elements.auditRecGmv) {
    elements.auditRecGmv.textContent = attr ? formatINR(attr.protected_gmv) : '₹0.00';
  }
  if (elements.auditRecConfidence) {
    elements.auditRecConfidence.textContent = attr?.confidence != null ? formatPercentage(attr.confidence, 1) : '—';
  }

  // Section C: Chronological Stepper (Stages 1–8)
  if (!elements.auditTimelineStepper) return;

  const steps = [];

  // Stage 1: Ingestion
  if (timeline.events && timeline.events.length > 0) {
    const authEvent = timeline.events[0];
    steps.push({
      num: 1,
      name: 'Stage 1: Authorization Ingestion',
      status: 'completed',
      badge: 'INGESTED',
      badgeClass: 'badge-succeeded',
      time: authEvent.timestamp,
      details: [
        `Event: ${authEvent.event_type}`,
        `Amount: ${formatINR(authEvent.amount_minor_units)}`,
        `Initial Status: ${authEvent.status}`,
      ]
    });
  }

  // Stage 5: Prediction
  if (pred) {
    const isInsufficient = pred.status === 'INSUFFICIENT_DATA';
    steps.push({
      num: 5,
      name: 'Stage 5: Failure Prediction',
      status: isInsufficient ? 'skipped' : 'completed',
      badge: pred.status,
      badgeClass: isInsufficient ? 'badge-not-dispatched' : 'badge-succeeded',
      time: pred.predicted_at,
      details: [
        `Prediction ID: ${pred.prediction_id ? pred.prediction_id.slice(0, 8) : '—'}`,
        `Failure Probability: ${formatPercentage(pred.failure_probability, 1)}`,
        `Risk Band: ${pred.risk_band || 'N/A'}`,
      ]
    });
  } else {
    steps.push({
      num: 5,
      name: 'Stage 5: Failure Prediction',
      status: 'skipped',
      badge: 'NOT_EVALUATED',
      badgeClass: 'badge-not-dispatched',
      time: null,
      details: ['Payment occurred outside of active degradation window.']
    });
  }

  // Stage 6: Decision
  if (dec) {
    const isAct = dec.type === 'ACT';
    steps.push({
      num: 6,
      name: 'Stage 6: Intervention Decision',
      status: 'completed',
      badge: dec.type,
      badgeClass: isAct ? 'badge-act' : 'badge-monitor',
      time: dec.decided_at,
      details: [
        `Decision ID: ${dec.decision_id ? dec.decision_id.slice(0, 8) : '—'}`,
        `Verdict: ${dec.type}`,
        `Gate Status: ${dec.gate_verdict}`,
        `Selected Route: ${dec.route || 'None'}`,
      ]
    });
  } else {
    steps.push({
      num: 6,
      name: 'Stage 6: Intervention Decision',
      status: 'skipped',
      badge: 'NO_DECISION',
      badgeClass: 'badge-not-dispatched',
      time: null,
      details: ['No intervention policy evaluated for this payment.']
    });
  }

  // Stage 7: Command & Execution
  if (cmd || exec) {
    const execStatus = exec?.status || cmd?.status || 'DISPATCHED';
    const isSuccess = execStatus === 'SUCCESS' || execStatus === 'SUCCEEDED';
    steps.push({
      num: 7,
      name: 'Stage 7: Intervention Execution',
      status: isSuccess ? 'completed' : 'active',
      badge: execStatus,
      badgeClass: isSuccess ? 'badge-succeeded' : 'badge-dispatched',
      time: exec?.executed_at || cmd?.created_at,
      details: [
        `Command ID: ${cmd?.command_id ? cmd.command_id.slice(0, 8) : '—'}`,
        `Attempt ID: ${exec?.attempt_id ? exec.attempt_id.slice(0, 8) : '—'}`,
        `Route Key: ${cmd?.route || '—'}`,
        exec?.duration_ms != null ? `Execution Latency: ${exec.duration_ms}ms` : null,
      ].filter(Boolean)
    });
  } else if (dec && dec.type === 'MONITOR') {
    steps.push({
      num: 7,
      name: 'Stage 7: Intervention Execution',
      status: 'skipped',
      badge: 'MONITOR_ONLY',
      badgeClass: 'badge-not-dispatched',
      time: null,
      details: ['MONITOR policy verdict: no active intervention command dispatched.']
    });
  } else {
    steps.push({
      num: 7,
      name: 'Stage 7: Intervention Execution',
      status: 'skipped',
      badge: 'SKIPPED',
      badgeClass: 'badge-not-dispatched',
      time: null,
      details: ['No execution command created.']
    });
  }

  // Terminal Outcome Observation
  if (obs || (timeline.events && timeline.events.length > 1)) {
    const finalOutcome = obs?.outcome || info.terminal_status || 'UNKNOWN';
    const isCaptured = finalOutcome.toUpperCase() === 'CAPTURED';
    steps.push({
      num: 'Outcome',
      name: 'Terminal Outcome Observation',
      status: isCaptured ? 'completed' : 'active',
      badge: finalOutcome.toUpperCase(),
      badgeClass: isCaptured ? 'badge-captured' : 'badge-failed-outcome',
      time: obs?.observed_at || (timeline.events && timeline.events.length > 1 ? timeline.events[timeline.events.length - 1].timestamp : null),
      details: [
        obs?.observation_id ? `Observation ID: ${obs.observation_id.slice(0, 8)}` : null,
        `Observed Outcome: ${finalOutcome.toUpperCase()}`,
        `Terminal Event: ${timeline.events ? timeline.events[timeline.events.length - 1].event_type : '—'}`,
      ].filter(Boolean)
    });
  }

  // Stage 8: Attribution
  if (attr) {
    const isAttributed = attr.status === 'ATTRIBUTED';
    steps.push({
      num: 8,
      name: 'Stage 8: Counterfactual Attribution',
      status: isAttributed ? 'completed' : 'active',
      badge: attr.status,
      badgeClass: isAttributed ? 'badge-attributed' : 'badge-not-attributed',
      time: attr.attributed_at,
      details: [
        `Attribution ID: ${attr.attribution_id ? attr.attribution_id.slice(0, 8) : '—'}`,
        `Counterfactual: ${attr.counterfactual_outcome || 'WOULD_HAVE_FAILED'}`,
        `Protected GMV: ${formatINR(attr.protected_gmv)}`,
        attr.confidence != null ? `Attribution Confidence: ${formatPercentage(attr.confidence, 1)}` : null,
      ].filter(Boolean)
    });
  } else if (dec && dec.type === 'ACT') {
    steps.push({
      num: 8,
      name: 'Stage 8: Counterfactual Attribution',
      status: 'skipped',
      badge: 'NOT_ATTRIBUTED',
      badgeClass: 'badge-not-attributed',
      time: null,
      details: ['Intervention did not yield verified counterfactual protected GMV.']
    });
  } else {
    steps.push({
      num: 8,
      name: 'Stage 8: Counterfactual Attribution',
      status: 'skipped',
      badge: 'NOT_APPLICABLE',
      badgeClass: 'badge-not-dispatched',
      time: null,
      details: ['No intervention executed; attribution pipeline not engaged.']
    });
  }

  // Render stepper HTML
  elements.auditTimelineStepper.innerHTML = steps.map((step) => `
    <div class="audit-step-card ${step.status}">
      <div class="audit-step-dot"></div>
      <div class="audit-step-header">
        <span class="audit-step-name">${escapeHTML(step.name)}</span>
        <span class="audit-step-badge ${step.badgeClass}">${escapeHTML(step.badge)}</span>
      </div>
      ${step.time ? `<div class="audit-step-time">${formatDateTime(step.time)}</div>` : ''}
      <div class="audit-step-details">
        ${step.details.map((d) => `<div>${escapeHTML(d)}</div>`).join('')}
      </div>
    </div>
  `).join('');
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
