// Tenant dashboard: run the heavy report and show its result.
// The endpoint comes from data-report-url, so the backend decides the route.
(function () {
    "use strict";

    const container = document.getElementById("report");
    if (!container) return;

    const button = document.getElementById("report-button");
    const status = document.getElementById("report-status");
    const result = document.getElementById("report-result");
    const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" });

    function showStatus(kind, message) {
        status.className = `report__status report__status--${kind}`;
        status.textContent = message;
    }

    function cell(tag, text, className) {
        const el = document.createElement(tag);
        el.textContent = text;
        if (className) el.className = className;
        return el;
    }

    function renderResult(data) {
        // Only claim enforcement when the backend says the cgroup was applied.
        const note = cell("p", data.cgroup_enforced
            ? "Ran inside this tenant's cgroup. CPU and memory limits were enforced by the kernel."
            : data.governor_mode === "unlimited"
                ? "Ran inside this tenant's cgroup with limits off (baseline run). No CPU or memory limit was applied."
                : "Ran on the development stub. No cgroup limit was applied to this query.", "report__note");

        const rows = Array.isArray(data.rows) ? data.rows : [];
        if (rows.length === 0) {
            result.replaceChildren(note, cell("p", "No orders to report on.", "report__desc"));
            return;
        }

        const table = document.createElement("table");
        table.className = "table table--compact";
        const head = document.createElement("tr");
        head.append(cell("th", "Month"), cell("th", "Orders", "num"), cell("th", "Revenue", "num"));
        head.querySelectorAll("th").forEach((th) => th.setAttribute("scope", "col"));
        table.createTHead().append(head);

        const body = table.createTBody();
        rows.forEach((row) => {
            const tr = document.createElement("tr");
            tr.append(
                cell("td", row.month),
                cell("td", GK.formatNumber(row.orders), "num"),
                cell("td", money.format(row.revenue), "num"),
            );
            body.append(tr);
        });

        const wrap = document.createElement("div");
        wrap.className = "table-wrap";
        wrap.append(table);
        result.replaceChildren(note, wrap);
    }

    button.addEventListener("click", async () => {
        GK.setBusy(button, true, "Generating report…");
        result.hidden = true;
        showStatus("loading", "Running the heavy query. This can take several seconds.");
        try {
            const data = await GK.fetchJSON(container.dataset.reportUrl, { method: "POST" });
            showStatus("success", `Report completed in ${GK.formatNumber(data.duration_ms)} ms.`);
            renderResult(data);
            result.hidden = false;
        } catch (error) {
            showStatus("error", `Report failed: ${error.message}`);
        } finally {
            GK.setBusy(button, false);
        }
    });
})();
