// Admin dashboard: change a tenant's plan tier.
// Loaded only when the backend passes plan_change_url (see frontend/README.md).
// Success is shown only after the backend answers 2xx, and the new cgroup
// limits are reported as applied only if the response says so.
(function () {
    "use strict";

    const form = document.getElementById("plan-form");
    if (!form) return;

    const tenantSelect = document.getElementById("plan-tenant");
    const planSelect = document.getElementById("plan-name");
    const submit = document.getElementById("plan-submit");
    const errorText = document.getElementById("plan-error");
    const result = document.getElementById("plan-result");

    function showError(message) {
        errorText.textContent = message;
        errorText.hidden = false;
    }

    function showResult(kind, message) {
        result.className = `plan-result plan-result--${kind}`;
        result.textContent = message;
        result.hidden = false;
    }

    [tenantSelect, planSelect].forEach((el) => el.addEventListener("change", () => {
        errorText.hidden = true;
        result.hidden = true;
    }));

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        errorText.hidden = true;
        result.hidden = true;

        const row = document.querySelector(`#tenant-table tr[data-tenant-id="${tenantSelect.value}"]`);
        if (!tenantSelect.value || !row) return showError("Choose a tenant.");
        if (!planSelect.value) return showError("Choose a plan.");
        if (row.dataset.plan === planSelect.value) {
            return showError(`${row.dataset.tenantName} is already on the ${planSelect.value} plan.`);
        }

        const option = planSelect.selectedOptions[0];
        const ok = window.confirm(
            `Change ${row.dataset.tenantName} from ${row.dataset.plan} to ${planSelect.value}?\n\n` +
            `New limits: ${option.dataset.cpuQuota}% CPU (cpu.max), ${option.dataset.memoryLimit} MB (memory.high).`,
        );
        if (!ok) return;

        const url = form.dataset.urlTemplate.replace("{tenant_id}", encodeURIComponent(tenantSelect.value));
        GK.setBusy(submit, true, "Changing plan…");
        try {
            const data = await GK.fetchJSON(url, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ plan: planSelect.value }),
            });
            window.GKAdmin?.setTenantPlan(tenantSelect.value, data);

            // Expected quota in cpu.max form, e.g. 50% -> "50000 100000".
            const expected = `${Number(data.cpu_quota_percent) * 1000} 100000`;
            if (data.limits_applied === true && data.cpu_max === expected) {
                showResult("success", `${row.dataset.tenantName} is now on ${data.plan}. The backend confirmed cpu.max = ${data.cpu_max}.`);
            } else if (data.limits_applied === true) {
                showResult("warning", `Plan saved, but the backend reported cpu.max = ${data.cpu_max ?? "unknown"}, expected ${expected}. Check the cgroup.`);
            } else {
                showResult("warning", `Plan saved as ${data.plan}, but the backend did not confirm that the new cgroup limits were applied.`);
            }
        } catch (error) {
            if (error.status === 404 || error.status === 405 || error.status === 501) {
                showResult("error", "This backend cannot change plans yet (no plan-change endpoint). Nothing was changed.");
            } else {
                showResult("error", `Plan not changed: ${error.message}`);
            }
        } finally {
            GK.setBusy(submit, false);
        }
    });
})();
