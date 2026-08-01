// APEX AI Quant Trader Co-Pilot Client

// sessionStorage limits a stolen token to this browser session. The API still
// performs the authoritative subscription check for every premium request.
const accessToken = () => sessionStorage.getItem('atc_access_token') || '';
const originalFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const url = typeof input === 'string' ? input : input.url;
  if (url.startsWith('/auth/')) return originalFetch(input, init);
  const headers = new Headers(init.headers || (typeof input !== 'string' ? input.headers : undefined));
  const token = accessToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return originalFetch(input, { ...init, headers });
};

let tokenRefreshTimer = null;

function tokenExpiresWithin(token, seconds) {
  try {
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    return !Number.isFinite(payload.exp) || payload.exp <= Math.floor(Date.now() / 1000) + seconds;
  } catch (_) {
    return true;
  }
}

async function ensureFreshAccessToken() {
  const current = accessToken();
  if (current && !tokenExpiresWithin(current, 60)) return current;
  const response = await originalFetch('/auth/refresh', { method: 'POST' });
  if (!response.ok) {
    sessionStorage.removeItem('atc_access_token');
    window.location.reload();
    throw new Error('Your session expired. Please sign in again.');
  }
  const data = await response.json();
  sessionStorage.setItem('atc_access_token', data.access_token);
  scheduleAccessTokenRefresh(data.access_token);
  window.dispatchEvent(new CustomEvent('atc:token-refreshed', { detail: { token: data.access_token } }));
  return data.access_token;
}

function scheduleAccessTokenRefresh(token) {
  if (tokenRefreshTimer) clearTimeout(tokenRefreshTimer);
  try {
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    const delay = Math.max(5_000, payload.exp * 1000 - Date.now() - 60_000);
    tokenRefreshTimer = setTimeout(() => { ensureFreshAccessToken().catch(() => { }); }, delay);
  } catch (_) {
    ensureFreshAccessToken().catch(() => { });
  }
}

window.addEventListener('atc:authenticated', (event) => scheduleAccessTokenRefresh(event.detail.token));
window.addEventListener('atc:token-refreshed', (event) => scheduleAccessTokenRefresh(event.detail.token));
if (window.atcAuthenticated && accessToken()) scheduleAccessTokenRefresh(accessToken());

// State Variables
let socket = null;
let lastPrice = null;
let activeRetailStop = null;
let activeSmartStop = null;
let activeRetailTarget = null;
let activeSmartTarget = null;
let activeRetailRisk = null;
let activeSmartRisk = null;
let activeHistoricalStats = null;
let activeScannerInterval = null;
let lastMonitorEventKey = null;
let activeStreamSymbol = null;
let activeStreamTimeframe = null;
let activeNewsScope = 'token';
let lastReceivedNews = null;
let isMacroAlertDismissed = false;
let loaderInterval = null;
let membershipCapacityPoll = null;

const membershipCapacityTrigger = document.getElementById('membership-capacity-trigger');
const membershipCardLayer = document.getElementById('membership-card-layer');
const membershipCardClose = document.getElementById('membership-card-close');
const membershipCardScrim = document.getElementById('membership-card-scrim');
const membershipCardPlan = document.getElementById('membership-card-plan');
const membershipCardStatus = document.getElementById('membership-card-status');
const membershipCardExpiry = document.getElementById('membership-card-expiry-value');
const membershipCardCapacity = document.getElementById('membership-card-capacity-value');
const membershipCardCapacityFill = document.getElementById('membership-card-capacity-fill');
const membershipCardCapacityCopy = document.getElementById('membership-card-capacity-copy');
const membershipCardSlotList = document.getElementById('membership-card-slot-list');

const membershipPlanLabels = {
  monthly: 'Monthly analyst access',
  quarterly: 'Quarterly analyst access',
  half_yearly: 'Half-yearly analyst access',
  annual: 'Annual analyst access',
};

function closeMembershipCapacityCard() {
  if (!membershipCardLayer) return;
  membershipCardLayer.hidden = true;
  membershipCapacityTrigger?.setAttribute('aria-expanded', 'false');
  if (membershipCapacityPoll) clearInterval(membershipCapacityPoll);
  membershipCapacityPoll = null;
}

function renderMembershipCapacity(data) {
  const limit = Number(data?.limit || 0);
  const active = Number(data?.active_slots || 0);
  const endsAt = data?.plan_ends_at ? new Date(data.plan_ends_at) : null;
  const expiry = endsAt && !Number.isNaN(endsAt.getTime())
    ? endsAt.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' })
    : 'Managed locally';
  if (membershipCardPlan) membershipCardPlan.textContent = membershipPlanLabels[data?.plan_code] || 'Research membership';
  if (membershipCardStatus) membershipCardStatus.textContent = String(data?.status || 'active').replace(/_/g, ' ').toUpperCase();
  if (membershipCardExpiry) membershipCardExpiry.textContent = expiry;
  if (membershipCardCapacity) membershipCardCapacity.textContent = `${active} / ${limit}`;
  if (membershipCardCapacityFill) membershipCardCapacityFill.style.width = `${limit ? Math.min(100, (active / limit) * 100) : 0}%`;
  if (membershipCardCapacityCopy) membershipCardCapacityCopy.textContent = limit
    ? `${active === limit ? 'All available slots are in use.' : `${limit - active} live research ${limit - active === 1 ? 'slot' : 'slots'} available.`}`
    : 'Research capacity is unavailable.';
  if (!membershipCardSlotList) return;
  const slots = Array.isArray(data?.slots) ? data.slots : [];
  membershipCardSlotList.replaceChildren();
  if (!slots.length) {
    const empty = document.createElement('p');
    empty.className = 'membership-card-empty';
    empty.textContent = 'No live research currently running.';
    membershipCardSlotList.append(empty);
    return;
  }
  slots.slice(0, 2).forEach((slot) => {
    const row = document.createElement('div');
    row.className = 'membership-card-slot';
    const symbol = document.createElement('b');
    symbol.textContent = slot.symbol || 'RESEARCH';
    const timeframe = document.createElement('span');
    timeframe.textContent = slot.timeframe || 'LIVE';
    const channel = document.createElement('small');
    channel.textContent = slot.channel === 'rest' ? 'ONE-SHOT' : 'LIVE';
    row.append(symbol, timeframe, channel);
    membershipCardSlotList.append(row);
  });
  if (slots.length > 2) {
    const overflow = document.createElement('p');
    overflow.className = 'membership-card-empty';
    overflow.textContent = `+${slots.length - 2} more active research open ${slots.length - 2 === 1 ? 'slot' : 'slots'}`;
    membershipCardSlotList.append(overflow);
  }
}

async function loadMembershipCapacity() {
  if (!membershipCardLayer) return;
  try {
    const response = await fetch('/research/capacity');
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not load membership capacity.');
    renderMembershipCapacity(data);
  } catch (error) {
    if (membershipCardPlan) membershipCardPlan.textContent = 'Membership status unavailable';
    if (membershipCardStatus) membershipCardStatus.textContent = 'RETRY';
    if (membershipCardCapacityCopy) membershipCardCapacityCopy.textContent = error.message || 'Could not load capacity.';
  }
}

membershipCapacityTrigger?.addEventListener('click', async () => {
  const isOpen = !membershipCardLayer?.hidden;
  if (isOpen) return closeMembershipCapacityCard();
  membershipCardLayer.hidden = false;
  membershipCapacityTrigger.setAttribute('aria-expanded', 'true');
  await loadMembershipCapacity();
  membershipCapacityPoll = setInterval(loadMembershipCapacity, 20_000);
});
membershipCardClose?.addEventListener('click', closeMembershipCapacityCard);
membershipCardScrim?.addEventListener('click', closeMembershipCapacityCard);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !membershipCardLayer?.hidden) closeMembershipCapacityCard();
});

// DOM Elements
const configForm = document.getElementById('config-form');
const symbolInput = document.getElementById('symbol-input');
const timeframeSelect = document.getElementById('timeframe-select');
const aiToggle = document.getElementById('ai-toggle');
const connectBtn = document.getElementById('connect-btn');
const wsStatus = document.getElementById('ws-status');
const controlsCard = document.querySelector('.controls-card');
const councilProcessingTicker = document.getElementById('council-processing-ticker');
const appToastRegion = document.getElementById('app-toast-region');

window.showAppToast = function showAppToast(message, tone = 'info', duration = 4200) {
  if (!appToastRegion || !message) return;
  const toast = document.createElement('div');
  toast.className = `app-toast app-toast-${tone}`;
  const dot = document.createElement('span');
  dot.className = 'app-toast-dot';
  const copy = document.createElement('span');
  copy.className = 'app-toast-copy';
  copy.textContent = message;
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'app-toast-close';
  close.setAttribute('aria-label', 'Dismiss notification');
  close.textContent = '×';
  const dismiss = () => {
    toast.classList.remove('is-visible');
    window.setTimeout(() => toast.remove(), 220);
  };
  close.addEventListener('click', dismiss);
  toast.append(dot, copy, close);
  appToastRegion.append(toast);
  window.requestAnimationFrame(() => toast.classList.add('is-visible'));
  window.setTimeout(dismiss, duration);
};

function animateModalIn(modal) {
  if (!modal) return;
  modal.classList.remove('motion-open');
  window.requestAnimationFrame(() => modal.classList.add('motion-open'));
}

// Per-user OpenRouter connection. The browser only ever receives a masked key
// hint; the actual key is posted on save and is never rendered back.
let activeAiConnection = null;

function aiConnectionElements() {
  return {
    modal: document.getElementById('ai-connection-modal'),
    headerButton: document.getElementById('open-ai-connection-btn'),
    modelBadge: document.getElementById('ai-connection-model'),
    statusCopy: document.getElementById('ai-connection-status-copy'),
    saved: document.getElementById('ai-connection-saved'),
    keyHint: document.getElementById('ai-connection-key-hint'),
    savedModel: document.getElementById('ai-connection-saved-model'),
    keyInput: document.getElementById('ai-connection-api-key'),
    modelInput: document.getElementById('ai-connection-model-input'),
    message: document.getElementById('ai-connection-form-message'),
    saveButton: document.getElementById('save-ai-connection-btn'),
    removeButton: document.getElementById('remove-ai-connection-btn'),
  };
}

async function aiConnectionResponseError(response) {
  try {
    const data = await response.json();
    return data.detail || 'Could not update the AI connection.';
  } catch (_) {
    return 'Could not update the AI connection.';
  }
}

function renderAiConnection(connection) {
  activeAiConnection = connection || { connected: false };
  const elements = aiConnectionElements();
  const connected = Boolean(activeAiConnection.connected);
  elements.headerButton?.classList.toggle('is-connected', connected);
  if (elements.modelBadge) elements.modelBadge.textContent = connected ? (activeAiConnection.model || 'Connected') : 'Set key';
  if (elements.statusCopy) elements.statusCopy.textContent = connected
    ? `Connected with ${activeAiConnection.model}. You can rotate the key or change the model below.`
    : 'Connect your own key and choose the model you want to use for AI synthesis.';
  if (elements.saved) elements.saved.hidden = !connected;
  if (elements.keyHint) elements.keyHint.textContent = activeAiConnection.key_hint || '••••';
  if (elements.savedModel) elements.savedModel.textContent = activeAiConnection.model || '—';
  if (elements.modelInput && (connected || !elements.modelInput.value)) elements.modelInput.value = activeAiConnection.model || 'openrouter/free';
  if (elements.removeButton) elements.removeButton.hidden = !connected;
}

async function loadAiConnection({ quiet = false } = {}) {
  const response = await fetch('/ai-connection');
  if (!response.ok) {
    if (!quiet) throw new Error(await aiConnectionResponseError(response));
    return null;
  }
  const connection = await response.json();
  renderAiConnection(connection);
  return connection;
}

window.openAiConnectionModal = async function openAiConnectionModal() {
  const elements = aiConnectionElements();
  if (!elements.modal) return;
  elements.modal.style.display = 'flex';
  animateModalIn(elements.modal);
  if (elements.message) elements.message.textContent = '';
  if (elements.keyInput) elements.keyInput.value = '';
  try {
    await loadAiConnection();
  } catch (error) {
    if (elements.statusCopy) elements.statusCopy.textContent = error.message || 'Sign in to manage your AI connection.';
    if (elements.message) elements.message.textContent = error.message || 'Could not load your connection.';
  }
};

window.closeAiConnectionModal = function closeAiConnectionModal() {
  const modal = document.getElementById('ai-connection-modal');
  if (modal) { modal.classList.remove('motion-open'); modal.style.display = 'none'; }
};

window.saveAiConnection = async function saveAiConnection(event) {
  event.preventDefault();
  const elements = aiConnectionElements();
  const model = elements.modelInput?.value.trim() || '';
  const apiKey = elements.keyInput?.value || '';
  if (!model) {
    if (elements.message) elements.message.textContent = 'Enter the OpenRouter model ID you want to use.';
    return;
  }
  if (elements.message) { elements.message.textContent = ''; elements.message.classList.remove('is-success'); }
  if (elements.saveButton) { elements.saveButton.disabled = true; elements.saveButton.textContent = 'Saving…'; }
  try {
    const response = await fetch('/ai-connection/openrouter', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ api_key: apiKey, model }),
    });
    if (!response.ok) throw new Error(await aiConnectionResponseError(response));
    const connection = await response.json();
    renderAiConnection(connection);
    if (elements.keyInput) elements.keyInput.value = '';
    if (elements.message) { elements.message.textContent = 'Connection saved. Your next AI analysis will use this model.'; elements.message.classList.add('is-success'); }
    window.showAppToast?.('AI connection saved. Future analysis will use your selected model.', 'success');
  } catch (error) {
    if (elements.message) elements.message.textContent = error.message || 'Could not save the AI connection.';
  } finally {
    if (elements.saveButton) { elements.saveButton.disabled = false; elements.saveButton.textContent = 'Save connection'; }
  }
};

window.removeAiConnection = async function removeAiConnection() {
  if (!activeAiConnection?.connected || !window.confirm('Remove this saved OpenRouter key? AI synthesis will pause until you add another key.')) return;
  const elements = aiConnectionElements();
  if (elements.removeButton) { elements.removeButton.disabled = true; elements.removeButton.textContent = 'Removing…'; }
  try {
    const response = await fetch('/ai-connection/openrouter', { method: 'DELETE' });
    if (!response.ok) throw new Error(await aiConnectionResponseError(response));
    renderAiConnection({ connected: false });
    if (elements.keyInput) elements.keyInput.value = '';
    if (elements.message) { elements.message.textContent = 'Saved key removed.'; elements.message.classList.add('is-success'); }
    window.showAppToast?.('Saved AI connection removed.', 'info');
  } catch (error) {
    if (elements.message) elements.message.textContent = error.message || 'Could not remove the AI connection.';
  } finally {
    if (elements.removeButton) { elements.removeButton.disabled = false; elements.removeButton.textContent = 'Remove key'; }
  }
};

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') window.closeAiConnectionModal?.();
});

function startAiConnectionControls() {
  loadAiConnection({ quiet: true }).catch(() => { });
  if (new URLSearchParams(window.location.search).get('open') === 'ai-connection') {
    window.openAiConnectionModal?.();
  }
}
if (window.atcAuthenticated) startAiConnectionControls();
window.addEventListener('atc:authenticated', startAiConnectionControls);

// Macro Alert & RAG Stats Elements
const macroAlertBanner = document.getElementById('macro-alert-banner');
const macroAlertText = document.getElementById('macro-alert-text');
const closeMacroAlertBtn = document.getElementById('close-macro-alert');
const historicalStatsDisplay = document.getElementById('historical-stats-display');
const historicalStatsText = document.getElementById('historical-stats-text');
let macroAlertDismissTimer = null;

function dismissMacroAlert() {
  window.clearTimeout(macroAlertDismissTimer);
  macroAlertDismissTimer = null;
  isMacroAlertDismissed = true;
  macroAlertBanner?.classList.add('hidden');
}

function showMacroAlert(message) {
  if (!macroAlertBanner || isMacroAlertDismissed) return;
  const wasHidden = macroAlertBanner.classList.contains('hidden');
  macroAlertText.textContent = message;
  macroAlertBanner.classList.remove('hidden');
  if (wasHidden) {
    window.clearTimeout(macroAlertDismissTimer);
    macroAlertDismissTimer = window.setTimeout(dismissMacroAlert, 10_000);
  }
}

if (closeMacroAlertBtn) {
  closeMacroAlertBtn.addEventListener('click', () => {
    dismissMacroAlert();
  });
}

// Ticker Elements
const priceVal = document.getElementById('price-val');
const changeVal = document.getElementById('change-val');
const volumeVal = document.getElementById('volume-val');
const spreadVal = document.getElementById('spread-val');

// Signal Confirmation Evidence
const confirmStatusBadge = document.getElementById('confirmation-status-badge');
const confirmPlaybookVal = document.getElementById('confirmation-playbook-val');
const confirmPhaseVal = document.getElementById('confirmation-phase-val');
const confirmBosVal = document.getElementById('confirm-bos-val');
const confirmBreakoutVal = document.getElementById('confirm-breakout-val');
const confirmSweepVal = document.getElementById('confirm-sweep-val');
const confirmRvolVal = document.getElementById('confirm-rvol-val');
const confirmVwapVal = document.getElementById('confirm-vwap-val');
const confirmProfileVal = document.getElementById('confirm-profile-val');
const confirmCandleVal = document.getElementById('confirm-candle-val');
const confirmChochVal = document.getElementById('confirm-choch-val');
const confirmExecStrip = document.getElementById('confirmation-exec-strip');
const tapeEvidenceStatus = document.getElementById('tape-evidence-status');
const tapeVerdictValue = document.getElementById('tape-verdict-value');
const tapeAggressorValue = document.getElementById('tape-aggressor-value');
const tapeBuyValue = document.getElementById('tape-buy-value');
const tapeSellValue = document.getElementById('tape-sell-value');
const tapeDeltaValue = document.getElementById('tape-delta-value');
const tapeResponseValue = document.getElementById('tape-response-value');
const tapeAbsorptionValue = document.getElementById('tape-absorption-value');
const tapeExhaustionValue = document.getElementById('tape-exhaustion-value');
const tapeAlignmentValue = document.getElementById('tape-alignment-value');
const tapeLiquidationValue = document.getElementById('tape-liquidation-value');
const tapeConfidenceValue = document.getElementById('tape-confidence-value');
const tapeCapacityValue = document.getElementById('tape-capacity-value');
const tapeEvidenceNote = document.getElementById('tape-evidence-note');

// Decision Elements
const decisionCard = document.getElementById('decision-card-container');
const decisionVal = document.getElementById('decision-val');
const gradeVal = document.getElementById('grade-val');

// Confidence Indicators (Pie/Radar)
const radarBullish = document.getElementById('radar-bullish');
const radarBearish = document.getElementById('radar-bearish');
const radarUncertain = document.getElementById('radar-uncertain');
const radarBullishVal = document.getElementById('radar-bullish-val');
const radarBearishVal = document.getElementById('radar-bearish-val');
const radarUncertainVal = document.getElementById('radar-uncertain-val');

// Sizing & Risk Elements
const riskSide = document.getElementById('risk-side');
const riskEntry = document.getElementById('risk-entry');
const riskStop = document.getElementById('risk-stop');
const riskTarget = document.getElementById('risk-target');
const accSizeInput = document.getElementById('acc-size-input');
const riskPctInput = document.getElementById('risk-pct-input');
const riskAmtVal = document.getElementById('risk-amt-val');
const sizingUnitsVal = document.getElementById('sizing-units-val');
const stopMethodRetail = document.getElementById('stop-method-retail');
const stopMethodSmart = document.getElementById('stop-method-smart');
const wallPriceDisplay = document.getElementById('wall-price-display');

// Kelly Sizing
const kellyToggle = document.getElementById('kelly-toggle');
const kellyRecBox = document.getElementById('kelly-rec-box');
const kellyRecVal = document.getElementById('kelly-rec-val');
const kellyStatusText = document.getElementById('kelly-status-text');

// Economic Calendar Container & News
const calendarListContainer = document.getElementById('calendar-list-container');
const newsContainer = document.getElementById('news-list-container');
const logBox = document.getElementById('log-output-box');

// Signal Monitor elements
const monitorCard = document.getElementById('signal-monitor-card');
const monitorStatusVal = document.getElementById('monitor-status-val');
const monitorActionVal = document.getElementById('monitor-action-val');
const monitorReasonVal = document.getElementById('monitor-reason-val');
const monitorIdVal = document.getElementById('monitor-id-val');
const monitorSideVal = document.getElementById('monitor-side-val');
const monitorPriceVal = document.getElementById('monitor-price-val');
const monitorEntryVal = document.getElementById('monitor-entry-val');
const monitorEntryPriceVal = document.getElementById('monitor-entry-price-val');
const monitorStopVal = document.getElementById('monitor-stop-val');
const monitorEvidenceHero = document.getElementById('monitor-evidence-hero');
const monitorEvidenceState = document.getElementById('monitor-evidence-state');
const monitorEvidenceTitle = document.getElementById('monitor-evidence-title');
const monitorEvidenceDetail = document.getElementById('monitor-evidence-detail');
const monitorEvidenceMark = document.getElementById('monitor-evidence-mark');
const monitorMissingEvidence = document.getElementById('monitor-missing-evidence');
const monitorMissingTitle = document.getElementById('monitor-missing-title');
const monitorMissingDetail = document.getElementById('monitor-missing-detail');
const monitorConfirmationScenarios = document.getElementById('monitor-confirmation-scenarios');
const monitorMarketStory = document.getElementById('monitor-market-story');
const monitorStoryState = document.getElementById('monitor-story-state');
const monitorStoryDirection = document.getElementById('monitor-story-direction');
const monitorStoryEvent = document.getElementById('monitor-story-event');
const monitorStoryAge = document.getElementById('monitor-story-age');
const monitorStoryLevel = document.getElementById('monitor-story-level');
const monitorStoryInvalidation = document.getElementById('monitor-story-invalidation');
const monitorStoryHappened = document.getElementById('monitor-story-happened');
const monitorStoryNow = document.getElementById('monitor-story-now');
const monitorStoryNext = document.getElementById('monitor-story-next');
const monitorStoryFailure = document.getElementById('monitor-story-failure');
const monitorStoryAction = document.getElementById('monitor-story-action');
const monitorGateStrip = document.getElementById('monitor-gate-strip');
const monitorTradeLifecycle = document.getElementById('monitor-trade-lifecycle');
const monitorWatchPlan = document.getElementById('monitor-watch-plan');
const monitorWatchStepOneTitle = document.getElementById('monitor-watch-step-one-title');
const monitorWatchStepOneDetail = document.getElementById('monitor-watch-step-one-detail');
const monitorWatchStepTwoTitle = document.getElementById('monitor-watch-step-two-title');
const monitorWatchStepTwoDetail = document.getElementById('monitor-watch-step-two-detail');
const monitorTargetMilestones = document.getElementById('monitor-target-milestones');
const monitorJourneyProgressVal = document.getElementById('monitor-journey-progress-val');
const monitorJourneyProgressFill = document.getElementById('monitor-journey-progress-fill');
const monitorTp1Val = document.getElementById('monitor-tp1-val');
const monitorTp2Val = document.getElementById('monitor-tp2-val');
const monitorTp3Val = document.getElementById('monitor-tp3-val');
const monitorRunnerVal = document.getElementById('monitor-runner-val');
const monitorEventsList = document.getElementById('monitor-events-list');
const dismissSignalBtn = document.getElementById('dismiss-signal-btn');
const signalHistoryCount = document.getElementById('signal-history-count');
const signalHistoryList = document.getElementById('signal-history-list');
const historyWinsVal = document.getElementById('history-wins-val');
const historyLossesVal = document.getElementById('history-losses-val');
const historyOpenVal = document.getElementById('history-open-val');

