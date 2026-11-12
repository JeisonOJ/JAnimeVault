"use strict";
(() => {
    if (globalThis.__plannedSelectionStop) return; // One isolated-world instance per document.
    const sensitive = 'input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"],.monaco-editor,.CodeMirror';
    const listeners = [];
    let host, button, feedback, snapshot, consumed, timer, epoch = 0, sending = false;
    function blocked(node) {
        const element = node?.nodeType === 1 ? node : node?.parentElement;
        return Boolean(element?.closest(sensitive));
    }
    function selection() {
        // Never read form values, editable selections, or surrounding text.
        if (blocked(document.activeElement)) return null;
        const value = window.getSelection();
        if (!value || value.isCollapsed || value.rangeCount !== 1 || blocked(value.anchorNode) || blocked(value.focusNode)) return null;
        const range = value.getRangeAt(0);
        const ancestor = range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
        if (ancestor?.querySelectorAll && [...ancestor.querySelectorAll(sensitive)].some(element => range.intersectsNode(element))) return null;
        const text = value.toString();
        if (!text.trim() || [...text].length > 4096 || new TextEncoder().encode(text).length > 16384 ||
            /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/.test(text) ||
            [...text].some(char => char.codePointAt(0) >= 0xd800 && char.codePointAt(0) <= 0xdfff)) return null;
        const rects = [...range.getClientRects()].filter(rect => rect.width > 0 && rect.height > 0 &&
            rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth);
        if (!rects.length) return null;
        return {text, anchor: value.anchorNode, focus: value.focusNode, start: value.anchorOffset,
            end: value.focusOffset, rect: {top: Math.min(...rects.map(r => r.top)),
                bottom: Math.max(...rects.map(r => r.bottom)), right: Math.max(...rects.map(r => r.right))}};
    }
    function same(a, b) {
        return a && b && a.text === b.text && a.anchor === b.anchor && a.focus === b.focus && a.start === b.start && a.end === b.end;
    }
    function dismiss() {
        clearTimeout(timer); epoch++; snapshot = null;
        if (host) { host.hidden = true; host.style.setProperty("display", "none", "important"); }
    }
    function stop() {
        dismiss(); host?.remove();
        for (const [target, name, handler, options] of listeners) target.removeEventListener(name, handler, options);
        chrome.runtime.onMessage.removeListener(onMessage);
        delete globalThis.__plannedSelectionStop;
    }
    function listen(target, name, handler, options) {
        target.addEventListener(name, handler, options); listeners.push([target, name, handler, options]);
    }
    function create() {
        host = document.createElement("div");
        host.style.cssText = "all:initial!important;position:fixed!important;z-index:2147483000!important;max-width:calc(100vw - 16px)!important;";
        host.hidden = true; host.style.setProperty("display", "none", "important");
        const shadow = host.attachShadow({mode: "closed"});
        const style = document.createElement("style");
        style.textContent = ":host([hidden]){display:none!important}button{font:13px system-ui;color:#fff;background:#272238;border:1px solid #aa8cff;border-radius:8px;padding:7px 10px;cursor:pointer}button:focus-visible{outline:3px solid #66ddcc;outline-offset:2px}p{font:12px system-ui;color:#fff;background:#272238;margin:0;padding:6px;max-width:240px}";
        button = document.createElement("button"); button.type = "button";
        button.textContent = "+ Add to Planned"; button.setAttribute("aria-label", "Add selected text to Planned inbox");
        feedback = document.createElement("p"); feedback.setAttribute("role", "status"); feedback.hidden = true;
        shadow.append(style, button, feedback); document.documentElement.append(host);
        button.addEventListener("pointerdown", event => { if (event.isTrusted) event.preventDefault(); });
        button.addEventListener("click", async event => {
            if (!event.isTrusted || sending || button.disabled || !same(snapshot, selection())) return;
            event.preventDefault(); event.stopPropagation();
            const text = snapshot.text;
            consumed = snapshot;
            sending = true; button.disabled = true; snapshot = null; epoch++;
            feedback.hidden = false; feedback.textContent = "Saving to local inbox…";
            try {
                const result = await chrome.runtime.sendMessage({action: "overlayCapture", text});
                feedback.textContent = result?.ok ? `${result.state.inbox} inbox: ${result.state.status}`
                    : result?.error || "Not acknowledged. Check the extension popup before retrying.";
            } catch (_) {
                feedback.textContent = "Extension unavailable. Check the popup for a pending capture; otherwise use the context menu.";
            } finally {
                sending = false;
                // Remain disabled for this selection: a second click cannot create a new UUID.
                timer = setTimeout(dismiss, 5000);
            }
        });
    }
    async function update() {
        if (sending || document.activeElement === host) return;
        dismiss();
        const current = selection();
        if (!current) { consumed = null; return; }
        if (same(current, consumed)) return;
        const ticket = epoch;
        try {
            const permission = await chrome.runtime.sendMessage({action: "overlayAllowed"});
            if (!permission?.ok) { stop(); return; }
            if (ticket !== epoch || !same(current, selection())) return;
        } catch (_) { stop(); return; }
        if (!host) create();
        snapshot = current; button.disabled = false; feedback.hidden = true; host.hidden = false;
        host.style.setProperty("display", "block", "important");
        const bounds = host.getBoundingClientRect();
        const viewport = window.visualViewport;
        const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
        const right = left + (viewport?.width || innerWidth), bottom = top + (viewport?.height || innerHeight);
        const x = Math.max(left + 8, Math.min(current.rect.right - bounds.width, right - bounds.width - 8));
        let y = current.rect.top - bounds.height - 7;
        if (y < top + 8) y = current.rect.bottom + 7;
        y = Math.max(top + 8, Math.min(y, bottom - bounds.height - 8));
        host.style.setProperty("left", x + "px", "important");
        host.style.setProperty("top", y + "px", "important");
    }
    function changed() { clearTimeout(timer); timer = setTimeout(update, 80); }
    function onMessage(message, sender) {
        if (sender.id === chrome.runtime.id && message?.action === "stopOverlay") stop();
    }
    globalThis.__plannedSelectionStop = stop;
    chrome.runtime.onMessage.addListener(onMessage);
    listen(document, "selectionchange", changed);
    listen(document, "pointerup", event => { if (!event.composedPath().includes(host)) changed(); });
    listen(document, "keyup", event => { if (event.key !== "Escape" && event.key !== "Tab") changed(); });
    listen(document, "keydown", event => { if (event.key === "Escape") dismiss(); });
    listen(document, "pointerdown", event => { if (!event.composedPath().includes(host)) dismiss(); }, true);
    listen(window, "scroll", dismiss, {capture: true, passive: true});
    listen(window, "resize", dismiss);
    listen(window, "pagehide", dismiss);
    listen(window, "popstate", dismiss);
    listen(window, "hashchange", dismiss);
    if (window.navigation) listen(window.navigation, "navigate", dismiss);
    if (window.visualViewport) {
        listen(window.visualViewport, "resize", dismiss);
        listen(window.visualViewport, "scroll", dismiss);
    }
})();
