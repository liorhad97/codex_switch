import { useEffect, useRef, useState } from "react";

const USAGE_REFRESH_INTERVAL_MS = 60_000;

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

function buildUsageSummary(account) {
  const parts = [];
  if (account.rate_limits?.primary) {
    parts.push(`5h ${formatUsagePercent(account.rate_limits.primary.usedPercent)}`);
  }
  if (account.rate_limits?.secondary || isFreeAccount(account)) {
    parts.push(`Weekly ${formatUsagePercent(account.rate_limits?.secondary?.usedPercent)}`);
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
  const weeklyWindow = account?.rate_limits?.secondary;
  return typeof getRemainingFraction(weeklyWindow) === "number" && hasKnownResetTime(weeklyWindow);
}

function isPickableAccount(account) {
  return Boolean(account?.enabled && !account?.oauth);
}

function isFreeAccount(account) {
  const weeklyRemaining = getRemainingFraction(account?.rate_limits?.secondary);
  return Boolean(isPickableAccount(account) && hasUsageData(account) && typeof weeklyRemaining !== "number");
}

function calculatePlusAccountUsageCost(account) {
  const hourlyRemaining = getRemainingFraction(account?.rate_limits?.primary);
  const weeklyRemaining = getRemainingFraction(account?.rate_limits?.secondary);
  const knownWindows = [hourlyRemaining, weeklyRemaining].filter((value) => typeof value === "number");
  const hasHourly = typeof hourlyRemaining === "number";
  const hasWeekly = typeof weeklyRemaining === "number";

  if (
    !isPickableAccount(account) ||
    !hasPickableWeeklyWindow(account) ||
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
  const hourlyRemaining = getRemainingFraction(account?.rate_limits?.primary);

  if (!isFreeAccount(account) || typeof hourlyRemaining !== "number" || hourlyRemaining <= 0) {
    return Number.POSITIVE_INFINITY;
  }

  const resetPenalty = hasKnownResetTime(account?.rate_limits?.primary) ? 0 : 0.35;
  const headroomPenalty = 1 / (hourlyRemaining + 0.05);

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

function App() {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState(null);
  const [feedback, setFeedback] = useState("");
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

  const importAccounts = async () => {
    await runAction("import", () => request("/api/import", { method: "POST" }));
  };

  const addAccount = async () => {
    try {
      const payload = await runAction("add-account", () =>
        request("/api/accounts/add", { method: "POST", body: JSON.stringify({}) })
      );
      setFeedback("Sign-in started. Finish it in the browser panel.");
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

  return (
    <div className="app-frame" aria-busy={loading}>
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
                disabled={busyKey === "add-account" || pendingOAuthFlow?.status === "awaiting_browser"}
              >
                {busyKey === "add-account"
                  ? "Starting..."
                  : pendingOAuthFlow?.status === "awaiting_browser"
                    ? "Waiting..."
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

            {pendingOAuthFlow ? (
              <SignInPanel
                flow={pendingOAuthFlow}
                onOpen={openExternal}
                onCancel={cancelPendingSignIn}
                cancelBusy={busyKey === "cancel-pending-oauth"}
              />
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
                  <UsageStat label="5 hour window" window={selectedAccount.rate_limits?.primary} />
                  <UsageStat label="Weekly window" window={selectedAccount.rate_limits?.secondary} />
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
            pendingOAuthFlow ? (
              <SignInPanel
                flow={pendingOAuthFlow}
                onOpen={openExternal}
                onCancel={cancelPendingSignIn}
                cancelBusy={busyKey === "cancel-pending-oauth"}
              />
            ) : (
              <section className="relay-usage-stat relay-usage-stat-full">
                <p className="relay-empty-copy">Select an account to see usage.</p>
              </section>
            )
          )}
        </main>
      </div>
    </div>
  );
}

export default App;