// Exact Setup Details
const setupStatusVal = document.getElementById('setup-status-val');
const setupReasonVal = document.getElementById('setup-reason-val');
const setupSideVal = document.getElementById('setup-side-val');
const setupEntryVal = document.getElementById('setup-entry-val');
const setupStopVal = document.getElementById('setup-stop-val');
const setupRiskVal = document.getElementById('setup-risk-val');
const setupTp1Val = document.getElementById('setup-tp1-val');
const setupTp2Val = document.getElementById('setup-tp2-val');
const setupTp3Val = document.getElementById('setup-tp3-val');
const setupRunnerVal = document.getElementById('setup-runner-val');
const setupLeverageVal = document.getElementById('setup-leverage-val');
const leverageTableContainer = document.getElementById('leverage-table-container');
const setupLedgerCard = document.getElementById('setup-ledger-card');
const setupTypeVal = document.getElementById('setup-type-val');
const setupEvidenceHero = document.getElementById('setup-evidence-hero');
const setupEvidenceState = document.getElementById('setup-evidence-state');
const setupEvidenceTitle = document.getElementById('setup-evidence-title');
const setupEvidenceMark = document.getElementById('setup-evidence-mark');
const setupEntrySourceVal = document.getElementById('setup-entry-source-val');
const setupStopSourceVal = document.getElementById('setup-stop-source-val');
const setupObjectiveVal = document.getElementById('setup-objective-val');
const setupAllocationVal = document.getElementById('setup-allocation-val');
const setupExecutionVal = document.getElementById('setup-execution-val');
const setupMarketStory = document.getElementById('setup-market-story');
const setupStoryState = document.getElementById('setup-story-state');
const setupStoryDirection = document.getElementById('setup-story-direction');
const setupStoryEvent = document.getElementById('setup-story-event');
const setupStoryAge = document.getElementById('setup-story-age');
const setupStoryLevel = document.getElementById('setup-story-level');
const setupStoryInvalidation = document.getElementById('setup-story-invalidation');
const setupStoryReward = document.getElementById('setup-story-reward');
const setupStoryRewardState = document.getElementById('setup-story-reward-state');
const setupStoryRewardDetail = document.getElementById('setup-story-reward-detail');
const setupStoryHappened = document.getElementById('setup-story-happened');
const setupStoryNow = document.getElementById('setup-story-now');
const setupStoryNext = document.getElementById('setup-story-next');
const setupStoryFailure = document.getElementById('setup-story-failure');
const setupStoryAction = document.getElementById('setup-story-action');

// Causal market-context panel. This is deliberately separate from the
// legacy indicator telemetry so the dashboard explains evidence hierarchy.
const marketContextCard = document.getElementById('market-context-card');
const marketContextStatus = document.getElementById('market-context-status');
const marketContextScore = document.getElementById('market-context-score');
const marketContextSummary = document.getElementById('market-context-summary');
const contextRegime = document.getElementById('context-regime');
const contextPositioning = document.getElementById('context-positioning');
const contextOrderFlow = document.getElementById('context-order-flow');
const contextFunding = document.getElementById('context-funding');
const contextLiquidityAbove = document.getElementById('context-liquidity-above');
const contextLiquidityBelow = document.getElementById('context-liquidity-below');
const contextProfile = document.getElementById('context-profile');
const contextVwap = document.getElementById('context-vwap');
const marketContextContradictions = document.getElementById('market-context-contradictions');
const contextCoverage = document.getElementById('context-coverage');
const contextLimitations = document.getElementById('context-limitations');
const validationManifest = document.getElementById('validation-manifest');
const validationManifestTitle = document.getElementById('validation-manifest-title');
const validationManifestBadge = document.getElementById('validation-manifest-badge');
const validationRequiredValue = document.getElementById('validation-required-value');
const validationRequiredFill = document.getElementById('validation-required-fill');
const validationSupplementalValue = document.getElementById('validation-supplemental-value');
const validationSupplementalFill = document.getElementById('validation-supplemental-fill');
const validationManifestDetail = document.getElementById('validation-manifest-detail');

// economic data values
const regimeVal = document.getElementById('regime-val');
const fundingVal = document.getElementById('funding-val');
const oiVal = document.getElementById('oi-val');
// squeezeSignalVal and squeezeDescVal removed — replaced by confirmation evidence card
const liquidationContainer = document.getElementById('liquidation-list-container');

// AI Council elements
const councilAgentsGrid = document.getElementById('council-agents-grid');
const councilConsensusBadge = document.getElementById('council-consensus-badge');
const aiEmptyState = document.getElementById('ai-empty-state');
const aiReportBody = document.getElementById('ai-report-body');
const aiSentimentBadge = document.getElementById('ai-sentiment-badge');
const aiConfidenceVal = document.getElementById('ai-confidence-val');
const reportMdRender = document.getElementById('report-md-render');
const cioDecisionHero = document.getElementById('cio-decision-hero');
const cioTradeGrade = document.getElementById('cio-trade-grade');
const cioThesis = document.getElementById('cio-thesis');
const cioControlStrip = document.getElementById('cio-control-strip');
const cioMemoStatus = document.getElementById('cio-memo-status');

function cioSafeText(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
}

function cioLabel(value, fallback = '—') {
  const text = String(value ?? '').trim();
  return text ? text.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').toUpperCase() : fallback;
}

function setCioMemorandumPending(status = 'AWAITING EVIDENCE', thesis = 'The memorandum will summarize the committee’s measured evidence once a snapshot is available.') {
  if (cioDecisionHero) cioDecisionHero.className = 'cio-decision-hero neutral';
  if (aiSentimentBadge) {
    aiSentimentBadge.className = 'cio-decision-badge neutral';
    aiSentimentBadge.textContent = status;
  }
  if (cioTradeGrade) {
    cioTradeGrade.className = 'cio-grade-badge';
    cioTradeGrade.textContent = 'GRADE —';
  }
  if (aiConfidenceVal) aiConfidenceVal.textContent = '—';
  if (cioThesis) cioThesis.textContent = thesis;
  if (cioMemoStatus) {
    cioMemoStatus.className = 'cio-memo-status';
    cioMemoStatus.textContent = status;
  }
  if (cioControlStrip) {
    cioControlStrip.innerHTML = [
      ['Live confirmation', 'Awaiting snapshot'],
      ['Macro control', 'Awaiting snapshot'],
      ['Committee control', 'Awaiting evidence'],
    ].map(([label, value]) => `<div class="cio-control-chip"><span>${label}</span><strong>${value}</strong></div>`).join('');
  }
}

function renderCioMemorandum(data, ai, decision, confidence) {
  const normalizedDecision = String(decision || 'HOLD').toUpperCase();
  const decisionTone = normalizedDecision.includes('BUY') ? 'buy'
    : normalizedDecision.includes('SELL') ? 'sell'
      : normalizedDecision.includes('AVOID') || normalizedDecision.includes('OFFLINE') ? 'risk' : 'neutral';
  const liveConfirmation = data.gates?.live_confirmation || data.live_confirmation || ai.live_confirmation || {};
  const macro = data.macro_blockout || {};
  const riskWarnings = Array.isArray(ai.risk_warnings) ? ai.risk_warnings : [];
  const failedGate = data.failed_gate || liveConfirmation.reason || riskWarnings[0] || '';
  const livePassed = liveConfirmation.passed === true;
  const grade = data.trade_grade || ai.trade_grade || '—';
  const thesis = ai.explanation || ai.summary || (normalizedDecision === 'HOLD'
    ? 'The committee has not established a tradeable imbalance with sufficient evidence.'
    : 'The committee decision is based on the current synchronized market snapshot.');

  if (cioDecisionHero) cioDecisionHero.className = `cio-decision-hero ${decisionTone}`;
  if (aiSentimentBadge) {
    aiSentimentBadge.className = `cio-decision-badge ${decisionTone}`;
    aiSentimentBadge.textContent = cioLabel(normalizedDecision, 'AWAITING EVIDENCE');
  }
  if (cioTradeGrade) {
    cioTradeGrade.textContent = `GRADE ${cioLabel(grade)}`;
    cioTradeGrade.className = `cio-grade-badge grade-${String(grade).toLowerCase().replace('+', 'plus').replace(/[^a-z0-9]/g, '')}`;
  }
  if (aiConfidenceVal) aiConfidenceVal.textContent = Number.isFinite(Number(confidence)) ? Math.round(Number(confidence)) : '—';
  if (cioThesis) cioThesis.textContent = thesis;
  if (cioMemoStatus) {
    cioMemoStatus.textContent = macro.active ? 'MACRO RESTRICTED' : (livePassed ? 'LIVE CHECK PASSED' : 'RESEARCH / MANUAL CHECK');
    cioMemoStatus.className = `cio-memo-status ${macro.active || !livePassed ? 'caution' : 'verified'}`;
  }

  if (cioControlStrip) {
    const controls = [
      { label: 'Live confirmation', value: livePassed ? 'Confirmed' : (liveConfirmation.quality_badge || 'Manual review'), tone: livePassed ? 'ok' : 'caution' },
      { label: 'Macro control', value: macro.active ? (macro.reason || 'Block active') : 'No active block', tone: macro.active ? 'risk' : 'ok' },
      { label: 'Committee control', value: failedGate || 'No hard blocker reported', tone: failedGate ? 'caution' : 'ok' },
    ];
    cioControlStrip.innerHTML = controls.map(control => `
      <div class="cio-control-chip ${control.tone}">
        <span>${cioSafeText(control.label)}</span>
        <strong>${cioSafeText(control.value)}</strong>
      </div>`).join('');
  }

  // CIO text is model output. Render it as text, never executable HTML.
  if (reportMdRender) reportMdRender.textContent = ai.report_md || 'No memorandum narrative was generated for this snapshot.';
}

// Scanner Controls elements
const scannerLoopStatus = document.getElementById('scanner-loop-status');
const scannerLastRun = document.getElementById('scanner-last-run');
const scannerNextRun = document.getElementById('scanner-next-run');
const scannerEnableToggle = document.getElementById('scanner-enable-toggle');
const scannerDiscoveryToggle = document.getElementById('scanner-discovery-toggle');
const triggerScanBtn = document.getElementById('trigger-scan-btn');
const newPairInput = document.getElementById('new-pair-input');
const addPairBtn = document.getElementById('add-pair-btn');
const watchlistTagsContainer = document.getElementById('watchlist-tags-container');
const discoveredSectionBox = document.getElementById('discovered-section-box');
const discoveredTagsContainer = document.getElementById('discovered-tags-container');
const scannerEvidenceStatus = document.getElementById('scanner-evidence-status');
const scannerEvidenceSummary = document.getElementById('scanner-evidence-summary');
const scannerBlockerList = document.getElementById('scanner-blocker-list');

// Operations controls
const runBacktestBtn = document.getElementById('run-backtest-btn');
const trainModelBtn = document.getElementById('train-model-btn');
const opsResultsBox = document.getElementById('ops-results-box');
const opsResultsTitle = document.getElementById('ops-results-title');
const opsResultsBadge = document.getElementById('ops-results-badge');
const opsMetricsDisplay = document.getElementById('ops-metrics-display');
const opsResultsLog = document.getElementById('ops-results-log');
const opsCandlesInput = document.getElementById('ops-candles');

// Operations Log Console Helper
function logMsg(msg, type = 'system') {
  const timestamp = new Date().toLocaleTimeString();
  const entry = document.createElement('div');
  entry.className = `log-entry ${type}`;
  entry.textContent = `[${timestamp}] ${msg}`;
  logBox.appendChild(entry);
  logBox.scrollTop = logBox.scrollHeight;
}

// Formatting helpers
function formatCurrency(num) {
  if (num === null || num === undefined || isNaN(num)) return '-';
  if (num >= 1) return num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return num.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 8 });
}

function formatVolume(num) {
  if (!num || isNaN(num)) return '-';
  if (num >= 1.0e+9) return '$' + (num / 1.0e+9).toFixed(2) + 'B';
  if (num >= 1.0e+6) return '$' + (num / 1.0e+6).toFixed(2) + 'M';
  return '$' + num.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

let scannerStatusInitialized = false;
const canManageScanner = () => window.atcUserRole === 'admin';

function applyScannerPermissions() {
  const disabled = !canManageScanner();
  [scannerEnableToggle, scannerDiscoveryToggle, triggerScanBtn, addPairBtn, newPairInput].forEach(node => {
    if (node) node.disabled = disabled;
  });
  if (disabled && triggerScanBtn) triggerScanBtn.title = 'Platform-wide scanner controls are restricted to administrators.';
}

function renderScannerDiagnostics(diagnostics) {
  if (!scannerEvidenceStatus || !scannerEvidenceSummary || !scannerBlockerList) return;
  scannerBlockerList.replaceChildren();
  if (!diagnostics?.available) {
    scannerEvidenceStatus.textContent = 'History unavailable';
    scannerEvidenceSummary.textContent = 'Scanner diagnostics will appear after the database migration and the first completed scan.';
    return;
  }

  const observations = Number(diagnostics.observations || 0);
  const published = Number(diagnostics.published || 0);
  const tactical = Number(diagnostics.tactical_watches || 0);
  scannerEvidenceStatus.textContent = observations ? `${published} published · ${tactical} tactical` : 'Awaiting first scan';
  scannerEvidenceSummary.textContent = observations
    ? `${observations} recent autonomous decisions recorded. Tune the most frequent blocker, not activity volume.`
    : 'No completed scanner decision yet. Enable the scanner, then run a watchlist scan to establish a baseline.';

  const blockers = Array.isArray(diagnostics.blocker_counts) ? diagnostics.blocker_counts : [];
  if (!blockers.length && observations) {
    const chip = document.createElement('span');
    chip.className = 'scanner-blocker-chip';
    chip.textContent = 'No primary publication blocker recorded.';
    scannerBlockerList.appendChild(chip);
  }
  blockers.forEach(({ reason, count }) => {
    const chip = document.createElement('span');
    chip.className = 'scanner-blocker-chip';
    chip.textContent = `${count}× ${reason || 'Unclassified blocker'}`;
    scannerBlockerList.appendChild(chip);
  });
}

// ── Autonomous Scanner Integration ──────────────────────────────────────────

async function fetchScannerStatus() {
  // The scanner control card is intentionally absent from the Research Page.
  // Keep the admin integration dormant without polling hidden endpoints.
  if (!scannerLoopStatus) return;
  try {
    const response = await fetch('/scanner/status');
    if (!response.ok) throw new Error("Failed to fetch scanner status.");
    const data = await response.json();

    // Initialize toggles from server configuration state on first load
    if (!scannerStatusInitialized) {
      if (scannerEnableToggle) scannerEnableToggle.checked = data.autonomous_scan_enabled || false;
      if (scannerDiscoveryToggle) scannerDiscoveryToggle.checked = data.autonomous_pair_discovery || false;
      scannerStatusInitialized = true;
    }

    // Update scanner meta indicators
    const isScanning = data.is_scanning;
    const currentSymbol = data.current_symbol;

    if (isScanning) {
      scannerLoopStatus.textContent = currentSymbol ? `Scanning ${currentSymbol}...` : "Scanning...";
      scannerLoopStatus.className = "accent-text pulse";
    } else {
      scannerLoopStatus.textContent = "Idle";
      scannerLoopStatus.className = "";
    }

    scannerLastRun.textContent = data.last_scan_time ? new Date(data.last_scan_time).toLocaleTimeString() : "Never";
    scannerNextRun.textContent = data.next_scan_time ? new Date(data.next_scan_time).toLocaleTimeString() : "Pending";
    renderScannerDiagnostics(data.diagnostics);

    // Update watchlist tags
    const watchlist = data.watchlist || [];
    watchlistTagsContainer.innerHTML = '';

    if (watchlist.length === 0) {
      watchlistTagsContainer.innerHTML = '<span class="text-muted" style="font-size: 0.75rem;">Watchlist empty</span>';
    } else {
      watchlist.forEach(sym => {
        const activeSymbol = symbolInput.value.trim().toUpperCase();
        const isCurrent = sym === activeSymbol;

        const tag = document.createElement('span');
        tag.className = `tag ${isCurrent ? 'active' : ''}`;
        tag.innerHTML = `
          <span class="symbol-name">${sym}</span>
          <span class="remove-btn" data-sym="${sym}">&times;</span>
        `;

        // Load in connect console on tag click
        tag.querySelector('.symbol-name').addEventListener('click', () => {
          symbolInput.value = sym;
          logMsg(`Watchlist load: selected ${sym}. Triggering connect.`, 'system');
          configForm.dispatchEvent(new Event('submit'));
        });

        // Remove tag handler
        const removeButton = tag.querySelector('.remove-btn');
        removeButton.hidden = !canManageScanner();
        removeButton.addEventListener('click', (e) => {
          e.stopPropagation();
          if (canManageScanner()) removeWatchlistPair(sym);
        });

        watchlistTagsContainer.appendChild(tag);
      });
    }

    // Dynamic AI discovery rendering
    const discovered = data.discovered_pairs || [];
    if (discovered.length > 0 && scannerDiscoveryToggle.checked) {
      discoveredSectionBox.classList.remove('hidden');
      discoveredTagsContainer.innerHTML = '';
      discovered.forEach(sym => {
        const tag = document.createElement('span');
        tag.className = 'tag discovered';
        tag.innerHTML = `
          <span>+ ${sym}</span>
        `;
        tag.addEventListener('click', () => {
          addWatchlistPair(sym);
        });
        discoveredTagsContainer.appendChild(tag);
      });
    } else {
      discoveredSectionBox.classList.add('hidden');
    }

  } catch (err) {
    console.error("Scanner status check failed:", err);
  }
}

async function addWatchlistPair(symbol) {
  if (!canManageScanner()) return;
  const sym = symbol.toUpperCase().trim();
  if (!sym) return;
  try {
    // Fetch current list first
    const statusResp = await fetch('/scanner/watchlist');
    const { watchlist } = await statusResp.json();
    if (watchlist.includes(sym)) {
      logMsg(`${sym} is already in the watchlist.`, 'system');
      return;
    }

    const newWatchlist = [...watchlist, sym];
    const response = await fetch('/scanner/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbols: newWatchlist })
    });
    if (!response.ok) throw new Error("Failed to add watchlist item.");
    logMsg(`Watchlist: added ${sym}`, 'system');
    newPairInput.value = '';
    fetchScannerStatus();
  } catch (err) {
    logMsg(`Failed to add watchlist item: ${err.message}`, 'error');
  }
}

async function removeWatchlistPair(symbol) {
  if (!canManageScanner()) return;
  try {
    const statusResp = await fetch('/scanner/watchlist');
    const { watchlist } = await statusResp.json();
    const newWatchlist = watchlist.filter(s => s !== symbol);

    const response = await fetch('/scanner/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbols: newWatchlist })
    });
    if (!response.ok) throw new Error("Failed to remove watchlist item.");
    logMsg(`Watchlist: removed ${symbol}`, 'system');
    fetchScannerStatus();
  } catch (err) {
    logMsg(`Failed to remove watchlist item: ${err.message}`, 'error');
  }
}

// Manual scan run trigger
triggerScanBtn?.addEventListener('click', async () => {
  if (!canManageScanner()) return;
  try {
    triggerScanBtn.disabled = true;
    logMsg("Watchlist manual scan triggered...", "system");
    const response = await fetch('/scanner/trigger', { method: 'POST' });
    const res = await response.json();
    logMsg(res.message, "system");
    setTimeout(() => {
      triggerScanBtn.disabled = false;
      fetchScannerStatus();
    }, 3000);
  } catch (err) {
    triggerScanBtn.disabled = false;
    logMsg(`Manual scan trigger failed: ${err.message}`, 'error');
  }
});

addPairBtn?.addEventListener('click', () => {
  addWatchlistPair(newPairInput.value);
});

newPairInput?.addEventListener('keypress', (e) => {
  if (e.key === 'Enter') addWatchlistPair(newPairInput.value);
});

// Watchlist Discovery toggles UI responses
scannerDiscoveryToggle?.addEventListener('change', async () => {
  if (!canManageScanner()) return;
  const isChecked = scannerDiscoveryToggle.checked;
  logMsg(`Updating AI pair discovery state: ${isChecked ? "ON" : "OFF"}...`, 'system');
  try {
    const response = await fetch('/scanner/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ discovery: isChecked })
    });
    const res = await response.json();
    if (!response.ok) throw new Error(res.detail || "Toggle failed.");
    logMsg(`AI discovery is now ${isChecked ? "active" : "disabled"}.`, 'system');
    fetchScannerStatus();
  } catch (err) {
    logMsg(`Failed to toggle AI discovery: ${err.message}`, 'error');
    scannerDiscoveryToggle.checked = !isChecked;
  }
});

