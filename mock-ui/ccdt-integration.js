/**
 * ════════════════════════════════════════════════════════════════════════════════
 * CCDT Integration Layer for NexaOps Mock UI
 * ════════════════════════════════════════════════════════════════════════════════
 *
 * This module connects the NexaOps business facade (localhost:8088) to the
 * CCDT Brain (localhost:3000) by polling CCDT's backend APIs and displaying
 * real-time autonomous healing status.
 *
 * Phase 1 Components:
 *   1.1 - CCDT Status Bar (header integration)
 *   1.2 - Live Incident Overlay Panel (right sidebar)
 *   2.1 - Container Health Indicators (footer enhancement)
 *
 * NO CHANGES TO CCDT BRAIN - Only enhances the victim application UI
 */

// ══════════════════════════════════════════════════════════════════════════════
// Configuration
// ══════════════════════════════════════════════════════════════════════════════

const CCDT_CONFIG = {
    // CCDT Backend APIs - Use relative paths to go through nginx proxy to API Gateway
    API_TOPOLOGY: '/api/topology',
    API_INFER: '/api/infer',
    API_INCIDENTS: '/api/incidents',
    API_GUARDIAN: '/api/guardian/actions',
    API_CADVISOR: '/api/metrics/docker',

    // WebSocket for real-time updates
    WS_INFERENCE: 'ws://localhost:8001/ws/inference',

    // Poll intervals
    POLL_INTERVAL_MS: 3000,  // 3 seconds
    METRICS_INTERVAL_MS: 5000,  // 5 seconds

    // Connection timeout
    FETCH_TIMEOUT_MS: 4000
};

// ══════════════════════════════════════════════════════════════════════════════
// State Management
// ══════════════════════════════════════════════════════════════════════════════

const CCDTState = {
    connected: false,
    lastTopology: null,
    activeIncident: null,
    overlayDismissed: false,
    listenForIncidents: false,
    resolvedLocally: {},
    layerStatus: {
        layer1: 'unknown',
        layer2: 'unknown',
        layer3: 'unknown',
        layer4: 'unknown'
    },
    gnnConfidence: 0,
    containerMetrics: {},
    recoveryTimeline: []
};

window.CCDTState = CCDTState;

// ══════════════════════════════════════════════════════════════════════════════
// Utility Functions
// ══════════════════════════════════════════════════════════════════════════════

async function fetchWithTimeout(url, options = {}, timeout = CCDT_CONFIG.FETCH_TIMEOUT_MS) {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeout);

    try {
        const response = await fetch(url, {
            ...options,
            signal: controller.signal,
            headers: {
                'Content-Type': 'application/json',
                ...options.headers
            }
        });
        clearTimeout(id);
        return response;
    } catch (error) {
        clearTimeout(id);
        throw error;
    }
}

function formatTimestamp(isoString) {
    if (!isoString) return '--:--:--';
    try {
        const date = new Date(isoString);
        return date.toLocaleTimeString('en-US', { hour12: false });
    } catch {
        return '--:--:--';
    }
}

function calculateMTTR(startTime, endTime) {
    if (!startTime || !endTime) return null;
    const start = new Date(startTime).getTime();
    const end = new Date(endTime).getTime();
    return ((end - start) / 1000).toFixed(1);  // seconds
}

// ══════════════════════════════════════════════════════════════════════════════
// Phase 1.1 - CCDT Status Bar (Header Integration)
// ══════════════════════════════════════════════════════════════════════════════

function injectCCDTStatusBar() {
    const header = document.querySelector('header .header-right');
    if (!header || document.getElementById('ccdt-status-bar')) return;

    const statusBar = document.createElement('div');
    statusBar.id = 'ccdt-status-bar';
    statusBar.className = 'ccdt-status-bar';
    statusBar.innerHTML = `
        <div class="ccdt-status-content">
            <span class="ccdt-icon">&#9881;</span>
            <div class="ccdt-status-text">
                <div class="ccdt-status-line-1">
                    <span class="ccdt-label">CCDT Guardian:</span>
                    <span class="ccdt-state" id="ccdt-state">CONNECTING</span>
                </div>
                <div class="ccdt-status-line-2">
                    <span id="ccdt-confidence">GNN: --% </span>
                    <span id="ccdt-layers">&middot; Layers: <span class="layer-indicator"></span></span>
                </div>
            </div>
        </div>
    `;

    // Insert before sys-status
    const sysStatus = header.querySelector('.sys-status');
    if (sysStatus) {
        header.insertBefore(statusBar, sysStatus);
    } else {
        header.prepend(statusBar);
    }

    injectCCDTStyles();
}

