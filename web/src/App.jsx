import { useEffect, useRef, useState } from "react";

const USAGE_REFRESH_INTERVAL_MS = 60_000;
const updaterBridge = typeof window !== "undefined" ? window.codexSwitchUpdater || null : null;

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    },
    ...options
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

function formatStatus(status) {
  const labels = {
    connected: "Connected",
    disconnected: "Disconnected",
    pending_oauth: "Waiting for sign-in",
    error: "Needs attention",
    available: "Available",
    unknown: "Unknown"
  };
  return labels[status] || status;
}

function clampPercent(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return 0;
  }
  return Math.max(0, Math.min(100, value));
}

function remainingPercentValue(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  return Math.max(0, Math.min(100, 100 - clampPercent(value)));
}

function formatUsagePercent(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "n/a";
  }
  return `${remainingPercentValue(value)}%`;
}

function formatResetAtDisplay(window) {
  const unixSeconds = window?.resetsAt;
  if (!unixSeconds) {
    return "Reset time unavailable";
  }

  const date = new Date(unixSeconds * 1000);
  const minutes = window?.windowDurationMins;
  if (typeof minutes === "number" && minutes >= 10080) {
    return `Resets ${date.toLocaleDateString("en-US", { month: "short", day: "numeric" })}`;
  }

  return `Resets ${date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}`;
}

function getWindowDuration(window) {
  const minutes = window?.windowDurationMins;
  if (typeof minutes !== "number" || Number.isNaN(minutes) || minutes <= 0) {
    return null;
  }
  return minutes;
}

function isWeeklyDuration(minutes) {
  return typeof minutes === "number" && minutes >= 10080;
}

function formatWindowShortLabel(window, fallback) {
  const minutes = getWindowDuration(window);
  if (minutes === null) {
    return fallback;
  }
  if (isWeeklyDuration(minutes)) {
    return "Weekly";
  }
  if (minutes % 60 === 0) {
    return `${minutes / 60}h`;
  }
  return `${minutes}m`;
}

function getUsageWindows(account) {
  const windows = [account?.rate_limits?.primary, account?.rate_limits?.secondary].filter(
    (window) => window && typeof window === "object"
  );
  let hourlyWindow = null;
  let weeklyWindow = null;

  for (const window of windows) {
    const minutes = getWindowDuration(window);
    if (minutes === null) {
      continue;
    }
    if (isWeeklyDuration(minutes)) {
      if (!weeklyWindow || minutes > getWindowDuration(weeklyWindow)) {
        weeklyWindow = window;
      }
      continue;
    }
    if (!hourlyWindow || minutes < getWindowDuration(hourlyWindow)) {
      hourlyWindow = window;
    }
  }

  if (!hourlyWindow && !weeklyWindow) {
    return {
      hourly: account?.rate_limits?.primary || null,
      weekly: account?.rate_limits?.secondary || null
    };
  }

  return {
    hourly: hourlyWindow,
    weekly: weeklyWindow
  };
}

function buildUsageSummary(account) {
  const usageWindows = getUsageWindows(account);
  const parts = [];
  if (usageWindows.hourly) {
    parts.push(`${formatWindowShortLabel(usageWindows.hourly, "Usage")} ${formatUsagePercent(usageWindows.hourly.usedPercent)}`);
  }
  if (usageWindows.weekly) {
    parts.push(`${formatWindowShortLabel(usageWindows.weekly, "Weekly")} ${formatUsagePercent(usageWindows.weekly.usedPercent)}`);
  }
  if (parts.length > 0) {
    return parts.join(" • ");
  }
  return formatStatus(account.status);
}

function hasUsageData(account) {
  return Boolean(account?.rate_limits?.primary || account?.rate_limits?.secondary);
}

function getRemainingFraction(window) {
  const remaining = remainingPercentValue(window?.usedPercent);
  if (remaining === null) {
    return null;
  }
  return remaining / 100;
}

function hasKnownResetTime(window) {
  return typeof window?.resetsAt === "number" && Number.isFinite(window.resetsAt) && window.resetsAt > 0;
}

function hasPickableWeeklyWindow(account) {
  const weeklyWindow = getUsageWindows(account).weekly;
  return typeof getRemainingFraction(weeklyWindow) === "number" && hasKnownResetTime(weeklyWindow);
}

function isPickableAccount(account) {
  return Boolean(account?.enabled && !account?.oauth);
}

