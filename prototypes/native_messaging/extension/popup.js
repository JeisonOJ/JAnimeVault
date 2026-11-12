"use strict";

const text = document.getElementById("text");
const send = document.getElementById("send");
const retry = document.getElementById("retry");
const status = document.getElementById("status");
document.getElementById("identity").textContent = "Extension ID: " + chrome.runtime.id;

function showState(state) {
    send.disabled = Boolean(state && !state.delivered);
    retry.disabled = !state;
    if (state) {
        status.textContent = (state.delivered ? (state.inbox === "production" ? "Production inbox: " : "Prototype inbox: ") + state.status : "Pending in browser; not acknowledged.") +
            "\nCapture UUID: " + state.capture.capture_id;
    }
}

async function refresh() {
    const result = await chrome.runtime.sendMessage({action: "state"});
    if (!result?.ok) throw new Error(result?.error || "Could not read test state.");
    showState(result.state);
    if (result.feedback) status.textContent = result.feedback.message +
        (result.state ? "\nCapture UUID: " + result.state.capture.capture_id : "");
    return result.state;
}

async function perform(action) {
    send.disabled = retry.disabled = true;
    status.textContent = "Sending to test host…";
    try {
        const result = await chrome.runtime.sendMessage(action === "send" ? {action, text: text.value} : {action});
        await refresh();
        if (!result?.ok) throw new Error(result?.error || "No extension response.");
        showState(result.state);
    } catch (error) {
        status.textContent = error.message;
        // Reopening the popup also restores durable pending state.
    }
}

send.addEventListener("click", () => perform("send"));
retry.addEventListener("click", () => perform("retry"));
refresh().then(state => {
    if (state) text.value = state.capture.text;
}).catch(error => { status.textContent = error.message; });

for (const [id, enable] of [["enable-site", true], ["disable-site", false]]) {
    document.getElementById(id).addEventListener("click", async () => {
        const label = document.getElementById("site-status");
        try {
            const url = new URL(document.getElementById("site").value);
            if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.port ||
                url.pathname !== "/" || url.search || url.hash || url.hostname.includes("*")) throw new Error("Enter an exact website origin, such as https://www.youtube.com");
            // Called directly from this button gesture, never at startup.
            const request = {origins: [url.origin + "/*"]};
            const changed = await (enable ? chrome.permissions.request(request) : chrome.permissions.remove(request));
            if (!changed) { label.textContent = "Permission denied or unchanged; overlay not enabled by this action."; return; }
            const result = await chrome.runtime.sendMessage({action: "overlaySync"});
            label.textContent = result.ok ? (enable ? "Approved. Reload this webpage to enable the button." : "Access revoked. Reload open webpages to remove old script instances.") : result.error;
        } catch (error) { label.textContent = error.message; }
    });
}
