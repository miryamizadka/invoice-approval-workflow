(function () {
  "use strict";

  const POLL_INTERVAL_MS = 10000;

  const ROUTE_ORDER = ["auto_approve", "human_review", "reject", "duplicate"];
  const ROUTE_LABELS = {
    auto_approve: "Auto-approved",
    human_review: "Human review",
    reject: "Rejected",
    duplicate: "Duplicate",
  };

  const dashboardContent = document.getElementById("dashboard-content");
  const dashboardMessage = document.getElementById("dashboard-message");

  function formatMoney(amountsByCurrency) {
    const entries = Object.entries(amountsByCurrency).sort(([a], [b]) => a.localeCompare(b));
    // sorted alphabetically - stable tile order across refreshes, not
    // whatever order the JSON object's keys happened to arrive in
    if (entries.length === 0) return "—";
    return entries
      .map(
        ([currency, amount]) =>
          `${currency} ${Number(amount).toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })}`
      )
      .join(", ");
  }

  function formatRate(rate) {
    return `${(rate * 100).toFixed(1)}%`;
  }

  function formatRouteBreakdown(countsByRoute) {
    return ROUTE_ORDER.filter((route) => countsByRoute[route] !== undefined)
      .map((route) => `${ROUTE_LABELS[route]}: ${countsByRoute[route]}`)
      .join(" · ");
  }

  function renderStatCard(label, value, category) {
    const card = document.createElement("article");
    // category distinguishes WHO produced this number (the system vs a
    // human), not whether the value is "good" or "bad" - there's no
    // absolute threshold that makes a rate good or bad, so it's never
    // colored based on the value itself.
    card.className = category ? `stat-card stat-card-${category}` : "stat-card";

    const heading = document.createElement("h2");
    heading.textContent = label;
    card.appendChild(heading);

    const valueEl = document.createElement("p");
    valueEl.textContent = value;
    card.appendChild(valueEl);

    return card;
  }

  function renderSummary(summary) {
    dashboardContent.innerHTML = "";
    if (summary.total_invoices === 0) {
      dashboardContent.textContent = "No invoices processed yet.";
      return;
    }

    const grid = document.createElement("div");
    grid.className = "stat-grid";
    grid.appendChild(
      renderStatCard("Auto-Approval Rate", formatRate(summary.auto_approval_rate), "auto")
    );
    grid.appendChild(
      renderStatCard("Human Escalation Rate", formatRate(summary.human_escalation_rate), "human")
    );
    grid.appendChild(
      renderStatCard("Money Auto-Approved", formatMoney(summary.money_auto_approved), "auto")
    );
    grid.appendChild(
      renderStatCard("Money Human-Approved", formatMoney(summary.money_human_approved), "human")
    );
    grid.appendChild(
      renderStatCard("Total Invoices Processed", String(summary.total_invoices), "neutral")
    );
    dashboardContent.appendChild(grid);

    // Rates only cover auto_approve/human_review - reject/duplicate make up
    // the rest, so the two rates deliberately don't sum to 100%. This line
    // shows exactly where the remainder went instead of leaving it unexplained.
    const breakdown = document.createElement("p");
    breakdown.className = "route-breakdown";
    breakdown.textContent = `Breakdown: ${formatRouteBreakdown(summary.counts_by_route)}`;
    dashboardContent.appendChild(breakdown);

    const updated = document.createElement("p");
    updated.className = "last-updated";
    updated.textContent = `Last updated: ${summary.generated_at}`;
    dashboardContent.appendChild(updated);
  }

  async function loadSummary() {
    try {
      const summary = await apiGet("/audit/summary");
      renderSummary(summary);
    } catch (err) {
      showError(dashboardMessage, err.message);
    }
  }

  loadSummary();
  setInterval(loadSummary, POLL_INTERVAL_MS);
})();
