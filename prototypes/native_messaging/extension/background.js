"use strict";

const HOST_NAME = "com.externaldiskindexer.planned_transport_test";
let busy = false;
const MENU_ID = "add-to-planned";

function installSelectionMenu() {
    chrome.contextMenus.removeAll(() => {
        if (chrome.runtime.lastError) return;
        chrome.contextMenus.create({id: MENU_ID, title: "Add to Planned", contexts: ["selection"]});
    });
}
chrome.runtime.onInstalled.addListener(installSelectionMenu);

let permissionSync = Promise.resolve();
function syncOverlayScripts() {
    permissionSync = permissionSync.catch(() => {}).then(async () => {
        const granted = await chrome.permissions.getAll();
        const matches = (granted.origins || []).filter(origin => /^https?:\/\/[^/*:]+\/\*$/.test(origin));
        const registered = await chrome.scripting.getRegisteredContentScripts({ids: ["planned-selection"]});
        if (registered.length) await chrome.scripting.unregisterContentScripts({ids: ["planned-selection"]});
        if (matches.length) await chrome.scripting.registerContentScripts([{
            id: "planned-selection", matches, js: ["selection.js"], world: "ISOLATED",
            allFrames: false, runAt: "document_idle", persistAcrossSessions: true
        }]);
    });
    return permissionSync;
}
chrome.runtime.onInstalled.addListener(() => syncOverlayScripts().catch(() => {}));
chrome.permissions.onAdded.addListener(() => syncOverlayScripts().catch(() => {}));
chrome.permissions.onRemoved.addListener(() => {
    syncOverlayScripts().catch(() => {});
    // Revoking a grant does not unload already injected JavaScript. Tear down
    // existing overlays; every future capture also rechecks current permission.
    chrome.tabs.query({}).then(tabs => Promise.all(tabs.map(tab =>
        chrome.tabs.sendMessage(tab.id, {action: "stopOverlay"}).catch(() => {})))).catch(() => {});
});

async function allowedOverlaySender(sender) {
    if (sender.id !== chrome.runtime.id || !Number.isInteger(sender.tab?.id) || sender.frameId !== 0) return false;
    try {
        const url = new URL(sender.url);
        if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.port ||
            (sender.origin && sender.origin !== url.origin)) return false;
        return await chrome.permissions.contains({origins: [url.origin + "/*"]});
    } catch (_) { return false; }
}

async function publishFeedback(result) {
    const feedback = {status: result.ok ? result.state.status : result.status || "failed", message: result.ok
        ? `${result.state.inbox === "production" ? "Production" : "Prototype"} inbox: ${result.state.status}. Not yet confirmed imported into Planned.`
        : result.error};
    try { await chrome.storage.local.set({captureFeedback: feedback}); } catch (_) { /* Badge still reports the result. */ }
    await Promise.all([
        chrome.action.setBadgeText({text: result.ok ? "OK" : feedback.status === "queued" ? "Q" : "!"}),
        chrome.action.setTitle({title: "Planned: " + feedback.message})
    ]).catch(() => {});
    return result;
}

chrome.contextMenus.onClicked.addListener(info => {
    if (info.menuItemId !== MENU_ID) return;
    // Use the browser's selection verbatim; never inspect the page or its URL.
    return handleTestAction({action: "send", text: info.selectionText}).then(publishFeedback);
});

async function handleTestAction(message) {
    if (message.action === "state") {
        const stored = await chrome.storage.local.get(["testCapture", "captureFeedback"]);
        return {ok: true, state: stored.testCapture || null, feedback: stored.captureFeedback || null};
    }
    if (busy) {
        return {ok: false, error: "A capture is already being sent."};
    }
    busy = true;
    let retained = false;
    let previouslyDelivered = false;
    try {
        let state = (await chrome.storage.local.get("testCapture")).testCapture;
        if (message.action === "send") {
            if (state && !state.delivered) {
                throw new Error("A capture is pending. Use Retry same capture; its original text is preserved.");
            }
            const text = message.text;
            if (typeof text !== "string" || !text.trim() || [...text].length > 4096 ||
                new TextEncoder().encode(text).length > 16384 ||
                /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/.test(text) ||
                [...text].some(char => char.codePointAt(0) >= 0xd800 && char.codePointAt(0) <= 0xdfff)) {
                throw new Error("Enter nonblank text up to 4096 Unicode characters / 16 KiB, without unsupported control characters.");
            }
            state = {capture: {version: 1, type: "capture", capture_id: crypto.randomUUID(),
                text, captured_at: new Date().toISOString()}, delivered: false};
            // Persist before contacting the host. No automatic retries or timers.
            await chrome.storage.local.set({testCapture: state, captureFeedback: {status: "queued",
                message: "Queued in browser; native delivery not yet acknowledged. Open the popup to retry."}});
        } else if (message.action !== "retry" || !state) {
            throw new Error("No previous test capture to retry.");
        }
        retained = true;
        previouslyDelivered = state.delivered;
        let response;
        try {
            response = await chrome.runtime.sendNativeMessage(HOST_NAME, state.capture);
        } catch (error) {
            throw new Error("Not acknowledged. Capture retained in browser; retry after fixing the host. " + error.message);
        }
        if (!response || response.ok !== true || response.capture_id !== state.capture.capture_id ||
            !["saved", "already_saved"].includes(response.status)) {
            throw new Error("Not acknowledged. " + (response?.message || "Invalid host response."));
        }
        state = {...state, delivered: true, status: response.status,
            inbox: response.inbox === "production" ? "production" : "prototype"};
        await chrome.storage.local.set({testCapture: state});
        return {ok: true, state};
    } catch (error) {
        return {ok: false, status: retained && !previouslyDelivered ? "queued" : "failed", error: previouslyDelivered
            ? "Retry failed; the previously acknowledged capture is retained. " + error.message : retained
            ? "Queued in browser; delivery not acknowledged. Open the popup to retry. " + error.message
            : "Not captured. " + error.message};
    } finally {
        busy = false;
    }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || typeof message !== "object" || Array.isArray(message)) return false;
    const keys = Object.keys(message).sort().join(",");
    if (["overlayCapture", "overlayAllowed"].includes(message.action)) {
        if (keys !== (message.action === "overlayCapture" ? "action,text" : "action") ||
            (message.action === "overlayCapture" && typeof message.text !== "string")) return false;
        (async () => {
            if (!await allowedOverlaySender(sender)) return {ok: false, error: "Website access is not approved. Nothing captured."};
            if (message.action === "overlayAllowed") return {ok: true};
            return publishFeedback(await handleTestAction({action: "send", text: message.text}));
        })().then(sendResponse, () => sendResponse({ok: false, error: "Overlay unavailable. Nothing captured; use the context menu."}));
        return true;
    }
    if (sender.id !== chrome.runtime.id || sender.tab || sender.url !== chrome.runtime.getURL("popup.html") ||
        !["state", "send", "retry", "overlaySync"].includes(message.action) ||
        keys !== (message.action === "send" ? "action,text" : "action")) return false;
    if (message.action === "overlaySync") {
        syncOverlayScripts().then(() => sendResponse({ok: true}), () => sendResponse({ok: false, error: "Could not update overlay. Reload the extension."}));
        return true;
    }
    const operation = handleTestAction(message);
    (message.action === "state" ? operation : operation.then(publishFeedback))
        .then(sendResponse, () => sendResponse({ok: false, error: "Test action failed."}));
    return true;
});
