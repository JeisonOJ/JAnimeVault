"use strict";
// Offline Chrome API mocks only. No browser, registry, host process, or network.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");

const extension = path.join(__dirname, "..", "extension");
const manifest = JSON.parse(fs.readFileSync(path.join(extension, "manifest.json"), "utf8"));
assert.deepEqual(manifest.permissions, ["nativeMessaging", "storage", "contextMenus", "scripting"]);
assert.deepEqual(manifest.optional_host_permissions, ["https://*/*", "http://*/*"]);
assert.equal(manifest.manifest_version, 3);
assert.equal(manifest.host_permissions, undefined);
assert.equal(manifest.content_scripts, undefined);

const saved = {};
const calls = [];
let fail = true;
let listener;
let hold;
let gate;
let installed, clicked, menu;
const installHandlers = [];
let replyStatus = "already_saved";
let failStorage = false;
const badges = [];
let allowed = false, added, removed;
const registrations = [], stopped = [];
const chrome = {
    permissions: {
        getAll: async () => ({origins: allowed ? ["https://example.com/*"] : []}),
        contains: async () => allowed,
        onAdded: {addListener: callback => {added = callback;}},
        onRemoved: {addListener: callback => {removed = callback;}}
    },
    scripting: {getRegisteredContentScripts: async () => [], unregisterContentScripts: async () => {}, registerContentScripts: async items => registrations.push(items)},
    tabs: {query: async () => [{id: 5}], sendMessage: async (id, message) => stopped.push(message)},
    contextMenus: {
        removeAll: callback => {menu = null; callback();},
        create: properties => {menu = properties;},
        onClicked: {addListener: callback => {clicked = callback;}}
    },
    action: {setBadgeText: async value => badges.push(value.text), setTitle: async () => {}},
    storage: {local: {
        get: async keys => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map(key =>
            [key, saved[key] ? structuredClone(saved[key]) : undefined])),
        set: async value => {if (failStorage) throw new Error("Mock storage full"); Object.assign(saved, structuredClone(value));}
    }},
    runtime: {
        id: "a".repeat(32), getURL: page => "chrome-extension://" + "a".repeat(32) + "/" + page,
        onMessage: {addListener: callback => {listener = callback;}},
        onInstalled: {addListener: callback => {installHandlers.push(callback); installed = () => installHandlers.forEach(handler => handler());}},
        sendNativeMessage: async (host, capture) => {
            assert.equal(host, "com.externaldiskindexer.planned_transport_test");
            assert.equal(saved.testCapture.capture.capture_id, capture.capture_id); // Stored first.
            calls.push(structuredClone(capture));
            if (gate) await gate;
            if (fail) throw new Error("Mock host unavailable");
            return {ok: true, capture_id: capture.capture_id, status: replyStatus, inbox: "production"};
        }
    }
};
function restartWorker() {
    vm.runInNewContext(fs.readFileSync(path.join(extension, "background.js"), "utf8"),
                       {chrome, crypto: webcrypto, TextEncoder, Date, URL});
}
restartWorker();
const sender = {id: chrome.runtime.id, url: chrome.runtime.getURL("popup.html")};
function action(message) {
    return new Promise(resolve => assert.equal(listener(message, sender, resolve), true));
}