scannerEnableToggle?.addEventListener('change', async () => {
  if (!canManageScanner()) return;
  const isChecked = scannerEnableToggle.checked;
  logMsg(`Updating background scanner state: ${isChecked ? "ON" : "OFF"}...`, 'system');
  try {
    const response = await fetch('/scanner/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: isChecked })
    });
    const res = await response.json();
    if (!response.ok) throw new Error(res.detail || "Toggle failed.");
    logMsg(`Background scanner loop is now ${isChecked ? "active" : "disabled"}.`, 'system');
    fetchScannerStatus();
  } catch (err) {
    logMsg(`Failed to toggle background scanner: ${err.message}`, 'error');
    scannerEnableToggle.checked = !isChecked;
  }
});

// Do not probe premium scanner routes while the subscription gate is still
// restoring a session. The gate emits this event only after it has a valid
// access token and has verified entitlement.
if (scannerLoopStatus) {
  activeScannerInterval = setInterval(() => {
    if (window.atcAuthenticated && canManageScanner()) fetchScannerStatus();
  }, 30000);
  window.addEventListener('atc:authenticated', () => {
    applyScannerPermissions();
    if (canManageScanner()) fetchScannerStatus();
  });
  applyScannerPermissions();
  if (window.atcAuthenticated && canManageScanner()) fetchScannerStatus();
}

// ── Rendering dashboard logic ──────────────────────────────────────────────

function marketStoryLabel(value, fallback = 'UNAVAILABLE') {
  const text = String(value ?? '').trim();
  return text ? text.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').toUpperCase() : fallback;
}

function marketStoryPrice(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric > 0 ? formatCurrency(numeric) : '—';
}

function resolveMarketStory({
  marketStory = {},
  structureStory = {},
  setupStory = {},
  preferredDirection = '',
  preferSetupStory = false,
} = {}) {
  const isPopulatedObject = value => Boolean(
    value
    && typeof value === 'object'
    && !Array.isArray(value)
    && Object.keys(value).length,
  );
  const wrappedPrimary = isPopulatedObject(structureStory?.primary) ? structureStory.primary : null;
  const directStructureStory = (
    structureStory?.schema_version === 'market_story.v1'
    || Object.prototype.hasOwnProperty.call(structureStory || {}, 'available')
  ) ? structureStory : null;
  const primary = wrappedPrimary
    || (isPopulatedObject(marketStory) ? marketStory : directStructureStory)
    || {};
  const directionalCandidates = preferSetupStory
    ? [
      setupStory?.directional_view,
      setupStory,
      structureStory?.directional_view,
      primary.actionability,
    ]
    : [
      structureStory?.directional_view,
      setupStory?.directional_view,
      setupStory,
      primary.actionability,
    ];
  const directional = directionalCandidates.find(isPopulatedObject) || {};
  const directionalOwnsEvent = Object.prototype.hasOwnProperty.call(directional, 'aligned_event')
    || Object.prototype.hasOwnProperty.call(directional, 'selected_event');
  const event = directional.aligned_event
    || directional.selected_event
    || setupStory.selected_event
    || (directionalOwnsEvent ? null : primary.latest_event)
    || {};
  const state = directional.state
    || setupStory.state
    || primary.current_state
    || primary.actionability?.status
    || 'NO_ACTIVE_EVENT';
  const direction = directional.direction
    || event.direction
    || primary.actionability?.direction
    || preferredDirection
    || 'NEUTRAL';
  const explicitActionable = [directional.actionable, setupStory.actionable]
    .find(value => typeof value === 'boolean');
  const actionabilityKnown = typeof explicitActionable === 'boolean'
    || typeof primary.actionability?.actionable === 'boolean';
  const actionable = typeof explicitActionable === 'boolean'
    ? explicitActionable
    : primary.actionability?.actionable === true;
  const explicitChaseRule = [directional.chase_prohibited, setupStory.chase_prohibited]
    .find(value => typeof value === 'boolean');
  const chaseProhibited = typeof explicitChaseRule === 'boolean'
    ? explicitChaseRule
    : (
      (!directionalOwnsEvent && primary.actionability?.chase_prohibited === true)
      || ['EXTENDED_DO_NOT_CHASE', 'LATE_STRUCTURE_DO_NOT_CHASE', 'PULLBACK_REQUIRED', 'MISSED', 'INVALIDATED', 'EXPIRED'].includes(String(state).toUpperCase())
    );
  const available = primary.available === true
    || structureStory?.schema_version === 'structure_story.v1'
    || Boolean(event.detected || event.event_id);
  const age = event.age_bars ?? primary.actionability?.event_age_bars;
  const eventType = event.type || primary.actionability?.event_type;
  const eventLevel = event.break_level
    ?? event.broken_level
    ?? event.swept_level
    ?? primary.actionability?.event_level;
  const invalidation = event.invalidation_level;
  const eventMatchesLatest = !event.event_id
    || !primary.latest_event?.event_id
    || event.event_id === primary.latest_event.event_id;
  const continuation = eventMatchesLatest && Array.isArray(primary.next_scenarios)
    ? primary.next_scenarios.find(item => item?.scenario === 'CONTINUATION') || primary.next_scenarios[0]
    : null;
  const failure = eventMatchesLatest && Array.isArray(primary.next_scenarios)
    ? primary.next_scenarios.find(item => item?.scenario === 'FAILURE_OR_REVERSAL') || primary.next_scenarios[1]
    : null;
  const eventDirection = marketStoryLabel(event.direction || direction, 'NEUTRAL');
  const generatedHappened = event.detected || event.event_id
    ? `${eventDirection} ${marketStoryLabel(eventType, 'MARKET EVENT')} occurred ${Number(age) === 0 ? 'on the latest completed candle' : `${age ?? '—'} completed bars ago`} through ${marketStoryPrice(eventLevel)}.`
    : 'No recent completed-candle structure break or liquidity sweep is active.';
  const primaryLatestId = primary.latest_event?.event_id;
  const happened = primary.what_happened && (!event.event_id || !primaryLatestId || event.event_id === primaryLatestId)
    ? primary.what_happened
    : generatedHappened;
  const happeningNow = directional.reason
    || setupStory.reason
    || primary.what_is_happening
    || 'The current price location is waiting for completed-candle classification.';
  const next = continuation?.condition
    || (
      String(direction).toUpperCase() === 'BULLISH'
        ? 'Continuation requires completed closes to hold the event level while flow and positioning remain bullish.'
        : String(direction).toUpperCase() === 'BEARISH'
          ? 'Continuation requires completed closes to hold below the event level while flow and positioning remain bearish.'
          : 'A fresh completed event with aligned live evidence is required before manual review.'
    );
  const failureCondition = failure?.condition
    || (
      String(direction).toUpperCase() === 'BULLISH'
        ? 'A completed close below invalidation, or a newer bearish event, cancels this branch.'
        : String(direction).toUpperCase() === 'BEARISH'
          ? 'A completed close above invalidation, or a newer bullish event, cancels this branch.'
          : 'Remain in observation mode until a directional event becomes active.'
    );
  let tone = 'monitoring';
  if (String(state).toUpperCase() === 'INVALIDATED') tone = 'invalidated';
  else if (chaseProhibited || ['EXTENDED_DO_NOT_CHASE', 'LATE_STRUCTURE_DO_NOT_CHASE', 'PULLBACK_REQUIRED', 'MISSED', 'EXPIRED'].includes(String(state).toUpperCase())) tone = 'stale';
  else if (actionable || ['ACTIONABLE_NOW', 'RETESTING'].includes(String(state).toUpperCase())) tone = 'actionable';
  const actionText = chaseProhibited
    ? `${marketStoryLabel(state)} · DO NOT CHASE`
    : actionable
      ? 'ACTIONABLE FOR MANUAL REVIEW · LIVE CONTROLS STILL APPLY'
      : available
        ? `${marketStoryLabel(state)} · OBSERVATION ONLY`
        : 'OBSERVATION ONLY · WAITING FOR A QUALIFIED EVENT';
  return {
    available,
    state,
    direction,
    actionable,
    actionabilityKnown,
    chaseProhibited,
    tone,
    event,
    eventType,
    age,
    eventLevel,
    invalidation,
    happened,
    happeningNow,
    next,
    failureCondition,
    actionText,
  };
}

function renderMarketStorySurface(elements, story) {
  if (!elements?.panel) return;
  elements.panel.dataset.storyTone = story.tone;
  if (elements.state) elements.state.textContent = marketStoryLabel(story.state, 'AWAITING STORY');
  if (elements.direction) {
    elements.direction.textContent = marketStoryLabel(story.direction, 'NO DIRECTION');
    elements.direction.dataset.direction = String(story.direction || '').toLowerCase();
  }
  if (elements.event) elements.event.textContent = marketStoryLabel(story.eventType, 'NO ACTIVE EVENT');
  if (elements.age) elements.age.textContent = story.age === undefined || story.age === null ? '—' : `${story.age} BAR${Number(story.age) === 1 ? '' : 'S'}`;
  if (elements.level) elements.level.textContent = marketStoryPrice(story.eventLevel);
  if (elements.invalidation) elements.invalidation.textContent = marketStoryPrice(story.invalidation);
  if (elements.happened) elements.happened.textContent = story.happened;
  if (elements.now) elements.now.textContent = story.happeningNow;
  if (elements.next) elements.next.textContent = story.next;
  if (elements.failure) elements.failure.textContent = `Failure branch: ${story.failureCondition}`;
  if (elements.action) {
    const copy = elements.action.querySelector('span');
    if (copy) copy.textContent = story.actionText;
  }
}

function renderTradeSetup(setup, marketStory = {}, structureStory = {}) {
  const setupStory = setup?.market_story || {};
  const remainingReward = setup?.remaining_reward || {};
  const rewardAdequacyKnown = typeof remainingReward.adequate === 'boolean';
  const rewardAdequate = remainingReward.adequate === true;
  const rewardInsufficient = remainingReward.adequate === false;
  const rewardReason = remainingReward.reason || '';
  let story = resolveMarketStory({
    marketStory,
    structureStory,
    setupStory,
    preferredDirection: setup?.side === 'LONG' ? 'BULLISH' : setup?.side === 'SHORT' ? 'BEARISH' : '',
    preferSetupStory: true,
  });
  if (rewardInsufficient) {
    story = {
      ...story,
      actionable: false,
      chaseProhibited: true,
      tone: 'stale',
      happeningNow: rewardReason || 'Too little reward remains between the current entry location and the mapped liquidity objective.',
      next: 'Wait for a fresh event or a materially better retest location that restores acceptable reward-to-risk.',
      actionText: 'INSUFFICIENT REMAINING REWARD · DO NOT CHASE',
    };
  }
  const timingBlocked = story.chaseProhibited === true;
  const entryBlocked = rewardInsufficient || timingBlocked;
  renderMarketStorySurface({
    panel: setupMarketStory,
    state: setupStoryState,
    direction: setupStoryDirection,
    event: setupStoryEvent,
    age: setupStoryAge,
    level: setupStoryLevel,
    invalidation: setupStoryInvalidation,
    happened: setupStoryHappened,
    now: setupStoryNow,
    next: setupStoryNext,
    failure: setupStoryFailure,
    action: setupStoryAction,
  }, story);
  if (setupStoryReward) {
    setupStoryReward.dataset.rewardTone = rewardInsufficient ? 'insufficient' : rewardAdequate ? 'adequate' : 'unknown';
  }
  if (setupStoryRewardState) {
    const rewardR = Number(remainingReward.to_liquidity_objective_r);
    const hasRewardR = remainingReward.to_liquidity_objective_r !== null
      && remainingReward.to_liquidity_objective_r !== undefined
      && Number.isFinite(rewardR);
    setupStoryRewardState.textContent = rewardInsufficient
      ? `INSUFFICIENT${hasRewardR ? ` · ${rewardR.toFixed(2)}R` : ''} · DO NOT CHASE`
      : rewardAdequate
        ? `ADEQUATE${hasRewardR ? ` · ${rewardR.toFixed(2)}R` : ''}`
        : 'NOT ASSESSED';
  }
  if (setupStoryRewardDetail) {
    const objectiveKind = marketStoryLabel(remainingReward.objective_kind, 'LIQUIDITY OBJECTIVE');
    const objectivePrice = marketStoryPrice(remainingReward.objective_price);
    const minimumR = Number(remainingReward.minimum_required_r);
    const threshold = remainingReward.minimum_required_r !== null
      && remainingReward.minimum_required_r !== undefined
      && Number.isFinite(minimumR)
      ? `${minimumR.toFixed(2)}R minimum`
      : 'the required minimum';
    setupStoryRewardDetail.textContent = rewardReason || (
      rewardAdequacyKnown
        ? `${objectiveKind} at ${objectivePrice} · compared with ${threshold}.`
        : 'Reward capacity will be measured from the current entry location.'
    );
  }
  const status = setup?.status || 'NO_TRADE';
  const label = value => String(value || 'UNAVAILABLE').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').toUpperCase();
  setupStatusVal.textContent = entryBlocked ? 'DO NOT CHASE' : status.replace(/_/g, ' ');
  setupStatusVal.className = `setup-status ${entryBlocked ? 'blocked_by_reward' : status.toLowerCase()}`;
  setupReasonVal.textContent = entryBlocked
    ? rewardInsufficient
      ? rewardReason || 'The setup is mapped, but too little reward remains to justify chasing the move.'
      : story.happeningNow || 'The directional campaign is already mature; wait for a new causal structure.'
    : setup?.reason || 'Waiting for a qualified setup.';
  if (setupLedgerCard) setupLedgerCard.className = `card exact-setup-card setup-ledger-card ledger-${status.toLowerCase()}${entryBlocked ? ' reward-insufficient' : ''}`;
  if (setupEvidenceHero) setupEvidenceHero.className = `setup-evidence-hero ${entryBlocked ? 'watch_only' : status.toLowerCase()}`;
  if (setupEvidenceState) setupEvidenceState.textContent = `PLAN STATE · ${rewardInsufficient ? 'INSUFFICIENT REWARD' : timingBlocked ? label(story.state) : label(status)}`;
  if (setupTypeVal) setupTypeVal.textContent = label(setup?.setup_type || 'NO SCENARIO');
  if (setupEvidenceTitle) setupEvidenceTitle.textContent = entryBlocked
    ? rewardInsufficient
      ? 'Scenario mapped, but remaining reward is insufficient'
      : String(story.state).toUpperCase() === 'PULLBACK_REQUIRED'
        ? 'Breakout entry blocked — wait for a completed pullback'
        : 'Move already consumed — wait for a new causal structure'
    : status === 'NO_TRADE'
      ? 'No directional causal scenario is currently established'
      : status === 'WATCH_ONLY' ? 'Directional value-retest scenario mapped'
        : status === 'BLOCKED_BY_MACRO' ? 'Scenario mapped, but macro controls block release'
          : 'Institutional controls passed for manual review';
  if (setupEvidenceMark) setupEvidenceMark.textContent = entryBlocked ? '×' : status === 'NO_TRADE' ? '—' : status === 'WATCH_ONLY' ? '◌' : status === 'BLOCKED_BY_MACRO' ? '!' : '✓';

  if (!setup || status === 'NO_TRADE') {
    setupSideVal.textContent = '-';
    setupSideVal.className = '';
    setupEntryVal.textContent = '-';
    setupStopVal.textContent = '-';
    setupRiskVal.textContent = '-';
    setupTp1Val.textContent = '-';
    setupTp2Val.textContent = '-';
    setupTp3Val.textContent = '-';
    setupRunnerVal.textContent = '-';
    setupLeverageVal.textContent = '-';
    if (setupEntrySourceVal) setupEntrySourceVal.textContent = 'ENTRY SOURCE · —';
    if (setupStopSourceVal) setupStopSourceVal.textContent = 'INVALIDATION · —';
    if (setupObjectiveVal) setupObjectiveVal.textContent = 'OBJECTIVE · —';
    if (setupAllocationVal) setupAllocationVal.textContent = 'NO ALLOCATION';
    if (setupExecutionVal) setupExecutionVal.textContent = 'MANUAL ONLY';
    leverageTableContainer.innerHTML = '<div class="empty-leverage">No leverage plan loaded.</div>';
    return;
  }

  const entry = setup.entry || {};
  const stop = setup.stop || {};
  const targets = setup.targets || {};
  const position = setup.position || {};
  const leverage = setup.leverage || {};

  setupSideVal.textContent = setup.side || '-';
  setupSideVal.className = 'value ' + (setup.side || '').toLowerCase();
  setupEntryVal.textContent = `${formatCurrency(entry.zone_low)} - ${formatCurrency(entry.zone_high)} (${formatCurrency(entry.reference)})`;
  setupStopVal.textContent = `${formatCurrency(stop.selected)} / ${stop.method || '-'}`;
  setupRiskVal.textContent = `$${formatCurrency(position.risk_amount_usd)} | ${stop.distance_pct ?? '-'}% stop`;
  setupTp1Val.textContent = formatCurrency(targets.tp1_1r);
  setupTp2Val.textContent = formatCurrency(targets.tp2_2r);
  setupTp3Val.textContent = formatCurrency(targets.tp3_3r);
  setupRunnerVal.textContent = formatCurrency(targets.runner_5r);
  setupLeverageVal.textContent = `${leverage.max_sensible || leverage.recommended || '-'}x MAX`;
  if (setupEntrySourceVal) setupEntrySourceVal.textContent = entryBlocked
    ? `NO LIVE ENTRY · ${label(story.state)}`
    : `ENTRY SOURCE · ${label(entry.mode)}`;
  if (setupStopSourceVal) setupStopSourceVal.textContent = `INVALIDATION · ${label(stop.liquidity_reference_kind || stop.method)}`;
  if (setupObjectiveVal) setupObjectiveVal.textContent = `OBJECTIVE · ${label(setup.liquidity_objective?.kind || 'RISK LADDER')}`;
  if (setupAllocationVal) setupAllocationVal.textContent = setup.execution_permitted
    ? `${position.risk_pct ?? 0}% RISK · $${formatCurrency(position.risk_amount_usd)}`
    : 'ZERO RISK · WATCH';
  if (setupExecutionVal) setupExecutionVal.textContent = entryBlocked
    ? 'DO NOT ENTER'
    : setup.execution_permitted ? 'MANUAL REVIEW' : 'NO AUTO-EXECUTION';

  const rows = Array.isArray(leverage.options) ? leverage.options : [];
  if (rows.length === 0) {
    leverageTableContainer.innerHTML = '<div class="empty-leverage">No leverage plan loaded.</div>';
  } else {
    leverageTableContainer.innerHTML = rows.map(row => `
      <div class="leverage-row ${row.allowed ? 'allowed' : 'blocked'}">
        <span>${row.leverage}x</span>
        <span>${row.allowed ? 'Usable' : 'Rejected'}</span>
        <span>Liq ${formatCurrency(row.approx_liquidation)}</span>
        <span>Buffer ${row.buffer_after_stop_pct}%</span>
      </div>
    `).join('');
  }
}

function recalculateSizing() {
  const accountSize = parseFloat(accSizeInput.value) || 0;
  const manualRiskPct = parseFloat(riskPctInput.value) || 0;

  if (accountSize <= 0) {
    riskAmtVal.textContent = '$0.00';
    sizingUnitsVal.textContent = '0.00 units';
    return;
  }

  const isSmart = stopMethodSmart && stopMethodSmart.checked;
  const activeStop = isSmart ? activeSmartStop : activeRetailStop;
  const activeRisk = isSmart ? activeSmartRisk : activeRetailRisk;
  const activeTarget = isSmart ? activeSmartTarget : activeRetailTarget;

  if (activeStop) riskStop.textContent = formatCurrency(activeStop);
  if (activeTarget) riskTarget.textContent = formatCurrency(activeTarget);

  let riskPct = manualRiskPct;

  if (activeHistoricalStats && activeHistoricalStats.similar_setups_count > 0) {
    const p = (activeHistoricalStats.historical_win_rate || 50) / 100;
    const entry = parseFloat(riskEntry.textContent) || 0;
    let b = 2.0;
    if (activeRisk && activeRisk > 0 && activeTarget && entry > 0) {
      b = Math.abs(activeTarget - entry) / activeRisk;
    }

    // Half-Kelly multiplier (conservative safety)
    const f_star = 0.5 * (p - (1.0 - p) / b);
    const kellyPct = Math.max(0, f_star * 100);

    kellyRecVal.textContent = kellyPct.toFixed(2) + '%';
    if (f_star > 0) {
      kellyStatusText.className = 'kelly-status-badge positive';
      kellyStatusText.textContent = 'Active';
    } else {
      kellyStatusText.className = 'kelly-status-badge negative';
      kellyStatusText.textContent = 'Negative Kelly';
    }
    kellyRecBox.classList.remove('hidden');

    if (kellyToggle && kellyToggle.checked) {
      riskPct = kellyPct;
    }
  } else {
    kellyRecVal.textContent = '0.00%';
    kellyStatusText.className = 'kelly-status-badge';
    kellyStatusText.textContent = 'No History';
    kellyRecBox.classList.remove('hidden');
  }

  if (riskPct <= 0) {
    riskAmtVal.textContent = '$0.00';
    sizingUnitsVal.textContent = '0.00 units';
    return;
  }

  const riskAmount = accountSize * (riskPct / 100);
  riskAmtVal.textContent = '$' + riskAmount.toFixed(2);

  if (activeRisk && activeRisk > 0) {
    const units = riskAmount / activeRisk;
    sizingUnitsVal.textContent = units.toFixed(6) + ' units';
  } else {
    sizingUnitsVal.textContent = '0.00 units';
  }
}

accSizeInput.addEventListener('input', recalculateSizing);
riskPctInput.addEventListener('input', recalculateSizing);
if (stopMethodRetail) stopMethodRetail.addEventListener('change', recalculateSizing);
if (stopMethodSmart) stopMethodSmart.addEventListener('change', recalculateSizing);
if (kellyToggle) kellyToggle.addEventListener('change', recalculateSizing);

