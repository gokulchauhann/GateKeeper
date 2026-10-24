// Shared helpers for GateKeeper dashboard pages, exposed as window.GK.
(function () {
    "use strict";

    // fetch() wrapper: sends the session cookie, parses JSON and turns
    // network or HTTP failures into an Error with a readable message.
    async function fetchJSON(url, options = {}) {
        let response;
        try {
            response = await fetch(url, {
                credentials: "same-origin",
                ...options,
                headers: { Accept: "application/json", ...(options.headers || {}) },
            });
        } catch (error) {
            throw new Error("Couldn't reach the server. Check that it's running and try again.");
        }

        let body = null;
        try {
            body = await response.json();
        } catch (error) {
            // Not JSON (for example an HTML error page); handled below.
        }

        if (!response.ok) {
            const detail = body && typeof body.detail === "string" ? body.detail : `Request failed (${response.status}).`;
            const error = new Error(detail);
            error.status = response.status;   // lets callers tell "signed out" (401/403) from other failures
            throw error;
        }
        return body;
    }

    const numberFormat = new Intl.NumberFormat();
    const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

    function formatNumber(value) {
        return numberFormat.format(value);
    }

    function formatTime(iso) {
        const date = new Date(iso);
        return Number.isNaN(date.getTime()) ? "—" : timeFormat.format(date);
    }

    // Show server-rendered ISO timestamps in the viewer's local time.
    function localizeTimes(root = document) {
        root.querySelectorAll("time[data-local-time]").forEach((el) => {
            const iso = el.getAttribute("datetime");
            if (iso) el.textContent = formatTime(iso);
        });
    }

    // Puts a button into a loading state and back. The label lives in the
    // button's <span> when it has an icon.
    function setBusy(button, busy, busyLabel) {
        const label = button.querySelector("span") || button;
        if (busy) {
            button.dataset.idleLabel = label.textContent;
            label.textContent = busyLabel;
        } else if (button.dataset.idleLabel) {
            label.textContent = button.dataset.idleLabel;
        }
        button.disabled = busy;
        button.setAttribute("aria-busy", String(busy));
    }

    window.GK = { fetchJSON, formatNumber, formatTime, localizeTimes, setBusy };

    document.addEventListener("DOMContentLoaded", () => localizeTimes());
})();