function isFreeAccount(account) {
  const usageWindows = getUsageWindows(account);
  const rateLimitPlanType = account?.rate_limits?.planType;
  return Boolean(
    isPickableAccount(account) &&
      hasUsageData(account) &&
      (rateLimitPlanType === "free" || (!usageWindows.hourly && usageWindows.weekly))
  );
}

function calculatePlusAccountUsageCost(account) {
  const usageWindows = getUsageWindows(account);
  const hourlyRemaining = getRemainingFraction(usageWindows.hourly);
  const weeklyRemaining = getRemainingFraction(usageWindows.weekly);
  const knownWindows = [hourlyRemaining, weeklyRemaining].filter((value) => typeof value === "number");
  const hasHourly = typeof hourlyRemaining === "number";
  const hasWeekly = typeof weeklyRemaining === "number";

  if (
    !isPickableAccount(account) ||
    !hasPickableWeeklyWindow(account) ||
    !hasHourly ||
    !hasWeekly ||
    knownWindows.length === 0 ||
    knownWindows.some((value) => value <= 0)
  ) {
    return Number.POSITIVE_INFINITY;
  }

  const minRemaining = Math.min(...knownWindows);
  const weightedRemaining =
    (hasHourly ? hourlyRemaining * 0.68 : 0) +
    (hasWeekly ? weeklyRemaining * 0.32 : 0);
  const balanceGap = hasHourly && hasWeekly ? Math.abs(hourlyRemaining - weeklyRemaining) : 1;
  const missingWindowPenalty = (2 - knownWindows.length) * 1.8;
  const bottleneckPenalty = 3.4 / (minRemaining + 0.03);
  const headroomPenalty = 1.8 / (weightedRemaining + 0.05);
  const imbalancePenalty = balanceGap * 1.9;
  const hourlyLowPenalty = hasHourly ? 1.35 / (hourlyRemaining + 0.08) : 1.9;
  const weeklyLowPenalty = hasWeekly ? 0.75 / (weeklyRemaining + 0.05) : 1.4;

  return (
    missingWindowPenalty +
    bottleneckPenalty +
    headroomPenalty +
    imbalancePenalty +
    hourlyLowPenalty +
    weeklyLowPenalty
  );
}

function calculateFreeAccountUsageCost(account) {
  const usageWindows = getUsageWindows(account);
  const bestAvailableWindow = usageWindows.weekly || usageWindows.hourly;
  const remaining = getRemainingFraction(bestAvailableWindow);

  if (!isFreeAccount(account) || typeof remaining !== "number" || remaining <= 0) {
    return Number.POSITIVE_INFINITY;
  }

  const resetPenalty = hasKnownResetTime(bestAvailableWindow) ? 0 : 0.35;
  const headroomPenalty = 1 / (remaining + 0.05);

  return resetPenalty + headroomPenalty;
}

function getBestAccountByCost(accounts, calculateCost) {
  return (accounts || []).reduce((bestAccount, candidate) => {
    const candidateCost = calculateCost(candidate);
    if (!Number.isFinite(candidateCost)) {
      return bestAccount;
    }

    if (!bestAccount) {
      return candidate;
    }

    const bestCost = calculateCost(bestAccount);
    return candidateCost < bestCost ? candidate : bestAccount;
  }, null);
}

function getRecommendedPlusAccount(accounts) {
  return getBestAccountByCost(accounts, calculatePlusAccountUsageCost);
}

function getRecommendedFreeAccount(accounts) {
  return getBestAccountByCost(accounts, calculateFreeAccountUsageCost);
}

function getRecommendedAccount(accounts) {
  return getRecommendedPlusAccount(accounts) || getRecommendedFreeAccount(accounts);
}

function getOauthUrl(flow) {
  return flow?.settings_url || flow?.verification_uri || flow?.help_url || null;
}

function getSelectedAccountFromState(payload) {
  const accounts = payload?.accounts || [];
  return accounts.find((account) => account.id === payload?.selected_account_id) || accounts[0] || null;
}

function formatCheckedAtDisplay(value) {
  if (!value) {
    return null;
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return null;
  }

  return `Checked ${date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}`;
}

function GearIcon() {
  return (
    <svg className="relay-gear-icon" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 2.8v2.3M12 18.9v2.3M4.9 4.9l1.6 1.6M17.5 17.5l1.6 1.6M2.8 12h2.3M18.9 12h2.3M4.9 19.1l1.6-1.6M17.5 6.5l1.6-1.6" />
    </svg>
  );
}