function startLoaderAnimation() {
  if (loaderInterval) clearInterval(loaderInterval);
  let activeIndex = 0;

  loaderInterval = setInterval(() => {
    const consoles = document.querySelectorAll('.scifi-loading-console');
    consoles.forEach(con => {
      const lines = con.querySelectorAll('.loader-line');
      if (lines.length > 0) {
        lines.forEach(line => line.classList.remove('blinking'));
        const targetIndex = activeIndex % lines.length;
        lines[targetIndex].classList.add('blinking');
      }
    });
    activeIndex++;
  }, 4000);
}

function stopLoaderAnimation() {
  if (loaderInterval) {
    clearInterval(loaderInterval);
    loaderInterval = null;
  }
}

// Live WebSockets control & user disconnect intent tracking
let userIntentDisconnect = true;
let reconnectTimer = null;

function disconnectStream() {
  userIntentDisconnect = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  stopLoaderAnimation();
  if (socket) {
    socket.onclose = null; // Prevent onclose from triggering auto-reconnect
    socket.close(1000, 'User Disconnected');
    socket = null;
  }
  activeStreamSymbol = null;
  activeStreamTimeframe = null;
  updateConnectionUI(false);
  logMsg('Live stream completely disconnected by user.', 'system');
}

function updateConnectionUI(connected) {
  const dot = wsStatus.querySelector('.dot');
  const text = wsStatus.querySelector('.text');

  if (connected) {
    dot.className = 'dot green';
    text.textContent = `Streaming ${symbolInput.value.toUpperCase()} (${timeframeSelect.value})`;
    if (connectBtn) {
      connectBtn.disabled = false;
      connectBtn.style.opacity = '1';
      connectBtn.style.cursor = 'pointer';
      connectBtn.textContent = 'Disconnect Live Stream';
      connectBtn.className = 'btn disconnect-btn';
    }
    if (controlsCard) controlsCard.classList.add('active-connection');
    if (decisionCard) decisionCard.classList.add('active-connection');

    // Set active synchronizing tags
    decisionVal.textContent = 'SYNCHRONIZING...';
    decisionVal.style.color = 'var(--neon-gold)';
    decisionVal.style.textShadow = '0 0 10px rgba(241, 188, 0, 0.45)';

    // Build the evidence dossier with a compact loading console.
    councilAgentsGrid.innerHTML = `
      <div class="scifi-loading-console" style="width: 100%; margin: 0.5rem 0;">
        <div class="loader-line blinking">[ APEX NETWORK LINK ACTIVE ]</div>
        <div class="loader-line" style="color: var(--neon-gold); margin: 0.25rem 0;">[ SYNCING QUANT MARKET FEEDS... ]</div>
        <div class="loader-line">[ INGESTING LIQUIDITY GUARDRAILS ]</div>
        <div class="loader-line" style="color: var(--neon-purple); margin: 0.25rem 0;">[ CONVENING 9 MULTI-AGENT ANALYSTS ]</div>
        <div class="progress-bar-container">
          <div class="progress-bar-fill"></div>
        </div>
      </div>
    `;

    // Show report box and place loader inside
    aiEmptyState.classList.add('hidden');
    aiReportBody.classList.remove('hidden');
    setCioMemorandumPending('SYNCING EVIDENCE', 'A fresh, synchronized evidence snapshot is being assembled for CIO review.');
    reportMdRender.innerHTML = `
      <div class="scifi-loading-console" style="width: 100%; margin: 0.5rem 0;">
        <div class="loader-line blinking">[ ESTABLISHING SECURE INTEL STREAM ]</div>
        <div class="loader-line" style="color: var(--neon-gold); margin: 0.25rem 0;">[ GENERATING SMC STRATEGIST SCHEMAS... ]</div>
        <div class="loader-line">[ DRAFTING CHIEF INVESTMENT REPORT ]</div>
        <div class="progress-bar-container">
          <div class="progress-bar-fill"></div>
        </div>
      </div>
    `;

    startLoaderAnimation();

    // Set processing ticker status
    if (councilProcessingTicker) {
      councilProcessingTicker.className = "processing-ticker active-scanning";
      const txt = councilProcessingTicker.querySelector('.ticker-text');
      if (txt) txt.textContent = "SCANNING INTEL...";
    }
  } else {
    stopLoaderAnimation();
    if (councilProcessingTicker) {
      councilProcessingTicker.className = "processing-ticker";
      const txt = councilProcessingTicker.querySelector('.ticker-text');
      if (txt) txt.textContent = "STANDBY";
    }
    dot.className = 'dot red';
    text.textContent = 'Disconnected';
    if (connectBtn) {
      connectBtn.disabled = false;
      connectBtn.style.opacity = '1';
      connectBtn.style.cursor = 'pointer';
      connectBtn.textContent = 'Start Live Stream';
      connectBtn.className = 'btn primary-btn';
    }
    if (controlsCard) controlsCard.classList.remove('active-connection');
    if (decisionCard) decisionCard.classList.remove('active-connection');

    // Clear status values
    decisionVal.textContent = 'OFFLINE';
    decisionVal.style.color = '';
    decisionVal.style.textShadow = '';
    decisionCard.className = 'card decision-card';
    gradeVal.textContent = '-';
    gradeVal.className = 'grade-badge';

    // Reset variables
    activeRetailStop = null;
    activeSmartStop = null;
    activeRetailRisk = null;
    activeSmartRisk = null;
    activeRetailTarget = null;
    activeSmartTarget = null;
    if (wallPriceDisplay) wallPriceDisplay.textContent = '-';

    // Reset Confidence indicators
    radarBullish.style.width = '0%';
    radarBullishVal.textContent = '0%';
    radarBearish.style.width = '0%';
    radarBearishVal.textContent = '0%';
    radarUncertain.style.width = '100%';
    radarUncertainVal.textContent = '100%';

    // Reset AI Reports
    aiEmptyState.classList.remove('hidden');
    aiReportBody.classList.add('hidden');
    setCioMemorandumPending();
    reportMdRender.innerHTML = `
      <div class="empty-state" id="ai-empty-state">
        <span class="icon">🤖</span>
        <p>Build the evidence dossier to render the Chief Investment Officer's memorandum.</p>
      </div>
    `;
    councilConsensusBadge.textContent = "NO CONSENSUS";
    councilConsensusBadge.className = "consensus-badge";
    councilAgentsGrid.classList.add('is-empty');
    councilAgentsGrid.innerHTML = `
      <div class="empty-council-state">
        <span class="icon">💬</span>
        <p>Activate the live stream or trigger a scan to build the committee dossier.</p>
      </div>
    `;

    // Reset warnings and context
    macroAlertBanner.classList.add('hidden');
    historicalStatsDisplay.classList.add('hidden');
    calendarListContainer.innerHTML = '<div class="empty-calendar">No economic events today.</div>';
    newsContainer.innerHTML = '<li class="empty-news">No recent headlines found.</li>';
    liquidationContainer.innerHTML = '<div class="empty-liq">No liquidation magnets loaded.</div>';

    if (regimeVal) regimeVal.textContent = '-';
    if (fundingVal) fundingVal.textContent = '-';
    if (oiVal) oiVal.textContent = '-';
    renderConfirmationEvidence({});

    renderTradeSetup(null);
    renderSignalMonitor(null);
    window.HawkEyeChart?.reset();
  }
}

