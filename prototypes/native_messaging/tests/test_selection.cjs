"use strict";
// Small isolated DOM model; not a substitute for browser layout/security testing.
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
module.exports = async function () {
    class Target {
        constructor() {this.events = new Map(); this.children = []; this.nodeType = 1;
            this.style = {setProperty: (key, value) => {this[key] = value;}};}
        addEventListener(name, handler) {if (!this.events.has(name)) this.events.set(name, []); this.events.get(name).push(handler);}
        removeEventListener(name, handler) {this.events.set(name, (this.events.get(name) || []).filter(item => item !== handler));}
        emit(name, value = {}) {return Promise.all((this.events.get(name) || []).map(handler => handler(value)));}
        append(...children) {this.children.push(...children);}
        attachShadow() {this.shadow = new Target(); return this.shadow;}
        setAttribute() {}
        closest() {return this.editable ? this : null;}
        querySelectorAll() {return [];}
        getBoundingClientRect() {return {width: 60, height: 32};}
        remove() {this.removed = true;}
    }
    const document = new Target(), window = new Target();
    document.documentElement = new Target(); document.activeElement = new Target();
    document.createElement = () => new Target();
    window.navigation = new Target();
    window.visualViewport = new Target();
    Object.assign(window.visualViewport, {width: 100, height: 100, offsetLeft: 0, offsetTop: 0});
    let selected = ' <script>not executable</script> 勇者 🌸 ', collapsed = false, allowed = true, unavailable = false;
    const anchor = new Target(), focus = new Target();
    let rects = [{top: 95, bottom: 105, right: 100, left: 80, width: 20, height: 10}];
    const range = {commonAncestorContainer: anchor, getClientRects: () => rects};
    window.getSelection = () => ({isCollapsed: collapsed, rangeCount: 1, anchorNode: anchor, focusNode: focus,
        anchorOffset: 0, focusOffset: 3, toString: () => selected, getRangeAt: () => range});
    let runtimeListener, timerId = 0;
    const timers = new Map(), captures = [];
    const chrome = {runtime: {id: "test", onMessage: {
        addListener: handler => {runtimeListener = handler;}, removeListener: () => {runtimeListener = null;}
    }, sendMessage: async message => {
        if (unavailable) throw new Error("worker gone");
        if (message.action === "overlayAllowed") return {ok: allowed};
        captures.push(message);
        return {ok: false, status: "queued", error: "Queued in browser"};
    }}};
    const context = vm.createContext({document, window, chrome, TextEncoder, innerWidth: 100, innerHeight: 100,
        setTimeout: (callback, delay) => {timers.set(++timerId, {callback, delay}); return timerId;},
        clearTimeout: id => timers.delete(id)});
    const code = fs.readFileSync(path.join(__dirname, "../extension/selection.js"), "utf8");
    async function select() {
        await document.emit("selectionchange");
        for (const [id, timer] of [...timers]) if (timer.delay === 80) {timers.delete(id); await timer.callback();}
    }
    vm.runInContext(code, context);
    const initialListeners = document.events.get("selectionchange").length;
    vm.runInContext(code, context);
    assert.equal(document.events.get("selectionchange").length, initialListeners);
    await select();
    const host = document.documentElement.children[0], button = host.shadow.children[1], feedback = host.shadow.children[2];
    assert.equal(host.hidden, false);
    assert.equal(captures.length, 0); // Selection sends permission check only, never text.
    assert.equal(host.left, "32px");
    assert.equal(host.top, "56px");
    rects = [{top: 10, bottom: 20, right: 30, left: 10, width: 20, height: 10}];
    await select(); assert.equal(host.top, "27px"); assert.equal(host.left, "8px");
    rects = [{top: 70, bottom: 80, right: 95, left: 50, width: 45, height: 10},
             {top: 85, bottom: 95, right: 60, left: 40, width: 20, height: 10}];
    await select(); assert.equal(host.top, "31px"); assert.equal(host.left, "32px");
    await window.emit("resize"); assert.equal(host.hidden, true);
    context.innerWidth = 200; context.innerHeight = 200;
    window.visualViewport.width = 200; window.visualViewport.height = 200;
    await select(); assert.equal(host.left, "35px");
    await window.visualViewport.emit("resize"); assert.equal(host.hidden, true);
    context.innerWidth = 100; context.innerHeight = 100;
    window.visualViewport.width = 100; window.visualViewport.height = 100;
    await select(); await window.visualViewport.emit("scroll"); assert.equal(host.hidden, true);
    const validRects = rects; rects = []; await select(); assert.equal(host.hidden, true);
    rects = validRects; await select();
    const click = trusted => ({isTrusted: trusted, preventDefault() {}, stopPropagation() {}});
    await button.emit("click", click(false));
    assert.equal(captures.length, 0);
    await Promise.all([button.emit("click", click(true)), button.emit("click", click(true))]);
    assert.equal(captures.length, 1);
    assert.equal(captures[0].text, selected);
    assert.equal(feedback.textContent, "Queued in browser");
    await select(); await button.emit("click", click(true));
    assert.equal(captures.length, 1); // Same consumed selection cannot resubmit.
    selected = "A new title"; await select();
    selected = "Stale changed selection"; await button.emit("click", click(true));
    assert.equal(captures.length, 1);
    await select(); await document.emit("keydown", {key: "Escape"}); assert.equal(host.hidden, true);
    await select(); await window.emit("scroll"); assert.equal(host.hidden, true);
    await select(); await window.navigation.emit("navigate"); assert.equal(host.hidden, true);
    assert.equal(document.events.get("selectionchange").length, initialListeners);
    document.activeElement.editable = true; await select(); assert.equal(host.hidden, true);
    document.activeElement.editable = false; selected = "x".repeat(4097); await select(); assert.equal(host.hidden, true);
    selected = "Normal"; collapsed = true; await select(); assert.equal(host.hidden, true);
    collapsed = false; await select();
    runtimeListener({action: "stopOverlay"}, {id: "test"});
    assert.equal(host.removed, true); assert.equal(document.events.get("selectionchange").length, 0);
    allowed = false; vm.runInContext(code, context); await select();
    assert.equal(document.events.get("selectionchange").length, 0);
    allowed = true; unavailable = true; vm.runInContext(code, context); await select();
    assert.equal(document.events.get("selectionchange").length, 0);
    const nodes = new Map();
    const popupDocument = {getElementById: id => {
        if (!nodes.has(id)) nodes.set(id, new Target());
        return nodes.get(id);
    }};
    let grant = false;
    const requests = [], removals = [];
    const popupChrome = {runtime: {id: "test", sendMessage: async message =>
        message.action === "state" ? {ok: true, state: null} : {ok: true}}, permissions: {
        request: async request => {requests.push(request); return grant;},
        remove: async request => {removals.push(request); return true;}
    }};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../extension/popup.js"), "utf8"),
        {document: popupDocument, chrome: popupChrome, URL});
    popupDocument.getElementById("site").value = "https://example.com";
    await nodes.get("enable-site").emit("click");
    assert.match(nodes.get("site-status").textContent, /denied/);
    grant = true; await nodes.get("enable-site").emit("click");
    assert.match(nodes.get("site-status").textContent, /Approved/);
    assert.deepEqual([...requests.at(-1).origins], ["https://example.com/*"]);
    await nodes.get("disable-site").emit("click");
    assert.equal(removals.length, 1);
    for (const invalid of ["https://*.example.com", "https://example.com/path", "file:///test", "https://user:pass@example.com"]) {
        nodes.get("site").value = invalid;
        await nodes.get("enable-site").emit("click");
    }
    assert.equal(requests.length, 2);
    console.log("Overlay isolated DOM checks passed");
};
