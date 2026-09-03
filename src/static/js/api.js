/**
 * Centralized API Client Module — Phase 7A
 * 
 * Interacts exclusively with read-only inspection endpoints.
 * Handles minor-unit currency conversion and standard formatting.
 */

const BASE_URL = typeof window !== 'undefined' && window.location ? window.location.origin : 'http://127.0.0.1:8000';

/**
 * Base fetch helper with timeout and standardized error handling.
 */
async function fetchJSON(endpoint, options = {}) {
  const url = `${BASE_URL}${endpoint}`;
  try {
    const response = await fetch(url, {
      headers: {
        'Accept': 'application/json',
        ...options.headers,
      },
      ...options,
    });

    if (!response.ok) {
      let detail = `HTTP ${response.status} ${response.statusText}`;
      try {
        const errorData = await response.json();
        if (errorData.detail) {
          detail = typeof errorData.detail === 'string' 
            ? errorData.detail 
            : JSON.stringify(errorData.detail);
        }
      } catch {
        // Fallback to generic status text if response is not JSON
      }
      throw new Error(detail);
    }

    return await response.json();
  } catch (error) {
    console.error(`API Error on ${endpoint}:`, error);
    throw error;
  }
}

/**
 * Queries the database-aware /health endpoint.
 * Returns { status: "ok" } on 200, throws on 503/failure.
 */
export async function getHealth() {
  return await fetchJSON('/health');
}

/**
 * Queries the executive summary metrics from /api/v1/summary.
 */
export async function getSummary() {
  return await fetchJSON('/api/v1/summary');
}

/**
 * Queries degradation episodes from /api/v1/episodes.
 * @param {number} limit 
 */
export async function getEpisodes(limit = 10) {
  const boundedLimit = Math.max(1, Math.min(100, limit));
  return await fetchJSON(`/api/v1/episodes?limit=${boundedLimit}`);
}

/**
 * Queries root cause analysis and candidate causes for an episode from /api/v1/rca/{episode_id}.
 * @param {string} episodeId
 */
export async function getRCA(episodeId) {
  if (!episodeId) {
    throw new Error('Episode ID is required');
  }
  return await fetchJSON(`/api/v1/rca/${encodeURIComponent(episodeId)}`);
}

/**
 * Formats integer minor units to INR currency string.
 * Example: 1041823 -> "₹10,418.23"
 * @param {number|null|undefined} minorUnits 
 */
export function formatINR(minorUnits) {
  if (minorUnits === null || minorUnits === undefined || isNaN(minorUnits)) {
    return '₹0.00';
  }
  const units = Number(minorUnits) / 100.0;
  return units.toLocaleString('en-IN', {
    style: 'currency',
    currency: 'INR',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * Formats a decimal ratio to a percentage string.
 * Example: 0.688 -> "68.8%", 0.6 -> "60.0%"
 * @param {number|null|undefined} ratio 
 * @param {number} decimals 
 */
export function formatPercentage(ratio, decimals = 1) {
  if (ratio === null || ratio === undefined || isNaN(ratio)) {
    return '0.0%';
  }
  const pct = Number(ratio) * 100;
  return `${pct.toFixed(decimals)}%`;
}

/**
 * Formats integer count with locale grouping.
 * Example: 3582 -> "3,582"
 * @param {number|null|undefined} count 
 */
export function formatCount(count) {
  if (count === null || count === undefined || isNaN(count)) {
    return '0';
  }
  return Number(count).toLocaleString('en-IN');
}

/**
 * Formats an ISO datetime string to a human-readable string.
 * @param {string|null|undefined} isoString 
 */
export function formatDateTime(isoString) {
  if (!isoString) return '—';
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    return d.toLocaleString('en-IN', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }) + ' UTC';
  } catch {
    return isoString;
  }
}