async function startStream(symbol, timeframe, useAi) {
  // Clear any existing reconnect timers
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  // Detach any existing socket event handlers before closing
  if (socket) {
    socket.onclose = null;
    socket.close();
    socket = null;
  }

  userIntentDisconnect = false;
  activeStreamSymbol = symbol;
  activeStreamTimeframe = timeframe;

  if (connectBtn) {
    connectBtn.disabled = true;
    connectBtn.style.opacity = '0.7';
    connectBtn.style.cursor = 'not-allowed';
    connectBtn.textContent = '⏳ Connecting...';
  }

  if (window.Notification && Notification.permission === "default") {
    Notification.requestPermission();
  }

  let token;
  try {
    token = await ensureFreshAccessToken();
  } catch (error) {
    userIntentDisconnect = true;
    updateConnectionUI(false);
    return;
  }

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws/analyze`;

  logMsg(`Connecting WebSocket: ${wsUrl}...`, 'system');
  socket = new WebSocket(wsUrl, ['atc-auth', token]);

  socket.onopen = () => {
    logMsg('WebSocket connection active.', 'system');
    updateConnectionUI(true);

    // Subscribe
    const subPayload = { symbol, timeframe, use_ai: useAi };
    socket.send(JSON.stringify(subPayload));
    logMsg(`Subscribed to ${symbol} [${timeframe}]. AI review convened.`, 'ws-send');
  };

  socket.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.error) {
        const capacityError = payload.code === 'research_capacity_exceeded';
        logMsg(capacityError ? `Research capacity reached: ${payload.error}` : `Server Error: ${payload.error}`, 'error');
        if (capacityError) window.showAppToast?.(payload.error, 'error');
        userIntentDisconnect = true;
        if (socket) {
          socket.close(1000, "Server error reported");
        }
        return;
      }

      logMsg(`Received update tick. Price: ${payload.market.last_price}`, 'ws-receive');
      renderDashboard(payload);

      // Trigger notification
      if (window.Notification && Notification.permission === "granted") {
        const lastSignal = window.lastNotifiedSignal || "";
        const currentSigKey = `${payload.symbol}:${payload.decision}:${payload.signal_monitor?.status}`;
        if (currentSigKey !== lastSignal && (payload.decision.includes("WATCH") || payload.decision.includes("TP") || payload.decision.includes("EXIT"))) {
          new Notification(`APEX AI Signal: ${payload.symbol}`, {
            body: `Verd: ${payload.decision} | Conf: ${payload.confidence}% | Stage: ${payload.signal_monitor?.status || 'Active'}`,
          });
          window.lastNotifiedSignal = currentSigKey;
        }
      }
    } catch (e) {
      console.error(e);
      logMsg(`Message parsing error: ${e.message}`, 'error');
    }
  };

  socket.onclose = (event) => {
    socket = null;
    updateConnectionUI(false);
    logMsg(`WebSocket closed (code ${event.code}${event.reason ? `: ${event.reason}` : ''}).`, event.code === 1000 ? 'system' : 'error');
    // Only attempt reconnect if the user did NOT intentionally disconnect
    if (!userIntentDisconnect && !event.wasClean) {
      logMsg('Connection dropped unexpectedly. Reconnecting in 5s...', 'error');
      reconnectTimer = setTimeout(() => {
        if (!userIntentDisconnect && socket === null) {
          startStream(symbol, timeframe, useAi);
        }
      }, 5000);
    }
  };

  socket.onerror = (error) => {
    logMsg(`WebSocket error occurred.`, 'error');
    updateConnectionUI(false);
  };
}

function useAnalysisSnapshot(payload) {
  const snapshot = payload?.analysis_snapshot;
  if (!snapshot || snapshot.schema_version !== 'analysis_snapshot.v1') return payload;
  const causal = snapshot.causal || {};
  const execution = snapshot.execution || {};
  const telemetry = snapshot.telemetry || {};
  const research = snapshot.research || {};
  return {
    ...payload,
    symbol: snapshot.symbol || payload.symbol,
    timeframe: snapshot.timeframe || payload.timeframe,
    market: snapshot.market || payload.market,
    chart: snapshot.chart || payload.chart || {},
    data_quality: snapshot.source_coverage || payload.data_quality,
    quantitative: snapshot.quantitative || payload.quantitative,
    market_context: causal.market_context || {},
    market_structure: causal.market_structure || {},
    market_story: causal.market_story || payload.market_story || {},
    liquidity_map: causal.liquidity_map || {},
    liquidity_sweep: causal.liquidity_sweep || {},
    positioning: causal.positioning || {},
    volatility_context: causal.volatility_context || {},
    volume_profile: causal.volume_profile || {},
    vwap_context: causal.vwap_context || {},
    order_book_pressure: execution.order_book_pressure || {},
    derivatives: execution.derivatives || {},
    funding_rate: execution.derivatives?.funding_rate,
    open_interest: execution.derivatives?.open_interest,
    liquidations: execution.derivatives?.liquidations || {},
    trade_setup: execution.trade_setup || {},
    signal_monitor: execution.signal_monitor || {},
    live_confirmation: execution.live_confirmation || {},
    execution_tape: execution.execution_tape || payload.execution_tape || {},
    gates: {
      ...(payload.gates || {}),
      data_freshness: execution.data_freshness || {},
      liquidity: execution.liquidity || {},
      live_confirmation: execution.live_confirmation || {},
    },
    regime: telemetry.regime || payload.regime,
    risk_appetite_proxy: telemetry.risk_appetite_proxy || {},
    sentiment: telemetry.sentiment || {},
    news_sentiment: research.news_sentiment || { token: [], global: [] },
    calendar_events: research.calendar_events || [],
    macro_blockout: research.macro_blockout || { active: false, reason: '' },
  };
}

function renderConfirmationEvidence(data) {
  const lc = data.live_confirmation || {};
  const metrics = lc.metrics || {};
  const structChecks = lc.structure_checks || {};
  const liveChecks = lc.live_checks || {};
  const structure = data.market_structure || {};
  const marketStory = data.market_story || {};
  const firstEvidenceObject = (...values) => values.find(
    value => value && typeof value === 'object' && Object.keys(value).length > 0
  ) || {};
  const contextDirection = String(data.market_context?.direction || '').toUpperCase();
  const storyDirection = String(
    marketStory.actionability?.direction
    || structure.story_actionability?.direction
    || ''
  ).toUpperCase();
  const direction = String(
    lc.direction && lc.direction !== 'NEUTRAL'
      ? lc.direction
      : contextDirection === 'LONG'
        ? 'BULLISH'
        : contextDirection === 'SHORT'
          ? 'BEARISH'
          : storyDirection
  ).toUpperCase();
  const status = lc.status || '';

  // Helper: set value + color class
  const setVal = (el, text, cls) => {
    if (!el) return;
    el.textContent = text;
    el.className = `value font-mono ${cls || ''}`;
    el.style.fontSize = '0.75rem';
  };

  // Status badge
  if (confirmStatusBadge) {
    const isConfirmed = status === 'LIVE_CONFIRMED_REVIEW';
    const isRejected = status.includes('REJECTED');
    const isStructureRejected = status === 'STRUCTURE_REJECTED';
    const directionalPlanAvailable = structChecks.directional_plan !== false
      && direction !== '' && direction !== 'NEUTRAL';
    const observedEvidenceAvailable = marketStory.available === true
      || Boolean(structure.phase)
      || data.liquidity_sweep?.detected === true
      || data.vwap_context?.available === true
      || data.volume_profile?.available === true;
    if (isConfirmed) {
      confirmStatusBadge.textContent = '✓ CONFIRMED';
      confirmStatusBadge.className = 'telemetry-badge confirmed';
    } else if (isStructureRejected && directionalPlanAvailable) {
      confirmStatusBadge.textContent = 'STRUCTURE FAIL';
      confirmStatusBadge.className = 'telemetry-badge rejected';
    } else if (isRejected && !isStructureRejected) {
      confirmStatusBadge.textContent = 'NOT CONFIRMED';
      confirmStatusBadge.className = 'telemetry-badge rejected';
    } else if (direction && direction !== 'NEUTRAL') {
      confirmStatusBadge.textContent = 'WATCHING';
      confirmStatusBadge.className = 'telemetry-badge watching';
    } else if (observedEvidenceAvailable) {
      confirmStatusBadge.textContent = 'EVIDENCE READY';
      confirmStatusBadge.className = 'telemetry-badge watching';
    } else {
      confirmStatusBadge.textContent = 'SCANNING';
      confirmStatusBadge.className = 'telemetry-badge';
    }
  }

  // Playbook & Phase
  const playbook = metrics.playbook || '';
  const primaryPhase = metrics.primary_phase || structure.phase || '';
  const higherPhase = metrics.higher_phase || '';
  if (confirmPlaybookVal) {
    const label = playbook && playbook !== 'NONE'
      ? playbook.replace(/_/g, ' ')
      : direction && direction !== 'NEUTRAL'
        ? 'DIRECTIONAL WATCH'
        : 'NO DIRECTIONAL PLAYBOOK';
    confirmPlaybookVal.textContent = label;
    confirmPlaybookVal.style.color = playbook && playbook !== 'NONE'
      ? 'var(--neon-blue)' : 'var(--text-muted)';
  }
  if (confirmPhaseVal) {
    confirmPhaseVal.textContent = primaryPhase && higherPhase
      ? `${primaryPhase} · HTF ${higherPhase}`.replace(/_/g, ' ')
      : primaryPhase ? primaryPhase.replace(/_/g, ' ')
        : '—';
  }

  // Completed-candle structure event. Prefer the canonical selected event so
  // this card, the setup ledger, and the monitor describe the same event.
  const selectedStructureEvent = firstEvidenceObject(
    metrics.selected_structure_event,
    marketStory.latest_structure_event,
  );
  const selectedEventType = String(selectedStructureEvent.type || '').toUpperCase();
  const bos = selectedStructureEvent.detected && ['BOS', 'CHOCH'].includes(selectedEventType)
    ? selectedStructureEvent
    : firstEvidenceObject(metrics.bos, structure.bos);
  if (bos.detected) {
    const dir = String(bos.direction || '').toUpperCase();
    const eventType = marketStoryLabel(bos.type || 'BOS');
    setVal(confirmBosVal, `${dir} ${eventType}`, dir === 'BULLISH' ? 'positive' : dir === 'BEARISH' ? 'negative' : '');
  } else {
    setVal(confirmBosVal, 'NO BREAK', 'neutral');
  }

  // Structure lifecycle. Structured state replaces inference from a reason
  // sentence when the current backend supports market_story.v1.
  const compactStory = metrics.structure_story || lc.structure_story || {};
  const structureState = compactStory.setup_state || compactStory.directional_view?.state;
  const normalizedStructureState = String(
    structureState
    || structure.story_state
    || marketStory.current_state
    || marketStory.actionability?.status
    || ''
  ).toUpperCase();
  const breakoutConfirmed = structChecks.confirmed_completed_breakout;
  if (['ACTIONABLE_NOW', 'RETESTING'].includes(normalizedStructureState)) {
    setVal(confirmBreakoutVal, marketStoryLabel(normalizedStructureState), 'positive');
  } else if (normalizedStructureState === 'PULLBACK_REQUIRED') {
    setVal(confirmBreakoutVal, 'PULLBACK REQUIRED', 'warning');
  } else if (['EXTENDED_DO_NOT_CHASE', 'LATE_STRUCTURE_DO_NOT_CHASE', 'MISSED', 'EXPIRED'].includes(normalizedStructureState)) {
    setVal(confirmBreakoutVal, 'DO NOT CHASE', 'warning');
  } else if (normalizedStructureState === 'INVALIDATED') {
    setVal(confirmBreakoutVal, 'INVALIDATED', 'negative');
  } else if (normalizedStructureState === 'DEVELOPING') {
    setVal(confirmBreakoutVal, 'MONITORING', 'warning');
  } else if (normalizedStructureState) {
    setVal(confirmBreakoutVal, marketStoryLabel(normalizedStructureState), 'neutral');
  } else if (breakoutConfirmed === true) {
    setVal(confirmBreakoutVal, 'CONFIRMED', 'positive');
  } else if (breakoutConfirmed === false) {
    setVal(confirmBreakoutVal, 'AWAITED', 'warning');
  } else {
    setVal(confirmBreakoutVal, '—', 'neutral');
  }

  // Liquidity Sweep
  const sweep = firstEvidenceObject(
    metrics.sweep,
    data.liquidity_sweep,
    structure.liquidity_sweep,
    marketStory.latest_liquidity_event,
  );
  if (sweep.detected) {
    const dir = String(sweep.direction || '').replace(/_/g, ' ').toUpperCase();
    const quality = sweep.quality === 'strong' ? ' ★' : '';
    setVal(confirmSweepVal, `${dir}${quality}`,
      dir.includes('BULLISH') ? 'positive' : dir.includes('BEARISH') ? 'negative' : 'warning');
  } else {
    setVal(confirmSweepVal, 'NOT DETECTED', 'neutral');
  }

  // Relative Volume
  const rvol = Number(
    metrics.event_rvol
    ?? metrics.rvol
    ?? selectedStructureEvent.relative_volume
    ?? sweep.relative_volume
  );
  if (Number.isFinite(rvol)) {
    const passed = rvol >= 1.5;
    setVal(confirmRvolVal, `${rvol.toFixed(2)}x`, passed ? 'positive' : 'warning');
  } else {
    setVal(confirmRvolVal, '—', 'neutral');
  }

  // VWAP Acceptance
  const vwap = firstEvidenceObject(metrics.vwap, data.vwap_context);
  if (vwap.available) {
    const relation = String(vwap.price_relation || '').replace(/_/g, ' ');
    const expected = direction === 'BULLISH' ? 'ABOVE ALL' : direction === 'BEARISH' ? 'BELOW ALL' : '';
    const aligned = relation === expected;
    setVal(confirmVwapVal, relation || 'UNKNOWN', aligned ? 'positive' : 'warning');
  } else {
    setVal(confirmVwapVal, 'UNAVAILABLE', 'neutral');
  }

  // Volume Profile
  const profile = firstEvidenceObject(metrics.volume_profile, data.volume_profile);
  if (profile.available) {
    const loc = String(profile.location || '').replace(/_/g, ' ');
    setVal(confirmProfileVal, loc || 'UNKNOWN',
      loc.includes('ACCEPTANCE') ? 'positive' : 'warning');
  } else {
    setVal(confirmProfileVal, 'UNAVAILABLE', 'neutral');
  }

  // Decisive Candle
  const bodyRatio = Number(
    metrics.event_body_ratio
    ?? metrics.body_ratio
    ?? selectedStructureEvent.body_ratio
    ?? sweep.body_ratio
  );
  if (Number.isFinite(bodyRatio)) {
    const passed = bodyRatio >= 0.55;
    setVal(confirmCandleVal, `Body ${(bodyRatio * 100).toFixed(0)}%`, passed ? 'positive' : 'warning');
  } else {
    setVal(confirmCandleVal, '—', 'neutral');
  }

  // Structure Opposition (CHoCH)
  const choch = firstEvidenceObject(metrics.choch, structure.choch);
  const structNotOpposed = structChecks.structure_not_opposed;
  if (structNotOpposed === true) {
    setVal(confirmChochVal, 'CLEAR', 'positive');
  } else if (structNotOpposed === false) {
    setVal(confirmChochVal, 'OPPOSED', 'negative');
  } else if (choch.detected) {
    const dir = String(choch.direction || '').toUpperCase();
    setVal(confirmChochVal, `${dir} CHoCH`, 'negative');
  } else {
    setVal(confirmChochVal, 'CLEAR', 'neutral');
  }

  // Live Execution Check Chips
  if (confirmExecStrip) {
    const checks = [
      ['Data', liveChecks.data_complete],
      ['Spread', liveChecks.spread_within_limit],
      ['Depth', liveChecks.depth_aligned],
      ['Taker Flow', liveChecks.taker_flow_aligned],
      ['Price/OI', liveChecks.price_oi_aligned],
      ['Funding', liveChecks.funding_not_crowded],
      ['Execution', liveChecks.execution_evidence_confirmed],
      ['Actual Flow', liveChecks.actual_flow_aligned],
      ['Tape Opposition', liveChecks.actual_flow_not_opposed],
      ['Depth Capacity', liveChecks.execution_capacity_sufficient],
    ];
    confirmExecStrip.innerHTML = checks.map(([label, passed]) => {
      const cls = passed === true ? 'passed' : passed === false ? 'failed' : '';
      return `<span class="confirm-exec-chip ${cls}"><span class="exec-dot"></span>${label}</span>`;
    }).join('');
  }

  const executionTape = data.execution_tape || {};
  const actualFlow = executionTape.actual_flow || lc.live_evidence?.actual_flow_evidence || {};
  const flowAvailable = actualFlow.available === true;
  const flowBias = String(actualFlow.bias || 'UNAVAILABLE').toUpperCase();
  const flowVerdict = String(actualFlow.status || 'UNAVAILABLE').toUpperCase();
  const networkStatus = String(executionTape.status || 'UNAVAILABLE').toUpperCase();
  const setTapeValue = (element, text, tone = 'neutral') => {
    if (!element) return;
    element.textContent = text;
    element.dataset.tone = tone;
  };
  const compactUsd = (value) => value >= 1_000_000
    ? `$${(value / 1_000_000).toFixed(1)}M`
    : value >= 1_000
      ? `$${(value / 1_000).toFixed(1)}K`
      : `$${Math.round(value || 0).toLocaleString()}`;
  const flowTone = flowBias === 'BULLISH'
    ? 'positive'
    : flowBias === 'BEARISH'
      ? 'negative'
      : flowVerdict.includes('ABSORBED') || flowVerdict.includes('EXHAUSTION')
        ? 'warning'
        : 'neutral';
  const buyNotional = Number(actualFlow.buy_notional);
  const sellNotional = Number(actualFlow.sell_notional);
  const netDelta = Number(actualFlow.net_delta_usd);
  const buyRatio = Number(actualFlow.aggressive_buy_ratio);
  setTapeValue(
    tapeVerdictValue,
    flowAvailable
      ? `${flowVerdict.replace(/_/g, ' ')} · ${flowBias}`
      : 'WARMING PUBLIC TAPE',
    flowAvailable ? flowTone : 'neutral',
  );
  setTapeValue(
    tapeAggressorValue,
    String(actualFlow.active_aggressor || 'UNAVAILABLE').replace(/_/g, ' '),
    flowTone,
  );
  setTapeValue(
    tapeBuyValue,
    Number.isFinite(buyNotional)
      ? `${compactUsd(buyNotional)}${Number.isFinite(buyRatio) ? ` · ${(buyRatio * 100).toFixed(1)}%` : ''}`
      : 'AWAITING TRADES',
    flowBias === 'BULLISH' ? 'positive' : 'neutral',
  );
  setTapeValue(
    tapeSellValue,
    Number.isFinite(sellNotional)
      ? `${compactUsd(sellNotional)}${Number.isFinite(buyRatio) ? ` · ${((1 - buyRatio) * 100).toFixed(1)}%` : ''}`
      : 'AWAITING TRADES',
    flowBias === 'BEARISH' ? 'negative' : 'neutral',
  );
  setTapeValue(
    tapeDeltaValue,
    Number.isFinite(netDelta)
      ? `${netDelta >= 0 ? '+' : '−'}${compactUsd(Math.abs(netDelta))} · ${String(actualFlow.cvd_trend || 'FLAT')}`
      : 'UNAVAILABLE',
    flowTone,
  );
  setTapeValue(
    tapeResponseValue,
    String(actualFlow.price_response || 'UNAVAILABLE').replace(/_/g, ' '),
    flowVerdict.includes('CONFIRMED') ? 'positive' : flowAvailable ? 'warning' : 'neutral',
  );
  const absorption = String(actualFlow.absorption || 'NOT_DETECTED').toUpperCase();
  setTapeValue(
    tapeAbsorptionValue,
    absorption.replace(/_/g, ' '),
    absorption === 'NOT_DETECTED' ? 'positive' : 'warning',
  );
  const exhaustion = String(actualFlow.exhaustion || 'NONE').toUpperCase();
  setTapeValue(
    tapeExhaustionValue,
    exhaustion.replace(/_/g, ' '),
    exhaustion === 'NONE' ? 'positive' : 'warning',
  );
  const alignment = String(actualFlow.cross_market_alignment || 'UNAVAILABLE').toUpperCase();
  setTapeValue(
    tapeAlignmentValue,
    alignment.replace(/_/g, ' '),
    alignment === 'ALIGNED' ? 'positive' : alignment === 'DIVERGENT' ? 'warning' : 'neutral',
  );

  const liquidation = executionTape.observed_liquidations || {};
  const longLiquidated = Number(liquidation.long_liquidated_notional);
  const shortLiquidated = Number(liquidation.short_liquidated_notional);
  setTapeValue(
    tapeLiquidationValue,
    liquidation.observed
      ? `LONG $${Math.round(longLiquidated || 0).toLocaleString()} / SHORT $${Math.round(shortLiquidated || 0).toLocaleString()}`
      : liquidation.available
        ? 'LIVE · NO EVENTS IN WINDOW'
        : 'NOT OBSERVED / FEED UNAVAILABLE',
    liquidation.observed ? 'warning' : liquidation.available ? 'positive' : 'neutral',
  );
  const confidence = String(actualFlow.confidence || 'UNAVAILABLE').toUpperCase();
  const sourceCount = Number(actualFlow.qualified_source_count || 0);
  setTapeValue(
    tapeConfidenceValue,
    `${confidence} · ${sourceCount}/4 SOURCES`,
    confidence === 'HIGH' ? 'positive' : confidence === 'MEDIUM' ? 'warning' : 'neutral',
  );
  const capacity = lc.live_evidence?.execution_capacity || {};
  const capacityEvaluated = capacity.evaluated === true;
  const capacityLimit = Number(capacity.maximum_notional_at_10pct_depth);
  const plannedNotional = Number(capacity.planned_notional_usd);
  setTapeValue(
    tapeCapacityValue,
    capacityEvaluated
      ? `PLAN ${compactUsd(plannedNotional)} / MAX ${compactUsd(capacityLimit)}`
      : 'NOT EVALUATED',
    capacityEvaluated
      ? (liveChecks.execution_capacity_sufficient ? 'positive' : 'negative')
      : 'neutral',
  );

  if (tapeEvidenceStatus) {
    tapeEvidenceStatus.textContent = networkStatus;
    tapeEvidenceStatus.className = `venue-evidence-badge ${networkStatus === 'HEALTHY' ? 'healthy' : networkStatus === 'PARTIAL' ? 'degraded' : 'starting'}`;
  }
  if (tapeEvidenceNote) {
    tapeEvidenceNote.textContent = networkStatus === 'SUBSCRIBING'
      ? 'The selected market was registered automatically. Binance and Bybit spot/perpetual feeds are warming one shared server-side tape.'
      : flowAvailable
        ? `${sourceCount}/4 sources are flow-qualified. Spot/perpetual agreement changes confidence; it is not a mandatory venue gate.`
        : 'Live aggressor evidence is partial or warming. The system keeps it unknown instead of treating missing flow as neutral.';
  }
}



function renderMarketContext(data) {
  if (!marketContextCard) return;
  const context = data.market_context || {};
  const components = context.components || {};
  const positioning = data.positioning || {};
  const liquidity = data.liquidity_map || {};
  const profile = data.volume_profile || {};
  const vwap = data.vwap_context || {};
  const volatility = data.volatility_context || {};
  const structure = data.market_structure || {};
  const direction = context.direction || 'WAIT';
  const coverage = context.coverage || {};
  const manifest = data.evidence_manifest
    || data.institutional_committee?.evidence_manifest
    || data.ai_analysis?.institutional_dossier?.evidence_manifest
    || {};
  const state = direction === 'LONG' ? 'long' : direction === 'SHORT' ? 'short' : 'wait';
  const directionalText = direction === 'WAIT' ? 'WAIT' : `${direction} CANDIDATE`;
  const score = Number(context.score);
  const componentTone = (component) => component?.bias === 'BULLISH' ? 'positive' : component?.bias === 'BEARISH' ? 'negative' : '';
  const setMetric = (element, text, component) => {
    if (!element) return;
    element.textContent = text || 'UNAVAILABLE';
    element.className = componentTone(component);
  };
  const poolText = (pool) => {
    if (!pool) return 'No mapped pool';
    const label = String(pool.kind || 'liquidity level').replace(/_/g, ' ');
    return `${label} · ${formatCurrency(pool.price)}`;
  };

  marketContextStatus.textContent = directionalText;
  marketContextStatus.className = `market-context-status ${state}`;
  marketContextScore.innerHTML = `${Number.isFinite(score) && direction !== 'WAIT' ? Math.round(score) : '--'}<small>/100</small>`;

  const phase = structure.phase || components.regime_structure?.evidence || 'UNKNOWN';
  const volatilityState = volatility.state || 'UNKNOWN';
  const positioningState = positioning.state || components.positioning?.evidence || 'UNKNOWN';
  const fundingRate = Number(positioning.funding_rate ?? data.funding_rate);
  const fundingText = [
    positioning.crowding && positioning.crowding !== 'NEUTRAL' ? positioning.crowding.replace(/_/g, ' ') : 'FUNDING NEUTRAL',
    positioning.delta_divergence && positioning.delta_divergence !== 'NONE' ? positioning.delta_divergence.replace(/_/g, ' ') : 'DELTA ALIGNED',
  ].join(' · ');
  const flowBias = components.order_flow?.bias || 'NEUTRAL';

  setMetric(contextRegime, `${String(phase).replace(/_/g, ' ')} · ${String(volatilityState).replace(/_/g, ' ')}`, components.regime_structure);
  setMetric(contextPositioning, `${String(positioningState).replace(/_/g, ' ')}${positioning.oi_change_pct !== undefined ? ` · ${Number(positioning.oi_change_pct).toFixed(2)}% OI` : ''}`, components.positioning);
  setMetric(contextOrderFlow, `${String(flowBias).replace(/_/g, ' ')} FLOW`, components.order_flow);
  setMetric(contextFunding, `${fundingText}${Number.isFinite(fundingRate) ? ` · ${(fundingRate * 100).toFixed(3)}%` : ''}`, components.positioning);
  contextLiquidityAbove.textContent = poolText(liquidity.nearest_above);
  contextLiquidityBelow.textContent = poolText(liquidity.nearest_below);
  contextProfile.textContent = profile.available ? `${String(profile.location || 'UNKNOWN').replace(/_/g, ' ')} · POC ${formatCurrency(profile.poc)}` : 'Profile unavailable';
  contextVwap.textContent = vwap.available ? String(vwap.price_relation || 'UNKNOWN').replace(/_/g, ' ') : 'VWAP unavailable';
  contextCoverage.textContent = `${coverage.available_domains || 0} / ${coverage.required_domains || 8} required domains`;

  if (validationManifest) {
    const requiredAvailable = Number(manifest.required_available || 0);
    const requiredTotal = Number(manifest.required_total || 4);
    const supplementalAvailable = Number(manifest.supplemental_available || 0);
    const supplementalTotal = Number(manifest.supplemental_total || 13);
    const coreReady = manifest.core_ready === true;
    const hasManifest = manifest.schema_version === 'evidence_manifest.v1' || Object.keys(manifest).length > 0;
    const missingRequired = Array.isArray(manifest.missing_required) ? manifest.missing_required : [];
    const unknownSupplemental = Array.isArray(manifest.unknown_supplemental) ? manifest.unknown_supplemental : [];
    validationManifest.dataset.state = coreReady ? 'ready' : hasManifest ? 'blocked' : 'waiting';
    validationManifestTitle.textContent = coreReady
      ? 'ALL PUBLICATION CONTROLS PRESENT'
      : hasManifest
        ? 'REQUIRED CONTROL MISSING'
        : 'AWAITING COMMITTEE SNAPSHOT';
    validationManifestBadge.textContent = coreReady ? 'CORE READY' : hasManifest ? 'FAIL CLOSED' : 'WAITING';
    validationManifestBadge.className = `validation-manifest-badge ${coreReady ? 'ready' : hasManifest ? 'blocked' : 'waiting'}`;
    validationRequiredValue.textContent = `${requiredAvailable} / ${requiredTotal}`;
    validationSupplementalValue.textContent = `${supplementalAvailable} / ${supplementalTotal}`;
    validationRequiredFill.style.width = `${requiredTotal ? Math.min(100, requiredAvailable / requiredTotal * 100) : 0}%`;
    validationSupplementalFill.style.width = `${supplementalTotal ? Math.min(100, supplementalAvailable / supplementalTotal * 100) : 0}%`;
    validationManifestDetail.textContent = missingRequired.length
      ? `Missing required: ${missingRequired.join(', ').replace(/_/g, ' ')}. Publication is blocked.`
      : unknownSupplemental.length
        ? `${unknownSupplemental.length} supplemental domain${unknownSupplemental.length === 1 ? '' : 's'} unavailable; none are counted as neutral evidence.`
        : 'Required controls and all measured supplemental domains are present.';
  }

  const contradictions = Array.isArray(context.contradictions) ? context.contradictions : [];
  marketContextContradictions.hidden = contradictions.length === 0;
  marketContextContradictions.textContent = contradictions.length ? `Contradictory evidence: ${contradictions.join(', ').replace(/_/g, ' ')}.` : '';
  const limitations = context.limitations || liquidity.limitations || [];
  contextLimitations.textContent = Array.isArray(limitations) && limitations.length ? limitations[0] : 'Evidence coverage updates with every completed snapshot.';

  const reasons = [];
  if (positioningState !== 'UNKNOWN') reasons.push(String(positioningState).replace(/_/g, ' ').toLowerCase());
  if (components.order_flow?.bias && components.order_flow.bias !== 'NEUTRAL') reasons.push(`${String(components.order_flow.bias).toLowerCase()} order flow`);
  if (data.liquidity_sweep?.detected) reasons.push('a completed liquidity sweep');
  marketContextSummary.textContent = direction === 'WAIT'
    ? 'No aligned causal setup yet. The system is waiting for regime, liquidity, positioning, and flow to agree.'
    : `The ${direction.toLowerCase()} context is supported by ${reasons.join(', ') || 'available market-context evidence'}.`;
}

function renderDashboard(data) {
  data = useAnalysisSnapshot(data);
  stopLoaderAnimation();
  activeHistoricalStats = data.historical_stats || null;
  if (data.report_md) {
    window.lastReportMd = data.report_md;
  } else if (data.cio_result && data.cio_result.report_md) {
    window.lastReportMd = data.cio_result.report_md;
  }
  renderMarketContext(data);
  window.HawkEyeChart?.consume(data.chart);

  // Macro Alert Banner
  if (data.macro_blockout && data.macro_blockout.active) {
    showMacroAlert(`High Impact Macro Alert: ${data.macro_blockout.reason} Restricted sizing active.`);
  } else {
    window.clearTimeout(macroAlertDismissTimer);
    macroAlertDismissTimer = null;
    macroAlertBanner?.classList.add('hidden');
    isMacroAlertDismissed = false; // Reset dismissal state when macro clears
  }

  // RAG stats
  if (data.historical_stats && data.historical_stats.similar_setups_count > 0) {
    historicalStatsDisplay.classList.remove('hidden');
    historicalStatsText.textContent = `RAG Memory Matches: ${data.historical_stats.similar_setups_count} similar cycles (Win Rate: ${data.historical_stats.historical_win_rate}%)`;
  } else {
    historicalStatsDisplay.classList.add('hidden');
  }

  // US Calendar
  if (data.calendar_events && data.calendar_events.length > 0) {
    calendarListContainer.innerHTML = '';
    data.calendar_events.forEach(event => {
      const item = document.createElement('div');
      const imp = (event.importance || 'LOW').toLowerCase();
      const title = event.title || event.event || event.name || 'Macro Economic Event';
      let timeDisplay = event.time || event.date || 'Today';
      try {
        if (timeDisplay && (timeDisplay.includes('T') || timeDisplay.includes('-'))) {
          const dt = new Date(timeDisplay);
          if (!isNaN(dt.getTime())) {
            timeDisplay = dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
          }
        }
      } catch (e) { }

      item.className = `calendar-item ${imp}`;
      item.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.25rem;">
          <strong style="font-size: 0.8rem; color: var(--text-primary);">${title}</strong>
          <span class="importance-badge ${imp}" style="font-size: 0.65rem; font-weight: 800; padding: 2px 6px; border-radius: 4px; text-transform: uppercase;">${event.importance || 'LOW'}</span>
        </div>
        <div style="font-size: 0.72rem; color: var(--text-muted);">
          Time: ${timeDisplay} | Country: ${event.country || 'US'}
        </div>
      `;
      calendarListContainer.appendChild(item);
    });
  } else {
    calendarListContainer.innerHTML = '<div class="empty-calendar">No high-impact events scheduled.</div>';
  }

  // Live Price Ticker Flashing
  const currentPrice = parseFloat(data.market.last_price);
  if (lastPrice !== null) {
    if (currentPrice > lastPrice) {
      priceVal.style.color = 'var(--neon-green)';
      setTimeout(() => priceVal.style.color = '', 400);
    } else if (currentPrice < lastPrice) {
      priceVal.style.color = 'var(--neon-red)';
      setTimeout(() => priceVal.style.color = '', 400);
    }
  }
  lastPrice = currentPrice;
  priceVal.textContent = formatCurrency(currentPrice);

  const rawChange = parseFloat(data.market.price_change_pct_24h || 0);
  changeVal.textContent = (rawChange >= 0 ? '+' : '') + rawChange.toFixed(2) + '%';
  changeVal.style.color = rawChange >= 0 ? 'var(--neon-green)' : 'var(--neon-red)';

  volumeVal.textContent = formatVolume(parseFloat(data.market.quote_volume_24h || 0));

  // Signal Confirmation Evidence
  renderConfirmationEvidence(data);

  // Actual bid/ask spread from this same snapshot's order book.  BBW is a
  // volatility transform, not an execution spread.
  const orderBookPressure = data.order_book_pressure || {};
  const snapshotSpread = Number(orderBookPressure.spread_pct);
  spreadVal.textContent = Number.isFinite(snapshotSpread) ? `${snapshotSpread.toFixed(3)}%` : '-';

  // Decision room
  const decision = data.market_decision || data.decision || 'HOLD';
  decisionVal.textContent = decision;

  if (gradeVal) {
    gradeVal.textContent = data.trade_grade || 'C';
    const cleanGrade = (data.trade_grade || 'C').toLowerCase().replace('+', '-plus');
    gradeVal.className = `grade-badge ${cleanGrade}`;
    gradeVal.style.display = 'inline-block';
  }

  // Add card colors based on side
  decisionCard.className = 'card decision-card';
  const side = data.signal_monitor?.side || '';
  if (side === 'LONG' || decision.includes('BUY')) {
    decisionCard.className = 'card decision-card buy';
  } else if (side === 'SHORT' || decision.includes('SELL')) {
    decisionCard.className = 'card decision-card sell';
  } else if (decision === 'WATCH' || decision.includes('WATCH')) {
    decisionCard.className = 'card decision-card watch';
  } else if (decision === 'AVOID') {
    decisionCard.className = 'card decision-card avoid';
  } else {
    decisionCard.className = 'card decision-card hold';
  }

  // Update Pie/Radar bars
  const confidence = data.confidence || 0;
  if (side === 'LONG' || decision.includes('BUY')) {
    radarBullish.style.width = confidence + '%';
    radarBullishVal.textContent = confidence + '%';
    radarBearish.style.width = '3%';
    radarBearishVal.textContent = '3%';
    radarUncertain.style.width = (100 - confidence - 3) + '%';
    radarUncertainVal.textContent = (100 - confidence - 3) + '%';
  } else if (side === 'SHORT' || decision.includes('SELL')) {
    radarBearish.style.width = confidence + '%';
    radarBearishVal.textContent = confidence + '%';
    radarBullish.style.width = '3%';
    radarBullishVal.textContent = '3%';
    radarUncertain.style.width = (100 - confidence - 3) + '%';
    radarUncertainVal.textContent = (100 - confidence - 3) + '%';
  } else {
    radarUncertain.style.width = '100%';
    radarUncertainVal.textContent = '100%';
    radarBullish.style.width = '0%';
    radarBullishVal.textContent = '0%';
    radarBearish.style.width = '0%';
    radarBearishVal.textContent = '0%';
  }

  // Dynamic signals monitoring and setups
  const signal = data.signal || {};
  const structureStory = data.live_confirmation?.structure_story || {};
  renderTradeSetup(data.trade_setup, data.market_story || {}, structureStory);
  renderSignalMonitor({
    ...(data.signal_monitor || {}),
    structure_story: data.signal_monitor?.structure_story || structureStory || {},
    market_story: data.signal_monitor?.market_story || data.market_story || {},
    market_story_actionability: (
      data.signal_monitor?.market_story_actionability
      || structureStory?.directional_view
      || data.market_context?.actionability
      || {}
    ),
  });

  // Derivatives (regime, funding, OI)
  if (regimeVal) regimeVal.textContent = (data.regime || 'RANGING').replace(/_/g, ' ');
  if (fundingVal && data.funding_rate !== undefined) {
    fundingVal.textContent = (data.funding_rate * 100).toFixed(4) + '%';
  }
  if (oiVal && data.open_interest !== undefined) {
    oiVal.textContent = formatVolume(data.open_interest);
  }

  // Price Magnets
  if (liquidationContainer && data.liquidations && data.liquidations.nearest_short_magnet !== undefined) {
    liquidationContainer.innerHTML = '';
    const liq = data.liquidations;

    const shortItem = document.createElement('div');
    shortItem.className = 'liquidation-item';
    const shortBarWidth = Math.max(10, Math.min(100, 100 - (liq.short_distance_pct * 15)));
    shortItem.innerHTML = `
      <div class="liquidation-info">
        <span>Modelled short-liquidity zone</span>
        <span style="color: var(--neon-red)">$${formatCurrency(liq.nearest_short_magnet)}</span>
      </div>
      <div class="liquidation-bar-container">
        <div class="liquidation-bar short" style="width: ${shortBarWidth}%"></div>
      </div>
      <div class="liquidation-meta">
        <span>Distance: +${liq.short_distance_pct.toFixed(2)}% | Strength: ${liq.short_magnet_strength}/99</span>
        <span>Proxy OI weight: ${formatVolume(liq.estimated_short_liquidity)}</span>
      </div>
    `;

    const longItem = document.createElement('div');
    longItem.className = 'liquidation-item';
    const longBarWidth = Math.max(10, Math.min(100, 100 - (liq.long_distance_pct * 15)));
    longItem.innerHTML = `
      <div class="liquidation-info">
        <span>Modelled long-liquidity zone</span>
        <span style="color: var(--neon-green)">$${formatCurrency(liq.nearest_long_magnet)}</span>
      </div>
      <div class="liquidation-bar-container">
        <div class="liquidation-bar long" style="width: ${longBarWidth}%"></div>
      </div>
      <div class="liquidation-meta">
        <span>Distance: -${liq.long_distance_pct.toFixed(2)}% | Strength: ${liq.long_magnet_strength}/99</span>
        <span>Proxy OI weight: ${formatVolume(liq.estimated_long_liquidity)}</span>
      </div>
    `;

    liquidationContainer.appendChild(shortItem);
    liquidationContainer.appendChild(longItem);
  } else if (liquidationContainer) {
    liquidationContainer.innerHTML = '<div class="empty-liq">No liquidation magnets found.</div>';
  }

  // Risk Sizing info populate
  const riskSetup = data.trade_setup;
  if (riskSetup && riskSetup.status !== "NO_TRADE") {
    riskSide.textContent = riskSetup.side;
    riskSide.style.color = riskSetup.side === 'LONG' ? 'var(--neon-green)' : 'var(--neon-red)';
    riskEntry.textContent = formatCurrency(riskSetup.entry?.reference);

    activeRetailStop = parseFloat(riskSetup.stop?.selected);
    activeSmartStop = parseFloat(riskSetup.stop?.selected);
    activeRetailRisk = parseFloat(riskSetup.stop?.risk_per_unit);
    activeSmartRisk = parseFloat(riskSetup.stop?.risk_per_unit);
    activeRetailTarget = parseFloat(riskSetup.targets?.tp1_1r);
    activeSmartTarget = parseFloat(riskSetup.targets?.tp3_3r);

    if (riskSetup.stop?.method) {
      wallPriceDisplay.textContent = riskSetup.stop.method;
    } else {
      wallPriceDisplay.textContent = 'None';
    }
  } else {
    riskSide.textContent = 'NO TRADE';
    riskSide.style.color = '';
    riskEntry.textContent = '-';
    riskStop.textContent = '-';
    riskTarget.textContent = '-';

    activeRetailStop = null;
    activeSmartStop = null;
    activeRetailRisk = null;
    activeSmartRisk = null;
    activeRetailTarget = null;
    activeSmartTarget = null;
    wallPriceDisplay.textContent = '-';
  }
  recalculateSizing();

  // GDELT news
  lastReceivedNews = data.news_sentiment || null;
  renderNewsList();

  // Institutional evidence and controls rendering
  const ai = data.ai_analysis;
  if (ai && !ai.error) {
    // Set processing ticker status
    if (councilProcessingTicker) {
      councilProcessingTicker.className = "processing-ticker active-live";
      const txt = councilProcessingTicker.querySelector('.ticker-text');
      if (txt) txt.textContent = "FEED ACTIVE";
    }
    aiEmptyState.classList.add('hidden');
    aiReportBody.classList.remove('hidden');
    const confidence = ai.confidence_pct || 0;
    aiConfidenceVal.textContent = confidence;

    // Update the overall circular SVG gauge ring
    const ring = document.getElementById('overall-gauge-ring');
    const percentText = document.getElementById('overall-gauge-percent');
    if (ring && percentText) {
      // circumference = 2 * PI * r = 2 * 3.14159 * 46 = 289
      const offset = 289 - (289 * confidence) / 100;
      ring.style.strokeDashoffset = offset;
      percentText.textContent = `${confidence}%`;

      let strokeColor = 'var(--neon-blue)';
      if (decision.includes('BUY')) strokeColor = 'var(--neon-green)';
      else if (decision.includes('SELL')) strokeColor = 'var(--neon-red)';
      else if (decision.includes('OFFLINE') || decision.includes('WAIT')) strokeColor = 'var(--neon-gold)';

      ring.style.stroke = strokeColor;
    }

    // Tally consensus badge
    const agreement = data.ai_analysis?.agent_agreement || {};
    councilConsensusBadge.textContent = `${decision} [${agreement.bullish || 0}B / ${agreement.bearish || 0}S / ${agreement.neutral || 0}N]`;
    if (decision === 'BUY_WATCH') {
      councilConsensusBadge.className = "consensus-badge bullish";
    } else if (decision === 'SELL_WATCH') {
      councilConsensusBadge.className = "consensus-badge bearish";
    } else {
      councilConsensusBadge.className = "consensus-badge neutral";
    }

    renderCioMemorandum(data, ai, decision, confidence);

    function escapeDossierText(value) {
      return String(value ?? '').replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]);
    }

    function dossierList(items, emptyCopy) {
      const rows = Array.isArray(items) ? items.filter(Boolean) : [];
      return rows.length
        ? `<ul class="dossier-list">${rows.map(item => `<li>${escapeDossierText(item)}</li>`).join('')}</ul>`
        : `<p class="dossier-narrative">${escapeDossierText(emptyCopy)}</p>`;
    }

    // Global Agent Dossier Modal Controller
    window.openAgentDossierModal = function (title, dataJsonStr) {
      const modal = document.getElementById('agent-dossier-modal');
      if (!modal) return;
      let data = {};
      try {
        data = typeof dataJsonStr === 'string' ? JSON.parse(decodeURIComponent(dataJsonStr)) : dataJsonStr;
      } catch (e) {
        data = {};
      }

      const agentDetailsMap = {
        'Quant Research Engine': { icon: '∑', desc: 'Probability, Expected Value & Regime' },
        'Market Microstructure Engine': { icon: '📊', desc: 'Depth, Flow, Liquidity & Execution Limits' },
        'Derivatives Engine': { icon: '📈', desc: 'Funding, OI, Positioning & Coverage Gaps' },
        'Macro Intelligence Engine': { icon: '🌐', desc: 'Rates, DXY, Liquidity & Event Risk' },
        'Risk Committee': { icon: '🛡️', desc: 'EV, Drawdown, Exposure & Allocation Vetoes' },
        'Adversarial Review Engine': { icon: '⚖️', desc: 'Contradictions, Failure Modes & Falsification' }
      };

      const details = agentDetailsMap[title] || { icon: '🤖', desc: 'Co-Pilot Analysis Role' };
      const iconEl = document.getElementById('dossier-modal-icon');
      const titleEl = document.getElementById('dossier-modal-title');
      const subtitleEl = document.getElementById('dossier-modal-subtitle');
      const badgeEl = document.getElementById('dossier-modal-badge');
      const bodyEl = document.getElementById('dossier-modal-body');

      if (iconEl) {
        iconEl.textContent = details.icon;
        iconEl.classList.add('dossier-modal-icon-shell');
      }
      if (titleEl) titleEl.textContent = title;
      if (subtitleEl) subtitleEl.textContent = details.desc;

      const bias = data.bias || (data.severity_score !== undefined ? `Severity: ${data.severity_score}/10` : data.status || '');
      const convictionVal = data.confidence_pct !== undefined ? data.confidence_pct : (data.severity_score !== undefined ? data.severity_score * 10 : (data.approved_for_allocation ? 100 : 0));

      let badgeStyle = "background: rgba(255,255,255,0.05); color: var(--text-secondary);";
      if (bias === "BULLISH") {
        badgeStyle = "background: rgba(80, 205, 137, 0.2); color: #50cd89; border: 1px solid rgba(80, 205, 137, 0.5); font-weight: 800;";
      } else if (bias === "BEARISH") {
        badgeStyle = "background: rgba(241, 65, 108, 0.2); color: #f1416c; border: 1px solid rgba(241, 65, 108, 0.5); font-weight: 800;";
      }

      if (badgeEl) {
        badgeEl.innerHTML = `
      <span class="dossier-status-badge" style="${badgeStyle}">${escapeDossierText(bias || 'NEUTRAL')}</span>
      <span class="dossier-status-badge" style="background: rgba(255,255,255,.05); color: var(--text-primary); border: 1px solid rgba(255,255,255,.1);">${escapeDossierText(convictionVal)}% CONFIDENCE</span>
    `;
      }

      // Extract narrative text
      let narrativeText = data.narrative || data.analysis || data.contrarian_view || data.pre_mortem_critique || data.justification || data.details || data.critique || data.summary || '';
      if (!narrativeText && Array.isArray(data.evidence)) {
        const evidence = data.evidence.map(item => `- **${item.metric || 'Evidence'}:** ${JSON.stringify(item.value)} (${item.source || 'source unavailable'})`);
        const contradictions = (data.contradictory_evidence || []).map(item => `- ${item}`);
        const unknowns = (data.unknowns || []).map(item => `- ${item}`);
        narrativeText = [
          evidence.length ? `### Measured Evidence\n${evidence.join('\n')}` : '',
          contradictions.length ? `### Contradictory Evidence\n${contradictions.join('\n')}` : '',
          unknowns.length ? `### Unknowns\n${unknowns.join('\n')}` : '',
        ].filter(Boolean).join('\n\n');
      }

      // If narrative is a generic placeholder or missing, extract matching section from lastReportMd
      if ((!narrativeText || narrativeText.includes("Derived from") || narrativeText.length < 35) && window.lastReportMd) {
        const titleIndexMap = {
          'Market Structure Analyst': 1,
          'Order Flow Specialist': 2,
          'Derivatives Analyst': 3,
          'Macro Strategist': 4,
          'Sentiment Analyst': 5,
          'Quant Analyst': 6,
          'Risk Manager': 7,
          "Devil's Advocate": 8,
          "Pre-Mortem Analyst": 9
        };
        const titleKeywords = {
          'Market Structure Analyst': ['market structure', 'structure'],
          'Order Flow Specialist': ['order flow', 'flow'],
          'Derivatives Analyst': ['derivatives', 'positioning'],
          'Macro Strategist': ['macro', 'calendar'],
          'Sentiment Analyst': ['sentiment', 'narrative'],
          'Quant Analyst': ['quant'],
          'Risk Manager': ['risk manager', 'risk'],
          "Devil's Advocate": ['devil', 'advocate'],
          "Pre-Mortem Analyst": ['pre-mortem', 'pre mortem', 'failure']
        };

        const targetIdx = titleIndexMap[title];
        const sections = window.lastReportMd.split(/\n(?=#{1,4}\s+|\b\d+\.\s+)/g);
        let foundSection = null;

        if (targetIdx) {
          const numRegex = new RegExp(`^(?:#{1,4}\\s*)?${targetIdx}\\.\\s+`);
          for (const sec of sections) {
            if (numRegex.test(sec.trim())) {
              foundSection = sec.trim();
              break;
            }
          }
        }

        if (!foundSection) {
          const keywords = titleKeywords[title] || [];
          for (const sec of sections) {
            const secLower = sec.toLowerCase();
            if (keywords.some(kw => secLower.includes(kw))) {
              foundSection = sec.strip ? sec.strip() : sec.trim();
              break;
            }
          }
        }

        if (foundSection) narrativeText = foundSection;
      }

      // Extract telemetry fields for detailed grid
      const ignoreKeys = new Set([
        'bias', 'conviction', 'severity_score', 'narrative', 'analysis',
        'contrarian_view', 'pre_mortem_critique', 'justification', 'details',
        'critique', 'summary', 'error', 'status'
      ]);

      const telemetryItems = [];
      for (const [key, val] of Object.entries(data)) {
        if (!ignoreKeys.has(key) && val !== null && val !== undefined && val !== '') {
          const formattedKey = key.replace(/_/g, ' ').toUpperCase();
          let formattedVal = val;
          if (typeof val === 'object') formattedVal = JSON.stringify(val);
          telemetryItems.push(`
        <div style="background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; padding: 8px 12px; display: flex; flex-direction: column; gap: 3px;">
          <span style="font-size: 0.6rem; color: var(--text-muted); font-weight: 700; letter-spacing: 0.5px;">${escapeDossierText(formattedKey)}</span>
          <span style="font-size: 0.78rem; color: var(--neon-blue); font-family: var(--font-mono); font-weight: 600;">${escapeDossierText(formattedVal)}</span>
        </div>
      `);
        }
      }

      const telemetryGridHtml = telemetryItems.length > 0 ? `
    <div style="margin-top: 1rem; padding-top: 0.75rem; border-top: 1px dashed rgba(255,255,255,0.1);">
      <div style="font-size: 0.72rem; font-weight: 800; color: var(--neon-blue); letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 0.5rem;">
        📊 Key Quantitative Metrics & Telemetry
      </div>
      <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 0.5rem;">
        ${telemetryItems.join('')}
      </div>
    </div>
  ` : '';

      if (bodyEl) {
        bodyEl.innerHTML = `
      <div style="font-size: 0.72rem; font-weight: 800; color: var(--neon-blue); letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 0.6rem;">
        📋 Engine Evidence Dossier
      </div>
      ${escapeDossierText(narrativeText || 'No detailed narrative provided.')}
      ${telemetryGridHtml}
    `;
      }

      if (bodyEl) {
        const evidence = Array.isArray(data.evidence) ? data.evidence : [];
        const evidenceRows = evidence.length ? evidence.map(item => `
      <div class="dossier-evidence-row">
        <div><span class="dossier-metric">${escapeDossierText(item.metric || 'Measured evidence')}</span><span class="dossier-value">${escapeDossierText(typeof item.value === 'object' ? JSON.stringify(item.value) : item.value)}</span></div>
        <div class="dossier-source">${escapeDossierText(item.source || 'Source unavailable')}</div>
      </div>`).join('') : '<p class="dossier-narrative">No measured evidence was returned by this engine.</p>';
        bodyEl.innerHTML = `
      <div class="dossier-brief">
        <div class="dossier-summary">
          <section><h4 class="dossier-section-label">Executive summary</h4><p class="dossier-narrative">${escapeDossierText(narrativeText || 'This engine returned structured evidence without a narrative summary.')}</p></section>
          <section class="dossier-thesis"><h4 class="dossier-section-label">Current stance</h4><p class="dossier-narrative">${escapeDossierText(bias || 'NEUTRAL')} bias with ${escapeDossierText(convictionVal)}% evidence confidence. This is a research input, not execution authority.</p></section>
        </div>
        <div class="dossier-evidence-grid">
          <section class="dossier-panel"><h4 class="dossier-section-label">Measured evidence</h4>${evidenceRows}</section>
          <section class="dossier-panel is-risk"><h4 class="dossier-section-label">Contradictory evidence</h4>${dossierList(data.contradictory_evidence, 'No contradictory evidence was reported.')}</section>
          <section class="dossier-panel is-unknown"><h4 class="dossier-section-label">Current unknowns</h4>${dossierList(data.unknowns, 'No additional unknowns were reported.')}</section>
          <section class="dossier-panel"><h4 class="dossier-section-label">Engine limitations</h4>${dossierList(data.limitations, 'No engine limitations were supplied.')}</section>
        </div>
      </div>`;
        bodyEl.scrollTop = 0;
      }
      modal.style.display = 'flex';
      animateModalIn(modal);
    };

    window.closeAgentDossierModal = function () {
      const modal = document.getElementById('agent-dossier-modal');
      if (modal) { modal.classList.remove('motion-open'); modal.style.display = 'none'; }
    };

    if (!window.agentDossierEscapeBound) {
      document.addEventListener('keydown', event => {
        if (event.key === 'Escape') window.closeAgentDossierModal?.();
      });
      window.agentDossierEscapeBound = true;
    }

    // Render independent evidence engines and control committees.
    if (ai.agent_reports && Object.values(ai.agent_reports).some(report => report && !report.error)) {
      councilAgentsGrid.innerHTML = '';
      councilAgentsGrid.classList.remove('is-empty');
      const agents = ai.agent_reports;

      const dossierData = new Map();
      const createAgentCardHtml = (title, data, cssClass) => {
        if (!data || data.error) return '';
        const dossierKey = crypto.randomUUID();
        dossierData.set(dossierKey, data);

        const bias = data.bias || (data.severity_score !== undefined ? `Severity: ${data.severity_score}/10` : data.status || '');

        let badgeStyle = "background: rgba(255,255,255,0.05); color: var(--text-secondary);";
        let gaugeColor = "var(--neon-blue)";

        if (bias === "BULLISH") {
          badgeStyle = "background: var(--neon-green-glow); color: var(--neon-green); border: 1px solid rgba(0,255,135,0.25);";
          gaugeColor = "var(--neon-green)";
        } else if (bias === "BEARISH") {
          badgeStyle = "background: var(--neon-red-glow); color: var(--neon-red); border: 1px solid rgba(255,0,127,0.25);";
          gaugeColor = "var(--neon-red)";
        } else if (bias.includes("Severity")) {
          const score = Number(data.severity_score || 0);
          if (score >= 6) {
            badgeStyle = "background: var(--neon-red-glow); color: var(--neon-red);";
            gaugeColor = "var(--neon-red)";
          } else {
            badgeStyle = "background: var(--neon-green-glow); color: var(--neon-green);";
            gaugeColor = "var(--neon-green)";
          }
        } else {
          gaugeColor = "var(--neon-blue)";
        }

        const agentDetailsMap = {
          'Quant Research Engine': { icon: '∑', desc: 'Probability, Expected Value & Regime' },
          'Market Microstructure Engine': { icon: '📊', desc: 'Depth, Flow, Liquidity & Execution Limits' },
          'Derivatives Engine': { icon: '📈', desc: 'Funding, OI, Positioning & Coverage Gaps' },
          'Macro Intelligence Engine': { icon: '🌐', desc: 'Rates, DXY, Liquidity & Event Risk' },
          'Risk Committee': { icon: '🛡️', desc: 'EV, Drawdown, Exposure & Allocation Vetoes' },
          'Adversarial Review Engine': { icon: '⚖️', desc: 'Contradictions, Failure Modes & Falsification' }
        };

        const details = agentDetailsMap[title] || { icon: '🤖', desc: 'Co-Pilot Analysis Role' };
        const icon = details.icon;
        const desc = details.desc;
        const convictionVal = Math.max(0, Math.min(100, Number(data.confidence_pct !== undefined ? data.confidence_pct : (data.severity_score !== undefined ? data.severity_score * 10 : (data.approved_for_allocation ? 100 : 0))) || 0));
        const biasHtml = bias ? `<span class="meta-badge" style="${badgeStyle}">${escapeDossierText(bias)}</span>` : '';

        return `
          <button type="button" class="agent-card ${cssClass}" data-dossier-key="${dossierKey}" data-dossier-title="${escapeDossierText(title)}" style="position: relative; display: flex; width: 100%; border: 0; text-align: left; align-items: center; justify-content: space-between; gap: 1rem; padding: 1rem 1.25rem; min-height: 85px; cursor: pointer; transition: all 0.2s ease;">
            <div style="position: absolute; top: 0; left: 0; width: 5px; height: 5px; border-top: 1px solid rgba(255,255,255,0.12); border-left: 1px solid rgba(255,255,255,0.12); pointer-events: none;"></div>
            <div style="position: absolute; bottom: 0; right: 0; width: 5px; height: 5px; border-bottom: 1px solid rgba(255,255,255,0.12); border-right: 1px solid rgba(255,255,255,0.12); pointer-events: none;"></div>
            
            <div style="display: flex; flex-direction: column; gap: 0.25rem; flex-grow: 1;">
              <div style="display: flex; align-items: center; gap: 0.5rem;">
                <span class="agent-icon" style="font-size: 1.15rem;">${icon}</span>
                <span class="agent-name" style="font-size: 0.8rem; font-weight: 700; color: var(--text-primary);">${escapeDossierText(title)}</span>
              </div>
              <span class="agent-subtitle" style="font-size: 0.62rem; color: var(--text-muted); font-weight: 500; letter-spacing: 0.1px;">${desc}</span>
              <div style="margin-top: 0.35rem; display: flex; align-items: center; gap: 0.5rem;">
                ${biasHtml}
                <span style="font-size: 0.62rem; color: var(--neon-blue); background: rgba(62, 151, 255, 0.12); border: 1px solid rgba(62, 151, 255, 0.3); padding: 2px 7px; border-radius: 4px; font-weight: 700;">🔍 View Dossier</span>
              </div>
            </div>

            <div class="agent-radial-gauge" style="position: relative; width: 46px; height: 46px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; background: rgba(0,0,0,0.15); border-radius: 50%; border: 1px dashed rgba(255,255,255,0.03);">
              <svg width="46" height="46" viewBox="0 0 46 46" style="transform: rotate(-90deg); width: 46px; height: 46px;">
                <circle cx="22" cy="23" r="18" fill="none" stroke="rgba(255, 255, 255, 0.04)" stroke-width="4"></circle>
                <circle cx="22" cy="23" r="18" fill="none" stroke="${gaugeColor}" stroke-width="4" stroke-dasharray="113" stroke-dashoffset="${113 - (113 * convictionVal) / 100}" style="transition: stroke-dashoffset 0.6s ease-in-out; filter: drop-shadow(0 0 3px ${gaugeColor});"></circle>
              </svg>
              <span class="font-mono" style="position: absolute; font-size: 0.6rem; font-weight: 700; color: #fff;">${convictionVal}%</span>
            </div>
          </button>
        `;
      };

      councilAgentsGrid.innerHTML += createAgentCardHtml('Quant Research Engine', agents.quant_research_engine, 'quant');
      councilAgentsGrid.innerHTML += createAgentCardHtml('Market Structure Engine', agents.market_structure_engine, 'structure');
      councilAgentsGrid.innerHTML += createAgentCardHtml('Market Microstructure Engine', agents.market_microstructure_engine, 'flow');
      councilAgentsGrid.innerHTML += createAgentCardHtml('Derivatives Engine', agents.derivatives_engine, 'derivatives');
      councilAgentsGrid.innerHTML += createAgentCardHtml('Macro Intelligence Engine', agents.macro_intelligence_engine, 'macro');
      councilAgentsGrid.innerHTML += createAgentCardHtml('Risk Committee', agents.risk_committee, 'risk');
      councilAgentsGrid.innerHTML += createAgentCardHtml('Adversarial Review Engine', agents.adversarial_review_engine, 'devil');
      councilAgentsGrid.querySelectorAll('[data-dossier-key]').forEach(card => {
        card.addEventListener('click', () => window.openAgentDossierModal(
          card.dataset.dossierTitle || 'Agent Dossier', dossierData.get(card.dataset.dossierKey) || {},
        ));
      });
    } else {
      councilAgentsGrid.classList.add('is-empty');
      councilAgentsGrid.innerHTML = `
        <div class="empty-council-state">
          <span class="icon">💬</span>
          <p>Please add Openrouter API key to activate Apex network link. Waiting for the evidence committee dossier.</p>
        </div>
      `;
    }
  } else {
    aiEmptyState.classList.remove('hidden');
    aiReportBody.classList.add('hidden');
    councilConsensusBadge.textContent = "NO CONSENSUS";
    councilConsensusBadge.className = "consensus-badge";
    councilAgentsGrid.classList.add('is-empty');
    councilAgentsGrid.innerHTML = `
      <div class="empty-council-state">
        <span class="icon">💬</span>
        <p>Build the committee dossier by streaming or triggering the scanner.</p>
      </div>
    `;
  }
}