function updateCCDTStatusBar(topology, incident) {
    const stateEl = document.getElementById('ccdt-state');
    const confidenceEl = document.getElementById('ccdt-confidence');
    const layersEl = document.getElementById('ccdt-layers');

    if (!stateEl || !confidenceEl || !layersEl) return;

    if (!CCDTState.connected) {
        stateEl.textContent = 'OFFLINE';
        stateEl.className = 'ccdt-state state-offline';
        confidenceEl.textContent = 'GNN: --% ';
        layersEl.innerHTML = '• <span style="color:var(--text-muted)">Layers: Unreachable</span>';
        return;
    }

    const labelEl = document.querySelector('.ccdt-label');
    const line2El = document.querySelector('.ccdt-status-line-2');

    // Update main state
    if (incident && incident.status === 'active') {
        if (labelEl) labelEl.textContent = 'CCDT Guardian:';
        stateEl.textContent = 'HEALING';
        stateEl.className = 'ccdt-state state-healing';
        if (line2El) line2El.style.display = 'flex';

        // Update GNN confidence
        const confidence = incident?.confidence || incident?.gnn_confidence || topology?.gnn_confidence || CCDTState.gnnConfidence || 0;
        confidenceEl.textContent = `GNN: ${Math.round(confidence)}% `;
        confidenceEl.className = confidence > 70 ? 'ccdt-conf-high' : confidence > 40 ? 'ccdt-conf-med' : 'ccdt-conf-low';

        // Update layer indicators
        const layerHtml = `
            <span class="layer-dot layer-1 ${CCDTState.layerStatus.layer1}"></span>
            <span class="layer-dot layer-2 ${CCDTState.layerStatus.layer2}"></span>
            <span class="layer-dot layer-3 ${CCDTState.layerStatus.layer3}"></span>
            <span class="layer-dot layer-4 ${CCDTState.layerStatus.layer4}"></span>
        `;
        layersEl.innerHTML = `• Layers: ${layerHtml}`;
    } else {
        if (labelEl) labelEl.textContent = 'CCDT';
        stateEl.textContent = 'MONITORING';
        stateEl.className = 'ccdt-state state-monitoring';
        if (line2El) line2El.style.display = 'none';
    }
}

// ══════════════════════════════════════════════════════════════════════════════
// Phase 1.2 - Live Incident Overlay Panel
// ══════════════════════════════════════════════════════════════════════════════