function UsageStat({ label, window }) {
  return (
    <section className="relay-usage-stat">
      <p className="relay-usage-stat-label">{label}</p>
      <p className="relay-usage-stat-value">{formatUsagePercent(window?.usedPercent)}</p>
      <p className="relay-usage-stat-reset">{formatResetAtDisplay(window)}</p>
    </section>
  );
}

function SignInPanel({ flow, onOpen, onCancel, cancelBusy = false }) {
  const oauthUrl = getOauthUrl(flow);

  return (
    <section className="relay-usage-stat relay-pending-panel">
      <div className="relay-pending-head">
        <p className="relay-usage-stat-label">
          {flow?.status === "error" ? "Sign-in failed" : "Finish sign-in"}
        </p>
        {onCancel ? (
          <button
            type="button"
            className="relay-panel-close"
            onClick={onCancel}
            disabled={cancelBusy}
            aria-label="Cancel sign-in"
            title="Cancel sign-in"
          >
            X
          </button>
        ) : null}
      </div>
      <p className="relay-empty-copy">
        Open the ChatGPT sign-in page in your browser. Once the login finishes, the account will appear here automatically.
      </p>
      {flow?.user_code ? <p className="relay-pending-code">Code: {flow.user_code}</p> : null}
      {oauthUrl ? (
        <>
          <input
            type="text"
            readOnly
            value={oauthUrl}
            className="relay-link-input"
            onFocus={(event) => event.currentTarget.select()}
            aria-label="Sign-in link"
          />
          <div className="relay-main-actions">
            <button
              type="button"
              className="relay-account-action relay-account-action-primary"
              onClick={() => onOpen(oauthUrl)}
            >
              Open Sign-In Link
            </button>
          </div>
        </>
      ) : null}
      {flow?.error ? <div className="relay-feedback relay-feedback-warn">{flow.error}</div> : null}
    </section>
  );
}

function AccountCard({ account, selected, recommended, onSelect }) {
  return (
    <button
      type="button"
      className={["relay-account-card", selected ? "is-selected" : ""].join(" ")}
      onClick={() => onSelect(account.id)}
    >
      <div className="relay-account-card-head">
        <div className="relay-avatar">{account.title?.slice(0, 1)?.toUpperCase() || "?"}</div>
        <div className="relay-account-card-copy">
          <div className="relay-account-title-row">
            <h3 className="relay-account-title">{account.title}</h3>
            {account.app_primary ? <span className="relay-pill relay-pill-primary">Primary</span> : null}
            {isFreeAccount(account) ? <span className="relay-pill relay-pill-free">Free</span> : null}
            {recommended ? <span className="relay-pill relay-pill-recommended">Recommended</span> : null}
          </div>
          <p className="relay-account-copy">{buildUsageSummary(account)}</p>
        </div>
      </div>
      <div className="relay-account-footer">
        <span className="relay-status-text">{formatStatus(account.status)}</span>
        {account.last_error ? <span className="relay-account-error">{account.last_error}</span> : null}
      </div>
    </button>
  );
}

function UpdatePanel({ updateState, busyKey, onCheck, onDownload, onInstall }) {
  const supported = Boolean(updateState?.supported && updateState?.configured);
  const checkedAt = formatCheckedAtDisplay(updateState?.checkedAt);
  const checking = busyKey === "check-update" || updateState?.phase === "checking";
  const updateBusy =
    checking ||
    busyKey === "download-update" ||
    busyKey === "install-update" ||
    updateState?.phase === "downloading";
  const checkDisabled = !supported || updateBusy;
  const message = updateState
    ? updateState.message || "Check for updates."
    : "Loading update status...";
  let actionLabel = null;
  let action = null;
  let disabled = false;

  if (busyKey === "download-update" || updateState?.phase === "downloading") {
    const progress = typeof updateState?.progressPercent === "number" ? ` ${Math.round(updateState.progressPercent)}%` : "";
    actionLabel = `Downloading...${progress}`;
    disabled = true;
  } else if (busyKey === "install-update") {
    actionLabel = "Restarting...";
    disabled = true;
  } else if (updateState?.phase === "available") {
    actionLabel = updateState.version ? `Download ${updateState.version}` : "Download Update";
    action = onDownload;
  } else if (updateState?.phase === "downloaded") {
    actionLabel = "Restart to Update";
    action = onInstall;
  }

  return (
    <section className="relay-usage-stat relay-update-panel">
      <div className="relay-update-head">
        <div>
          <p className="relay-usage-stat-label">App Update</p>
          <p className="relay-update-message">{message}</p>
        </div>
        {updateState?.version ? <span className="relay-pill relay-pill-recommended">v{updateState.version}</span> : null}
      </div>
      <div className="relay-update-actions">
        <button
          type="button"
          className="relay-account-action relay-account-action-primary"
          onClick={onCheck}
          disabled={checkDisabled}
          title={!supported ? message : undefined}
        >
          {checking ? "Checking..." : "Check Updates"}
        </button>
        {actionLabel ? (
          <button
            type="button"
            className="relay-account-action"
            onClick={action}
            disabled={disabled || !action}
          >
            {actionLabel}
          </button>
        ) : null}
        {checkedAt ? <span className="relay-update-meta">{checkedAt}</span> : null}
      </div>
    </section>
  );
}