// Config form submit
configForm.addEventListener('submit', (e) => {
  e.preventDefault();

  if (!window.atcAuthenticated) {
    logMsg('Sign in and subscription verification must finish before starting a stream.', 'error');
    return;
  }

  if (connectBtn && connectBtn.disabled) return;

  const symbol = symbolInput.value.trim().toUpperCase();
  const timeframe = timeframeSelect.value;
  const useAi = aiToggle.checked;

  if (!symbol) {
    logMsg('Please enter a valid symbol.', 'error');
    return;
  }

  // If stream is active AND the symbol and interval match the running config, disconnect it.
  // Otherwise, start the stream directly (which automatically disconnects any existing socket first).
  if (socket && symbol === activeStreamSymbol && timeframe === activeStreamTimeframe) {
    disconnectStream();
  } else {
    startStream(symbol, timeframe, useAi);
  }
});

// Dismiss Active Signal
async function dismissActiveSignal() {
  if (!activeSignalId) return;
  localStorage.setItem(`dismissed_signal_${activeSignalId}`, 'true');
  try {
    await fetch(`/signals/${activeSignalId}/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'Cancelled by operator.' }),
    });
    logMsg(`Signal #${activeSignalId} dismissed.`, 'system');
  } catch (error) {
    // If signal was already terminal on the server, cancel returns 200, but request is sent.
    logMsg(`Signal #${activeSignalId} dismissed locally.`, 'system');
  }
  renderSignalMonitor(null);
}

if (dismissSignalBtn) dismissSignalBtn.addEventListener('click', dismissActiveSignal);

function monitorControlState(reason, terms, fallback = 'OBSERVED') {
  const text = String(reason || '').toLowerCase();
  return terms.some(term => text.includes(term)) ? 'AWAITED' : fallback;
}

function renderMonitorConfirmationScenario(data) {
  if (!monitorConfirmationScenarios) return { institutional: false, tactical: false, tacticalCandidate: false };
  const scenarios = data.confirmation_scenarios || {};
  const institutional = scenarios.institutional || {};
  const tactical = scenarios.tactical || {};
  const tacticalCandidate = Boolean(tactical.candidate || tactical.passed);
  const selected = institutional.passed ? institutional : (tacticalCandidate ? tactical : null);
  const isInstitutional = selected === institutional;
  const isTacticalConfirmed = selected === tactical && Boolean(tactical.passed);
  if (!selected) {
    monitorConfirmationScenarios.hidden = true;
    monitorConfirmationScenarios.innerHTML = '';
    return { institutional: false, tactical: false, tacticalCandidate: false };
  }
  const setup = data.candidate_setup || {};
  const entry = setup.entry || {};
  const stop = setup.stop || {};
  const scenarioStoryState = String(selected.market_story_state || '').toUpperCase();
  const scenarioInvalidated = scenarioStoryState === 'INVALIDATED';
  const scenarioStale = ['EXTENDED_DO_NOT_CHASE', 'LATE_STRUCTURE_DO_NOT_CHASE', 'PULLBACK_REQUIRED', 'MISSED', 'EXPIRED'].includes(scenarioStoryState);
  const scenarioActionable = selected.market_story_actionable === true && !scenarioInvalidated && !scenarioStale;
  const levelClass = `${isInstitutional ? 'institutional' : 'tactical'}${scenarioInvalidated ? ' story-invalidated' : scenarioStale ? ' story-stale' : scenarioActionable ? ' story-actionable' : ''}`;
  const headline = isInstitutional
    ? 'Higher-timeframe and primary-timeframe evidence are aligned.'
    : 'Primary-timeframe evidence is valid; higher timeframe is not aligned.';
  const label = isInstitutional ? 'INSTITUTIONAL CONFIRMATION' : 'TACTICAL CONFIRMATION · LOWER CONFIDENCE';
  const htfState = String(selected.higher_timeframe_state || '').toUpperCase();
  const defaultHeadline = isInstitutional
    ? headline
    : isTacticalConfirmed
      ? headline
      : 'Primary-timeframe scenario is mapped; its required proof is still awaited.';
  const defaultLabel = isInstitutional
    ? label
    : isTacticalConfirmed
      ? label
      : 'PRIMARY-TIMEFRAME WATCH · NOT TRADEABLE';
  const displayHeadline = scenarioInvalidated
    ? 'The selected completed-candle event has been invalidated.'
    : scenarioStale
      ? scenarioStoryState === 'PULLBACK_REQUIRED'
        ? 'The breakout is not an entry; a completed pullback is required.'
        : 'The selected event is outside its fresh-entry window.'
      : scenarioActionable
        ? 'A fresh primary-timeframe event is inside its review window.'
        : defaultHeadline;
  const displayLabel = scenarioInvalidated
    ? 'MARKET STORY INVALIDATED · WAIT FOR NEW EVENT'
    : scenarioStale
      ? `${marketStoryLabel(scenarioStoryState)} · DO NOT CHASE`
      : defaultLabel;
  const scenarioReason = selected.market_story_reason || selected.reason || 'Measured evidence is aligned for this scenario.';
  const displayHtfLabel = selected.higher_timeframe_aligned
    ? 'ALIGNED'
    : htfState === 'UNAVAILABLE'
      ? 'UNAVAILABLE'
      : 'NOT ALIGNED';
  monitorConfirmationScenarios.hidden = false;
  monitorConfirmationScenarios.innerHTML = `
    <div class="monitor-confirmation-panel ${levelClass}">
      <div>
        <span class="monitor-confirmation-kicker">${displayLabel}</span>
        <strong>${displayHeadline}</strong>
        <p>${cioSafeText(scenarioReason)}</p>
      </div>
      <div class="monitor-confirmation-levels" aria-label="Scenario setup levels">
        <span>SIDE<b>${cioSafeText(setup.side || data.side || '—')}</b></span>
        <span>ENTRY<b>${cioSafeText(formatCurrency(entry.reference))}</b></span>
        <span>STOP<b>${cioSafeText(formatCurrency(stop.selected || stop.current))}</b></span>
        <span>HTF<b>${displayHtfLabel}</b></span>
      </div>
    </div>`;
  return { institutional: Boolean(institutional.passed), tactical: Boolean(tactical.passed), tacticalCandidate };
}

function renderMonitorEvidenceState(data, status, isPublished, isTerminalFailure) {
  const scenarios = data.confirmation_scenarios || {};
  const institutionalConfirmed = Boolean(scenarios.institutional?.passed);
  const tactical = scenarios.tactical || {};
  const tacticalReady = Boolean(tactical.passed) && !institutionalConfirmed;
  const tacticalCandidate = Boolean(tactical.candidate || tactical.passed) && !institutionalConfirmed;
  const structureStory = (
    data.structure_story
    || data.live_confirmation?.structure_story
    || data.gates?.live_confirmation?.structure_story
    || {}
  );
  const marketStory = data.market_story
    || (structureStory?.schema_version === 'market_story.v1' ? structureStory : {})
    || {};
  const story = resolveMarketStory({
    marketStory,
    structureStory,
    setupStory: data.market_story_actionability || {},
    preferredDirection: data.side === 'LONG' ? 'BULLISH' : data.side === 'SHORT' ? 'BEARISH' : '',
  });
  renderMarketStorySurface({
    panel: monitorMarketStory,
    state: monitorStoryState,
    direction: monitorStoryDirection,
    event: monitorStoryEvent,
    age: monitorStoryAge,
    level: monitorStoryLevel,
    invalidation: monitorStoryInvalidation,
    happened: monitorStoryHappened,
    now: monitorStoryNow,
    next: monitorStoryNext,
    failure: monitorStoryFailure,
    action: monitorStoryAction,
  }, story);

  // Prefer the primary-timeframe evidence gap when its playbook exists. The
  // institutional HTF-rejection sentence is still preserved in the scenario.
  const reason = (tacticalCandidate && tactical.reason) || data.reason || data.approval?.blockers?.[0] || 'Waiting for qualified setup.';
  const reasonLower = reason.toLowerCase();
  const hasStructuredStory = Boolean(
    structureStory?.schema_version
    || marketStory?.schema_version
    || story.actionabilityKnown,
  );
  const storyState = String(story.state || 'NO_ACTIVE_EVENT').toUpperCase();
  const invalidatedStory = hasStructuredStory && storyState === 'INVALIDATED';
  const staleStory = hasStructuredStory && (
    story.chaseProhibited
    || ['EXTENDED_DO_NOT_CHASE', 'LATE_STRUCTURE_DO_NOT_CHASE', 'PULLBACK_REQUIRED', 'MISSED', 'EXPIRED'].includes(storyState)
  ) && !invalidatedStory;
  const actionableStory = hasStructuredStory && story.actionable && !story.chaseProhibited;
  const waitingForBreak = hasStructuredStory
    ? ['DEVELOPING', 'NO_ACTIVE_EVENT', 'NO_DIRECTION', 'UNAVAILABLE'].includes(storyState)
    : /has not closed through|structure level|completed.*break|breakout/.test(reasonLower);
  const blocked = isTerminalFailure;
  const state = blocked
    ? 'blocked'
    : isPublished
      ? 'published'
      : invalidatedStory
        ? 'invalidated'
        : staleStory
          ? 'stale'
          : actionableStory
            ? 'actionable'
            : tacticalCandidate
              ? 'tactical'
              : waitingForBreak
                ? 'waiting'
                : 'scanning';
  const stateCopy = blocked
    ? 'TRADE CLOSED / INVALIDATED'
    : isPublished
      ? 'SIGNAL PUBLISHED'
      : invalidatedStory
        ? 'MARKET STORY INVALIDATED'
        : staleStory
          ? storyState === 'PULLBACK_REQUIRED'
            ? 'BREAKOUT BLOCKED · WAIT FOR PULLBACK'
            : 'MOVE CONSUMED · DO NOT CHASE'
          : actionableStory
            ? storyState === 'RETESTING' ? 'RETEST IN REVIEW WINDOW' : 'FRESH EVENT · ACTIONABLE WATCH'
            : tacticalReady
              ? 'TACTICAL WATCH'
              : tacticalCandidate
                ? 'PRIMARY-TIMEFRAME WATCH'
                : waitingForBreak
                  ? 'WAITING FOR A FRESH COMPLETED EVENT'
                  : 'EVIDENCE WATCH';
  const title = blocked ? 'Published lifecycle has reached a terminal state'
    : isPublished ? 'Published signal is under lifecycle control'
      : invalidatedStory ? 'The prior market event no longer supports this setup'
        : staleStory ? storyState === 'PULLBACK_REQUIRED'
          ? 'A completed pullback is required before entry review'
          : 'The directional move is already consumed'
          : actionableStory ? (storyState === 'RETESTING'
            ? 'Price is retesting a fresh completed-candle event'
            : 'A fresh completed-candle event is ready for review')
            : tacticalReady ? 'Primary-timeframe setup is ready for tactical review'
              : tacticalCandidate ? 'Primary-timeframe scenario is awaiting measured proof'
                : waitingForBreak ? 'Fresh completed structure event awaited'
                  : 'Monitoring for a qualified causal alignment';
  const detail = blocked ? reason
    : isPublished ? 'Entry, stop, targets, and outcome controls are now active for this published signal.'
      : invalidatedStory || staleStory || actionableStory ? story.happeningNow
        : tacticalReady ? 'The setup is visible above, but only higher-timeframe alignment can upgrade it to institutional confirmation.'
          : tacticalCandidate ? reason
            : waitingForBreak ? story.happeningNow
              : reason;

  if (monitorEvidenceHero) monitorEvidenceHero.className = `monitor-evidence-hero ${state}`;
  if (monitorEvidenceState) monitorEvidenceState.textContent = `EVIDENCE STATE · ${stateCopy}`;
  if (monitorEvidenceTitle) monitorEvidenceTitle.textContent = title;
  if (monitorEvidenceDetail) monitorEvidenceDetail.textContent = detail;
  if (monitorEvidenceMark) monitorEvidenceMark.textContent = blocked || invalidatedStory ? '!' : isPublished ? '✓' : staleStory ? '×' : actionableStory ? '◎' : tacticalCandidate || waitingForBreak ? '↗' : '…';
  if (monitorMissingEvidence) monitorMissingEvidence.classList.toggle('hidden', isPublished || blocked);
  if (monitorMissingTitle) monitorMissingTitle.textContent = invalidatedStory
    ? 'A new causal branch is required'
    : staleStory
      ? 'Fresh event required · do not chase'
      : actionableStory
        ? institutionalConfirmed ? 'Live publication controls' : 'Institutional confirmation controls'
        : tacticalReady
          ? 'Higher-timeframe alignment'
          : tacticalCandidate
            ? 'Required primary-timeframe confirmation'
            : waitingForBreak
              ? 'Next completed structure event'
              : 'Next required evidence';
  if (monitorMissingDetail) monitorMissingDetail.textContent = invalidatedStory
    ? `${story.happeningNow} ${story.next}`
    : staleStory
      ? `${story.happeningNow} ${story.next}`
      : actionableStory
        ? `${story.next} This remains a manual research watch until every publication control passes.`
        : tacticalReady
          ? 'This remains a tactical watch. It does not publish a trade signal until the higher timeframe confirms the same direction.'
          : tacticalCandidate
            ? `${reason} This is a research watch only; it cannot publish a trade signal.`
            : waitingForBreak
              ? `${story.next} Intrabar movement is not treated as confirmation.`
              : reason;

  if (monitorTradeLifecycle) monitorTradeLifecycle.classList.toggle('hidden', !isPublished);
  if (monitorWatchPlan) monitorWatchPlan.classList.toggle('hidden', isPublished);
  if (monitorWatchStepOneTitle) monitorWatchStepOneTitle.textContent = hasStructuredStory
    ? `${marketStoryLabel(story.eventType, 'Market story')} · ${marketStoryLabel(story.state)}`
    : 'Causal context mapped';
  if (monitorWatchStepOneDetail) monitorWatchStepOneDetail.textContent = hasStructuredStory
    ? story.happened
    : 'Regime, liquidity, positioning, and order flow are assessed from the same snapshot.';
  if (monitorWatchStepTwoTitle) monitorWatchStepTwoTitle.textContent = invalidatedStory
    ? 'Wait for a replacement event'
    : staleStory
      ? 'Do not chase the old move'
      : actionableStory
        ? 'Verify live causal controls'
        : tacticalCandidate
          ? 'Primary-timeframe proof awaited'
          : waitingForBreak
            ? 'Fresh completed event awaited'
            : 'Evidence control awaited';
  if (monitorWatchStepTwoDetail) monitorWatchStepTwoDetail.textContent = hasStructuredStory
    ? (invalidatedStory || staleStory ? story.next : `${story.happeningNow} ${story.next}`)
    : tacticalCandidate
      ? reason
      : waitingForBreak
        ? 'A completed close through the relevant structure level is required before any signal can be published.'
        : reason;

  if (monitorGateStrip) {
    const publicationCoverage = data.publication_coverage || data.live_confirmation?.publication_coverage || data.gates?.live_confirmation?.publication_coverage || {};
    const actualFlowEvidence = data.actual_flow_evidence || {};
    const actualFlowState = actualFlowEvidence.available === false
      ? 'UNAVAILABLE'
      : actualFlowEvidence.status
        ? String(actualFlowEvidence.status).toUpperCase().replace(/_/g, ' ')
        : 'CHECKING';
    const institutionalScenario = scenarios.institutional || {};
    const liveChecks = (
      institutionalScenario.live_checks
      || data.live_confirmation?.live_checks
      || data.gates?.live_confirmation?.live_checks
      || {}
    );
    const hasCheck = name => Object.prototype.hasOwnProperty.call(liveChecks, name);
    const checkState = (name, passed = 'CONFIRMED', failed = 'AWAITED') => (
      isPublished
        ? 'CONFIRMED'
        : hasCheck(name)
          ? liveChecks[name] === true ? passed : failed
          : 'CHECKING'
    );
    const inputsComplete = publicationCoverage.inputs_complete ?? publicationCoverage.ready;
    const publicationInputsState = inputsComplete === true
      ? 'COMPLETE'
      : inputsComplete === false
        ? 'PARTIAL'
        : 'CHECKING';
    const confirmationReady = (
      publicationCoverage.confirmation_ready
      ?? publicationCoverage.signal_publication_ready
      ?? institutionalScenario.passed
    );
    const signalConfirmationState = isPublished || confirmationReady === true
      ? 'CONFIRMED'
      : blocked
        ? 'BLOCKED'
        : confirmationReady === false
          ? 'AWAITED'
          : 'CHECKING';
    const orderFlowState = checkState(
      'taker_flow_aligned',
      actualFlowEvidence.available === false ? 'REST CONFIRMED' : 'CONFIRMED',
    );
    const macroControl = data.macro_control || {};
    const macroState = isPublished
      ? 'CONFIRMED'
      : macroControl.status
        ? String(macroControl.status).toUpperCase()
        : monitorControlState(reason, ['macro', 'calendar'], 'CLEAR');

    const structureControl = isPublished
      ? 'CONFIRMED'
      : hasStructuredStory
        ? invalidatedStory
          ? 'INVALIDATED'
          : staleStory
            ? 'DO NOT CHASE'
            : actionableStory
              ? marketStoryLabel(storyState)
              : waitingForBreak
                ? 'AWAITED'
                : marketStoryLabel(storyState, 'MONITORING')
        : waitingForBreak
          ? 'AWAITED'
          : monitorControlState(reason, ['structure', 'break'], 'MONITORING');
    const controls = [
      ['Structure', structureControl],
      ['Liquidity', checkState('spread_within_limit', 'OBSERVED')],
      ['Order flow', orderFlowState],
      ['Actual flow', actualFlowState],
      ['Positioning', checkState('price_oi_aligned')],
      ['Publication inputs', publicationInputsState],
      ['Signal confirmation', signalConfirmationState],
      ['Macro', macroState],
    ];
    const entryConfirmation = data.entry_confirmation || data.entry?.confirmation || {};
    if (status === 'PENDING_ENTRY') {
      const entryProofState = String(entryConfirmation.state || 'WAITING_FOR_ZONE').replace(/_/g, ' ');
      controls.splice(7, 0, ['Entry proof', entryProofState]);
    }
    const verifiedStates = new Set(['CONFIRMED', 'CONFIRMED AT PUBLICATION', 'REST CONFIRMED', 'BUYING CONFIRMED', 'SELLING CONFIRMED', 'CLEAR', 'COMPLETE', 'OBSERVED', 'SUPPORTIVE', 'ACTIONABLE NOW', 'RETESTING']);
    const awaitedStates = new Set(['AWAITED', 'DEVELOPING', 'PULLBACK REQUIRED', 'NO ACTIVE EVENT', 'PARTIAL', 'UNAVAILABLE', 'BALANCED', 'MIXED', 'NEUTRAL', 'CHECKING', 'WAITING FOR ZONE', 'WAITING FOR FRESH RETEST', 'ARMED AWAITING RECLAIM AND FLOW', 'COMPLETED RETEST ARMED', 'RECLAIM MOVED AWAY']);
    const blockedStates = new Set(['BLOCKED', 'OPPOSED', 'BUYERS ABSORBED', 'SELLERS ABSORBED', 'BUYER EXHAUSTION', 'SELLER EXHAUSTION', 'DO NOT CHASE', 'LATE STRUCTURE DO NOT CHASE', 'EXTENDED DO NOT CHASE', 'MISSED', 'EXPIRED', 'INVALIDATED']);
    monitorGateStrip.innerHTML = controls.map(([label, value]) => {
      const className = verifiedStates.has(value) ? 'verified' : awaitedStates.has(value) ? 'awaited' : blockedStates.has(value) ? 'blocked' : '';
      return `<span class="monitor-gate-chip ${className}"><b>${label}</b><strong>${value}</strong></span>`;
    }).join('');
  }
  return { state, stateCopy, story };
}

function renderSignalMonitor(monitor) {
  const data = monitor || { status: 'SCANNING', action: 'SCANNING', reason: 'Waiting for qualified setup.', events: [] };

  if (data.id && localStorage.getItem(`dismissed_signal_${data.id}`)) {
    renderSignalMonitor(null);
    return;
  }

  activeSignalId = data.id || null;
  const status = data.status || 'SCANNING';
  const action = data.action || 'SCANNING';
  const isOpen = ['PENDING_ENTRY', 'ACTIVE', 'TP1_SECURED', 'TP2_SECURED'].includes(status);
  const isExit = ['STOPPED_OUT', 'INVALIDATED'].includes(status);
  const isPublished = Boolean(data.id) || isOpen || isExit;
  renderMonitorConfirmationScenario(data);
  const evidence = renderMonitorEvidenceState(data, status, isPublished, isExit || ['CANCELLED', 'EXPIRED'].includes(status));

  if (monitorCard) monitorCard.className = `card signal-monitor-card ${status.toLowerCase()} monitor-${evidence.state}`;
  if (monitorStatusVal) {
    let displayStatusClass = status.toLowerCase();
    if (!isPublished && evidence.state === 'invalidated') {
      monitorStatusVal.textContent = 'STORY INVALIDATED';
      displayStatusClass = 'invalidated';
    } else if (!isPublished && evidence.state === 'stale') {
      monitorStatusVal.textContent = 'DO NOT CHASE';
      displayStatusClass = 'stale';
    } else if (!isPublished && evidence.state === 'actionable') {
      monitorStatusVal.textContent = evidence.story?.state === 'RETESTING' ? 'RETEST WATCH' : 'ACTIONABLE WATCH';
      displayStatusClass = 'actionable';
    } else if (!isPublished && evidence.state === 'tactical') {
      monitorStatusVal.textContent = 'TACTICAL WATCH';
      displayStatusClass = 'tactical';
    } else if (!isPublished && evidence.state === 'waiting') {
      monitorStatusVal.textContent = 'WAITING FOR PROOF';
      displayStatusClass = 'waiting';
    } else if (status === 'SCANNING' && evidence.state === 'scanning') {
      monitorStatusVal.innerHTML = `
        <span class="scanner-status-pulse-dot" style="display: inline-block; width: 5px; height: 5px; background-color: var(--neon-blue); border-radius: 50%; margin-right: 4px; box-shadow: 0 0 6px var(--neon-blue); animation: processing-ticker-glow 1s infinite alternate ease-in-out; flex-shrink: 0;"></span>
        SCANNING
      `;
    } else {
      monitorStatusVal.textContent = status.replace(/_/g, ' ');
    }
    monitorStatusVal.className = `monitor-status ${displayStatusClass}`;
  }
  if (monitorActionVal) {
    monitorActionVal.textContent = !isPublished && evidence.state === 'stale'
      ? 'DO NOT CHASE'
      : !isPublished && evidence.state === 'invalidated'
        ? 'WAIT FOR NEW EVENT'
        : !isPublished && evidence.state === 'actionable'
          ? 'MANUAL REVIEW'
          : !isPublished && (evidence.state === 'waiting' || evidence.state === 'tactical')
            ? 'WATCH'
            : action.replace(/_/g, ' ');
  }
  if (monitorReasonVal) {
    const tactical = data.confirmation_scenarios?.tactical || {};
    const useTacticalReason = Boolean(tactical.candidate || tactical.passed) && !Boolean(data.confirmation_scenarios?.institutional?.passed);
    const useStoryReason = ['invalidated', 'stale', 'actionable', 'waiting'].includes(evidence.state);
    const reasonText = (useStoryReason && evidence.story?.happeningNow)
      || (useTacticalReason && tactical.reason)
      || data.reason
      || 'Scanning for opportunities.';
    if (status === 'SCANNING' && evidence.state === 'scanning') {
      monitorReasonVal.innerHTML = `
        <div style="display: inline-flex; align-items: center; gap: 0.35rem;">
          <span class="ticker-dot active-scanning" style="display: inline-block; width: 6px; height: 6px; background-color: var(--neon-blue); border-radius: 50%; box-shadow: 0 0 8px var(--neon-blue); animation: processing-ticker-glow 1s infinite alternate ease-in-out; flex-shrink: 0;"></span>
          <span>${reasonText}</span>
        </div>
      `;
    } else {
      monitorReasonVal.textContent = reasonText;
    }
  }
  if (monitorIdVal) monitorIdVal.textContent = data.id ? `Signal #${data.id}` : 'No active signal';
  if (monitorSideVal) {
    monitorSideVal.textContent = data.side || '-';
    monitorSideVal.className = (data.side || '').toLowerCase();
  }
  if (monitorPriceVal) monitorPriceVal.textContent = formatCurrency(data.current_price);
  if (monitorEntryVal) {
    const entry = data.entry || {};
    monitorEntryVal.textContent = `${formatCurrency(entry.low)} - ${formatCurrency(entry.high)}`;
  }
  if (monitorEntryPriceVal) {
    monitorEntryPriceVal.textContent = `${formatCurrency(data.entry?.low)} - ${formatCurrency(data.entry?.high)}`;
  }
  if (monitorStopVal) monitorStopVal.textContent = formatCurrency(data.stop?.current);
  if (monitorTp1Val) monitorTp1Val.textContent = formatCurrency(data.targets?.tp1);
  if (monitorTp2Val) monitorTp2Val.textContent = formatCurrency(data.targets?.tp2);
  if (monitorTp3Val) monitorTp3Val.textContent = formatCurrency(data.targets?.tp3);
  if (monitorRunnerVal) monitorRunnerVal.textContent = formatCurrency(data.targets?.runner);

  if (monitorTargetMilestones) {
    const stage = Number(data.targets?.stage || 0);
    const isSuccessful = status === 'COMPLETED' && stage >= 3;
    const isTerminalFailure = ['STOPPED_OUT', 'INVALIDATED', 'CANCELLED', 'EXPIRED'].includes(status);
    const entryConfirmed = ['ACTIVE', 'TP1_SECURED', 'TP2_SECURED', 'TP3_SECURED', 'COMPLETED'].includes(status);
    const progress = Math.max(0, Math.min(100, Number(data.progress_pct ?? data.progress_to_tp1_pct ?? 0)));
    monitorTargetMilestones.querySelectorAll('.target-milestone').forEach(node => {
      const milestoneStage = Number(node.dataset.stage || 0);
      const detail = node.querySelector('small');
      node.classList.remove('completed', 'current', 'upcoming', 'blocked');
      if (milestoneStage === 0) {
        if (entryConfirmed) {
          node.classList.add('completed');
          if (detail) detail.textContent = `FILLED @ ${formatCurrency(data.entry?.price ?? data.entry?.reference)}`;
        } else if (isTerminalFailure) {
          node.classList.add('blocked');
          if (detail) detail.textContent = 'CANCELLED';
        } else {
          node.classList.add('current');
          if (detail) {
            const confirmationState = String(data.entry_confirmation?.state || data.entry?.confirmation?.state || 'WAITING_FOR_ZONE');
            detail.textContent = confirmationState === 'ARMED_AWAITING_RECLAIM_AND_FLOW'
              ? 'ARMED · RECLAIM + FLOW'
              : confirmationState === 'COMPLETED_RETEST_ARMED'
                ? 'RETEST HELD · LIVE PROOF'
                : confirmationState === 'RECLAIM_MOVED_AWAY'
                  ? 'WAIT FOR NEXT RETEST'
                  : 'WAIT FOR ENTRY ZONE';
          }
        }
      } else if (milestoneStage <= stage) {
        node.classList.add('completed');
        if (detail) detail.textContent = 'SECURED';
      } else if (isSuccessful) {
        node.classList.add('blocked');
        if (detail) detail.textContent = 'CLOSED AT TP3';
      } else if (isTerminalFailure && milestoneStage === stage + 1) {
        node.classList.add('blocked');
        if (detail) detail.textContent = status === 'COMPLETED' ? 'PROTECTED EXIT' : 'EXIT / CLOSED';
      } else if (milestoneStage === stage + 1 && status !== 'PENDING_ENTRY') {
        node.classList.add('current');
        if (detail) detail.textContent = `${data.progress_label || 'IN PROGRESS'} · ${progress.toFixed(0)}%`;
      } else {
        node.classList.add('upcoming');
        if (detail) detail.textContent = status === 'PENDING_ENTRY' && milestoneStage === 1 ? 'WAIT FOR ENTRY' : 'WAITING';
      }
    });
  }
  const journeyProgress = Math.max(0, Math.min(100, Number(data.journey_progress_pct || 0)));
  if (monitorJourneyProgressVal) monitorJourneyProgressVal.textContent = `${journeyProgress.toFixed(1)}%`;
  if (monitorJourneyProgressFill) monitorJourneyProgressFill.style.width = `${journeyProgress}%`;
  const hasSignal = !!data.id;
  if (dismissSignalBtn) dismissSignalBtn.classList.toggle('hidden', !hasSignal);

  if (monitorEventsList) {
    monitorEventsList.innerHTML = '';
    const events = Array.isArray(data.events) ? data.events.slice().reverse() : [];
    if (!events.length) {
      const empty = document.createElement('div');
      empty.className = 'monitor-empty';
      empty.textContent = data.approval?.blockers?.[0] || 'No published signal yet.';
      monitorEventsList.appendChild(empty);
    } else {
      events.forEach(event => {
        const row = document.createElement('div');
        row.className = `monitor-event ${event.kind || ''}`;
        const title = document.createElement('strong');
        title.textContent = event.title || 'Signal update';
        const detail = document.createElement('span');
        detail.textContent = event.detail || '';
        row.append(title, detail);
        monitorEventsList.appendChild(row);
      });
      const latest = events[0];
      const key = `${data.id || ''}:${latest.at || ''}:${latest.kind || ''}`;
      if (key !== lastMonitorEventKey) {
        if (isExit) logMsg(`BOT EXIT: ${latest.detail || data.reason}`, 'error');
        else if (latest.kind?.includes('tp')) logMsg(`BOT TARGET SECURED: ${latest.title}`, 'system');
        else if (latest.kind === 'entry_confirmed') logMsg('BOT ENTRY CONFIRMED: active position monitoring.', 'system');
        lastMonitorEventKey = key;
      }
    }
  }
}

const OPEN_SIGNAL_STATUSES = new Set(['PENDING_ENTRY', 'ACTIVE', 'TP1_SECURED', 'TP2_SECURED']);
const SUCCESS_SIGNAL_STATUSES = new Set(['COMPLETED']);
const FAILURE_SIGNAL_STATUSES = new Set(['STOPPED_OUT', 'INVALIDATED']);

function signalOutcome(status) {
  if (SUCCESS_SIGNAL_STATUSES.has(status)) return { label: 'SUCCESS', className: 'success' };
  if (FAILURE_SIGNAL_STATUSES.has(status)) return { label: 'FAILED', className: 'failure' };
  if (OPEN_SIGNAL_STATUSES.has(status)) return { label: 'ACTIVE', className: 'active' };
  if (status === 'EXPIRED') return { label: 'EXPIRED', className: 'expired' };
  return { label: 'CANCELLED', className: 'cancelled' };
}

function formatSignalTime(value) {
  if (!value) return '—';
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? '—' : timestamp.toLocaleString();
}

function renderSignalHistory(signals) {
  const history = Array.isArray(signals) ? signals : [];
  const wins = history.filter(signal => SUCCESS_SIGNAL_STATUSES.has(signal.status)).length;
  const losses = history.filter(signal => FAILURE_SIGNAL_STATUSES.has(signal.status)).length;
  const open = history.filter(signal => OPEN_SIGNAL_STATUSES.has(signal.status)).length;

  if (signalHistoryCount) signalHistoryCount.textContent = `${history.length} signal${history.length === 1 ? '' : 's'}`;
  if (historyWinsVal) historyWinsVal.textContent = String(wins);
  if (historyLossesVal) historyLossesVal.textContent = String(losses);
  if (historyOpenVal) historyOpenVal.textContent = String(open);
  if (!signalHistoryList) return;

  signalHistoryList.innerHTML = '';
  if (!history.length) {
    const empty = document.createElement('div');
    empty.className = 'monitor-empty';
    empty.textContent = 'No published signals yet.';
    signalHistoryList.appendChild(empty);
    return;
  }

  history.forEach(signal => {
    const outcome = signalOutcome(signal.status);
    const row = document.createElement('div');
    row.className = `signal-history-row ${outcome.className}`;

    const identity = document.createElement('div');
    identity.className = 'signal-history-identity';
    const title = document.createElement('strong');
    title.textContent = `#${signal.id ?? '—'} ${signal.symbol || 'Unknown'} ${signal.side || ''}`;
    const meta = document.createElement('span');
    meta.textContent = `${signal.timeframe || '—'} · ${formatSignalTime(signal.closed_at || signal.last_evaluated_at || signal.published_at)}`;
    identity.append(title, meta);

    const detail = document.createElement('div');
    detail.className = 'signal-history-detail';
    const levels = document.createElement('span');
    levels.textContent = `Entry ${formatCurrency(signal.entry?.price ?? signal.entry?.low)} · Stop ${formatCurrency(signal.stop?.current)}`;
    const reason = document.createElement('small');
    reason.textContent = signal.reason || signal.exit_reason || (outcome.className === 'active' ? 'Monitoring live setup.' : 'Signal closed.');
    detail.append(levels, reason);

    const badge = document.createElement('span');
    badge.className = `signal-outcome-badge ${outcome.className}`;
    badge.textContent = outcome.label;
    row.append(identity, detail, badge);
    signalHistoryList.appendChild(row);
  });
}

async function refreshSignalHistory() {
  try {
    const response = await fetch('/signals/history?limit=50');
    if (!response.ok) throw new Error('Signal history request failed.');
    const data = await response.json();
    renderSignalHistory(data.signals);
  } catch (error) {
    console.warn('Signal history refresh failed:', error);
  }
}

let signalHistoryTimer = null;

function startSignalHistoryPolling() {
  if (signalHistoryTimer) return;
  refreshSignalHistory();
  // History changes only when a signal advances; a 15-second cadence keeps
  // the ledger current without every open dashboard tab polling the server.
  signalHistoryTimer = setInterval(refreshSignalHistory, 15000);
}

// economic and backtest/training operations handlers
if (runBacktestBtn) {
  runBacktestBtn.addEventListener('click', async () => {
    const symbol = symbolInput.value.trim().toUpperCase() || 'BTCUSDT';
    const timeframe = timeframeSelect.value;
    const limit = parseInt(opsCandlesInput.value) || 500;

    logMsg(`Backtest started: ${symbol}/${timeframe}...`, 'system');
    runBacktestBtn.disabled = true;

    try {
      const response = await fetch('/quant/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, timeframe, candle_limit: limit })
      });
      const report = await response.json();
      runBacktestBtn.disabled = false;
      if (!response.ok) throw new Error(report.detail || 'Execution failed.');

      opsResultsBox.classList.remove('hidden');
      opsResultsTitle.textContent = 'Backtest Replay Report';

      opsMetricsDisplay.innerHTML = `
        <div class="ops-metric-item"><span>Win Rate</span><strong>${(report.win_rate * 100).toFixed(1)}%</strong></div>
        <div class="ops-metric-item"><span>Total Trades</span><strong>${report.total_trades}</strong></div>
        <div class="ops-metric-item"><span>Sharpe Ratio</span><strong>${report.sharpe_ratio.toFixed(2)}</strong></div>
        <div class="ops-metric-item"><span>Expectancy</span><strong>${report.expectancy.toFixed(2)} R</strong></div>
        <div class="ops-metric-item"><span>TP1 Hit Rate</span><strong>${(report.tp1_hit_rate * 100).toFixed(1)}%</strong></div>
        <div class="ops-metric-item"><span>Stopped Rate</span><strong>${(report.stopped_out_rate * 100).toFixed(1)}%</strong></div>
      `;

      const lastTrades = report.trades.slice(-5).reverse();
      opsResultsLog.innerHTML = '<h4>Replay Trades:</h4>' + lastTrades.map(t =>
        `<div class="ops-log-row ${t.status.toLowerCase()}">
          [${t.side}] Entry: ${formatCurrency(t.entry)} | Exit: ${formatCurrency(t.exit)} | Return: ${t.r_return >= 0 ? '+' : ''}${t.r_return}R
        </div>`
      ).join('');

      logMsg(`Backtest complete. Win Rate: ${(report.win_rate * 100).toFixed(1)}%`, 'system');
    } catch (err) {
      runBacktestBtn.disabled = false;
      logMsg(`Backtest error: ${err.message}`, 'error');
    }
  });
}

