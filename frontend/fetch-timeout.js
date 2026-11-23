/**
 * fetch() with a deadline.
 *
 * A backend that accepts the connection but never answers leaves the fetch promise unsettled
 * forever, which is how the Operations & Sync History modal sat on "Loading operation logs..."
 * with no error and no retry (issue #92). This wraps fetch with an AbortController deadline and
 * reports the deadline as a distinct TimeoutError so callers can say "backend unreachable"
 * instead of echoing a raw transport message.
 *
 * Loaded before app.js in the SPA; also exported for the Node regression test.
 */
(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    } else {
        root.fetchWithTimeout = api.fetchWithTimeout;
    }
})(typeof self !== "undefined" ? self : this, function () {
    async function fetchWithTimeout(url, options = {}, timeoutMs = 8000) {
        const controller = new AbortController();
        const callerSignal = options.signal;
        let timedOut = false;

        const timer = setTimeout(() => {
            timedOut = true;
            controller.abort();
        }, timeoutMs);

        const onCallerAbort = () => controller.abort();
        if (callerSignal) {
            if (callerSignal.aborted) controller.abort();
            else callerSignal.addEventListener("abort", onCallerAbort, { once: true });
        }

        try {
            return await fetch(url, { ...options, signal: controller.signal });
        } catch (err) {
            if (timedOut) {
                const timeoutErr = new Error(
                    `no response within ${Math.round(timeoutMs / 1000)}s`
                );
                timeoutErr.name = "TimeoutError";
                timeoutErr.cause = err;
                throw timeoutErr;
            }
            throw err;
        } finally {
            clearTimeout(timer);
            if (callerSignal) callerSignal.removeEventListener("abort", onCallerAbort);
        }
    }

    return { fetchWithTimeout };
});