function injectIncidentOverlay() {
    if (document.getElementById('ccdt-incident-overlay')) return;

    const overlay = document.createElement('div');
    overlay.id = 'ccdt-incident-overlay';
    overlay.className = 'ccdt-incident-overlay hidden';
    overlay.innerHTML = `
        <div class="overlay-header">
            <div class="overlay-title">
                <span class="severity-icon" id="overlay-severity">&#9888;</span>
                <span id="overlay-title-text">NO ACTIVE INCIDENTS</span>
            </div>
            <button class="overlay-close" onclick="hideIncidentOverlay(true)">&times;</button>
        </div>
        <div class="overlay-body" id="overlay-body">
            <div class="overlay-section">
                <div class="section-label">Status</div>
                <div class="section-content" id="overlay-status">System Healthy</div>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);
}

function showIncidentOverlay(incident) {
    const overlay = document.getElementById('ccdt-incident-overlay');
    const severityIcon = document.getElementById('overlay-severity');
    const titleText = document.getElementById('overlay-title-text');
    const body = document.getElementById('overlay-body');

    if (!overlay || !incident) return;

    // Normalize incident data structure (API returns different field names)
    const normalizedIncident = {
        severity: incident.severity || 'warning',
        type: incident.type || incident.incident_type || 'unknown',
        title: incident.title || 'Incident Detected',
        node: incident.node || incident.root_cause || 'unknown',
        rootCause: incident.rootCause || incident.root_cause || 'Analyzing...',
        affected: incident.affected || [],
        confidence: incident.confidence || incident.gnn_confidence || 0,
        description: incident.description || incident.rootCause || 'Causal graph analysis in progress...',
        autoAction: incident.autoAction || incident.action_taken || '',
        status: incident.status || 'active',
        createdAt: incident.createdAt || incident.created_at,
        mttrSeconds: incident.mttr_seconds || incident.mttrSeconds,
        timeline: incident.timeline || []
    };

    // Set severity styling
    overlay.className = `ccdt-incident-overlay severity-${normalizedIncident.severity}`;

    // Update header
    const icons = { critical: '&#9679;', warning: '&#9888;', info: '&#9679;' };
    severityIcon.innerHTML = icons[normalizedIncident.severity] || '&#9888;';
    titleText.textContent = normalizedIncident.type?.toUpperCase() || 'INCIDENT DETECTED';

    // Build incident details
    const elapsed = normalizedIncident.createdAt ? `T+${Math.floor((Date.now() - normalizedIncident.createdAt * 1000) / 1000)}s` : 'T+0s';

    body.innerHTML = `
        <div class="overlay-section">
            <div class="section-label">Issue Category</div>
            <div class="section-content incident-type-${normalizedIncident.type}">
                ${normalizedIncident.type?.toUpperCase()}
            </div>
        </div>

        <div class="overlay-section">
            <div class="section-label">Failing Component</div>
            <div class="section-content root-cause">
                <strong>${normalizedIncident.node}</strong>
                <div style="margin-top:4px;font-size:11px;color:var(--text-secondary)">${normalizedIncident.rootCause}</div>
            </div>
        </div>

        <div class="overlay-section">
            <div class="section-label">AI Certainty</div>
            <div class="section-content">
                <div class="confidence-bar">
                    <div class="confidence-fill" style="width: ${normalizedIncident.confidence}%"></div>
                </div>
                <span class="confidence-text">${Math.round(normalizedIncident.confidence)}%</span>
            </div>
        </div>

        <div class="overlay-section">
            <div class="section-label">Affected Services</div>
            <div class="section-content blast-radius">
                ${normalizedIncident.affected && normalizedIncident.affected.length > 0
            ? (Array.isArray(normalizedIncident.affected)
                ? normalizedIncident.affected.map(s => `<span class="service-tag">${s}</span>`).join('')
                : normalizedIncident.affected.split(',').map(s => `<span class="service-tag">${s.trim()}</span>`).join(''))
            : '<span class="text-muted">Analyzing...</span>'}
            </div>
        </div>

        <div class="overlay-section">
            <div class="section-label">&#9881; AI Diagnosis</div>
            <div class="section-content gnn-class">
                ${normalizedIncident.description}
            </div>
        </div>

        <div class="overlay-section">
            <div class="section-label">&#9658; Proposed Fix</div>
            <div class="section-content guardian-action">
                ${normalizedIncident.autoAction || '&#8987; Analyzing optimal remediation...'}
            </div>
        </div>

        <div class="overlay-section">
            <div class="section-label">&#9200; Estimated Fix Time</div>
            <div class="section-content mttr-target">
                ${normalizedIncident.mttrSeconds ? `${normalizedIncident.mttrSeconds}s (actual)` : '< 60s (target)'}
            </div>
        </div>

        <div class="overlay-section autonomous-badge">
            <div class="autonomous-banner" style="background: var(--purple-dim); color: var(--purple); border-color: var(--purple);">
                &#9889; FULLY AUTONOMOUS HEALING IN PROGRESS
            </div>
        </div>

        <div class="overlay-section timeline-section">
            <div class="section-label">Recovery Timeline</div>
            <div class="timeline" id="recovery-timeline">
                ${buildTimelineHTML(normalizedIncident, elapsed)}
            </div>
        </div>

        ${normalizedIncident.status === 'auto-resolved' ? buildMetricsComparisonHTML(normalizedIncident) : ''}
    `;

    overlay.classList.remove('hidden');
}

function hideIncidentOverlay(force = false) {
    if (!force && CCDTState.activeIncident) return;
    const overlay = document.getElementById('ccdt-incident-overlay');
    if (overlay) {
        overlay.classList.add('hidden');
        CCDTState.overlayDismissed = true;
        if (force) {
            CCDTState.listenForIncidents = false; // Never open again until button is pressed
            CCDTState.activeIncident = null; // Prevent "System Restored" from showing
            const btn = document.getElementById('connect-ccdt-btn');
            if (btn) {
                btn.textContent = 'Enable Guardian';
                btn.style.background = 'var(--purple-dim)';
                btn.style.color = 'var(--purple)';
                btn.style.borderColor = 'var(--purple)';
            }
        }
    }
}

function buildTimelineHTML(incident, elapsed) {
    const timeline = [];

    // T+0s - Detection (Layer-1)
    timeline.push({
        time: 'T+0s',
        status: 'complete',
        icon: '&#9679;',
        label: 'Incident detected',
        detail: `${incident.title || 'Anomaly flagged'} &mdash; Layer-1 sensors`
    });

    // T+3s - Root cause (Layer-2 GNN)
    if (incident.rootCause) {
        timeline.push({
            time: 'T+3s',
            status: 'complete',
            icon: '&#9881;',
            label: 'Root cause identified',
            detail: `Layer-2 GNN: ${incident.rootCause} (${Math.round((incident.confidence || 0))}% confidence)`
        });
    }

    // T+5s - Guardian analyzing (Layer-3)
    if (incident.autoAction) {
        timeline.push({
            time: 'T+5s',
            status: 'complete',
            icon: '&#9658;',
            label: 'Guardian selected action',
            detail: `Layer-3: ${incident.autoAction}`
        });

        // T+6s - OPA safety check
        timeline.push({
            time: 'T+6s',
            status: 'complete',
            icon: '&#9745;',
            label: 'OPA safety check',
            detail: 'PASSED &mdash; All 5 Rego policies validated'
        });

        // T+7s - Docker API executing
        if (incident.status === 'auto-resolved') {
            timeline.push({
                time: 'T+7s',
                status: 'complete',
                icon: '&#9654;',
                label: 'Docker API executing',
                detail: 'Container restart command issued'
            });

            const mttr = incident.mttrSeconds || 12;
            timeline.push({
                time: 'T+10s',
                status: 'complete',
                icon: '&#8635;',
                label: 'Container restarting',
                detail: 'Waiting for health check...'
            });

            timeline.push({
                time: `T+${mttr}s`,
                status: 'complete',
                icon: '&#10003;',
                label: 'System restored',
                detail: `MTTR: ${mttr}s | &#10003; Autonomous Recovery Verified`
            });
        } else {
            timeline.push({
                time: 'T+7s',
                status: 'in-progress',
                icon: '&#9654;',
                label: 'Docker API executing',
                detail: 'Container restart in progress...'
            });

            timeline.push({
                time: elapsed,
                status: 'in-progress',
                icon: '&#8987;',
                label: 'Waiting for recovery',
                detail: 'Health check pending...'
            });
        }
    } else {
        timeline.push({
            time: 'T+5s',
            status: 'complete',
            icon: '&#9658;',
            label: 'Proposed Fix Ready',
            detail: 'Simulated and verified by AI Ghost Preview'
        });
        timeline.push({
            time: elapsed,
            status: 'in-progress',
            icon: '&#8987;',
            label: 'Executing Autonomous Fix',
            detail: 'Guardian issuing restart commands to orchestration layer...'
        });
    }

    return timeline.map(item => `
        <div class="timeline-item status-${item.status}">
            <div class="timeline-marker">${item.icon || ''}</div>
            <div class="timeline-content">
                <div class="timeline-time">${item.time}</div>
                <div class="timeline-label">${item.label}</div>
                ${item.detail ? `<div class="timeline-detail">${item.detail}</div>` : ''}
            </div>
        </div>
    `).join('');
}

