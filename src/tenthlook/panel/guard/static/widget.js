/* Panel Guard chat widget. Embed with:
 *   <script src="https://YOUR-BACKEND/widget.js" data-endpoint="https://YOUR-BACKEND/widget/chat" data-title="Help"></script>
 * Every reply comes from the backend after the guard has checked it. The widget itself makes one kind of
 * request (POST to data-endpoint) and stores nothing in the browser.
 */
(function () {
  "use strict";
  const me = document.currentScript;
  const endpoint = (me && me.dataset.endpoint) || "/widget/chat";
  const title = (me && me.dataset.title) || "Chat with us";
  let session = null, busy = false;

  const css = `
  .pg-w-btn{position:fixed;right:20px;bottom:20px;z-index:2147482000;border:0;border-radius:999px;padding:12px 18px;background:#2f5d8a;color:#fff;font:600 14px system-ui,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.2)}
  .pg-w-box{position:fixed;right:20px;bottom:76px;z-index:2147482000;width:min(360px,calc(100vw - 32px));height:min(480px,calc(100vh - 110px));display:none;flex-direction:column;background:#fff;color:#1d1d1b;border:1px solid #dedbd2;border-radius:12px;box-shadow:0 6px 24px rgba(0,0,0,.18);font:14px/1.45 system-ui,sans-serif;overflow:hidden}
  .pg-w-box.open{display:flex} .pg-w-head{padding:10px 14px;background:#2f5d8a;color:#fff;font-weight:600}
  .pg-w-log{flex:1;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:8px}
  .pg-w-m{max-width:85%;padding:7px 10px;border-radius:10px;white-space:pre-wrap;word-wrap:break-word} .pg-w-u{align-self:flex-end;background:#e7eef6} .pg-w-b{align-self:flex-start;background:#f2f0ea}
  .pg-w-form{display:flex;border-top:1px solid #dedbd2} .pg-w-form input{flex:1;border:0;padding:10px;font:inherit;outline:none;background:transparent;color:inherit} .pg-w-form button{border:0;background:transparent;color:#2f5d8a;font-weight:600;padding:0 14px;cursor:pointer}
  @media (prefers-color-scheme: dark){.pg-w-box{background:#1d1d1b;color:#ecebe6;border-color:#34332f}.pg-w-u{background:#22303d}.pg-w-b{background:#2a2925}.pg-w-form{border-color:#34332f}}`;
  const style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);

  const btn = document.createElement("button"); btn.className = "pg-w-btn"; btn.type = "button"; btn.textContent = title;
  const box = document.createElement("div"); box.className = "pg-w-box"; box.setAttribute("role", "dialog"); box.setAttribute("aria-label", title);
  box.innerHTML = `<div class="pg-w-head"></div><div class="pg-w-log" aria-live="polite"></div><form class="pg-w-form"><input aria-label="Message" placeholder="Type a message…" autocomplete="off"><button type="submit">Send</button></form>`;
  box.querySelector(".pg-w-head").textContent = title;
  document.body.append(btn, box);
  const log = box.querySelector(".pg-w-log"), input = box.querySelector("input");

  function add(text, who) { const m = document.createElement("div"); m.className = "pg-w-m " + (who === "u" ? "pg-w-u" : "pg-w-b"); m.textContent = text; log.appendChild(m); log.scrollTop = log.scrollHeight; return m; }
  btn.onclick = () => { box.classList.toggle("open"); if (box.classList.contains("open")) input.focus(); };
  box.querySelector("form").onsubmit = async (e) => {
    e.preventDefault();
    const text = input.value.trim(); if (!text || busy) return;
    input.value = ""; add(text, "u"); busy = true;
    const pending = add("…", "b");
    try {
      const r = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session, message: text }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || r.status);
      session = d.session; pending.textContent = d.reply;
    } catch (err) {
      pending.textContent = "Sorry, something went wrong. Please try again in a moment.";
    } finally { busy = false; }
  };
})();