function MaintenancePanel({ busyKey, repairResult, onFix }) {
  const fixing = busyKey === "fix-common-issues";
  const fixedCount = repairResult?.fixed?.length || 0;
  const warningCount = repairResult?.warnings?.length || 0;
  const summary = repairResult
    ? warningCount > 0
      ? `Scan finished with ${warningCount} warning${warningCount === 1 ? "" : "s"}.`
      : `Scan finished. ${fixedCount} item${fixedCount === 1 ? "" : "s"} fixed.`
    : null;

  return (
    <section className="relay-usage-stat relay-maintenance-panel">
      <div>
        <p className="relay-usage-stat-label">Maintenance</p>
        {summary ? <p className="relay-update-message">{summary}</p> : null}
      </div>
      <button
        type="button"
        className="relay-account-action relay-account-action-primary"
        onClick={onFix}
        disabled={fixing}
      >
        {fixing ? "Fixing..." : "Fix Common Switch Issues"}
      </button>
    </section>
  );
}

function SettingsPopover({
  open,
  updateState,
  busyKey,
  repairResult,
  onCheck,
  onDownload,
  onInstall,
  onFixCommonIssues,
  onClose
}) {
  if (!open) {
    return null;
  }

  return (
    <div className="relay-popover-layer relay-settings-layer" onClick={onClose}>
      <section
        id="settings-popover"
        className="relay-floating-panel relay-settings-popover"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="relay-popover-head">
          <div>
            <p className="relay-main-label">Settings</p>
            <h2 id="settings-title" className="relay-popover-title">App Controls</h2>
          </div>
          <button
            type="button"
            className="relay-panel-close"
            onClick={onClose}
            aria-label="Close settings"
            title="Close settings"
          >
            X
          </button>
        </div>
        <UpdatePanel
          updateState={updateState}
          busyKey={busyKey}
          onCheck={onCheck}
          onDownload={onDownload}
          onInstall={onInstall}
        />
        <MaintenancePanel
          busyKey={busyKey}
          repairResult={repairResult}
          onFix={onFixCommonIssues}
        />
      </section>
    </div>
  );
}

function PendingSignInOverlay({ flow, onOpen, onCancel, cancelBusy = false }) {
  if (!flow) {
    return null;
  }

  return (
    <div className="relay-popover-layer relay-signin-layer">
      <div
        className="relay-signin-window"
        role="dialog"
        aria-modal="true"
        aria-label="Add account sign-in"
        onClick={(event) => event.stopPropagation()}
      >
        <SignInPanel
          flow={flow}
          onOpen={onOpen}
          onCancel={onCancel}
          cancelBusy={cancelBusy}
        />
      </div>
    </div>
  );
}