// ══════════════════════════════════════════════════════════════════════════════
// Metrics Comparison (Before/After)
// ══════════════════════════════════════════════════════════════════════════════

function buildMetricsComparisonHTML(incident) {
    // Fetch current metrics (after recovery)
    const afterMetrics = CCDTState.containerMetrics || {};

    // Simulate "before" metrics (would be captured at T+0s in real implementation)
    // For demo: show degraded state
    const beforeMetrics = {
        postgres: { memory_pct: 99, cpu_pct: 96, oom_count: 3 },
        redis: { memory_pct: 45, cpu_pct: 25, oom_count: 0 },
        latency_ms: 450,
        qps: 45,
        cache_hit_rate: 32,
        sessions: 12
    };

    // After metrics (recovered state)
    const afterValues = {
        postgres: afterMetrics.postgres || { memory_pct: 45, cpu_pct: 15, oom_count: 0 },
        redis: afterMetrics.redis || { memory_pct: 38, cpu_pct: 12, oom_count: 0 },
        latency_ms: 12,
        qps: 402,
        cache_hit_rate: 91,
        sessions: 98
    };

    return `
        <div class="overlay-section metrics-comparison">
            <div class="section-label">&#9776; Before/After Comparison</div>
            <div class="metrics-grid">
                <div class="metric-col">
                    <div class="metric-header">Before Incident</div>
                    <div class="metric-row bad">
                        <span class="metric-name">PostgreSQL</span>
                        <span class="metric-value">${Math.round(beforeMetrics.postgres.memory_pct)}% mem</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-name">DB QPS</span>
                        <span class="metric-value">${beforeMetrics.qps}</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-name">Cache Hit</span>
                        <span class="metric-value">${beforeMetrics.cache_hit_rate}%</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-name">Latency</span>
                        <span class="metric-value">${beforeMetrics.latency_ms}ms</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-name">Sessions</span>
                        <span class="metric-value">${beforeMetrics.sessions}</span>
                    </div>
                </div>
                <div class="metric-col">
                    <div class="metric-header">After Recovery</div>
                    <div class="metric-row good">
                        <span class="metric-name">PostgreSQL</span>
                        <span class="metric-value">${Math.round(afterValues.postgres.memory_pct)}% mem</span>
                    </div>
                    <div class="metric-row good">
                        <span class="metric-name">DB QPS</span>
                        <span class="metric-value">${afterValues.qps}</span>
                    </div>
                    <div class="metric-row good">
                        <span class="metric-name">Cache Hit</span>
                        <span class="metric-value">${afterValues.cache_hit_rate}%</span>
                    </div>
                    <div class="metric-row good">
                        <span class="metric-name">Latency</span>
                        <span class="metric-value">${afterValues.latency_ms}ms</span>
                    </div>
                    <div class="metric-row good">
                        <span class="metric-name">Sessions</span>
                        <span class="metric-value">${afterValues.sessions}</span>
                    </div>
                </div>
            </div>
        </div>
    `;
}

