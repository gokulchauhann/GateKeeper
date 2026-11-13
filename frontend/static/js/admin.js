// Admin dashboard: live tenant usage from the usage endpoint
// (GET /admin/usage, shape in docs/interface-contract.md), polled every
// POLL_MS while the page is visible. Updates the tenant table, the stat cards,
// two Chart.js line charts and the throttle-event log.
//
// Only values returned by the backend are shown; nothing is generated here.
// cpu_percent is a share of ONE core (100 = one full core), the same unit as
// the plan's cpu_quota_percent. throttled_ms is CPU time withheld by the kernel
// during one monitor interval, not a running total.
(function () {
    "use strict";

    const table = document.getElementById("tenant-table");
    if (!table) return;

    const POLL_MS = 2000;               // the backend monitor samples every 2 s
    const MAX_BACKOFF_MS = 30000;       // slowest retry after repeated errors
    const WINDOW_MS = 5 * 60 * 1000;    // charts keep the last 5 minutes
    const MAX_LOG_ROWS = 100;
    const COLORS = ["#8aa2ff", "#6cc4f5", "#cfaaff", "#4ed69b", "#f4bd55", "#f47878"];
    const GRID = "rgba(154, 165, 184, 0.14)";

    const refreshButton = document.getElementById("usage-refresh");
    const toggleButton = document.getElementById("live-toggle");
    const statusPill = document.getElementById("live-status");
    const errorBox = document.getElementById("usage-error");
    const updated = document.getElementById("usage-updated");
    const chartNotice = document.getElementById("chart-notice");
    const logBody = document.querySelector("#throttle-log tbody");
    const logEmpty = document.getElementById("throttle-empty");
    const logCount = document.getElementById("throttle-count");
    const warnRatio = Number(table.dataset.warnRatio) || 0.9;

    // Keep in sync with usage_status() in backend/usage.py and status_badge in macros/ui.html.
    const STATUS = {
        healthy: { label: "Healthy", tone: "success" },
        warning: { label: "Near limit", tone: "warning" },
        throttled: { label: "Throttled", tone: "danger" },
        idle: { label: "Idle", tone: "neutral" },
    };

    // One entry per table row. Quotas are read from the row each time, so a
    // plan change (setTenantPlan) is picked up everywhere.
    const tenants = [...table.querySelectorAll("tbody tr[data-tenant-id]")].map((row, i) => ({
        id: row.dataset.tenantId,
        name: row.dataset.tenantName,
        row,
        color: COLORS[i % COLORS.length],
        lastTimestamp: null,   // timestamp of the last sample plotted, to skip repeats
    }));
    const byId = new Map(tenants.map((t) => [t.id, t]));
    const cpuQuota = (t) => Number(t.row.dataset.cpuQuota);
    const memoryLimit = (t) => Number(t.row.dataset.memoryLimit);

    // ---------- Table and stat cards ----------

    function statusFor(sample, quota, limit) {
        if (!sample) return "idle";
        if (sample.throttled_ms > 0) return "throttled";
        if (sample.cpu_percent >= quota * warnRatio || sample.memory_used_mb >= limit * warnRatio) {
            return "warning";
        }
        return "healthy";
    }

    function updateMeter(row, field, used, limit, unit, decimals) {
        const text = row.querySelector(`[data-field="${field}-text"]`);
        const bar = row.querySelector(`[data-field="${field}-bar"]`);
        let pct = 0;
        let tone = "idle";
        if (used == null) {
            text.textContent = "No session";
        } else {
            pct = limit ? Math.min((used / limit) * 100, 100) : 0;
            tone = pct >= 100 ? "danger" : pct >= warnRatio * 100 ? "warning" : "ok";
            text.textContent = `${used.toFixed(decimals)}${unit} / ${limit}${unit}`;
        }
        bar.setAttribute("aria-valuenow", String(Math.round(pct)));
        bar.firstElementChild.className = `meter__fill meter__fill--${tone}`;
        bar.firstElementChild.style.width = `${pct.toFixed(1)}%`;
    }

    function setStat(field, value) {
        const el = document.querySelector(`[data-stat="${field}"]`);
        if (el) el.textContent = value;
    }

    function applyUsage(samples) {
        const latest = new Map(samples.map((s) => [String(s.tenant_id), s]));
        let active = 0;
        let throttled = 0;
        let cpuTotal = 0;

        tenants.forEach((t) => {
            const sample = latest.get(t.id) || null;
            updateMeter(t.row, "cpu", sample && sample.cpu_percent, cpuQuota(t), "%", 1);
            updateMeter(t.row, "memory", sample && sample.memory_used_mb, memoryLimit(t), " MB", 0);
            t.row.querySelector('[data-field="throttled"]').textContent =
                sample ? `${GK.formatNumber(sample.throttled_ms)} ms` : "—";

            const status = statusFor(sample, cpuQuota(t), memoryLimit(t));
            const badge = t.row.querySelector('[data-field="status"]');
            badge.textContent = STATUS[status].label;
            badge.className = `badge badge--${STATUS[status].tone}`;

            if (sample) {
                active += 1;
                cpuTotal += sample.cpu_percent;
            }
            if (status === "throttled") throttled += 1;
        });

        setStat("active", active);
        setStat("throttled", throttled);
        setStat("avg-cpu", active ? `${(cpuTotal / active).toFixed(1)}%` : "—");
        document.querySelector('[data-stat-card="throttled"]')?.classList.toggle("stat--danger", throttled > 0);

        const newest = samples.map((s) => s.timestamp).sort().pop();
        if (newest) {
            updated.setAttribute("datetime", newest);
            updated.textContent = GK.formatTime(newest);
        }
    }

    // ---------- Charts ----------

    let cpuChart = null;
    let memoryChart = null;

    const timeLabel = (ms) => GK.formatTime(new Date(ms).toISOString());

    function chartOptions(yTitle, suggestedMax, formatValue) {
        return {
            animation: false,
            responsive: true,
            maintainAspectRatio: false,
            parsing: false,
            interaction: { mode: "nearest", axis: "x", intersect: false },
            scales: {
                x: { type: "linear", bounds: "data",
                     ticks: { callback: (value) => timeLabel(value), maxTicksLimit: window.innerWidth < 640 ? 3 : 6 },
                     grid: { color: GRID } },
                y: { beginAtZero: true, suggestedMax, title: { display: true, text: yTitle },
                     grid: { color: GRID } },
            },
            plugins: {
                legend: {
                    labels: { boxWidth: 12, filter: (item, data) => !data.datasets[item.datasetIndex].isQuota },
                },
                tooltip: {
                    filter: (item) => item.parsed.y != null,
                    callbacks: {
                        title: (items) => (items.length ? timeLabel(items[0].parsed.x) : ""),
                        label: (item) => `${item.dataset.tenantName}${item.dataset.isQuota ? " quota" : ""}: ${formatValue(item.parsed.y)}`,
                    },
                },
            },
        };
    }

    function line(t, label, extra = {}) {
        return { label, tenantName: t.name, data: [], borderColor: t.color, backgroundColor: t.color,
                 borderWidth: 2, pointRadius: 0, pointHitRadius: 6, spanGaps: false, ...extra };
    }

    function chartLabels() {
        tenants.forEach((t, i) => {
            if (cpuChart) {
                cpuChart.data.datasets[2 * i].label = `${t.name} (quota ${cpuQuota(t)}%)`;
                cpuChart.data.datasets[2 * i + 1].data.forEach((p) => { p.y = cpuQuota(t); });
            }
            if (memoryChart) memoryChart.data.datasets[i].label = `${t.name} (limit ${memoryLimit(t)} MB)`;
        });
    }

    function createCharts() {
        if (!window.Chart) {
            chartNotice.textContent = "Charts unavailable: the Chart.js library could not be loaded. The table still updates.";
            return;
        }
        Chart.defaults.color = "#9aa5b8";
        Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

        const cpuSets = [];
        tenants.forEach((t) => {
            cpuSets.push(line(t, ""));
            cpuSets.push(line(t, "", { isQuota: true, borderWidth: 1, borderDash: [6, 4] }));
        });
        cpuChart = new Chart(document.getElementById("cpu-chart"), {
            type: "line",
            data: { datasets: cpuSets },
            options: chartOptions("% of one core", 100, (v) => `${v.toFixed(1)}%`),
        });
        memoryChart = new Chart(document.getElementById("memory-chart"), {
            type: "line",
            data: { datasets: tenants.map((t) => line(t, "")) },
            options: chartOptions("MB", undefined, (v) => `${v.toFixed(0)} MB`),
        });
        chartLabels();
    }

    function trimAndDraw() {
        if (!cpuChart) return;
        const series = [...cpuChart.data.datasets.filter((d) => !d.isQuota), ...memoryChart.data.datasets];
        const xs = series.flatMap((d) => d.data.map((p) => p.x));
        if (!xs.length) return;
        const maxX = Math.max(...xs);
        const minX = maxX - WINDOW_MS;
        series.forEach((d) => { d.data = d.data.filter((p) => p.x >= minX); });

        const firstX = Math.min(...series.flatMap((d) => d.data.map((p) => p.x)));
        tenants.forEach((t, i) => {
            const plotted = cpuChart.data.datasets[2 * i].data.some((p) => p.y != null);
            cpuChart.data.datasets[2 * i + 1].data = plotted
                ? [{ x: firstX, y: cpuQuota(t) }, { x: maxX, y: cpuQuota(t) }] : [];
        });
        cpuChart.update("none");
        memoryChart.update("none");
    }

    // One sample for one tenant. Repeats of the same sample are ignored, because
    // the backend returns the latest stored sample until the monitor writes a new one.
    function plotSample(t, s) {
        if (s.timestamp === t.lastTimestamp) return false;
        t.lastTimestamp = s.timestamp;
        const x = Date.parse(s.timestamp);
        const i = tenants.indexOf(t);
        if (cpuChart) {
            cpuChart.data.datasets[2 * i].data.push({ x, y: s.cpu_percent });
            memoryChart.data.datasets[i].data.push({ x, y: s.memory_used_mb });
        }
        if (s.throttled_ms > 0) logThrottle(t, s.timestamp, s.throttled_ms);
        return true;
    }

    // A tenant that had samples and now has none went idle: end its line there
    // instead of drawing a straight line across the gap.
    function markIdle(t) {
        if (!cpuChart || t.lastTimestamp === null) return;
        const i = tenants.indexOf(t);
        [cpuChart.data.datasets[2 * i], memoryChart.data.datasets[i]].forEach((d) => {
            const last = d.data[d.data.length - 1];
            if (last && last.y != null) d.data.push({ x: last.x + 1, y: null });
        });
    }

    function recordSamples(samples) {
        const latest = new Map(samples.map((s) => [String(s.tenant_id), s]));
        tenants.forEach((t) => {
            const s = latest.get(t.id);
            if (s) plotSample(t, s);
            else markIdle(t);
        });
        trimAndDraw();
        const anyData = tenants.some((t) => t.lastTimestamp !== null);
        if (cpuChart) {
            chartNotice.hidden = anyData;
            chartNotice.textContent = "No active tenant sessions yet. Lines appear when a tenant uses the database.";
        }
    }

    // ---------- Throttle log ----------

    const logged = new Set();

    function logThrottle(t, timestamp, throttledMs) {
        const key = `${t.id}|${timestamp}`;
        if (logged.has(key)) return;
        logged.add(key);

        const tr = document.createElement("tr");
        const time = document.createElement("time");
        time.setAttribute("datetime", timestamp);
        time.textContent = GK.formatTime(timestamp);
        const cells = [document.createElement("td"), document.createElement("td"), document.createElement("td")];
        cells[0].append(time);
        cells[1].textContent = t.name;
        cells[2].textContent = `${GK.formatNumber(throttledMs)} ms`;
        cells[2].className = "num";
        tr.append(...cells);
        logBody.prepend(tr);
        while (logBody.rows.length > MAX_LOG_ROWS) logBody.lastElementChild.remove();
        logCount.textContent = String(logged.size);
        logEmpty.hidden = true;
    }

    // ---------- Optional history endpoints (not in the backend yet) ----------

    async function loadHistory() {
        const historyUrl = table.dataset.historyUrl;
        const throttleUrl = table.dataset.throttleUrl;
        if (historyUrl) {
            const samples = await GK.fetchJSON(historyUrl);
            (Array.isArray(samples) ? samples : [])
                .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))
                .forEach((s) => { const t = byId.get(String(s.tenant_id)); if (t) plotSample(t, s); });
            trimAndDraw();
        }
        if (throttleUrl) {
            const events = await GK.fetchJSON(throttleUrl);
            (Array.isArray(events) ? events : [])
                .sort((a, b) => Date.parse(a.occurred_at) - Date.parse(b.occurred_at))
                .forEach((e) => { const t = byId.get(String(e.tenant_id)); if (t) logThrottle(t, e.occurred_at, e.throttled_ms); });
        }
    }

    // ---------- Polling ----------

    let timer = null;
    let inFlight = null;     // the running request, so two never overlap
    let paused = false;
    let failures = 0;

    function setPill(state, text) {
        statusPill.className = `live-pill live-pill--${state}`;
        statusPill.textContent = text;
    }

    function schedule(delay) {
        clearTimeout(timer);
        if (!paused && !document.hidden) timer = setTimeout(poll, delay);
    }

    async function fetchOnce() {
        try {
            const samples = await GK.fetchJSON(table.dataset.usageUrl);
            const list = Array.isArray(samples) ? samples : [];
            applyUsage(list);
            recordSamples(list);
            failures = 0;
            errorBox.hidden = true;
            if (!paused) setPill("live", "Live");
        } catch (error) {
            failures += 1;
            if (error.status === 401 || error.status === 403) {
                paused = true;           // signed out: stop asking
                setToggle();
                setPill("error", "Signed out");
                errorBox.textContent = "Your admin session has ended. Sign in again to resume live updates.";
            } else {
                setPill("error", "Connection problem, retrying");
                errorBox.textContent = `Couldn't refresh usage: ${error.message}`;
            }
            errorBox.hidden = false;
        }
    }

    function poll() {
        if (!inFlight) {
            clearTimeout(timer);
            inFlight = fetchOnce().finally(() => {
                inFlight = null;
                schedule(failures ? Math.min(POLL_MS * 2 ** failures, MAX_BACKOFF_MS) : POLL_MS);
            });
        }
        return inFlight;
    }

    const PAUSE_ICON = '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>';
    const PLAY_ICON = '<path d="m6 3 14 9-14 9z"/>';

    function setToggle() {
        toggleButton.setAttribute("aria-pressed", String(paused));
        toggleButton.querySelector("span").textContent = paused ? "Resume" : "Pause";
        toggleButton.querySelector("svg").innerHTML = paused ? PLAY_ICON : PAUSE_ICON;
    }

    toggleButton.addEventListener("click", () => {
        paused = !paused;
        setToggle();
        if (paused) {
            clearTimeout(timer);
            setPill("paused", "Paused");
        } else {
            poll();
        }
    });

    refreshButton.addEventListener("click", async () => {
        GK.setBusy(refreshButton, true, "Refreshing…");
        await poll();
        GK.setBusy(refreshButton, false);
    });

    document.addEventListener("visibilitychange", () => {
        if (document.hidden) clearTimeout(timer);
        else if (!paused) poll();
    });

    // Called by admin-plan.js after the backend confirms a plan change.
    function setTenantPlan(tenantId, plan) {
        const t = byId.get(String(tenantId));
        if (!t) return;
        Object.assign(t.row.dataset, {
            plan: plan.plan, cpuQuota: String(plan.cpu_quota_percent), memoryLimit: String(plan.memory_limit_mb),
        });
        const cell = t.row.querySelector('[data-field="plan"]');
        const badge = document.createElement("span");
        badge.className = `badge badge--plan badge--plan-${plan.plan}`;
        badge.textContent = plan.plan.charAt(0).toUpperCase() + plan.plan.slice(1);
        const limits = document.createElement("span");
        limits.className = "tenant-cell__id";
        limits.textContent = `${plan.cpu_quota_percent}% CPU · ${plan.memory_limit_mb} MB`;
        cell.replaceChildren(badge, limits);
        chartLabels();
        trimAndDraw();
        poll();
    }

    window.GKAdmin = { poll, applyUsage, setTenantPlan };

    createCharts();
    loadHistory()
        .catch((error) => {
            errorBox.textContent = `Couldn't load usage history: ${error.message}`;
            errorBox.hidden = false;
        })
        .finally(poll);
})();
