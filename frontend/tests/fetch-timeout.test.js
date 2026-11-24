/**
 * Regression test for issue #92: a backend that never answers must surface as a bounded
 * TimeoutError, not an unsettled promise.
 *
 * Run with: node frontend/tests/fetch-timeout.test.js
 */

const assert = require("node:assert");
const { fetchWithTimeout } = require("../fetch-timeout.js");

const realFetch = globalThis.fetch;

async function testResolvesThroughTheWrapper() {
    globalThis.fetch = async () => ({ ok: true, status: 200 });
    const res = await fetchWithTimeout("http://localhost/api/operations/logs", {}, 5000);
    assert.strictEqual(res.status, 200);
}

// A fetch that never answers on its own but rejects when the signal aborts, exactly like a real
// fetch against a server that accepted the connection and then stopped responding.
function hangingFetch(_url, options) {
    return new Promise((_resolve, reject) => {
        options.signal.addEventListener("abort", () => {
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
        });
    });
}

async function testNeverResolvingFetchHitsTheDeadline() {
    globalThis.fetch = hangingFetch;
    const startedAt = Date.now();
    await assert.rejects(
        () => fetchWithTimeout("http://localhost/api/operations/logs", {}, 40),
        (err) => {
            assert.strictEqual(err.name, "TimeoutError");
            assert.match(err.message, /no response within/);
            return true;
        }
    );
    const elapsed = Date.now() - startedAt;
    assert.ok(elapsed < 1000, `deadline fired late: ${elapsed}ms`);
}

async function testCallerAbortIsNotReportedAsATimeout() {
    globalThis.fetch = hangingFetch;
    const controller = new AbortController();
    const pending = fetchWithTimeout("http://localhost/x", { signal: controller.signal }, 5000);
    controller.abort();
    await assert.rejects(pending, (err) => err.name === "AbortError");
}

async function testFastFailurePassesThrough() {
    globalThis.fetch = async () => {
        throw new TypeError("Failed to fetch");
    };
    await assert.rejects(
        () => fetchWithTimeout("http://localhost/x", {}, 5000),
        (err) => err instanceof TypeError
    );
}

(async () => {
    try {
        await testResolvesThroughTheWrapper();
        await testNeverResolvingFetchHitsTheDeadline();
        await testCallerAbortIsNotReportedAsATimeout();
        await testFastFailurePassesThrough();
        console.log("fetch-timeout: 4 checks passed");
    } finally {
        globalThis.fetch = realFetch;
    }
})().catch((err) => {
    console.error("fetch-timeout: FAILED -", err);
    process.exit(1);
});