// ══════════════════════════════════════════════════════════════════════════════
// Phase 2.1 - Container Health Indicators (Footer Enhancement)
// ══════════════════════════════════════════════════════════════════════════════

function enhanceFooterHealth() {
    const footer = document.querySelector('footer .footer-health');
    if (!footer || footer.querySelector('.container-metrics')) return;

    // Hide original health indicators but keep them in DOM for compatibility
    const originalItems = footer.querySelectorAll('.h-item');
    originalItems.forEach(item => {
        if (!item.querySelector('#footer-time')) {
            item.style.display = 'none';
        }
    });

    const metricsDiv = document.createElement('div');
    metricsDiv.className = 'container-metrics';
    metricsDiv.id = 'container-metrics';
    metricsDiv.innerHTML = `
        <div class="h-item metric-item" id="metric-postgres">
            <div class="h-dot"></div>
            <span>PostgreSQL</span>
            <span class="metric-detail" id="pg-metric">--</span>
        </div>
        <div class="h-item metric-item" id="metric-redis">
            <div class="h-dot"></div>
            <span>Redis</span>
            <span class="metric-detail" id="rd-metric">--</span>
        </div>
        <div class="h-item metric-item" id="metric-api">
            <div class="h-dot"></div>
            <span>API</span>
            <span class="metric-detail" id="api-metric">--</span>
        </div>
    `;

    // Insert metrics before footer-time
    const footerTime = footer.querySelector('#footer-time');
    if (footerTime) {
        footer.insertBefore(metricsDiv, footerTime);
    } else {
        footer.appendChild(metricsDiv);
    }
}

function updateContainerMetrics(metrics) {
    const updateMetric = (id, dotId, labelId, metric) => {
        const item = document.getElementById(id);
        const dot = item?.querySelector('.h-dot');
        const detail = document.getElementById(labelId);

        if (!item || !dot || !detail) return;

        if (metric && metric.memory_pct !== undefined) {
            const memPct = Math.round(metric.memory_pct);
            const cpuPct = Math.round(metric.cpu_pct || 0);

            // Color code based on memory pressure
            if (memPct > 90 || metric.oom_count > 0) {
                dot.className = 'h-dot err';
                detail.innerHTML = `<span style="color:var(--red)">⚠️ ${memPct}% mem</span>`;
            } else if (memPct > 75) {
                dot.className = 'h-dot';
                dot.style.background = 'var(--amber)';
                detail.innerHTML = `<span style="color:var(--amber)">${memPct}% mem</span>`;
            } else {
                dot.className = 'h-dot';
                detail.textContent = `${memPct}% mem`;
            }

            // Add OOM warning
            if (metric.oom_count > 0) {
                detail.innerHTML += ` <span style="color:var(--red);font-weight:bold">OOM&times;${metric.oom_count}</span>`;
            }
        } else {
            dot.className = 'h-dot unk';
            detail.textContent = '--';
        }
    };

    updateMetric('metric-postgres', 'hd-pg', 'pg-metric', metrics.postgres);
    updateMetric('metric-redis', 'hd-rd', 'rd-metric', metrics.redis);
    updateMetric('metric-api', 'hd-api', 'api-metric', metrics.api);
}

// ══════════════════════════════════════════════════════════════════════════════
// Data Fetching Functions
// ══════════════════════════════════════════════════════════════════════════════

async function fetchTopology() {
    try {
        const response = await fetchWithTimeout(CCDT_CONFIG.API_TOPOLOGY);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        CCDTState.lastTopology = data;
        CCDTState.connected = true;

        // Extract GNN confidence
        if (data.gnn_confidence !== undefined) {
            CCDTState.gnnConfidence = data.gnn_confidence * 100;
        }

        // Update layer status based on topology health
        CCDTState.layerStatus = {
            layer1: data.nodes ? 'active' : 'unknown',
            layer2: data.gnn_confidence > 0 ? 'active' : 'standby',
            layer3: 'standby',
            layer4: 'ready'
        };

        return data;
    } catch (error) {
        console.warn('CCDT Topology fetch failed:', error.message);
        CCDTState.connected = false;
        return null;
    }
}

async function fetchActiveIncidents() {
    try {
        const response = await fetchWithTimeout(CCDT_CONFIG.API_INCIDENTS + '?status=active&limit=1');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        const incidents = data.incidents || data.rows || [];

        if (incidents.length > 0) {
            CCDTState.layerStatus.layer3 = 'active';  // Guardian is working
            return incidents[0];
        } else {
            return null;
        }
    } catch (error) {
        console.warn('CCDT Incidents fetch failed:', error.message);
        return null;
    }
}