if (trainModelBtn) {
  trainModelBtn.addEventListener('click', async () => {
    const symbol = symbolInput.value.trim().toUpperCase() || 'BTCUSDT';
    const timeframe = timeframeSelect.value;
    const limit = parseInt(opsCandlesInput.value) || 500;

    logMsg(`Model training started for ${symbol}...`, 'system');
    trainModelBtn.disabled = true;

    try {
      const response = await fetch('/quant/train', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, timeframe, candle_limit: limit })
      });
      const res = await response.json();
      trainModelBtn.disabled = false;
      if (!response.ok) throw new Error(res.detail || 'Training failed.');

      opsResultsBox.classList.remove('hidden');
      opsResultsTitle.textContent = 'ML Model Retrained';
      opsMetricsDisplay.innerHTML = `
        <div class="ops-metric-item"><span>Test IC</span><strong>${res.test_ic.toFixed(4)}</strong></div>
        <div class="ops-metric-item"><span>Train IC</span><strong>${res.train_ic.toFixed(4)}</strong></div>
        <div class="ops-metric-item"><span>Model Family</span><strong>Ridge Regression</strong></div>
      `;
      opsResultsLog.innerHTML = `<div class="ops-log-row system">Saved weights to app/ml/trained_weights.json. Edge updated.</div>`;
      logMsg(`Model retrained successfully. Test IC: ${res.test_ic.toFixed(4)}`, 'system');
    } catch (err) {
      trainModelBtn.disabled = false;
      logMsg(`Training error: ${err.message}`, 'error');
    }
  });
}