(async () => {
    await require("./test_selection.cjs")();
    installed();
    assert.equal(menu.title, "Add to Planned");
    assert.deepEqual([...menu.contexts], ["selection"]);
    assert.equal(listener({action: "send", text: "x"}, {id: "other", url: "https://example.com/"}, () => {}), false);
    assert.equal(listener({action: "unsupported"}, sender, () => {}), false);
    for (const text of ["", " ", "a".repeat(4097), "\0", "\ud800"]) {
        assert.equal((await action({action: "send", text})).ok, false);
    }
    assert.equal(calls.length, 0);
    assert.equal(saved.testCapture, undefined);
    assert.equal((await action({action: "retry"})).ok, false);

    const text = "  Re:Zero — 勇者 🌸\n ";
    assert.equal((await action({action: "send", text})).ok, false);
    assert.equal(saved.testCapture.delivered, false);
    assert.equal(saved.testCapture.capture.text, text);
    const original = structuredClone(saved.testCapture.capture);
    assert.equal((await action({action: "send", text: "Must not overwrite pending"})).ok, false);
    assert.equal(calls.length, 1); // No timer/background retry, no second pending capture.

    fail = false;
    assert.equal((await action({action: "retry"})).ok, true);
    assert.deepEqual(calls[1], original);
    assert.equal(saved.testCapture.delivered, true);
    assert.equal(saved.testCapture.inbox, "production");
    assert.equal((await action({action: "retry"})).ok, true);
    assert.deepEqual(calls[2], original);

    gate = new Promise(resolve => {hold = resolve;});
    const pending = action({action: "send", text: "Next capture"});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal((await action({action: "retry"})).ok, false); // Guard overlapping UI actions.
    hold();
    await pending;
    assert.notEqual(saved.testCapture.capture.capture_id, original.capture_id);
    assert.equal((await action({action: "state"})).state.delivered, true);
    gate = null;
    const selection = " \t勇者 — Re:Zero! 🌸\n ";
    replyStatus = "saved";
    await clicked({menuItemId: "add-to-planned", selectionText: selection});
    assert.equal(saved.testCapture.capture.text, selection);
    const selectionId = saved.testCapture.capture.capture_id;
    assert.equal(saved.captureFeedback.status, "saved");
    assert.equal(badges.at(-1), "OK");
    await clicked({menuItemId: "add-to-planned", selectionText: selection});
    assert.notEqual(saved.testCapture.capture.capture_id, selectionId);
    fail = true;
    await clicked({menuItemId: "add-to-planned", selectionText: selection});
    const queued = structuredClone(saved.testCapture.capture);
    assert.equal(saved.captureFeedback.status, "queued");
    assert.equal(badges.at(-1), "Q");
    const callCount = calls.length;
    await clicked({menuItemId: "add-to-planned", selectionText: "Do not overwrite pending"});
    assert.equal(saved.captureFeedback.status, "failed");
    assert.deepEqual(saved.testCapture.capture, queued);
    assert.equal(calls.length, callCount);
    restartWorker(); // Simulate browser restart / lost worker globals with persistent storage.
    assert.deepEqual((await action({action: "state"})).state.capture, queued);
    fail = false;
    replyStatus = "already_saved";
    await action({action: "retry"});
    assert.deepEqual(calls.at(-1), queued);
    assert.equal(saved.captureFeedback.status, "already_saved");
    for (const selectionText of [undefined, " ", "x".repeat(4097), "\0"]) {
        await clicked({menuItemId: "add-to-planned", selectionText});
        assert.equal(saved.captureFeedback.status, "failed");
        assert.deepEqual(saved.testCapture.capture, queued);
    }
    failStorage = true;
    const beforeStorageFailure = calls.length;
    const failure = await clicked({menuItemId: "add-to-planned", selectionText: "Cannot persist"});
    assert.equal(failure.status, "failed");
    assert.equal(calls.length, beforeStorageFailure);
    failStorage = false;
    const contentSender = {id: chrome.runtime.id, tab: {id: 5}, frameId: 0,
        url: "https://example.com/page", origin: "https://example.com"};
    function content(message, origin = contentSender) {
        return new Promise(resolve => {if (!listener(message, origin, resolve)) resolve(false);});
    }
    const beforeForged = calls.length;
    for (const message of [{action: "send", text: "forged"}, {action: "retry"},
        {action: "overlayCapture", text: "x", command: "bad"}, {action: "overlayCapture", text: 4}]) {
        assert.equal(await content(message), false);
    }
    assert.equal((await content({action: "overlayCapture", text: "not approved"})).ok, false);
    await added(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(registrations.length, 0); // No sites approved: no injected script.
    allowed = true;
    await action({action: "overlaySync"});
    assert.equal(registrations.at(-1)[0].world, "ISOLATED");
    assert.deepEqual([...registrations.at(-1)[0].matches], ["https://example.com/*"]);
    for (const origin of [{...contentSender, id: "forged"}, {...contentSender, frameId: 3},
        {...contentSender, url: "chrome://settings"}, {...contentSender, origin: "https://other.test"}]) {
        assert.equal((await content({action: "overlayCapture", text: "bad"}, origin)).ok, false);
    }
    assert.equal(calls.length, beforeForged);
    const html = '<script>alert("x")</script> 勇者';
    assert.equal((await content({action: "overlayCapture", text: html})).ok, true);
    assert.equal(calls.at(-1).text, html);
    fail = true;
    assert.equal((await content({action: "overlayCapture", text: "Offline overlay capture"})).status, "queued");
    const offlineOverlay = structuredClone(saved.testCapture.capture);
    fail = false; await action({action: "retry"});
    assert.deepEqual(calls.at(-1), offlineOverlay);
    allowed = false; removed(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(stopped.at(-1).action, "stopOverlay");
    assert.equal((await content({action: "overlayCapture", text: "revoked"})).ok, false);
    console.log("Extension offline checks passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