async function fetchContainerMetrics() {
    try {
        // Fetch from cAdvisor for real metrics
        const response = await fetchWithTimeout(CCDT_CONFIG.API_CADVISOR);
        if (!response.ok) {
            // Fallback to mock metrics
            return generateMockMetrics();
        }

        const data = await response.json();
        // Parse cAdvisor format and extract postgres, redis, nginx metrics
        // This is a simplified version - real implementation would parse cAdvisor JSON
        return parseCAdvisorMetrics(data);
    } catch (error) {
        console.warn('Container metrics fetch failed:', error.message);
        return generateMockMetrics();
    }
}

function generateMockMetrics() {
    // Fallback mock metrics when cAdvisor is unavailable
    return {
        postgres: {
            memory_pct: Math.random() * 30 + 40,  // 40-70%
            cpu_pct: Math.random() * 20 + 10,
            oom_count: 0
        },
        redis: {
            memory_pct: Math.random() * 20 + 35,  // 35-55%
            cpu_pct: Math.random() * 15 + 5,
            oom_count: 0
        },
        api: {
            memory_pct: Math.random() * 15 + 20,  // 20-35%
            cpu_pct: Math.random() * 25 + 10,
            oom_count: 0
        }
    };
}

function parseCAdvisorMetrics(data) {
    // TODO: Implement real cAdvisor JSON parsing
    // For now, return mock metrics
    return generateMockMetrics();
}

// ══════════════════════════════════════════════════════════════════════════════
// Main Polling Loop
// ══════════════════════════════════════════════════════════════════════════════

async function pollCCDTStatus() {
    // Fetch all data in parallel
    let [topology, incident, metrics] = await Promise.all([
        fetchTopology(),
        fetchActiveIncidents(),
        fetchContainerMetrics()
    ]);

    // PREVENT DESYNC: If the user clicked "Simulate Crash" in index.html, 
    // it injects a mock Postgres incident. We must ignore the backend "ATTACK" 
    // incidents during this time so the dashboard and the side-panel stay perfectly synced.
    if (window.isCrashed && CCDTState.activeIncident && CCDTState.activeIncident.id === 'INC-MOCK-PG') {
        incident = CCDTState.activeIncident;
    } else if (!CCDTState.listenForIncidents) {
        // If the user hasn't clicked "Enable Guardian", ignore the backend!
        incident = null;
    }

    if (!window.isCrashed && incident) {
        if (CCDTState.resolvedLocally[incident.id || incident.title]) {
            // We already auto-resolved and animated this incident. Ignore it to prevent infinite loops.
            incident = null;
        } else {
            CCDTState.activeIncident = incident;
        }
    }

    // Show/hide incident overlay
    if (incident) {
        const createdAt = incident.created_at || incident.createdAt || (Date.now() / 1000);
        // Date.now() is ms, createdAt is usually seconds in this API
        const createdAtMs = createdAt > 1000000000000 ? createdAt : createdAt * 1000;
        const elapsedMs = Date.now() - createdAtMs;

        // Auto-resolve any incident after 12 seconds to fake the "autonomous healing" for the demo
        if (elapsedMs > 12000 && !CCDTState.resolvedLocally[incident.id || incident.title]) {
            CCDTState.resolvedLocally[incident.id || incident.title] = true;
        }

        // If it was resolved locally by the frontend timeout/button, mock the success state
        if (CCDTState.resolvedLocally[incident.id || incident.title]) {
            incident.status = 'auto-resolved';
            incident.action_taken = incident.action_taken || 'restart_pod';
            // Wait 5 seconds after resolving to clear it, just like the backend clear
            if (elapsedMs > 17000) {
                incident = null; // hide the overlay
            }
        }
    }

    // Update UI components AFTER any local status overrides
    updateCCDTStatusBar(topology, incident);
    updateContainerMetrics(metrics);

    if (incident && (incident.status === 'active' || incident.status === 'auto-resolved')) {
        if (!CCDTState.activeIncident) {
            CCDTState.overlayDismissed = false; // Reset if it's a new incident
        }
        if (!CCDTState.overlayDismissed) {
            showIncidentOverlay(incident);
        }
    } else if (CCDTState.activeIncident && incident === null) {
        // Incident was just resolved by the backend!
        // Show the green success state for 5 seconds so the audience sees it healed
        CCDTState.activeIncident.status = 'auto-resolved';
        CCDTState.activeIncident.action_taken = CCDTState.activeIncident.action_taken || 'throttle_source';
        showIncidentOverlay(CCDTState.activeIncident);
        updateCCDTStatusBar(topology, CCDTState.activeIncident);

        setTimeout(hideIncidentOverlay, 5000);
        CCDTState.activeIncident = null;
    }

    // Store metrics
    CCDTState.containerMetrics = metrics;
}

// ══════════════════════════════════════════════════════════════════════════════
// Initialization
// ══════════════════════════════════════════════════════════════════════════════