function App() {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState(null);
  const [feedback, setFeedback] = useState("");
  const [updateState, setUpdateState] = useState(() =>
    updaterBridge
      ? null
      : {
          supported: false,
          configured: false,
          phase: "unavailable",
          version: null,
          progressPercent: null,
          message: "Automatic updates are available only in the installed desktop app.",
          checkedAt: null
        }
  );
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [signInWidgetOpen, setSignInWidgetOpen] = useState(false);
  const [repairResult, setRepairResult] = useState(null);
  const usagePollRef = useRef({ token: 0, timeouts: [] });

  const stopUsagePolling = () => {
    usagePollRef.current.token += 1;
    usagePollRef.current.timeouts.forEach((timeoutId) => window.clearTimeout(timeoutId));
    usagePollRef.current.timeouts = [];
  };

  const loadState = async () => {
    setLoading(true);
    try {
      const payload = await request("/api/state");
      setState(payload);
      setFeedback("");
    } catch (err) {
      setFeedback(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadState();
  }, []);

  useEffect(() => {
    if (!updaterBridge) {
      return undefined;
    }

    let mounted = true;
    updaterBridge
      .getState()
      .then((payload) => {
        if (mounted) {
          setUpdateState(payload);
        }
      })
      .catch(() => {
        // Ignore updater bridge errors in the standalone browser.
      });

    const unsubscribe = updaterBridge.onStateChanged((payload) => {
      if (mounted) {
        setUpdateState(payload);
      }
    });

    return () => {
      mounted = false;
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    const intervalId = window.setInterval(async () => {
      try {
        const payload = await request("/api/state?refresh_usage=1");
        setState(payload);
      } catch {
        // Keep the last known usage and try again on the next minute.
      }
    }, USAGE_REFRESH_INTERVAL_MS);

    return () => window.clearInterval(intervalId);
  }, []);

  const runAction = async (key, callback) => {
    setBusyKey(key);
    try {
      const payload = await callback();
      setState(payload);
      setFeedback("");
      return payload;
    } catch (err) {
      setFeedback(err.message);
      throw err;
    } finally {
      setBusyKey(null);
    }
  };

  const accounts = state?.accounts || [];
  const pendingOAuthFlow = state?.pending_oauth_flow || null;
  const selectedAccount =
    accounts.find((account) => account.id === state?.selected_account_id) || accounts[0] || null;
  const recommendedPlusAccount = getRecommendedPlusAccount(accounts);
  const recommendedAccount = recommendedPlusAccount || getRecommendedFreeAccount(accounts);
  const recommendedIsFreeFallback = Boolean(recommendedAccount && !recommendedPlusAccount && isFreeAccount(recommendedAccount));
  const selectedHasUsage = hasUsageData(selectedAccount);
  const selectedOauthFlow = selectedAccount?.oauth || null;
  const selectedOauthUrl = getOauthUrl(selectedOauthFlow);
  const selectedUsageWindows = getUsageWindows(selectedAccount);

  useEffect(() => {
    setSignInWidgetOpen(Boolean(pendingOAuthFlow));
  }, [pendingOAuthFlow?.account_id]);

  useEffect(() => {
    if (!selectedAccount || selectedHasUsage) {
      stopUsagePolling();
      return;
    }

    stopUsagePolling();
    const targetAccountId = selectedAccount.id;
    const token = usagePollRef.current.token;
    const delays = [1200, 2800, 5000, 8000];

    usagePollRef.current.timeouts = delays.map((delay) =>
      window.setTimeout(async () => {
        if (usagePollRef.current.token !== token) {
          return;
        }

        try {
          const payload = await request("/api/state");
          if (usagePollRef.current.token !== token) {
            return;
          }

          setState(payload);
          const refreshedSelected = getSelectedAccountFromState(payload);
          if (
            !refreshedSelected ||
            refreshedSelected.id !== targetAccountId ||
            hasUsageData(refreshedSelected)
          ) {
            stopUsagePolling();
          }
        } catch {
          // Leave the current state in place and try again on the next scheduled refresh.
        }
      }, delay)
    );

    return () => {
      if (usagePollRef.current.token === token) {
        stopUsagePolling();
      }
    };
  }, [selectedAccount?.id, selectedHasUsage]);

  useEffect(() => () => stopUsagePolling(), []);

  useEffect(() => {
    const shouldPoll = Boolean(pendingOAuthFlow || selectedOauthFlow);
    if (!shouldPoll) {
      return undefined;
    }

    const intervalId = window.setInterval(async () => {
      try {
        const payload = await request("/api/state");
        setState(payload);
      } catch {
        // Keep the last known state and try again on the next interval.
      }
    }, 1500);

    return () => window.clearInterval(intervalId);
  }, [pendingOAuthFlow?.account_id, pendingOAuthFlow?.status, selectedOauthFlow?.account_id, selectedOauthFlow?.status]);

  const openExternal = (url) => {
    window.open(url, "_blank", "noopener,noreferrer");
  };

  const selectAccount = async (accountId) => {
    await runAction(`select:${accountId}`, () =>
      request(`/api/accounts/${accountId}/select`, { method: "POST" })
    );
  };

  const selectRecommendedAccount = async () => {
    if (!recommendedAccount) {
      setFeedback("No Plus or free account has enough usage data yet to recommend one.");
      return;
    }

    try {
      await selectAccount(recommendedAccount.id);
      setFeedback(
        recommendedIsFreeFallback
          ? `Selected ${recommendedAccount.title} as the best free account because no Plus account has usage left.`
          : `Selected ${recommendedAccount.title} as the best balanced account for current usage.`
      );
    } catch {
      // Error is already surfaced in state.
    }
  };

  const runUpdaterAction = async (key, action) => {
    if (!updaterBridge) {
      return;
    }

    setBusyKey(key);
    try {
      const payload = await action();
      if (payload) {
        setUpdateState(payload);
      }
    } catch (err) {
      setFeedback(err.message);
    } finally {
      setBusyKey(null);
    }
  };

  const checkForUpdates = async () => {
    await runUpdaterAction("check-update", () => updaterBridge.checkForUpdates());
  };

  const downloadUpdate = async () => {
    await runUpdaterAction("download-update", () => updaterBridge.downloadUpdate());
  };

  const installUpdate = async () => {
    await runUpdaterAction("install-update", () => updaterBridge.installUpdate());
  };

  const importAccounts = async () => {
    await runAction("import", () => request("/api/import", { method: "POST" }));
  };

  const fixCommonSwitchIssues = async () => {
    setBusyKey("fix-common-issues");
    try {
      const payload = await request("/api/diagnostics/fix", { method: "POST" });
      if (payload.state) {
        setState(payload.state);
      }
      setRepairResult(payload);
      const fixedCount = payload.fixed?.length || 0;
      const warningCount = payload.warnings?.length || 0;
      setFeedback(
        warningCount > 0
          ? `Common issue scan finished with ${warningCount} warning${warningCount === 1 ? "" : "s"}.`
          : `Common issue scan finished. ${fixedCount} item${fixedCount === 1 ? "" : "s"} fixed.`
      );
    } catch (err) {
      setRepairResult({ ok: false, fixed: [], checks: [], warnings: [err.message] });
      setFeedback(err.message);
    } finally {
      setBusyKey(null);
    }
  };

  const addAccount = async () => {
    if (pendingOAuthFlow) {
      setSignInWidgetOpen(true);
      setFeedback("Sign-in is already waiting. Finish it in the pop-up widget.");
      return;
    }

    try {
      await runAction("add-account", () =>
        request("/api/accounts/add", { method: "POST", body: JSON.stringify({}) })
      );
      setSignInWidgetOpen(true);
      setFeedback("Sign-in started. Finish it in the pop-up widget.");
    } catch {
      // Error is already surfaced in state.
    }
  };

  const connectSelectedAccount = async () => {
    if (!selectedAccount) return;
    try {
      await runAction(`connect:${selectedAccount.id}`, () =>
        request(`/api/accounts/${selectedAccount.id}/connect`, { method: "POST" })
      );
      setFeedback("Sign-in updated. Finish it in your browser if needed.");
    } catch {
      // Error is already surfaced in state.
    }
  };

  const cancelPendingSignIn = async () => {
    try {
      await runAction("cancel-pending-oauth", () => request("/api/oauth/cancel", { method: "POST" }));
      setFeedback("Sign-in canceled.");
    } catch {
      // Error is already surfaced in state.
    }
  };

  const cancelSelectedSignIn = async () => {
    if (!selectedAccount) return;
    try {
      await runAction(`cancel-connect:${selectedAccount.id}`, () =>
        request(`/api/accounts/${selectedAccount.id}/connect/cancel`, { method: "POST" })
      );
      setFeedback("Sign-in canceled.");
    } catch {
      // Error is already surfaced in state.
    }
  };

  const setPrimary = async () => {
    if (!selectedAccount) return;
    try {
      await runAction(`primary:${selectedAccount.id}`, () =>
        request(`/api/accounts/${selectedAccount.id}/primary`, { method: "POST" })
      );
      setFeedback("Copied credentials into the main Codex profile.");
    } catch {
      // Error is already surfaced in state.
    }
  };

  const removeSelectedAccount = async () => {
    if (!selectedAccount) return;
    const confirmed = window.confirm(
      `Remove ${selectedAccount.title}? This deletes its managed profile folder and removes it from codex switch.`
    );
    if (!confirmed) {
      return;
    }

    try {
      await runAction(`remove:${selectedAccount.id}`, () =>
        request(`/api/accounts/${selectedAccount.id}`, { method: "DELETE" })
      );
      setFeedback(`${selectedAccount.title} was removed.`);
    } catch {
      // Error is already surfaced in state.
    }
  };

  const openSelectedAccountInCodexDesktop = async () => {
    if (!selectedAccount) return;
    try {
      await runAction(`launch-desktop:${selectedAccount.id}`, () =>
        request(`/api/accounts/${selectedAccount.id}/launch`, { method: "POST" })
      );
      setFeedback(`Set ${selectedAccount.title} as primary and opened Codex Desktop.`);
    } catch {
      // Error is already surfaced in state.
    }
  };

  const setSelectedAccountForVSCode = async () => {
    if (!selectedAccount) return;
    try {
      await runAction(`launch-vscode:${selectedAccount.id}`, () =>
        request(`/api/accounts/${selectedAccount.id}/launch-vscode`, { method: "POST" })
      );
      setFeedback(`Set ${selectedAccount.title} for Codex VS Code and reopened the extension.`);
    } catch {
      // Error is already surfaced in state.
    }
  };

  const codexDesktopBusy = busyKey === `launch-desktop:${selectedAccount?.id}`;
  const vscodeBusy = busyKey === `launch-vscode:${selectedAccount?.id}`;
  const updateNeedsAttention = updateState?.phase === "available" || updateState?.phase === "downloaded";

  return (
    <div className="app-frame" aria-busy={loading}>
      <button
        type="button"
        className={[
          "relay-header-button",
          "relay-gear-button",
          "relay-app-gear-button",
          settingsOpen ? "is-open" : "",
          updateNeedsAttention ? "has-alert" : ""
        ].join(" ")}
        onClick={() => setSettingsOpen((open) => !open)}
        aria-expanded={settingsOpen}
        aria-haspopup="dialog"
        aria-controls={settingsOpen ? "settings-popover" : undefined}
        aria-label={settingsOpen ? "Close settings" : "Open settings"}
        title="Settings"
      >
        <GearIcon />
      </button>
      <div className="app-shell">
        <aside className="relay-sidebar">
          <div className="relay-sidebar-header">
            <div>
              <h1 className="relay-page-title">codex switch</h1>
              <p className="relay-page-copy">Pick an account, see remaining usage, and open it.</p>
            </div>
            <div className="relay-header-actions">
              <button
                type="button"
                className="relay-header-button relay-header-button-primary"
                onClick={addAccount}
                disabled={busyKey === "add-account"}
              >
                {busyKey === "add-account"
                  ? "Starting..."
                  : pendingOAuthFlow
                    ? "Resume Sign-In"
                    : "Add Account"}
              </button>
              <button
                type="button"
                className="relay-header-button"
                onClick={importAccounts}
                disabled={busyKey === "import"}
              >
                {busyKey === "import" ? "Refreshing..." : "Refresh"}
              </button>
              <button
                type="button"
                className="relay-header-button relay-header-button-recommended"
                onClick={selectRecommendedAccount}
                disabled={!recommendedAccount || busyKey === `select:${recommendedAccount?.id}`}
              >
                {busyKey === `select:${recommendedAccount?.id}` ? "Choosing..." : "Pick Best"}
              </button>
            </div>
          </div>

          <div className="relay-sidebar-body">
            {loading ? <p className="relay-empty-copy">Loading accounts...</p> : null}

            {!loading && accounts.length === 0 ? (
              <p className="relay-empty-copy">No accounts found yet. Use Add Account or Refresh.</p>
            ) : null}

            <div className="relay-account-list">
              {accounts.map((account) => (
                <AccountCard
                  key={account.id}
                  account={account}
                  selected={selectedAccount?.id === account.id}
                  recommended={recommendedAccount?.id === account.id}
                  onSelect={selectAccount}
                />
              ))}
            </div>
          </div>
        </aside>

        <main className="relay-main-panel">
          {selectedAccount ? (
            <>
              <div className="relay-main-header">
                <div>
                  <p className="relay-main-label">{formatStatus(selectedAccount.status)}</p>
                  <h2 className="relay-main-title">{selectedAccount.title}</h2>
                  <p className="relay-main-copy">
                    {selectedHasUsage
                      ? "Current remaining usage for this account."
                      : selectedOauthUrl
                        ? "Finish sign-in in your browser. This view refreshes automatically."
                        : "Usage refreshes automatically after you open or select an account."}
                  </p>
                </div>
                {selectedAccount.app_primary ? <span className="relay-pill relay-pill-primary">Primary</span> : null}
              </div>

              {feedback ? <div className="relay-feedback">{feedback}</div> : null}

              {selectedOauthFlow && !selectedHasUsage ? (
                <SignInPanel
                  flow={selectedOauthFlow}
                  onOpen={openExternal}
                  onCancel={cancelSelectedSignIn}
                  cancelBusy={busyKey === `cancel-connect:${selectedAccount.id}`}
                />
              ) : null}

              {selectedHasUsage ? (
                <div className="relay-usage-grid">
                  <UsageStat label="5 hour window" window={selectedUsageWindows.hourly} />
                  <UsageStat label="Weekly window" window={selectedUsageWindows.weekly} />
                </div>
              ) : !selectedOauthFlow ? (
                <section className="relay-usage-stat relay-usage-stat-full">
                  <p className="relay-usage-stat-label">Usage</p>
                  <p className="relay-empty-copy">Checking usage. If it stays empty, open the account in Codex Desktop or Codex VS Code.</p>
                </section>
              ) : null}

              {selectedAccount.last_error ? (
                <div className="relay-feedback relay-feedback-warn">{selectedAccount.last_error}</div>
              ) : null}

              <div className="relay-main-actions">
                {!selectedAccount.app_primary && !selectedOauthFlow ? (
                  <button
                    type="button"
                    className="relay-account-action"
                    onClick={setPrimary}
                    disabled={busyKey === `primary:${selectedAccount.id}`}
                  >
                    {busyKey === `primary:${selectedAccount.id}` ? "Saving..." : "Set Primary"}
                  </button>
                ) : null}
                {selectedOauthUrl ? (
                  <button
                    type="button"
                    className="relay-account-action relay-account-action-primary"
                    onClick={() => openExternal(selectedOauthUrl)}
                  >
                    Open Sign-In Link
                  </button>
                ) : (
                  <>
                    <button
                      type="button"
                      className="relay-account-action relay-account-action-primary"
                      onClick={openSelectedAccountInCodexDesktop}
                      disabled={codexDesktopBusy}
                    >
                      {codexDesktopBusy ? "Opening..." : "Open in Codex Desktop"}
                    </button>
                    <button
                      type="button"
                      className="relay-account-action"
                      onClick={setSelectedAccountForVSCode}
                      disabled={vscodeBusy}
                    >
                      {vscodeBusy ? "Setting..." : "Open in Codex VS Code"}
                    </button>
                  </>
                )}
                {!selectedOauthFlow && !selectedHasUsage && selectedAccount.source?.startsWith("local_") ? (
                  <button
                    type="button"
                    className="relay-account-action"
                    onClick={connectSelectedAccount}
                    disabled={busyKey === `connect:${selectedAccount.id}`}
                  >
                    {busyKey === `connect:${selectedAccount.id}` ? "Starting..." : "Start Sign-In"}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="relay-account-action relay-account-action-danger"
                  onClick={removeSelectedAccount}
                  disabled={busyKey === `remove:${selectedAccount.id}`}
                >
                  {busyKey === `remove:${selectedAccount.id}` ? "Removing..." : "Remove Account"}
                </button>
              </div>
            </>
          ) : (
            <section className="relay-usage-stat relay-usage-stat-full">
              <p className="relay-empty-copy">Select an account to see usage.</p>
            </section>
          )}
        </main>
      </div>
      <SettingsPopover
        open={settingsOpen}
        updateState={updateState}
        busyKey={busyKey}
        repairResult={repairResult}
        onCheck={checkForUpdates}
        onDownload={downloadUpdate}
        onInstall={installUpdate}
        onFixCommonIssues={fixCommonSwitchIssues}
        onClose={() => setSettingsOpen(false)}
      />
      <PendingSignInOverlay
        flow={signInWidgetOpen ? pendingOAuthFlow : null}
        onOpen={openExternal}
        onCancel={cancelPendingSignIn}
        cancelBusy={busyKey === "cancel-pending-oauth"}
      />
    </div>
  );
}

export default App;