// Inner Card Tabs Switching Controller
const initInnerCardTabs = () => {
  document.querySelectorAll('.card-tabs').forEach(tabContainer => {
    const buttons = tabContainer.querySelectorAll('.card-tab-btn');
    const card = tabContainer.closest('.card');

    buttons.forEach(btn => {
      btn.addEventListener('click', () => {
        const targetTab = btn.getAttribute('data-card-tab');

        // Toggle button active classes
        buttons.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        // Toggle visibility of panels inside this card
        card.querySelectorAll('.card-tab-content').forEach(panel => {
          if (panel.getAttribute('data-card-tab') === targetTab) {
            panel.classList.remove('hidden');
          } else {
            panel.classList.add('hidden');
          }
        });
      });
    });
  });
};

initInnerCardTabs();

// URL Query Parameter Auto-Load
const initUrlQueryAutoLoad = () => {
  const urlParams = new URLSearchParams(window.location.search);
  const symbolParam = urlParams.get('symbol');
  if (symbolParam) {
    symbolInput.value = symbolParam.trim().toUpperCase();
    logMsg(`URL auto-load: selected ${symbolParam}. Triggering stream connection.`, 'system');
    configForm.dispatchEvent(new Event('submit'));
  }
};

let protectedDashboardStarted = false;

function startProtectedDashboardFeatures() {
  if (protectedDashboardStarted) return;
  protectedDashboardStarted = true;
  startSignalHistoryPolling();
  initUrlQueryAutoLoad();
}

if (window.atcAuthenticated) startProtectedDashboardFeatures();
window.addEventListener('atc:authenticated', startProtectedDashboardFeatures);

// News Rendering and Scope Selection Controller
function renderNewsList() {
  if (!lastReceivedNews) {
    newsContainer.innerHTML = '<li class="empty-news">No recent headlines found.</li>';
    return;
  }

  // Extract target news items list based on current scope
  let articles = [];
  if (Array.isArray(lastReceivedNews)) {
    // If backend returns a flat array, fallback
    articles = lastReceivedNews;
  } else {
    articles = activeNewsScope === 'global'
      ? (lastReceivedNews.global || [])
      : (lastReceivedNews.token || []);
  }

  if (articles.length > 0) {
    newsContainer.innerHTML = '';
    articles.forEach(art => {
      const li = document.createElement('li');
      const header = document.createElement('div');
      header.style.cssText = 'display: flex; align-items: flex-start; gap: 0.35rem; margin-bottom: 0.25rem;';
      const badge = document.createElement('span');
      badge.className = 'feed-badge';
      badge.textContent = String(art.feed || 'GDELT');
      const link = document.createElement('a');
      const rawUrl = String(art.url || '');
      try {
        const url = new URL(rawUrl, window.location.origin);
        if (url.protocol === 'https:' || url.protocol === 'http:') link.href = url.href;
      } catch (_) { /* leave an invalid provider URL non-clickable */ }
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = String(art.title || 'Untitled headline');
      header.append(badge, link);
      const meta = document.createElement('div');
      meta.className = 'news-meta';
      meta.style.paddingLeft = '2.75rem';
      const source = document.createElement('span');
      source.textContent = `Source: ${String(art.source || 'Unknown')}`;
      const date = document.createElement('span');
      date.textContent = `Date: ${String(art.date || 'Recent')}`;
      meta.append(source, date);
      li.append(header, meta);
      newsContainer.appendChild(li);
    });
  } else {
    newsContainer.innerHTML = `<li class="empty-news">No recent ${activeNewsScope === 'global' ? 'global' : 'token'} headlines found.</li>`;
  }
}

// News scope toggle button click handlers
const newsScopeToken = document.getElementById('news-scope-token');
const newsScopeGlobal = document.getElementById('news-scope-global');

if (newsScopeToken && newsScopeGlobal) {
  newsScopeToken.addEventListener('click', () => {
    activeNewsScope = 'token';
    newsScopeGlobal.classList.remove('active');
    newsScopeToken.classList.add('active');
    renderNewsList();
  });
  newsScopeGlobal.addEventListener('click', () => {
    activeNewsScope = 'global';
    newsScopeToken.classList.remove('active');
    newsScopeGlobal.classList.add('active');
    renderNewsList();
  });
}