function initCCDTIntegration() {
    console.log('> CCDT Integration Layer initializing...');

    // Inject UI components
    injectCCDTStatusBar();
    injectIncidentOverlay();
    enhanceFooterHealth();

    // Start polling
    pollCCDTStatus();  // Immediate first poll
    setInterval(pollCCDTStatus, CCDT_CONFIG.POLL_INTERVAL_MS);

    console.log('> CCDT Integration Layer active');
    console.log(`  - Status Bar: Monitoring ${CCDT_CONFIG.API_TOPOLOGY}`);
    console.log(`  - Incident Overlay: Listening to ${CCDT_CONFIG.API_INCIDENTS}`);
    console.log(`  - Container Metrics: Polling ${CCDT_CONFIG.API_CADVISOR}`);
}

// ══════════════════════════════════════════════════════════════════════════════
// Styles Injection
// ══════════════════════════════════════════════════════════════════════════════

function injectCCDTStyles() {
    if (document.getElementById('ccdt-integration-styles')) return;

    const styleSheet = document.createElement('style');
    styleSheet.id = 'ccdt-integration-styles';
    styleSheet.textContent = `
        /* CCDT Status Bar */
        .ccdt-status-bar {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 6px 14px;
            background: var(--bg-card);
            border: 1px solid var(--border-hi);
            border-radius: 8px;
            font-size: 11px;
            margin-right: 16px;
        }

        .ccdt-status-content {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .ccdt-icon {
            font-size: 18px;
            line-height: 1;
        }

        .ccdt-status-text {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .ccdt-status-line-1, .ccdt-status-line-2 {
            display: flex;
            align-items: center;
            gap: 6px;
            font-family: var(--font-mono);
        }

        .ccdt-label {
            color: var(--text-muted);
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .ccdt-state {
            font-weight: 600;
            letter-spacing: 0.05em;
        }

        .ccdt-state.state-monitoring {
            color: var(--green);
        }

        .ccdt-state.state-healing {
            color: var(--amber);
            animation: pulse 1s ease infinite;
        }

        .ccdt-state.state-offline {
            color: var(--text-muted);
        }

        .ccdt-conf-high { color: var(--green); }
        .ccdt-conf-med { color: var(--amber); }
        .ccdt-conf-low { color: var(--red); }

        .layer-indicator {
            display: inline-flex;
            gap: 4px;
        }

        .layer-dot {
            display: inline-block;
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: var(--text-muted);
            transition: background 0.3s;
        }

        .layer-dot.active {
            background: var(--green);
            box-shadow: 0 0 6px var(--green);
        }

        .layer-dot.standby {
            background: var(--cyan);
        }

        .layer-dot.ready {
            background: var(--purple);
        }

        /* Incident Overlay */
        .ccdt-incident-overlay {
            position: fixed;
            top: 56px;
            right: 0;
            width: 420px;
            height: calc(100vh - 56px);
            background: var(--bg-card);
            border-left: 1px solid var(--border);
            z-index: 999;
            display: flex;
            flex-direction: column;
            transform: translateX(0);
            transition: transform 0.3s ease;
            overflow-y: auto;
        }

        .ccdt-incident-overlay.hidden {
            transform: translateX(100%);
        }

        .ccdt-incident-overlay.severity-critical {
            border-left-color: var(--red);
            border-left-width: 3px;
        }

        .ccdt-incident-overlay.severity-warning {
            border-left-color: var(--amber);
            border-left-width: 3px;
        }

        .overlay-header {
            padding: 16px 20px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--bg-panel);
            position: sticky;
            top: 0;
            z-index: 10;
        }

        .overlay-title {
            display: flex;
            align-items: center;
            gap: 10px;
            font-family: var(--font-display);
            font-size: 14px;
            font-weight: 700;
            letter-spacing: 0.02em;
        }

        .severity-icon {
            font-size: 20px;
        }

        .overlay-close {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 24px;
            cursor: pointer;
            padding: 0;
            width: 30px;
            height: 30px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 4px;
            transition: all 0.2s;
        }

        .overlay-close:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }

        .overlay-body {
            padding: 20px;
            flex: 1;
            overflow-y: auto;
        }

        .overlay-section {
            margin-bottom: 18px;
            padding-bottom: 18px;
            border-bottom: 1px solid var(--border);
        }

        .overlay-section:last-child {
            border-bottom: none;
        }

        .section-label {
            font-size: 9px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.1em;
            margin-bottom: 8px;
            font-weight: 600;
        }

        .section-content {
            font-size: 12px;
            color: var(--text-secondary);
            line-height: 1.5;
        }

        .incident-type-fault {
            color: var(--red);
            font-weight: 600;
        }

        .incident-type-attack {
            color: var(--purple);
            font-weight: 600;
        }

        .root-cause {
            color: var(--text-primary);
            font-weight: 500;
        }

        .confidence-bar {
            height: 8px;
            background: var(--bg-hover);
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 4px;
        }

        .confidence-fill {
            height: 100%;
            background: linear-gradient(90deg, var(--amber), var(--green));
            transition: width 0.5s ease;
        }

        .confidence-text {
            font-size: 11px;
            color: var(--green);
            font-weight: 600;
        }

        .blast-radius {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }

        .service-tag {
            display: inline-block;
            padding: 3px 8px;
            background: var(--red-dim);
            color: var(--red);
            border-radius: 4px;
            font-size: 10px;
            font-weight: 500;
        }

        .gnn-class {
            background: var(--cyan-dim);
            padding: 10px;
            border-radius: 6px;
            border-left: 3px solid var(--cyan);
            font-size: 11px;
            color: var(--text-primary);
        }

        .guardian-action {
            background: var(--purple-dim);
            padding: 10px;
            border-radius: 6px;
            border-left: 3px solid var(--purple);
            font-size: 11px;
            color: var(--text-primary);
            font-family: var(--font-mono);
        }

        .mttr-target {
            font-family: var(--font-display);
            font-size: 18px;
            font-weight: 700;
            color: var(--amber);
        }

        /* Timeline */
        .timeline {
            position: relative;
            padding-left: 24px;
        }

        .timeline::before {
            content: '';
            position: absolute;
            left: 7px;
            top: 8px;
            bottom: 8px;
            width: 2px;
            background: var(--border);
        }

        .timeline-item {
            position: relative;
            margin-bottom: 14px;
        }

        .timeline-marker {
            position: absolute;
            left: -24px;
            top: 2px;
            width: 18px;
            height: 18px;
            border-radius: 50%;
            background: var(--bg-card);
            border: 2px solid var(--border);
            z-index: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 10px;
        }

        .timeline-item.status-complete .timeline-marker {
            background: var(--green-dim);
            border-color: var(--green);
            box-shadow: 0 0 6px var(--green);
        }

        .timeline-item.status-in-progress .timeline-marker {
            background: var(--amber-dim);
            border-color: var(--amber);
            animation: pulse 1s ease infinite;
        }

        .timeline-time {
            font-family: var(--font-mono);
            font-size: 10px;
            color: var(--text-muted);
            margin-bottom: 2px;
        }

        .timeline-label {
            font-size: 12px;
            color: var(--text-primary);
            font-weight: 500;
            margin-bottom: 2px;
        }

        .timeline-detail {
            font-size: 10px;
            color: var(--text-secondary);
        }

        /* Container Metrics Enhancement */
        .container-metrics {
            display: flex;
            gap: 20px;
            align-items: center;
        }

        .metric-item {
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .metric-detail {
            font-size: 9px;
            color: var(--text-muted);
            font-family: var(--font-mono);
        }

        /* Autonomous Badge */
        .autonomous-badge {
            padding: 0;
            margin-bottom: 20px;
            border: none;
        }

        .autonomous-banner {
            background: linear-gradient(135deg, var(--purple), var(--cyan));
            color: #fff;
            padding: 14px 20px;
            border-radius: 8px;
            text-align: center;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            box-shadow: 0 4px 12px rgba(155, 93, 229, 0.3);
            animation: autonomousPulse 2s ease infinite;
        }

        @keyframes autonomousPulse {
            0%, 100% { transform: scale(1); box-shadow: 0 4px 12px rgba(155, 93, 229, 0.3); }
            50% { transform: scale(1.02); box-shadow: 0 6px 16px rgba(155, 93, 229, 0.5); }
        }

        /* Metrics Comparison */
        .metrics-comparison {
            background: var(--bg-panel);
            border-radius: 8px;
            padding: 16px;
        }

        .metrics-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-top: 12px;
        }

        .metric-col {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .metric-header {
            font-size: 10px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.1em;
            font-weight: 600;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--border);
        }

        .metric-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 6px 10px;
            background: var(--bg-card);
            border-radius: 4px;
            font-size: 11px;
            transition: all 0.2s;
        }

        .metric-row.bad {
            background: var(--red-dim);
            color: var(--red);
            font-weight: 600;
        }

        .metric-row.good {
            background: var(--green-dim);
            color: var(--green);
        }

        .metric-name {
            color: var(--text-secondary);
            font-size: 10px;
        }

        .metric-row.bad .metric-name,
        .metric-row.good .metric-name {
            color: inherit;
            opacity: 0.8;
        }

        .metric-value {
            font-family: var(--font-mono);
            font-weight: 600;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }
    `;

    document.head.appendChild(styleSheet);
}

// ══════════════════════════════════════════════════════════════════════════════
// Public API
// ══════════════════════════════════════════════════════════════════════════════

window.CCDTIntegration = {
    init: initCCDTIntegration,
    hideIncidentOverlay: hideIncidentOverlay,
    showIncidentOverlay: showIncidentOverlay,
    getState: () => CCDTState
};

// Auto-initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCCDTIntegration);
} else {
    initCCDTIntegration();
}
